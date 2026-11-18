"""Region-aware Payment & Financial Engine service implementing PaymentServiceProtocol."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import qrcode
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.constants import (
    CryptoVerificationMode,
    Currency,
    FIAT_ENABLED_LANGUAGES,
    FiatMethodType,
    PaymentCategory,
    PaymentStatus,
    SettingKey,
    WalletLedgerRefType,
)
from app.core.exceptions import (
    AuthenticationError,
    DuplicateTransactionError,
    InvalidStateTransitionError,
    NotFoundError,
    UserBlockedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import decrypt_secret, encrypt_secret, validate_receipt_image
from app.database.models import (
    AppSetting,
    CryptoWallet,
    FiatPaymentMethod,
    Payment,
    User,
    WalletLedger,
)
from app.database.session import get_session_factory
from app.locales.i18n import TranslationService
from app.services.dtos import (
    AvailablePaymentMethodsDTO,
    CryptoDepositCreateDTO,
    CryptoWalletCreateDTO,
    CryptoWalletDTO,
    CryptoWalletUpdateDTO,
    ExchangeRateDTO,
    FiatDepositCreateDTO,
    FiatMethodCreateDTO,
    FiatMethodDTO,
    FiatMethodUpdateDTO,
    PaymentDTO,
    PaymentReviewDTO,
)
from app.services.gateways.crypto_explorers import get_crypto_explorer, get_crypto_gateway
from app.services.gateways.iranian_gateways import SUPPORTED_IRANIAN_GATEWAYS, get_iranian_gateway

logger = get_logger(__name__)

_USD_QUANT = Decimal("0.00000001")
_IRT_QUANT = Decimal("0.01")
_RATE_QUANT = Decimal("0.0001")
_SHEBA_RE = re.compile(r"^IR\d{24}$")


def _clean_opt(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _quantize_usd(value: Decimal, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, float) or not isinstance(value, Decimal):
        raise ValidationError("USD monetary amount must be a Decimal")
    if not value.is_finite():
        raise ValidationError("USD amount must be finite")
    if allow_zero:
        if value < Decimal("0"):
            raise ValidationError("USD amount cannot be negative")
    elif value <= Decimal("0"):
        raise ValidationError("USD amount must be greater than zero")
    return value.quantize(_USD_QUANT, rounding=ROUND_HALF_UP)


def _quantize_irt(value: Decimal, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, float) or not isinstance(value, Decimal):
        raise ValidationError("IRT monetary amount must be a Decimal")
    if not value.is_finite():
        raise ValidationError("IRT amount must be finite")
    if allow_zero:
        if value < Decimal("0"):
            raise ValidationError("IRT amount cannot be negative")
    elif value <= Decimal("0"):
        raise ValidationError("IRT amount must be greater than zero")
    return value.quantize(_IRT_QUANT, rounding=ROUND_HALF_UP)


def _normalize_card_number(card_number: str | None) -> str | None:
    cleaned = _clean_opt(card_number)
    if cleaned is None:
        return None
    digits = re.sub(r"[\s-]+", "", cleaned)
    if not digits.isdigit() or len(digits) != 16:
        raise ValidationError("Card number must contain exactly 16 digits")
    return digits


def _normalize_sheba(sheba: str | None) -> str | None:
    cleaned = _clean_opt(sheba)
    if cleaned is None:
        return None
    normalized = re.sub(r"[\s-]+", "", cleaned).upper()
    if not _SHEBA_RE.match(normalized):
        raise ValidationError("Sheba IBAN must start with 'IR' followed by 24 digits")
    return normalized


def _build_qr_png_bytes(data: str) -> bytes:
    """Render a PNG QR code for a wallet address (CPU-bound, run in worker thread)."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _is_unique_violation(exc: IntegrityError) -> bool:
    pgcode = getattr(exc.orig, "pgcode", None)
    if pgcode == "23505":
        return True
    msg = str(exc.orig or exc).lower()
    return "unique" in msg or "duplicate" in msg or "tx_hash" in msg or "authority_track_id" in msg


class PaymentService:
    """Async financial service managing region-aware payment methods, deposits, gateways, and atomic ledger credits."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._settings = settings or get_settings()
        self._http_client = http_client

    # -----------------------------------------------------------------------
    # Region-Aware Payment Routing
    # -----------------------------------------------------------------------
    async def get_available_methods_for_language(self, lang_code: str) -> AvailablePaymentMethodsDTO:
        """Return active payment methods filtered strictly by language/region rules."""
        lang = TranslationService.normalize_lang(lang_code)
        fiat_enabled = lang in FIAT_ENABLED_LANGUAGES
        crypto_wallets = await self.list_crypto_wallets(active_only=True)
        fiat_methods = await self.list_fiat_methods(active_only=True) if fiat_enabled else []
        return AvailablePaymentMethodsDTO(
            lang_code=lang,
            fiat_enabled=fiat_enabled,
            crypto_wallets=crypto_wallets,
            fiat_methods=fiat_methods,
        )

    # -----------------------------------------------------------------------
    # Dynamic Crypto Wallets CRUD & QR Generation
    # -----------------------------------------------------------------------
    async def list_crypto_wallets(self, *, active_only: bool = False) -> list[CryptoWalletDTO]:
        async with self._session_factory() as session:
            stmt = select(CryptoWallet)
            if active_only:
                stmt = stmt.where(CryptoWallet.is_active.is_(True))
            stmt = stmt.order_by(CryptoWallet.sort_order.asc(), CryptoWallet.id.asc())
            rows = (await session.execute(stmt)).scalars().all()
            return [CryptoWalletDTO.model_validate(row) for row in rows]

    async def get_crypto_wallet(self, wallet_id: int) -> CryptoWalletDTO:
        async with self._session_factory() as session:
            wallet = await session.get(CryptoWallet, wallet_id)
            if wallet is None:
                raise NotFoundError(f"CryptoWallet {wallet_id} not found")
            return CryptoWalletDTO.model_validate(wallet)

    async def create_crypto_wallet(self, data: CryptoWalletCreateDTO) -> CryptoWalletDTO:
        min_usd = _quantize_usd(data.min_deposit_usd, allow_zero=True)
        coin_name = data.coin_name.strip()
        symbol = data.symbol.strip().upper()
        network = data.network.strip()
        wallet_address = data.wallet_address.strip()
        if not coin_name or not symbol or not network or not wallet_address:
            raise ValidationError("coin_name, symbol, network, and wallet_address are required")

        async with self._session_factory() as session:
            async with session.begin():
                wallet = CryptoWallet(
                    coin_name=coin_name,
                    symbol=symbol,
                    network=network,
                    wallet_address=wallet_address,
                    qr_image_path=_clean_opt(data.qr_image_path),
                    min_deposit_usd=min_usd,
                    fee_note=_clean_opt(data.fee_note),
                    instructions=_clean_opt(data.instructions),
                    instructions_json=dict(data.instructions_json),
                    verification_mode=data.verification_mode.value,
                    explorer_config=dict(data.explorer_config),
                    is_active=data.is_active,
                    sort_order=data.sort_order,
                )
                session.add(wallet)
                await session.flush()
                await session.refresh(wallet)
                return CryptoWalletDTO.model_validate(wallet)

    async def update_crypto_wallet(self, wallet_id: int, data: CryptoWalletUpdateDTO) -> CryptoWalletDTO:
        updates = data.model_dump(exclude_unset=True)
        async with self._session_factory() as session:
            async with session.begin():
                wallet = await session.get(CryptoWallet, wallet_id)
                if wallet is None:
                    raise NotFoundError(f"CryptoWallet {wallet_id} not found")

                if "coin_name" in updates and updates["coin_name"] is not None:
                    cleaned = updates["coin_name"].strip()
                    if not cleaned:
                        raise ValidationError("coin_name cannot be empty")
                    wallet.coin_name = cleaned
                if "symbol" in updates and updates["symbol"] is not None:
                    cleaned = updates["symbol"].strip().upper()
                    if not cleaned:
                        raise ValidationError("symbol cannot be empty")
                    wallet.symbol = cleaned
                if "network" in updates and updates["network"] is not None:
                    cleaned = updates["network"].strip()
                    if not cleaned:
                        raise ValidationError("network cannot be empty")
                    wallet.network = cleaned
                if "wallet_address" in updates and updates["wallet_address"] is not None:
                    cleaned = updates["wallet_address"].strip()
                    if len(cleaned) < 4:
                        raise ValidationError("wallet_address is too short")
                    wallet.wallet_address = cleaned
                if "qr_image_path" in updates:
                    wallet.qr_image_path = _clean_opt(updates["qr_image_path"])
                if "min_deposit_usd" in updates and updates["min_deposit_usd"] is not None:
                    wallet.min_deposit_usd = _quantize_usd(updates["min_deposit_usd"], allow_zero=True)
                if "fee_note" in updates:
                    wallet.fee_note = _clean_opt(updates["fee_note"])
                if "instructions" in updates:
                    wallet.instructions = _clean_opt(updates["instructions"])
                if "instructions_json" in updates and updates["instructions_json"] is not None:
                    wallet.instructions_json = dict(updates["instructions_json"])
                if "verification_mode" in updates and updates["verification_mode"] is not None:
                    mode = updates["verification_mode"]
                    wallet.verification_mode = mode.value if isinstance(mode, CryptoVerificationMode) else str(mode)
                if "explorer_config" in updates and updates["explorer_config"] is not None:
                    wallet.explorer_config = dict(updates["explorer_config"])
                if "is_active" in updates and updates["is_active"] is not None:
                    wallet.is_active = bool(updates["is_active"])
                if "sort_order" in updates and updates["sort_order"] is not None:
                    wallet.sort_order = int(updates["sort_order"])

                await session.flush()
                await session.refresh(wallet)
                return CryptoWalletDTO.model_validate(wallet)

    async def delete_crypto_wallet(self, wallet_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                wallet = await session.get(CryptoWallet, wallet_id)
                if wallet is None:
                    raise NotFoundError(f"CryptoWallet {wallet_id} not found")
                await session.delete(wallet)

    async def generate_wallet_qr_bytes(self, wallet_address: str) -> bytes:
        """Generate a PNG QR code in a worker thread for the given wallet address."""
        cleaned = wallet_address.strip()
        if not cleaned:
            raise ValidationError("wallet_address cannot be empty for QR generation")
        return await asyncio.to_thread(_build_qr_png_bytes, cleaned)

    async def get_wallet_qr_bytes(self, wallet_id: int) -> bytes:
        """Return custom uploaded QR image bytes if present on disk, else auto-generate PNG QR."""
        wallet = await self.get_crypto_wallet(wallet_id)
        if wallet.qr_image_path:
            qr_path = Path(wallet.qr_image_path)
            if not qr_path.is_absolute():
                qr_path = Path(self._settings.UPLOAD_DIR) / qr_path
            if qr_path.is_file():
                return await asyncio.to_thread(qr_path.read_bytes)
        return await self.generate_wallet_qr_bytes(wallet.wallet_address)

    async def save_wallet_qr_image(
        self,
        wallet_id: int,
        image_bytes: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> CryptoWalletDTO:
        """Validate magic bytes/size and store a custom QR image for a crypto wallet."""
        detected_mime = validate_receipt_image(
            image_bytes,
            filename=filename,
            content_type=content_type,
            max_bytes=self._settings.MAX_UPLOAD_SIZE_BYTES,
        )
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[detected_mime]
        qr_dir = Path(self._settings.UPLOAD_DIR) / "qr"
        qr_dir.mkdir(parents=True, exist_ok=True)
        rel_name = f"wallet_{wallet_id}_{uuid.uuid4().hex[:12]}.{ext}"
        abs_path = qr_dir / rel_name
        await asyncio.to_thread(abs_path.write_bytes, image_bytes)
        return await self.update_crypto_wallet(
            wallet_id,
            CryptoWalletUpdateDTO(qr_image_path=str(Path("qr") / rel_name)),
        )

    # -----------------------------------------------------------------------
    # Iranian Fiat Payment Methods CRUD
    # -----------------------------------------------------------------------
    async def list_fiat_methods(self, *, active_only: bool = False) -> list[FiatMethodDTO]:
        async with self._session_factory() as session:
            stmt = select(FiatPaymentMethod)
            if active_only:
                stmt = stmt.where(FiatPaymentMethod.is_active.is_(True))
            stmt = stmt.order_by(FiatPaymentMethod.sort_order.asc(), FiatPaymentMethod.id.asc())
            rows = (await session.execute(stmt)).scalars().all()
            return [FiatMethodDTO.model_validate(row) for row in rows]

    async def get_fiat_method(self, method_id: int) -> FiatMethodDTO:
        async with self._session_factory() as session:
            method = await session.get(FiatPaymentMethod, method_id)
            if method is None:
                raise NotFoundError(f"FiatPaymentMethod {method_id} not found")
            return FiatMethodDTO.model_validate(method)

    async def create_fiat_method(self, data: FiatMethodCreateDTO) -> FiatMethodDTO:
        title = data.title.strip()
        if not title:
            raise ValidationError("Fiat payment method title is required")
        min_irt = _quantize_irt(data.min_deposit_irt, allow_zero=True)

        card_number = _normalize_card_number(data.card_number)
        sheba = _normalize_sheba(data.sheba)
        provider = _clean_opt(data.gateway_provider)
        if provider:
            provider = provider.lower()
        merchant_plain = _clean_opt(data.gateway_merchant_id)

        if data.method_type == FiatMethodType.CARD_TO_CARD and not card_number:
            raise ValidationError("card_to_card method requires a 16-digit card_number")
        if data.method_type == FiatMethodType.GATEWAY:
            if not provider or provider not in SUPPORTED_IRANIAN_GATEWAYS:
                raise ValidationError(
                    f"gateway_provider must be one of: {', '.join(sorted(SUPPORTED_IRANIAN_GATEWAYS))}"
                )

        encrypted_merchant = (
            encrypt_secret(merchant_plain, self._settings.ENCRYPTION_KEY)
            if merchant_plain
            else None
        )

        async with self._session_factory() as session:
            async with session.begin():
                method = FiatPaymentMethod(
                    method_type=data.method_type.value,
                    title=title,
                    card_number=card_number,
                    sheba=sheba,
                    cardholder_name=_clean_opt(data.cardholder_name),
                    bank_name=_clean_opt(data.bank_name),
                    gateway_provider=provider,
                    gateway_merchant_id_encrypted=encrypted_merchant,
                    min_deposit_irt=min_irt,
                    instructions=_clean_opt(data.instructions),
                    is_active=data.is_active,
                    sort_order=data.sort_order,
                )
                session.add(method)
                await session.flush()
                await session.refresh(method)
                return FiatMethodDTO.model_validate(method)

    async def update_fiat_method(self, method_id: int, data: FiatMethodUpdateDTO) -> FiatMethodDTO:
        updates = data.model_dump(exclude_unset=True)
        async with self._session_factory() as session:
            async with session.begin():
                method = await session.get(FiatPaymentMethod, method_id)
                if method is None:
                    raise NotFoundError(f"FiatPaymentMethod {method_id} not found")

                if "method_type" in updates and updates["method_type"] is not None:
                    mt = updates["method_type"]
                    method.method_type = mt.value if isinstance(mt, FiatMethodType) else str(mt)
                if "title" in updates and updates["title"] is not None:
                    cleaned = updates["title"].strip()
                    if not cleaned:
                        raise ValidationError("title cannot be empty")
                    method.title = cleaned
                if "card_number" in updates:
                    method.card_number = _normalize_card_number(updates["card_number"])
                if "sheba" in updates:
                    method.sheba = _normalize_sheba(updates["sheba"])
                if "cardholder_name" in updates:
                    method.cardholder_name = _clean_opt(updates["cardholder_name"])
                if "bank_name" in updates:
                    method.bank_name = _clean_opt(updates["bank_name"])
                if "gateway_provider" in updates:
                    provider = _clean_opt(updates["gateway_provider"])
                    if provider:
                        provider = provider.lower()
                        if provider not in SUPPORTED_IRANIAN_GATEWAYS:
                            raise ValidationError(f"Unsupported Iranian gateway provider: {provider}")
                    method.gateway_provider = provider
                if "gateway_merchant_id" in updates:
                    merchant_plain = _clean_opt(updates["gateway_merchant_id"])
                    method.gateway_merchant_id_encrypted = (
                        encrypt_secret(merchant_plain, self._settings.ENCRYPTION_KEY)
                        if merchant_plain
                        else None
                    )
                if "min_deposit_irt" in updates and updates["min_deposit_irt"] is not None:
                    method.min_deposit_irt = _quantize_irt(updates["min_deposit_irt"], allow_zero=True)
                if "instructions" in updates:
                    method.instructions = _clean_opt(updates["instructions"])
                if "is_active" in updates and updates["is_active"] is not None:
                    method.is_active = bool(updates["is_active"])
                if "sort_order" in updates and updates["sort_order"] is not None:
                    method.sort_order = int(updates["sort_order"])

                await session.flush()
                await session.refresh(method)
                return FiatMethodDTO.model_validate(method)

    async def delete_fiat_method(self, method_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                method = await session.get(FiatPaymentMethod, method_id)
                if method is None:
                    raise NotFoundError(f"FiatPaymentMethod {method_id} not found")
                await session.delete(method)

    # -----------------------------------------------------------------------
    # Exchange Rate Management (Manual + Safe Live Fetcher)
    # -----------------------------------------------------------------------
    async def get_exchange_rate(self) -> ExchangeRateDTO:
        async with self._session_factory() as session:
            rate_row = await session.get(AppSetting, SettingKey.USD_TO_TOMAN_RATE.value)
            mode_row = await session.get(AppSetting, SettingKey.RATE_MODE.value)

            rate = self._settings.USD_TO_TOMAN_RATE
            updated_at: datetime | None = None
            if rate_row is not None:
                try:
                    parsed = Decimal(rate_row.value.strip())
                    if parsed.is_finite() and parsed > Decimal("0"):
                        rate = parsed
                except (InvalidOperation, ValueError):
                    logger.warning("Invalid AppSetting %s value; using default", SettingKey.USD_TO_TOMAN_RATE.value)
                updated_at = rate_row.updated_at

            rate_mode = self._settings.RATE_MODE
            if mode_row is not None and mode_row.value.strip().lower() in {"manual", "live"}:
                rate_mode = mode_row.value.strip().lower()

            return ExchangeRateDTO(
                usd_to_toman_rate=rate.quantize(_RATE_QUANT, rounding=ROUND_HALF_UP),
                rate_mode=rate_mode,
                updated_at=updated_at,
            )

    async def update_exchange_rate(self, rate: Decimal, *, rate_mode: str = "manual") -> ExchangeRateDTO:
        if isinstance(rate, float) or not isinstance(rate, Decimal):
            raise ValidationError("Exchange rate must be a Decimal")
        if not rate.is_finite() or rate <= Decimal("0"):
            raise ValidationError("Exchange rate must be a positive finite Decimal")
        cleaned_mode = rate_mode.strip().lower()
        if cleaned_mode not in {"manual", "live"}:
            raise ValidationError("rate_mode must be 'manual' or 'live'")

        quantized = rate.quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)

        async with self._session_factory() as session:
            async with session.begin():
                rate_row = await session.get(AppSetting, SettingKey.USD_TO_TOMAN_RATE.value)
                if rate_row is None:
                    rate_row = AppSetting(
                        key=SettingKey.USD_TO_TOMAN_RATE.value,
                        value=str(quantized),
                        description="USD to Iranian Toman (IRT) conversion rate",
                    )
                    session.add(rate_row)
                else:
                    rate_row.value = str(quantized)

                mode_row = await session.get(AppSetting, SettingKey.RATE_MODE.value)
                if mode_row is None:
                    mode_row = AppSetting(
                        key=SettingKey.RATE_MODE.value,
                        value=cleaned_mode,
                        description="Exchange rate source mode: manual or live",
                    )
                    session.add(mode_row)
                else:
                    mode_row.value = cleaned_mode

                await session.flush()
                await session.refresh(rate_row)
                return ExchangeRateDTO(
                    usd_to_toman_rate=quantized,
                    rate_mode=cleaned_mode,
                    updated_at=rate_row.updated_at,
                )

    async def fetch_live_usd_to_toman_rate(
        self,
        *,
        api_url: str = "https://api.tetherland.com/currencies",
    ) -> Decimal:
        """Fetch live USDT-to-Toman rate using Decimal parsing with safe fallback to configured rate."""
        fallback = (await self.get_exchange_rate()).usd_to_toman_rate
        try:
            if self._http_client is not None:
                resp = await self._http_client.get(api_url, timeout=8.0)
            else:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(api_url)
            resp.raise_for_status()
            payload = resp.json()
            raw_price = (
                ((payload.get("data") or {}).get("currencies") or {}).get("USDT") or {}
            ).get("price")
            if raw_price is None and isinstance(payload, dict):
                raw_price = payload.get("usd_to_toman") or payload.get("rate")
            if raw_price is not None:
                parsed = Decimal(str(raw_price))
                if parsed.is_finite() and parsed > Decimal("0"):
                    return parsed.quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)
        except Exception as exc:
            logger.warning("Live USD->Toman rate lookup failed, falling back to stored rate: %s", exc)
        return fallback

    # -----------------------------------------------------------------------
    # Deposit Creation (Strict Replay & Minimum Validation)
    # -----------------------------------------------------------------------
    async def create_crypto_deposit(self, data: CryptoDepositCreateDTO) -> PaymentDTO:
        amount_usd = _quantize_usd(data.amount_usd)
        tx_hash = _clean_opt(data.tx_hash)

        async with self._session_factory() as session:
            async with session.begin():
                user = await session.get(User, data.user_id)
                if user is None:
                    raise NotFoundError(f"User {data.user_id} not found")
                if user.is_blocked:
                    raise UserBlockedError("Blocked users cannot submit deposits")

                wallet = await session.get(CryptoWallet, data.crypto_wallet_id)
                if wallet is None:
                    raise NotFoundError(f"CryptoWallet {data.crypto_wallet_id} not found")
                if not wallet.is_active:
                    raise ValidationError("Selected cryptocurrency wallet is currently inactive")
                if amount_usd < wallet.min_deposit_usd:
                    raise ValidationError(
                        f"Minimum deposit for {wallet.symbol} ({wallet.network}) is ${wallet.min_deposit_usd}"
                    )

                if tx_hash is not None:
                    existing_id = (
                        await session.execute(select(Payment.id).where(Payment.tx_hash == tx_hash))
                    ).scalar_one_or_none()
                    if existing_id is not None:
                        raise DuplicateTransactionError(
                            "This transaction hash (TXID) has already been submitted",
                            details={"field": "tx_hash", "tx_hash": tx_hash},
                        )

                payment = Payment(
                    user_id=user.id,
                    payment_category=PaymentCategory.CRYPTO.value,
                    currency=Currency.USD.value,
                    amount_usd=amount_usd,
                    amount_irt=Decimal("0.00"),
                    exchange_rate_used=None,
                    crypto_wallet_id=wallet.id,
                    fiat_method_id=None,
                    tx_hash=tx_hash,
                    authority_track_id=None,
                    receipt_file_id=_clean_opt(data.receipt_file_id),
                    receipt_local_path=_clean_opt(data.receipt_local_path),
                    receipt_note=_clean_opt(data.receipt_note),
                    status=PaymentStatus.PENDING.value,
                )
                session.add(payment)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    if _is_unique_violation(exc):
                        raise DuplicateTransactionError(
                            "Duplicate transaction hash or tracking ID detected",
                            details={"field": "tx_hash"},
                        ) from exc
                    raise
                await session.refresh(payment)
                return PaymentDTO.model_validate(payment)

    async def create_fiat_deposit(self, data: FiatDepositCreateDTO) -> PaymentDTO:
        amount_irt = _quantize_irt(data.amount_irt)
        if data.payment_category not in {PaymentCategory.FIAT_CARD, PaymentCategory.FIAT_GATEWAY}:
            raise ValidationError("Fiat deposit must use fiat_card or fiat_gateway payment_category")

        authority_track_id = _clean_opt(data.authority_track_id)
        rate_dto = await self.get_exchange_rate()
        rate = rate_dto.usd_to_toman_rate
        amount_usd_equiv = (amount_irt / rate).quantize(_USD_QUANT, rounding=ROUND_HALF_UP)

        async with self._session_factory() as session:
            async with session.begin():
                user = await session.get(User, data.user_id)
                if user is None:
                    raise NotFoundError(f"User {data.user_id} not found")
                if user.is_blocked:
                    raise UserBlockedError("Blocked users cannot submit deposits")

                method = await session.get(FiatPaymentMethod, data.fiat_method_id)
                if method is None:
                    raise NotFoundError(f"FiatPaymentMethod {data.fiat_method_id} not found")
                if not method.is_active:
                    raise ValidationError("Selected payment method is currently inactive")
                if amount_irt < method.min_deposit_irt:
                    raise ValidationError(
                        f"Minimum deposit for {method.title} is {method.min_deposit_irt} IRT"
                    )

                if authority_track_id is not None:
                    existing_id = (
                        await session.execute(
                            select(Payment.id).where(Payment.authority_track_id == authority_track_id)
                        )
                    ).scalar_one_or_none()
                    if existing_id is not None:
                        raise DuplicateTransactionError(
                            "This payment tracking/authority code has already been recorded",
                            details={"field": "authority_track_id", "authority_track_id": authority_track_id},
                        )

                payment = Payment(
                    user_id=user.id,
                    payment_category=data.payment_category.value,
                    currency=Currency.IRT.value,
                    amount_usd=amount_usd_equiv,
                    amount_irt=amount_irt,
                    exchange_rate_used=rate,
                    crypto_wallet_id=None,
                    fiat_method_id=method.id,
                    tx_hash=None,
                    authority_track_id=authority_track_id,
                    receipt_file_id=_clean_opt(data.receipt_file_id),
                    receipt_local_path=_clean_opt(data.receipt_local_path),
                    receipt_note=_clean_opt(data.receipt_note),
                    status=PaymentStatus.PENDING.value,
                )
                session.add(payment)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    if _is_unique_violation(exc):
                        raise DuplicateTransactionError(
                            "Duplicate authority/tracking ID detected",
                            details={"field": "authority_track_id"},
                        ) from exc
                    raise
                await session.refresh(payment)
                return PaymentDTO.model_validate(payment)

    async def initiate_fiat_gateway_deposit(
        self,
        *,
        user_id: int,
        fiat_method_id: int,
        amount_irt: Decimal,
        callback_base_url: str,
        description: str = "",
    ) -> tuple[PaymentDTO, str]:
        """Create a pending Iranian gateway deposit and request a checkout redirect URL."""
        quantized_irt = _quantize_irt(amount_irt)
        rate_dto = await self.get_exchange_rate()
        rate = rate_dto.usd_to_toman_rate
        amount_usd_equiv = (quantized_irt / rate).quantize(_USD_QUANT, rounding=ROUND_HALF_UP)

        async with self._session_factory() as session:
            async with session.begin():
                user = await session.get(User, user_id)
                if user is None:
                    raise NotFoundError(f"User {user_id} not found")
                if user.is_blocked:
                    raise UserBlockedError("Blocked users cannot submit deposits")

                method = await session.get(FiatPaymentMethod, fiat_method_id)
                if method is None:
                    raise NotFoundError(f"FiatPaymentMethod {fiat_method_id} not found")
                if not method.is_active:
                    raise ValidationError("Selected payment method is currently inactive")
                if method.method_type != FiatMethodType.GATEWAY.value or not method.gateway_provider:
                    raise ValidationError("Selected fiat method is not an online payment gateway")
                if quantized_irt < method.min_deposit_irt:
                    raise ValidationError(
                        f"Minimum deposit for {method.title} is {method.min_deposit_irt} IRT"
                    )

                merchant_id = (
                    decrypt_secret(method.gateway_merchant_id_encrypted, self._settings.ENCRYPTION_KEY)
                    if method.gateway_merchant_id_encrypted
                    else None
                )
                payment = Payment(
                    user_id=user.id,
                    payment_category=PaymentCategory.FIAT_GATEWAY.value,
                    currency=Currency.IRT.value,
                    amount_usd=amount_usd_equiv,
                    amount_irt=quantized_irt,
                    exchange_rate_used=rate,
                    fiat_method_id=method.id,
                    status=PaymentStatus.PENDING.value,
                )
                session.add(payment)
                await session.flush()

                provider = method.gateway_provider.lower()
                callback_url = f"{callback_base_url.rstrip('/')}/webhooks/payment/{provider}"
                adapter = get_iranian_gateway(
                    provider,
                    merchant_id=merchant_id,
                    settings=self._settings,
                    http_client=self._http_client,
                )
                gw_res = await adapter.request_payment(
                    amount_irt=quantized_irt,
                    callback_url=callback_url,
                    description=description or f"Wallet top-up #{payment.id}",
                    order_id=f"PAY-{payment.id}",
                )

                payment.authority_track_id = gw_res.authority_track_id
                try:
                    await session.flush()
                except IntegrityError as exc:
                    if _is_unique_violation(exc):
                        raise DuplicateTransactionError(
                            "Gateway returned a duplicate authority_track_id"
                        ) from exc
                    raise
                await session.refresh(payment)
                return PaymentDTO.model_validate(payment), gw_res.payment_url

    async def initiate_crypto_gateway_deposit(
        self,
        *,
        user_id: int,
        crypto_wallet_id: int,
        amount_usd: Decimal,
        callback_base_url: str,
        description: str = "",
    ) -> tuple[PaymentDTO, str]:
        """Create a pending crypto invoice via NOWPayments or Oxapay and return checkout URL."""
        quantized_usd = _quantize_usd(amount_usd)

        async with self._session_factory() as session:
            async with session.begin():
                user = await session.get(User, user_id)
                if user is None:
                    raise NotFoundError(f"User {user_id} not found")
                if user.is_blocked:
                    raise UserBlockedError("Blocked users cannot submit deposits")

                wallet = await session.get(CryptoWallet, crypto_wallet_id)
                if wallet is None:
                    raise NotFoundError(f"CryptoWallet {crypto_wallet_id} not found")
                if not wallet.is_active:
                    raise ValidationError("Selected cryptocurrency wallet is currently inactive")
                if quantized_usd < wallet.min_deposit_usd:
                    raise ValidationError(
                        f"Minimum deposit for {wallet.symbol} is ${wallet.min_deposit_usd}"
                    )

                mode = wallet.verification_mode
                if mode not in {CryptoVerificationMode.NOWPAYMENTS.value, CryptoVerificationMode.OXAPAY.value}:
                    raise ValidationError(f"Wallet verification_mode '{mode}' is not a hosted crypto gateway")

                payment = Payment(
                    user_id=user.id,
                    payment_category=PaymentCategory.CRYPTO.value,
                    currency=Currency.USD.value,
                    amount_usd=quantized_usd,
                    amount_irt=Decimal("0.00"),
                    crypto_wallet_id=wallet.id,
                    status=PaymentStatus.PENDING.value,
                )
                session.add(payment)
                await session.flush()

                callback_url = f"{callback_base_url.rstrip('/')}/webhooks/crypto/{mode}"
                gateway = get_crypto_gateway(
                    mode,
                    settings=self._settings,
                    gateway_config=wallet.explorer_config,
                    http_client=self._http_client,
                )
                inv = await gateway.create_invoice(
                    amount_usd=quantized_usd,
                    order_id=f"PAY-{payment.id}",
                    callback_url=callback_url,
                    pay_currency=wallet.symbol,
                    description=description or f"Crypto deposit #{payment.id}",
                )
                payment.authority_track_id = inv.track_id
                try:
                    await session.flush()
                except IntegrityError as exc:
                    if _is_unique_violation(exc):
                        raise DuplicateTransactionError("Duplicate crypto invoice track ID") from exc
                    raise
                await session.refresh(payment)
                return PaymentDTO.model_validate(payment), inv.payment_url

    # -----------------------------------------------------------------------
    # Atomic Idempotent Approval, Rejection & Gateway Verification
    # -----------------------------------------------------------------------
    async def _credit_locked_payment(
        self,
        session: AsyncSession,
        payment: Payment,
        *,
        admin_id: int | None,
        admin_note: str | None,
    ) -> None:
        """Credit User wallet under FOR UPDATE lock and append immutable WalletLedger row."""
        user = (
            await session.execute(select(User).where(User.id == payment.user_id).with_for_update())
        ).scalar_one_or_none()
        if user is None:
            raise NotFoundError(f"User {payment.user_id} not found")

        if payment.currency == Currency.USD.value:
            credit_amount = _quantize_usd(Decimal(payment.amount_usd))
            before = Decimal(user.balance_usd)
            after = (before + credit_amount).quantize(_USD_QUANT, rounding=ROUND_HALF_UP)
            user.balance_usd = after
        elif payment.currency == Currency.IRT.value:
            credit_amount = _quantize_irt(Decimal(payment.amount_irt))
            before = Decimal(user.balance_irt)
            after = (before + credit_amount).quantize(_IRT_QUANT, rounding=ROUND_HALF_UP)
            user.balance_irt = after
        else:
            raise ValidationError(f"Unsupported payment currency: {payment.currency}")

        ref_type = (
            WalletLedgerRefType.DEPOSIT_CRYPTO.value
            if payment.payment_category == PaymentCategory.CRYPTO.value
            else WalletLedgerRefType.DEPOSIT_FIAT.value
        )

        session.add(
            WalletLedger(
                user_id=user.id,
                currency=payment.currency,
                before_balance=before,
                change_amount=credit_amount,
                after_balance=after,
                reference_type=ref_type,
                reference_id=payment.id,
                admin_id=admin_id,
                note=admin_note or f"Deposit #{payment.id} approved",
            )
        )
        payment.status = PaymentStatus.APPROVED.value
        payment.admin_id = admin_id
        payment.admin_note = admin_note
        payment.reviewed_at = datetime.now(timezone.utc)

    async def review_payment(self, review: PaymentReviewDTO) -> PaymentDTO:
        """Atomically approve or reject a pending deposit with FOR UPDATE idempotency."""
        note = _clean_opt(review.admin_note)
        async with self._session_factory() as session:
            async with session.begin():
                payment = (
                    await session.execute(
                        select(Payment).where(Payment.id == review.payment_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if payment is None:
                    raise NotFoundError(f"Payment {review.payment_id} not found")
                if payment.status != PaymentStatus.PENDING.value:
                    raise InvalidStateTransitionError(
                        f"Payment {payment.id} is already '{payment.status}' and cannot be reviewed again"
                    )

                if review.approve:
                    await self._credit_locked_payment(
                        session,
                        payment,
                        admin_id=review.admin_id,
                        admin_note=note,
                    )
                else:
                    payment.status = PaymentStatus.REJECTED.value
                    payment.admin_id = review.admin_id
                    payment.admin_note = note
                    payment.reviewed_at = datetime.now(timezone.utc)

                await session.flush()
                await session.refresh(payment)
                return PaymentDTO.model_validate(payment)

    async def verify_gateway_callback(
        self,
        authority_track_id: str,
        gateway_params: dict[str, str],
        *,
        expected_provider: str | None = None,
    ) -> PaymentDTO:
        """Verify an Iranian payment gateway callback and atomically credit the user once."""
        cleaned_track = authority_track_id.strip()
        if not cleaned_track:
            raise ValidationError("authority_track_id is required")

        async with self._session_factory() as session:
            async with session.begin():
                payment = (
                    await session.execute(
                        select(Payment)
                        .where(Payment.authority_track_id == cleaned_track)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if payment is None:
                    raise NotFoundError(f"Payment with authority_track_id '{cleaned_track}' not found")
                if payment.status != PaymentStatus.PENDING.value:
                    raise InvalidStateTransitionError(
                        f"Payment {payment.id} is already '{payment.status}'"
                    )

                provider = (expected_provider or "").strip().lower()
                merchant_id: str | None = None
                if payment.fiat_method_id is not None:
                    method = await session.get(FiatPaymentMethod, payment.fiat_method_id)
                    if method is not None:
                        if not provider and method.gateway_provider:
                            provider = method.gateway_provider.lower()
                        elif provider and method.gateway_provider and method.gateway_provider.lower() != provider:
                            raise ValidationError(
                                f"Callback provider '{provider}' does not match payment provider '{method.gateway_provider}'"
                            )
                        if method.gateway_merchant_id_encrypted:
                            merchant_id = decrypt_secret(
                                method.gateway_merchant_id_encrypted,
                                self._settings.ENCRYPTION_KEY,
                            )
                if not provider:
                    provider = "zarinpal"

                merged_params = dict(gateway_params)
                merged_params.setdefault("order_id", f"PAY-{payment.id}")

                adapter = get_iranian_gateway(
                    provider,
                    merchant_id=merchant_id,
                    settings=self._settings,
                    http_client=self._http_client,
                )
                result = await adapter.verify_payment(
                    authority_track_id=cleaned_track,
                    amount_irt=Decimal(payment.amount_irt),
                    params=merged_params,
                )

                if result.verified:
                    ref_note = f"Gateway {provider} verified"
                    if result.ref_id:
                        ref_note = f"{ref_note} (ref_id={result.ref_id})"
                    await self._credit_locked_payment(
                        session,
                        payment,
                        admin_id=None,
                        admin_note=ref_note,
                    )
                else:
                    payment.status = PaymentStatus.REJECTED.value
                    payment.admin_note = result.message or f"Gateway {provider} verification failed"
                    payment.reviewed_at = datetime.now(timezone.utc)

                await session.flush()
                await session.refresh(payment)
                return PaymentDTO.model_validate(payment)

    async def verify_crypto_deposit_onchain(self, payment_id: int) -> PaymentDTO:
        """Verify a pending manual/explorer crypto deposit on-chain via TronGrid or BscScan."""
        async with self._session_factory() as session:
            async with session.begin():
                payment = (
                    await session.execute(
                        select(Payment).where(Payment.id == payment_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if payment is None:
                    raise NotFoundError(f"Payment {payment_id} not found")
                if payment.status != PaymentStatus.PENDING.value:
                    raise InvalidStateTransitionError(f"Payment {payment.id} is already '{payment.status}'")
                if not payment.tx_hash:
                    raise ValidationError("Payment does not have a tx_hash to verify on-chain")
                if payment.crypto_wallet_id is None:
                    raise ValidationError("Payment is not linked to a CryptoWallet")

                wallet = await session.get(CryptoWallet, payment.crypto_wallet_id)
                if wallet is None:
                    raise NotFoundError(f"CryptoWallet {payment.crypto_wallet_id} not found")

                explorer = get_crypto_explorer(
                    wallet.verification_mode,
                    settings=self._settings,
                    explorer_config=wallet.explorer_config,
                    http_client=self._http_client,
                )
                cfg = wallet.explorer_config or {}
                token_contract = cfg.get("token_contract")
                decimals = int(cfg.get("decimals", 6 if wallet.verification_mode == "trongrid" else 18))

                res = await explorer.verify_transaction(
                    tx_hash=payment.tx_hash,
                    expected_address=wallet.wallet_address,
                    min_amount=Decimal(payment.amount_usd),
                    token_contract=token_contract,
                    decimals=decimals,
                )
                if res.verified:
                    await self._credit_locked_payment(
                        session,
                        payment,
                        admin_id=None,
                        admin_note=f"On-chain verified via {res.provider}: {res.amount} {res.symbol or ''}".strip(),
                    )
                else:
                    payment.admin_note = res.message
                await session.flush()
                await session.refresh(payment)
                return PaymentDTO.model_validate(payment)

    async def process_crypto_webhook(
        self,
        provider: str,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        gateway_config: dict[str, Any] | None = None,
    ) -> PaymentDTO:
        """Verify HMAC signature for a crypto gateway IPN webhook and atomically credit user."""
        gateway = get_crypto_gateway(
            provider,
            settings=self._settings,
            gateway_config=gateway_config,
            http_client=self._http_client,
        )
        event = gateway.parse_webhook(raw_body, headers)
        if not event.verified_signature:
            raise AuthenticationError(f"Invalid {provider} webhook HMAC signature")

        parsed_payment_id: int | None = None
        if event.order_id and event.order_id.startswith("PAY-"):
            suffix = event.order_id[4:]
            if suffix.isdigit():
                parsed_payment_id = int(suffix)

        if not event.authority_track_id and parsed_payment_id is None:
            raise ValidationError("Crypto webhook missing track ID and order_id")

        async with self._session_factory() as session:
            async with session.begin():
                if event.authority_track_id:
                    stmt = (
                        select(Payment)
                        .where(Payment.authority_track_id == event.authority_track_id)
                        .with_for_update()
                    )
                    payment = (await session.execute(stmt)).scalar_one_or_none()
                else:
                    payment = None

                if payment is None and parsed_payment_id is not None:
                    stmt = select(Payment).where(Payment.id == parsed_payment_id).with_for_update()
                    payment = (await session.execute(stmt)).scalar_one_or_none()

                if payment is None:
                    raise NotFoundError("Matching crypto deposit payment not found")
                if payment.status != PaymentStatus.PENDING.value:
                    raise InvalidStateTransitionError(
                        f"Payment {payment.id} is already '{payment.status}'"
                    )

                if event.tx_hash and not payment.tx_hash:
                    payment.tx_hash = event.tx_hash.strip()

                if event.is_paid:
                    await self._credit_locked_payment(
                        session,
                        payment,
                        admin_id=None,
                        admin_note=f"Crypto webhook {event.provider} confirmed ({event.status})",
                    )
                elif event.is_failed:
                    payment.status = PaymentStatus.REJECTED.value
                    payment.admin_note = f"Crypto gateway status: {event.status}"
                    payment.reviewed_at = datetime.now(timezone.utc)

                try:
                    await session.flush()
                except IntegrityError as exc:
                    if _is_unique_violation(exc):
                        raise DuplicateTransactionError("Duplicate tx_hash in crypto webhook") from exc
                    raise
                await session.refresh(payment)
                return PaymentDTO.model_validate(payment)

    # -----------------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------------
    async def get_payment(self, payment_id: int) -> PaymentDTO:
        async with self._session_factory() as session:
            payment = await session.get(Payment, payment_id)
            if payment is None:
                raise NotFoundError(f"Payment {payment_id} not found")
            return PaymentDTO.model_validate(payment)

    async def get_payment_by_authority(self, authority_track_id: str) -> PaymentDTO:
        cleaned = authority_track_id.strip()
        async with self._session_factory() as session:
            payment = (
                await session.execute(select(Payment).where(Payment.authority_track_id == cleaned))
            ).scalar_one_or_none()
            if payment is None:
                raise NotFoundError(f"Payment with authority_track_id '{cleaned}' not found")
            return PaymentDTO.model_validate(payment)

    async def list_payments(
        self,
        *,
        status: PaymentStatus | None = None,
        user_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PaymentDTO]:
        async with self._session_factory() as session:
            stmt = select(Payment)
            if status is not None:
                status_val = status.value if isinstance(status, PaymentStatus) else str(status)
                stmt = stmt.where(Payment.status == status_val)
            if user_id is not None:
                stmt = stmt.where(Payment.user_id == user_id)
            stmt = stmt.order_by(Payment.created_at.desc(), Payment.id.desc()).limit(limit).offset(offset)
            rows = (await session.execute(stmt)).scalars().all()
            return [PaymentDTO.model_validate(row) for row in rows]
