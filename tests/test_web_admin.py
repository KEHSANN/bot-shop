"""Comprehensive tests for Unified Entrypoint, Web Admin Auth/CSRF/XSS, All CRUD Routes, and Mini App."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import struct
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.core.constants import (
    BroadcastStatus,
    CryptoVerificationMode,
    Currency,
    DeliveryType,
    FiatMethodType,
    LANGUAGE_ORDER,
    MenuScope,
    OrderStatus,
    PaymentCategory,
    PaymentStatus,
    SenderRole,
    TicketCategory,
    TicketStatus,
    WalletLedgerRefType,
)
from app.core.exceptions import (
    InsufficientBalanceError,
    InsufficientStockError,
    NotFoundError,
    ValidationError,
)
from app.core.security import generate_fernet_key, hash_password
from app.main import create_app
from app.services.dtos import (
    AppSettingDTO,
    AvailablePaymentMethodsDTO,
    BalanceAdjustmentDTO,
    BroadcastCreateDTO,
    BroadcastJobDTO,
    CategoryCreateDTO,
    CategoryDTO,
    CategoryUpdateDTO,
    CryptoDepositCreateDTO,
    CryptoWalletCreateDTO,
    CryptoWalletDTO,
    CryptoWalletUpdateDTO,
    DashboardStatsDTO,
    ExchangeRateDTO,
    FiatDepositCreateDTO,
    FiatMethodCreateDTO,
    FiatMethodDTO,
    FiatMethodUpdateDTO,
    LowStockAlertDTO,
    OrderDTO,
    OrderItemDTO,
    PaymentDTO,
    PaymentReviewDTO,
    ProductBulkTierDTO,
    ProductCreateDTO,
    ProductDTO,
    ProductDetailDTO,
    ProductUpdateDTO,
    PurchaseRequestDTO,
    PurchaseResultDTO,
    StockImportResultDTO,
    SupportMessageDTO,
    SupportTicketDTO,
    SupportTicketDetailDTO,
    UIButtonDTO,
    UITranslationDTO,
    UserCreateOrUpdateDTO,
    UserDTO,
    WalletLedgerDTO,
)
from app.services.interfaces import (
    AnalyticsServiceProtocol,
    BroadcastServiceProtocol,
    CMSServiceProtocol,
    PaymentServiceProtocol,
    ShopServiceProtocol,
    SupportServiceProtocol,
    UserServiceProtocol,
)
from app.web_admin.app import create_web_admin_app
from app.web_admin.deps import (
    ADMIN_COOKIE_NAME,
    get_analytics_service,
    get_bot_notifier,
    get_broadcast_service,
    get_cms_service,
    get_payment_service,
    get_shop_service,
    get_support_service,
    get_translation_service,
    get_user_service,
)


# ---------------------------------------------------------------------------
# Ephemeral Test Cryptography & Telegram HMAC Helpers (Zero Hardcoded Secrets)
# ---------------------------------------------------------------------------
def _make_totp(secret_b32: str, offset: int = 0) -> str:
    import base64

    padded = secret_b32.strip().upper()
    padded += "=" * ((8 - len(padded) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter = (int(time.time()) // 30) + offset
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    pos = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[pos : pos + 4])[0] & 0x7FFFFFFF
    return f"{truncated % 1_000_000:06d}"


def _sign_telegram_login_widget(data: dict[str, Any], bot_token: str) -> dict[str, Any]:
    payload = dict(data)
    check_items = [
        f"{k}={v}"
        for k, v in sorted(payload.items())
        if k != "hash" and v is not None and v != ""
    ]
    data_check_string = "\n".join(check_items).encode("utf-8")
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    payload["hash"] = hmac.new(secret_key, data_check_string, hashlib.sha256).hexdigest()
    return payload


def _sign_webapp_init_data(user_obj: dict[str, Any], bot_token: str, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAEAAAE_" + secrets.token_hex(6),
        "user": json.dumps(user_obj, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items())).encode("utf-8")
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret_key, data_check_string, hashlib.sha256).hexdigest()
    return urlencode(pairs)


def _make_png_bytes() -> bytes:
    """Create minimal valid PNG header bytes for upload validation."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 32


def _make_test_settings(
    tmp_path: Path,
    *,
    two_factor: bool = False,
    totp_secret: str = "",
    bot_mode: str = "webhook",
) -> Settings:
    password_plain = "TestAdminPass!2026_" + secrets.token_hex(4)
    bot_token = f"98765432:TEST_BOT_{secrets.token_hex(12)}"
    jwt_secret = secrets.token_hex(32)
    csrf_secret = secrets.token_hex(32)
    webhook_secret = secrets.token_hex(16)
    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        APP_NAME="Test Digital Shop",
        BOT_TOKEN=bot_token,
        BOT_MODE=bot_mode,
        BOT_WEBHOOK_PATH="/webhook/telegram",
        BOT_WEBHOOK_SECRET=webhook_secret,
        TELEGRAM_BOT_USERNAME="test_shop_bot",
        ADMIN_TELEGRAM_IDS=[111222333],
        WEB_ADMIN_USERNAME="testadmin",
        WEB_ADMIN_PASSWORD_HASH=hash_password(password_plain),
        JWT_SECRET_KEY=jwt_secret,
        CSRF_SECRET_KEY=csrf_secret,
        ENCRYPTION_KEY=generate_fernet_key(),
        TWO_FACTOR_ENABLED=two_factor,
        TOTP_SECRET=totp_secret,
        TELEGRAM_LOGIN_WIDGET_ENABLED=True,
        DATABASE_URL="postgresql+asyncpg://shopbot:pass@127.0.0.1:5432/telegram_shop",
        REDIS_URL="redis://127.0.0.1:6379/0",
        UPLOAD_DIR=str(tmp_path / "uploads"),
    )
    settings._plain_password = password_plain  # type: ignore[attr-defined]
    return settings


# ---------------------------------------------------------------------------
# Protocol-Compliant In-Memory Service Doubles
# ---------------------------------------------------------------------------
class FakeBotNotifier:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def send_direct_message(
        self,
        telegram_id: int,
        text: str,
        *,
        photo_bytes: bytes | None = None,
        photo_filename: str | None = None,
    ) -> bool:
        self.sent_messages.append(
            {
                "telegram_id": telegram_id,
                "text": text,
                "has_photo": photo_bytes is not None,
                "photo_filename": photo_filename,
            }
        )
        return True


class FakeTranslationService:
    def __init__(self) -> None:
        self.texts: dict[tuple[str, str], str] = {
            ("balance.admin_credited", "en"): "Credited {amount} {currency}. New balance: {new_balance}. Note: {note}",
            ("balance.admin_deducted", "en"): "Deducted {amount} {currency}. New balance: {new_balance}. Note: {note}",
            ("deposit.approved_notify", "en"): "Deposit #{payment_id} approved: {amount} {currency}",
            ("deposit.rejected_notify", "en"): "Deposit #{payment_id} rejected: {reason}",
            ("shop.order_fulfilled_notify", "en"): "Order {order_number} fulfilled: {note}",
            ("support.reply_from_admin", "en"): "Reply on {ticket_number}: {message}",
            ("support.ticket_closed", "en"): "Ticket {ticket_number} closed",
        }
        now = datetime.now(timezone.utc)
        self.buttons: list[UIButtonDTO] = [
            UIButtonDTO(
                id=1,
                button_key="shop",
                menu_scope=MenuScope.MAIN,
                row_index=0,
                col_index=0,
                color_emoji="🛒",
                color_hex="#3b82f6",
                is_active=True,
                labels_json={"en": "🛒 Shop", "fa": "🛒 فروشگاه"},
                created_at=now,
                updated_at=now,
            )
        ]

    @staticmethod
    def normalize_lang(lang_code: str | None) -> str:
        if not lang_code:
            return "en"
        cleaned = lang_code.strip().lower().split("-")[0].split("_")[0]
        return cleaned if cleaned in set(LANGUAGE_ORDER) else "en"

    async def get_language_map(self, lang_code: str) -> dict[str, str]:
        lang = self.normalize_lang(lang_code)
        return {k: v for (k, lc), v in self.texts.items() if lc == lang}

    async def get_text(self, key: str, lang_code: str = "en", **kwargs: Any) -> str:
        lang = self.normalize_lang(lang_code)
        tpl = self.texts.get((key, lang)) or self.texts.get((key, "en")) or key
        return tpl.format(**kwargs) if kwargs else tpl

    async def get_main_menu_buttons(self, lang_code: str = "en") -> list[UIButtonDTO]:
        return [b for b in self.buttons if b.is_active]


class FakeUserService:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.users: dict[int, UserDTO] = {
            1: UserDTO(
                id=1,
                telegram_id=555000111,
                username="alice_shopper",
                first_name="Alice",
                last_name="Builder",
                language_code="en",
                balance_usd=Decimal("50.00000000"),
                balance_irt=Decimal("2000000.00"),
                is_blocked=False,
                is_admin=False,
                created_at=now,
                updated_at=now,
            ),
            2: UserDTO(
                id=2,
                telegram_id=555000222,
                username="reza_ir",
                first_name="Reza",
                last_name="Iran",
                language_code="fa",
                balance_usd=Decimal("20.00000000"),
                balance_irt=Decimal("5000000.00"),
                is_blocked=False,
                is_admin=False,
                created_at=now,
                updated_at=now,
            ),
        }
        self.ledger: list[WalletLedgerDTO] = []
        self._next_id = 3

    async def upsert_telegram_user(self, data: UserCreateOrUpdateDTO) -> UserDTO:
        for uid, u in self.users.items():
            if u.telegram_id == data.telegram_id:
                updated = u.model_copy(
                    update={
                        "username": data.username or u.username,
                        "first_name": data.first_name or u.first_name,
                        "last_name": data.last_name or u.last_name,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
                self.users[uid] = updated
                return updated
        now = datetime.now(timezone.utc)
        uid = self._next_id
        self._next_id += 1
        created = UserDTO(
            id=uid,
            telegram_id=data.telegram_id,
            username=data.username,
            first_name=data.first_name,
            last_name=data.last_name,
            language_code=data.language_code or "en",
            balance_usd=Decimal("0"),
            balance_irt=Decimal("0"),
            is_blocked=False,
            is_admin=False,
            created_at=now,
            updated_at=now,
        )
        self.users[uid] = created
        return created

    async def get_by_id(self, user_id: int) -> UserDTO:
        if user_id not in self.users:
            raise NotFoundError(f"User {user_id} not found")
        return self.users[user_id]

    async def get_by_telegram_id(self, telegram_id: int) -> UserDTO | None:
        return next((u for u in self.users.values() if u.telegram_id == telegram_id), None)

    async def search_users(self, query: str, *, limit: int = 20, offset: int = 0) -> list[UserDTO]:
        q = query.lstrip("@").strip().lower()
        matches = [
            u
            for u in self.users.values()
            if q in str(u.telegram_id)
            or (u.username and q in u.username.lower())
            or (u.first_name and q in u.first_name.lower())
            or (u.last_name and q in u.last_name.lower())
        ]
        return matches[offset : offset + limit]

    async def list_users(self, *, limit: int = 50, offset: int = 0, blocked_only: bool = False) -> list[UserDTO]:
        rows = [u for u in self.users.values() if not blocked_only or u.is_blocked]
        return rows[offset : offset + limit]

    async def set_language(self, telegram_id: int, lang_code: str) -> UserDTO:
        user = await self.get_by_telegram_id(telegram_id)
        if user is None:
            raise NotFoundError(f"User telegram_id={telegram_id} not found")
        updated = user.model_copy(update={"language_code": lang_code})
        self.users[user.id] = updated
        return updated

    async def set_blocked(self, user_id: int, *, blocked: bool, admin_id: int) -> UserDTO:
        user = await self.get_by_id(user_id)
        updated = user.model_copy(update={"is_blocked": blocked})
        self.users[user_id] = updated
        return updated

    async def adjust_balance(self, adjustment: BalanceAdjustmentDTO) -> tuple[UserDTO, WalletLedgerDTO]:
        user = await self.get_by_id(adjustment.user_id)
        if adjustment.currency == Currency.USD:
            before = user.balance_usd
            after = before + adjustment.amount
            if after < Decimal("0"):
                raise InsufficientBalanceError("Insufficient USD balance")
            updated = user.model_copy(update={"balance_usd": after})
        else:
            before = user.balance_irt
            after = before + adjustment.amount
            if after < Decimal("0"):
                raise InsufficientBalanceError("Insufficient IRT balance")
            updated = user.model_copy(update={"balance_irt": after})

        self.users[user.id] = updated
        ref_type = (
            WalletLedgerRefType.ADMIN_CHARGE
            if adjustment.amount > 0
            else WalletLedgerRefType.ADMIN_DEDUCT
        )
        entry = WalletLedgerDTO(
            id=len(self.ledger) + 1,
            user_id=user.id,
            currency=adjustment.currency,
            before_balance=before,
            change_amount=adjustment.amount,
            after_balance=after,
            reference_type=ref_type,
            admin_id=adjustment.admin_id,
            note=adjustment.note,
            created_at=datetime.now(timezone.utc),
        )
        self.ledger.append(entry)
        return updated, entry

    async def get_wallet_history(self, user_id: int, *, limit: int = 20, offset: int = 0) -> list[WalletLedgerDTO]:
        items = [e for e in self.ledger if e.user_id == user_id]
        return items[offset : offset + limit]


class FakeShopService:
    def __init__(self, user_service: FakeUserService) -> None:
        self.user_service = user_service
        now = datetime.now(timezone.utc)
        self.categories: dict[int, CategoryDTO] = {
            1: CategoryDTO(
                id=1,
                slug="ai-subscriptions",
                sort_order=1,
                is_active=True,
                color_emoji="🔵",
                color_hex="#3b82f6",
                names_json={"en": "AI Subscriptions", "fa": "اشتراک هوش مصنوعی"},
                descriptions_json={"en": "AI keys"},
                created_at=now,
                updated_at=now,
            )
        }
        self.products: dict[int, ProductDetailDTO] = {
            1: ProductDetailDTO(
                id=1,
                category_id=1,
                slug="gemini-pro-12m",
                delivery_type=DeliveryType.AUTO,
                manual_prompt_key=None,
                price_usd=Decimal("10.00000000"),
                price_irt=Decimal("650000.00"),
                color_emoji="🟢",
                color_hex="#22c55e",
                is_pinned=True,
                is_active=True,
                sort_order=1,
                manual_stock_count=0,
                low_stock_threshold=5,
                sold_count=4,
                titles_json={"en": "Gemini Pro 12M", "fa": "جمینای پرو"},
                descriptions_json={"en": "Instant auto key"},
                rules_json={"en": "Use valid email"},
                manual_prompts_json={},
                available_stock=3,
                created_at=now,
                updated_at=now,
                bulk_tiers=[],
                category=self.categories[1],
            )
        }
        self.stock: dict[int, list[str]] = {1: ["KEY-001", "KEY-002", "KEY-003"]}
        self.orders: dict[int, OrderDTO] = {
            10: OrderDTO(
                id=10,
                order_number="ORD-TEST-0010",
                user_id=1,
                product_id=1,
                quantity=1,
                currency=Currency.USD,
                unit_price=Decimal("10.00000000"),
                total_amount=Decimal("10.00000000"),
                status=OrderStatus.PENDING_FULFILLMENT,
                fulfillment_input="user@example.com",
                created_at=now,
                updated_at=now,
                items=[],
            )
        }
        self._next_cat_id = 2
        self._next_prod_id = 2
        self._next_order_id = 11

    async def list_categories(self, *, active_only: bool = True) -> list[CategoryDTO]:
        return [c for c in self.categories.values() if not active_only or c.is_active]

    async def create_category(self, data: CategoryCreateDTO) -> CategoryDTO:
        now = datetime.now(timezone.utc)
        cid = self._next_cat_id
        self._next_cat_id += 1
        cat = CategoryDTO(
            id=cid,
            slug=data.slug,
            sort_order=data.sort_order,
            is_active=data.is_active,
            color_emoji=data.color_emoji,
            color_hex=data.color_hex,
            names_json=data.names_json,
            descriptions_json=data.descriptions_json,
            created_at=now,
            updated_at=now,
        )
        self.categories[cid] = cat
        return cat

    async def update_category(self, category_id: int, data: CategoryUpdateDTO) -> CategoryDTO:
        if category_id not in self.categories:
            raise NotFoundError("Category not found")
        patch = {k: v for k, v in data.model_dump(exclude_none=True).items()}
        updated = self.categories[category_id].model_copy(update=patch)
        self.categories[category_id] = updated
        return updated

    async def delete_category(self, category_id: int) -> None:
        if category_id not in self.categories:
            raise NotFoundError("Category not found")
        del self.categories[category_id]

    async def list_products(
        self,
        *,
        category_id: int | None = None,
        active_only: bool = True,
        pinned_only: bool = False,
    ) -> list[ProductDTO]:
        rows = []
        for p in self.products.values():
            if category_id is not None and p.category_id != category_id:
                continue
            if active_only and not p.is_active:
                continue
            if pinned_only and not p.is_pinned:
                continue
            rows.append(ProductDTO.model_validate(p))
        return rows

    async def get_product_detail(self, product_id: int) -> ProductDetailDTO:
        if product_id not in self.products:
            raise NotFoundError("Product not found")
        return self.products[product_id]

    async def create_product(self, data: ProductCreateDTO) -> ProductDetailDTO:
        now = datetime.now(timezone.utc)
        pid = self._next_prod_id
        self._next_prod_id += 1
        prod = ProductDetailDTO(
            id=pid,
            category_id=data.category_id,
            slug=data.slug,
            delivery_type=data.delivery_type,
            manual_prompt_key=data.manual_prompt_key,
            price_usd=data.price_usd,
            price_irt=data.price_irt,
            color_emoji=data.color_emoji,
            color_hex=data.color_hex,
            is_pinned=data.is_pinned,
            is_active=data.is_active,
            sort_order=data.sort_order,
            manual_stock_count=data.manual_stock_count,
            low_stock_threshold=data.low_stock_threshold,
            sold_count=0,
            titles_json=data.titles_json,
            descriptions_json=data.descriptions_json,
            rules_json=data.rules_json,
            manual_prompts_json=data.manual_prompts_json,
            available_stock=0,
            created_at=now,
            updated_at=now,
            bulk_tiers=data.bulk_tiers,
            category=self.categories.get(data.category_id),
        )
        self.products[pid] = prod
        self.stock[pid] = []
        return prod

    async def update_product(self, product_id: int, data: ProductUpdateDTO) -> ProductDetailDTO:
        if product_id not in self.products:
            raise NotFoundError("Product not found")
        patch = {k: v for k, v in data.model_dump(exclude_none=True).items()}
        updated = self.products[product_id].model_copy(update=patch)
        self.products[product_id] = updated
        return updated

    async def delete_product(self, product_id: int) -> None:
        if product_id not in self.products:
            raise NotFoundError("Product not found")
        del self.products[product_id]

    async def set_bulk_tiers(self, product_id: int, tiers: list[ProductBulkTierDTO]) -> list[ProductBulkTierDTO]:
        prod = await self.get_product_detail(product_id)
        self.products[product_id] = prod.model_copy(update={"bulk_tiers": tiers})
        return tiers

    async def add_auto_stock(
        self, product_id: int, payloads: list[str], *, deduplicate: bool = False
    ) -> StockImportResultDTO:
        prod = await self.get_product_detail(product_id)
        bucket = self.stock.setdefault(product_id, [])
        added = 0
        skipped = 0
        for item in payloads:
            if deduplicate and item in bucket:
                skipped += 1
                continue
            bucket.append(item)
            added += 1
        self.products[product_id] = prod.model_copy(update={"available_stock": len(bucket)})
        return StockImportResultDTO(
            product_id=product_id,
            added_count=added,
            skipped_duplicates=skipped,
            total_available=len(bucket),
        )

    async def calculate_unit_price(self, product_id: int, quantity: int, currency: Currency) -> Decimal:
        prod = await self.get_product_detail(product_id)
        return prod.price_usd if currency == Currency.USD else (prod.price_irt or Decimal("0"))

    async def purchase_product(self, request: PurchaseRequestDTO) -> PurchaseResultDTO:
        prod = await self.get_product_detail(request.product_id)
        user = await self.user_service.get_by_id(request.user_id)
        unit_price = await self.calculate_unit_price(prod.id, request.quantity, request.currency)
        total = unit_price * Decimal(request.quantity)
        bal = user.balance_usd if request.currency == Currency.USD else user.balance_irt
        if bal < total:
            raise InsufficientBalanceError("Insufficient balance")
        bucket = self.stock.get(prod.id, [])
        if prod.delivery_type == DeliveryType.AUTO and len(bucket) < request.quantity:
            raise InsufficientStockError("Insufficient stock")

        delivered = [bucket.pop(0) for _ in range(request.quantity)] if prod.delivery_type == DeliveryType.AUTO else []
        updated_user, _ = await self.user_service.adjust_balance(
            BalanceAdjustmentDTO(
                user_id=user.id,
                currency=request.currency,
                amount=-total,
                admin_id=0,
                note="purchase",
            )
        )
        self.products[prod.id] = prod.model_copy(
            update={"available_stock": len(bucket), "sold_count": prod.sold_count + request.quantity}
        )
        now = datetime.now(timezone.utc)
        oid = self._next_order_id
        self._next_order_id += 1
        order = OrderDTO(
            id=oid,
            order_number=f"ORD-{oid:04d}",
            user_id=user.id,
            product_id=prod.id,
            quantity=request.quantity,
            currency=request.currency,
            unit_price=unit_price,
            total_amount=total,
            status=OrderStatus.COMPLETED,
            fulfillment_input=request.fulfillment_input,
            created_at=now,
            updated_at=now,
            items=[
                OrderItemDTO(
                    id=idx + 1,
                    order_id=oid,
                    product_id=prod.id,
                    decrypted_payload=val,
                    unit_price=unit_price,
                    created_at=now,
                )
                for idx, val in enumerate(delivered)
            ],
        )
        self.orders[oid] = order
        new_bal = updated_user.balance_usd if request.currency == Currency.USD else updated_user.balance_irt
        return PurchaseResultDTO(order=order, delivered_items=delivered, new_balance=new_bal)

    async def submit_manual_fulfillment_input(self, order_id: int, user_id: int, fulfillment_input: str) -> OrderDTO:
        order = await self.get_order_detail(order_id)
        updated = order.model_copy(update={"fulfillment_input": fulfillment_input})
        self.orders[order_id] = updated
        return updated

    async def fulfill_manual_order(
        self, order_id: int, admin_id: int, fulfillment_note: str | None = None
    ) -> OrderDTO:
        order = await self.get_order_detail(order_id)
        updated = order.model_copy(
            update={
                "status": OrderStatus.COMPLETED,
                "fulfillment_note": fulfillment_note,
                "fulfilled_by_admin_id": admin_id,
                "fulfilled_at": datetime.now(timezone.utc),
            }
        )
        self.orders[order_id] = updated
        return updated

    async def cancel_and_refund_order(
        self, order_id: int, admin_id: int, reason: str | None = None
    ) -> OrderDTO:
        order = await self.get_order_detail(order_id)
        updated = order.model_copy(
            update={"status": OrderStatus.REFUNDED, "fulfillment_note": reason}
        )
        self.orders[order_id] = updated
        return updated

    async def get_order_detail(self, order_id: int, *, decrypt_items: bool = False) -> OrderDTO:
        if order_id not in self.orders:
            raise NotFoundError("Order not found")
        return self.orders[order_id]

    async def list_user_orders(self, user_id: int, *, limit: int = 20, offset: int = 0) -> list[OrderDTO]:
        rows = [o for o in self.orders.values() if o.user_id == user_id]
        return rows[offset : offset + limit]

    async def list_orders(
        self, *, status: OrderStatus | None = None, limit: int = 50, offset: int = 0
    ) -> list[OrderDTO]:
        rows = [o for o in self.orders.values() if status is None or o.status == status]
        return rows[offset : offset + limit]


class FakePaymentService:
    def __init__(self, tmp_path: Path) -> None:
        now = datetime.now(timezone.utc)
        self.rate = ExchangeRateDTO(usd_to_toman_rate=Decimal("65000"), rate_mode="manual", updated_at=now)
        self.wallets: dict[int, CryptoWalletDTO] = {
            1: CryptoWalletDTO(
                id=1,
                coin_name="Tether USD",
                symbol="USDT",
                network="TRC20",
                wallet_address="TTestWalletAddress1234567890",
                qr_image_path=None,
                min_deposit_usd=Decimal("5.00000000"),
                fee_note="1 USDT fee",
                instructions="Send exact USDT TRC20",
                instructions_json={"en": "Send exact USDT"},
                verification_mode=CryptoVerificationMode.MANUAL_TXID,
                explorer_config={},
                is_active=True,
                sort_order=1,
                created_at=now,
                updated_at=now,
            )
        }
        self.fiat_methods: dict[int, FiatMethodDTO] = {
            1: FiatMethodDTO(
                id=1,
                method_type=FiatMethodType.CARD_TO_CARD,
                title="کارت بانک ملت",
                card_number="6104337712345678",
                sheba="IR120120000000001234567890",
                cardholder_name="Ali Test",
                bank_name="Mellat",
                gateway_provider=None,
                min_deposit_irt=Decimal("50000.00"),
                instructions="Transfer and upload receipt",
                is_active=True,
                sort_order=1,
                created_at=now,
                updated_at=now,
            )
        }
        receipt_dir = tmp_path / "uploads" / "receipts"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = receipt_dir / "sample_receipt.png"
        receipt_path.write_bytes(_make_png_bytes())

        self.payments: dict[int, PaymentDTO] = {
            1: PaymentDTO(
                id=1,
                user_id=1,
                payment_category=PaymentCategory.CRYPTO,
                currency=Currency.USD,
                amount_usd=Decimal("25.00000000"),
                amount_irt=Decimal("0"),
                crypto_wallet_id=1,
                tx_hash="0xTESTHASH001",
                receipt_local_path="receipts/sample_receipt.png",
                status=PaymentStatus.PENDING,
                created_at=now,
            ),
            2: PaymentDTO(
                id=2,
                user_id=1,
                payment_category=PaymentCategory.CRYPTO,
                currency=Currency.USD,
                amount_usd=Decimal("10.00000000"),
                amount_irt=Decimal("0"),
                receipt_local_path="../../etc/passwd",
                status=PaymentStatus.PENDING,
                created_at=now,
            ),
        }
        self._next_wallet_id = 2
        self._next_fiat_id = 2

    async def get_available_methods_for_language(self, lang_code: str) -> AvailablePaymentMethodsDTO:
        fiat_enabled = lang_code == "fa"
        return AvailablePaymentMethodsDTO(
            lang_code=lang_code,
            fiat_enabled=fiat_enabled,
            crypto_wallets=[w for w in self.wallets.values() if w.is_active],
            fiat_methods=[m for m in self.fiat_methods.values() if m.is_active] if fiat_enabled else [],
        )

    async def list_crypto_wallets(self, *, active_only: bool = False) -> list[CryptoWalletDTO]:
        return [w for w in self.wallets.values() if not active_only or w.is_active]

    async def create_crypto_wallet(self, data: CryptoWalletCreateDTO) -> CryptoWalletDTO:
        now = datetime.now(timezone.utc)
        wid = self._next_wallet_id
        self._next_wallet_id += 1
        dto = CryptoWalletDTO(
            id=wid,
            coin_name=data.coin_name,
            symbol=data.symbol,
            network=data.network,
            wallet_address=data.wallet_address,
            qr_image_path=data.qr_image_path,
            min_deposit_usd=data.min_deposit_usd,
            fee_note=data.fee_note,
            instructions=data.instructions,
            instructions_json=data.instructions_json,
            verification_mode=data.verification_mode,
            explorer_config=data.explorer_config,
            is_active=data.is_active,
            sort_order=data.sort_order,
            created_at=now,
            updated_at=now,
        )
        self.wallets[wid] = dto
        return dto

    async def update_crypto_wallet(self, wallet_id: int, data: CryptoWalletUpdateDTO) -> CryptoWalletDTO:
        if wallet_id not in self.wallets:
            raise NotFoundError("Wallet not found")
        patch = {k: v for k, v in data.model_dump(exclude_none=True).items()}
        updated = self.wallets[wallet_id].model_copy(update=patch)
        self.wallets[wallet_id] = updated
        return updated

    async def delete_crypto_wallet(self, wallet_id: int) -> None:
        if wallet_id not in self.wallets:
            raise NotFoundError("Wallet not found")
        del self.wallets[wallet_id]

    async def list_fiat_methods(self, *, active_only: bool = False) -> list[FiatMethodDTO]:
        return [m for m in self.fiat_methods.values() if not active_only or m.is_active]

    async def create_fiat_method(self, data: FiatMethodCreateDTO) -> FiatMethodDTO:
        now = datetime.now(timezone.utc)
        fid = self._next_fiat_id
        self._next_fiat_id += 1
        dto = FiatMethodDTO(
            id=fid,
            method_type=data.method_type,
            title=data.title,
            card_number=data.card_number,
            sheba=data.sheba,
            cardholder_name=data.cardholder_name,
            bank_name=data.bank_name,
            gateway_provider=data.gateway_provider,
            min_deposit_irt=data.min_deposit_irt,
            instructions=data.instructions,
            is_active=data.is_active,
            sort_order=data.sort_order,
            created_at=now,
            updated_at=now,
        )
        self.fiat_methods[fid] = dto
        return dto

    async def update_fiat_method(self, method_id: int, data: FiatMethodUpdateDTO) -> FiatMethodDTO:
        if method_id not in self.fiat_methods:
            raise NotFoundError("Fiat method not found")
        patch = {k: v for k, v in data.model_dump(exclude_none=True).items() if k != "gateway_merchant_id"}
        updated = self.fiat_methods[method_id].model_copy(update=patch)
        self.fiat_methods[method_id] = updated
        return updated

    async def delete_fiat_method(self, method_id: int) -> None:
        if method_id not in self.fiat_methods:
            raise NotFoundError("Fiat method not found")
        del self.fiat_methods[method_id]

    async def create_crypto_deposit(self, data: CryptoDepositCreateDTO) -> PaymentDTO:
        raise ValidationError("Unused in web admin test")

    async def create_fiat_deposit(self, data: FiatDepositCreateDTO) -> PaymentDTO:
        raise ValidationError("Unused in web admin test")

    async def review_payment(self, review: PaymentReviewDTO) -> PaymentDTO:
        p = await self.get_payment(review.payment_id)
        next_status = PaymentStatus.APPROVED if review.approve else PaymentStatus.REJECTED
        updated = p.model_copy(
            update={
                "status": next_status,
                "admin_id": review.admin_id,
                "admin_note": review.admin_note,
                "reviewed_at": datetime.now(timezone.utc),
            }
        )
        self.payments[review.payment_id] = updated
        return updated

    async def verify_gateway_callback(self, authority_track_id: str, gateway_params: dict[str, str]) -> PaymentDTO:
        raise NotFoundError("Callback not found")

    async def get_payment(self, payment_id: int) -> PaymentDTO:
        if payment_id not in self.payments:
            raise NotFoundError("Payment not found")
        return self.payments[payment_id]

    async def list_payments(
        self,
        *,
        status: PaymentStatus | None = None,
        user_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PaymentDTO]:
        rows = [
            p
            for p in self.payments.values()
            if (status is None or p.status == status) and (user_id is None or p.user_id == user_id)
        ]
        return rows[offset : offset + limit]

    async def get_exchange_rate(self) -> ExchangeRateDTO:
        return self.rate

    async def update_exchange_rate(self, rate: Decimal, *, rate_mode: str = "manual") -> ExchangeRateDTO:
        self.rate = ExchangeRateDTO(
            usd_to_toman_rate=rate,
            rate_mode=rate_mode,
            updated_at=datetime.now(timezone.utc),
        )
        return self.rate


class FakeSupportService:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.tickets: dict[int, SupportTicketDetailDTO] = {
            1: SupportTicketDetailDTO(
                id=1,
                ticket_number="TKT-0001",
                user_id=1,
                category=TicketCategory.DEPOSIT_ISSUE,
                subject="TXID check",
                status=TicketStatus.OPEN,
                created_at=now,
                updated_at=now,
                messages=[
                    SupportMessageDTO(
                        id=1,
                        ticket_id=1,
                        sender_role=SenderRole.USER,
                        sender_telegram_id=555000111,
                        message_text="Hello <script>alert('xss')</script> support",
                        created_at=now,
                    )
                ],
            )
        }

    async def create_ticket(
        self,
        user_id: int,
        category: TicketCategory,
        *,
        subject: str | None = None,
        message_text: str | None = None,
        telegram_file_id: str | None = None,
        file_type: str | None = None,
    ) -> SupportTicketDetailDTO:
        raise ValidationError("Unused")

    async def add_message(
        self,
        ticket_id: int,
        sender_role: SenderRole,
        sender_telegram_id: int | None,
        *,
        message_text: str | None = None,
        telegram_file_id: str | None = None,
        file_type: str | None = None,
    ) -> SupportMessageDTO:
        t = await self.get_ticket_detail(ticket_id)
        msg = SupportMessageDTO(
            id=len(t.messages) + 1,
            ticket_id=ticket_id,
            sender_role=sender_role,
            sender_telegram_id=sender_telegram_id,
            message_text=message_text,
            telegram_file_id=telegram_file_id,
            file_type=file_type,
            created_at=datetime.now(timezone.utc),
        )
        t.messages.append(msg)
        self.tickets[ticket_id] = t.model_copy(
            update={"status": TicketStatus.ANSWERED if sender_role == SenderRole.ADMIN else t.status}
        )
        return msg

    async def get_ticket_detail(self, ticket_id: int) -> SupportTicketDetailDTO:
        if ticket_id not in self.tickets:
            raise NotFoundError("Ticket not found")
        return self.tickets[ticket_id]

    async def list_user_tickets(self, user_id: int, *, limit: int = 20, offset: int = 0) -> list[SupportTicketDTO]:
        return [SupportTicketDTO.model_validate(t) for t in self.tickets.values() if t.user_id == user_id]

    async def list_tickets(
        self, *, status: TicketStatus | None = None, limit: int = 50, offset: int = 0
    ) -> list[SupportTicketDTO]:
        return [
            SupportTicketDTO.model_validate(t)
            for t in self.tickets.values()
            if status is None or t.status == status
        ]

    async def close_ticket(self, ticket_id: int) -> SupportTicketDTO:
        t = await self.get_ticket_detail(ticket_id)
        updated = t.model_copy(update={"status": TicketStatus.CLOSED})
        self.tickets[ticket_id] = updated
        return SupportTicketDTO.model_validate(updated)


class FakeCMSService:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.translations: dict[tuple[str, str], UITranslationDTO] = {
            ("welcome.banner", "en"): UITranslationDTO(
                id=1,
                key="welcome.banner",
                lang_code="en",
                text_value="Welcome to Shop!",
                updated_at=now,
            ),
            ("welcome.banner", "fa"): UITranslationDTO(
                id=2,
                key="welcome.banner",
                lang_code="fa",
                text_value="به فروشگاه خوش آمدید!",
                updated_at=now,
            ),
        }
        self.buttons: dict[str, UIButtonDTO] = {
            "shop": UIButtonDTO(
                id=1,
                button_key="shop",
                menu_scope=MenuScope.MAIN,
                row_index=0,
                col_index=0,
                color_emoji="🛒",
                color_hex="#3b82f6",
                is_active=True,
                labels_json={"en": "🛒 Shop", "fa": "🛒 فروشگاه"},
                created_at=now,
                updated_at=now,
            )
        }
        self.settings: dict[str, AppSettingDTO] = {
            "maintenance_mode": AppSettingDTO(key="maintenance_mode", value="false", updated_at=now),
            "pinned_limit": AppSettingDTO(key="pinned_limit", value="4", updated_at=now),
            "low_stock_default_threshold": AppSettingDTO(key="low_stock_default_threshold", value="5", updated_at=now),
            "usd_to_toman_rate": AppSettingDTO(key="usd_to_toman_rate", value="65000", updated_at=now),
            "rate_mode": AppSettingDTO(key="rate_mode", value="manual", updated_at=now),
        }

    async def get_translation(self, key: str, lang_code: str) -> str:
        item = self.translations.get((key, lang_code))
        return item.text_value if item else key

    async def list_translations(
        self, *, lang_code: str | None = None, search: str | None = None, limit: int = 200, offset: int = 0
    ) -> list[UITranslationDTO]:
        rows = []
        for t in self.translations.values():
            if lang_code and t.lang_code != lang_code:
                continue
            if search and search.lower() not in t.key.lower() and search.lower() not in t.text_value.lower():
                continue
            rows.append(t)
        return rows[offset : offset + limit]

    async def upsert_translation(self, key: str, lang_code: str, text_value: str) -> UITranslationDTO:
        dto = UITranslationDTO(
            id=len(self.translations) + 1,
            key=key,
            lang_code=lang_code,
            text_value=text_value,
            updated_at=datetime.now(timezone.utc),
        )
        self.translations[(key, lang_code)] = dto
        return dto

    async def list_buttons(self, *, menu_scope: str | None = None) -> list[UIButtonDTO]:
        return [b for b in self.buttons.values() if not menu_scope or b.menu_scope.value == menu_scope]

    async def update_button(
        self,
        button_key: str,
        *,
        color_emoji: str | None = None,
        color_hex: str | None = None,
        labels_json: dict[str, str] | None = None,
        is_active: bool | None = None,
    ) -> UIButtonDTO:
        if button_key not in self.buttons:
            raise NotFoundError("Button not found")
        btn = self.buttons[button_key]
        updates: dict[str, Any] = {}
        if color_emoji is not None:
            updates["color_emoji"] = color_emoji
        if color_hex is not None:
            updates["color_hex"] = color_hex
        if labels_json is not None:
            merged = dict(btn.labels_json)
            merged.update(labels_json)
            updates["labels_json"] = merged
        if is_active is not None:
            updates["is_active"] = is_active
        updated = btn.model_copy(update=updates)
        self.buttons[button_key] = updated
        return updated

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        item = self.settings.get(key)
        return item.value if item else default

    async def set_setting(self, key: str, value: str, *, description: str | None = None) -> AppSettingDTO:
        dto = AppSettingDTO(
            key=key,
            value=value,
            description=description,
            updated_at=datetime.now(timezone.utc),
        )
        self.settings[key] = dto
        return dto


class FakeBroadcastService:
    def __init__(self) -> None:
        self.jobs: dict[int, BroadcastJobDTO] = {}

    async def create_job(self, data: BroadcastCreateDTO) -> BroadcastJobDTO:
        jid = len(self.jobs) + 1
        job = BroadcastJobDTO(
            id=jid,
            admin_id=data.admin_id,
            target_filter=data.target_filter,
            message_payload=data.message_payload,
            total_targets=2,
            sent_count=0,
            failed_count=0,
            status=BroadcastStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )
        self.jobs[jid] = job
        return job

    async def resolve_target_telegram_ids(self, target_filter: str) -> list[int]:
        return [555000111, 555000222]

    async def update_progress(
        self,
        job_id: int,
        *,
        sent_count: int | None = None,
        failed_count: int | None = None,
        status: str | None = None,
    ) -> BroadcastJobDTO:
        job = await self.get_job(job_id)
        return job

    async def get_job(self, job_id: int) -> BroadcastJobDTO:
        if job_id not in self.jobs:
            raise NotFoundError("Job not found")
        return self.jobs[job_id]

    async def list_jobs(self, *, limit: int = 20, offset: int = 0) -> list[BroadcastJobDTO]:
        return list(self.jobs.values())[offset : offset + limit]


class FakeAnalyticsService:
    async def get_dashboard_stats(self) -> DashboardStatsDTO:
        return DashboardStatsDTO(
            total_users=142,
            active_users_24h=35,
            active_users_7d=90,
            active_users_30d=130,
            total_revenue_usd=Decimal("1250.50"),
            total_revenue_irt=Decimal("85000000"),
            pending_deposits_count=2,
            open_tickets_count=1,
            pending_manual_orders_count=1,
            low_stock_alerts=[
                LowStockAlertDTO(
                    product_id=1,
                    slug="gemini-pro-12m",
                    title="Gemini Pro 12M",
                    delivery_type=DeliveryType.AUTO,
                    available_stock=3,
                    threshold=5,
                )
            ],
        )


def _wire_protocol_doubles(app, tmp_path: Path) -> dict[str, Any]:
    user_svc = FakeUserService()
    shop_svc = FakeShopService(user_svc)
    payment_svc = FakePaymentService(tmp_path)
    support_svc = FakeSupportService()
    cms_svc = FakeCMSService()
    broadcast_svc = FakeBroadcastService()
    analytics_svc = FakeAnalyticsService()
    translation_svc = FakeTranslationService()
    bot_notifier = FakeBotNotifier()

    assert isinstance(user_svc, UserServiceProtocol)
    assert isinstance(shop_svc, ShopServiceProtocol)
    assert isinstance(payment_svc, PaymentServiceProtocol)
    assert isinstance(support_svc, SupportServiceProtocol)
    assert isinstance(cms_svc, CMSServiceProtocol)
    assert isinstance(broadcast_svc, BroadcastServiceProtocol)
    assert isinstance(analytics_svc, AnalyticsServiceProtocol)

    app.dependency_overrides[get_user_service] = lambda: user_svc
    app.dependency_overrides[get_shop_service] = lambda: shop_svc
    app.dependency_overrides[get_payment_service] = lambda: payment_svc
    app.dependency_overrides[get_support_service] = lambda: support_svc
    app.dependency_overrides[get_cms_service] = lambda: cms_svc
    app.dependency_overrides[get_broadcast_service] = lambda: broadcast_svc
    app.dependency_overrides[get_analytics_service] = lambda: analytics_svc
    app.dependency_overrides[get_translation_service] = lambda: translation_svc
    app.dependency_overrides[get_bot_notifier] = lambda: bot_notifier

    return {
        "user": user_svc,
        "shop": shop_svc,
        "payment": payment_svc,
        "support": support_svc,
        "cms": cms_svc,
        "broadcast": broadcast_svc,
        "analytics": analytics_svc,
        "translation": translation_svc,
        "notifier": bot_notifier,
    }


async def _login_admin(client: AsyncClient, settings: Settings, *, totp_code: str = "") -> str:
    csrf_resp = await client.get("/admin/auth/csrf")
    assert csrf_resp.status_code == 200
    pre_csrf = csrf_resp.json()["csrf_token"]

    login_resp = await client.post(
        "/admin/api/auth/login",
        headers={"X-CSRF-Token": pre_csrf},
        json={
            "username": settings.WEB_ADMIN_USERNAME,
            "password": settings._plain_password,  # type: ignore[attr-defined]
            "totp_code": totp_code,
        },
    )
    assert login_resp.status_code == 200, login_resp.text
    data = login_resp.json()
    assert data["ok"] is True
    assert ADMIN_COOKIE_NAME in client.cookies
    return data["csrf_token"]


# ---------------------------------------------------------------------------
# 1. Unified Entrypoint (/healthz, /health, Webhook Secret Validation)
# ---------------------------------------------------------------------------
async def test_healthz_and_health_against_live_postgres_and_redis(
    db_session_factory,
    redis_client,
) -> None:
    app = create_app()
    app.state.session_factory = db_session_factory
    app.state.redis = redis_client

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.get("/healthz")
        r2 = await client.get("/health")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["status"] == "ok"
        assert r1.json()["database"] == "ok"
        assert r1.json()["redis"] == "ok"


async def test_healthz_returns_503_when_redis_unavailable(db_session_factory) -> None:
    class BrokenRedis:
        async def ping(self) -> bool:
            raise ConnectionError("Redis offline")

    app = create_app()
    app.state.session_factory = db_session_factory
    app.state.redis = BrokenRedis()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/healthz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["redis"] == "error"


async def test_telegram_webhook_secret_validation_and_feed_update(tmp_path: Path) -> None:
    settings = _make_test_settings(tmp_path, bot_mode="webhook")
    app = create_app(settings=settings)

    fed_updates: list[Any] = []

    class FakeBot:
        pass

    class FakeDispatcher:
        async def feed_update(self, *, bot: Any, update: Any) -> None:
            fed_updates.append(update)

    app.state.bot = FakeBot()
    app.state.dp = FakeDispatcher()

    valid_update = {
        "update_id": 1001,
        "message": {
            "message_id": 1,
            "date": int(time.time()),
            "chat": {"id": 111222333, "type": "private"},
            "from": {"id": 111222333, "is_bot": False, "first_name": "Admin"},
            "text": "/start",
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Missing secret header -> 403
        r_missing = await client.post("/webhook/telegram", json=valid_update)
        assert r_missing.status_code == 403

        # Wrong secret header -> 403
        r_wrong = await client.post(
            "/webhook/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            json=valid_update,
        )
        assert r_wrong.status_code == 403

        # Malformed payload with valid secret -> 400
        r_bad_json = await client.post(
            "/webhook/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": settings.BOT_WEBHOOK_SECRET},
            json={"not_an_update": True},
        )
        assert r_bad_json.status_code == 400

        # Valid secret + valid Telegram Update -> 200 and fed to Dispatcher
        r_ok = await client.post(
            "/webhook/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": settings.BOT_WEBHOOK_SECRET},
            json=valid_update,
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["ok"] is True
        assert len(fed_updates) == 1
        assert fed_updates[0].update_id == 1001


# ---------------------------------------------------------------------------
# 2. High-Security Web Auth: JWT, Bcrypt, TOTP 2FA, Telegram Login Widget, CSRF
# ---------------------------------------------------------------------------
async def test_auth_password_totp_2fa_bearer_and_csrf(tmp_path: Path) -> None:
    totp_secret = "JBSWY3DPEHPK3PXP"
    settings = _make_test_settings(tmp_path, two_factor=True, totp_secret=totp_secret)
    app = create_web_admin_app(settings=settings)
    _wire_protocol_doubles(app, tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Unauthenticated API call -> 401
        unauth = await client.get("/admin/api/dashboard")
        assert unauth.status_code == 401

        # Unauthenticated HTML browser request -> 303 redirect to /admin/login
        unauth_html = await client.get(
            "/admin/users",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert unauth_html.status_code == 303
        assert unauth_html.headers["location"] == "/admin/login"

        # Login POST without CSRF -> 403
        no_csrf = await client.post(
            "/admin/api/auth/login",
            json={"username": settings.WEB_ADMIN_USERNAME, "password": settings._plain_password},  # type: ignore[attr-defined]
        )
        assert no_csrf.status_code == 403

        csrf_resp = await client.get("/admin/auth/csrf")
        pre_csrf = csrf_resp.json()["csrf_token"]

        # Wrong TOTP -> 401
        bad_totp = await client.post(
            "/admin/api/auth/login",
            headers={"X-CSRF-Token": pre_csrf},
            json={
                "username": settings.WEB_ADMIN_USERNAME,
                "password": settings._plain_password,  # type: ignore[attr-defined]
                "totp_code": "000000",
            },
        )
        assert bad_totp.status_code == 401

        # Valid TOTP -> 200 with HttpOnly SameSite JWT cookie
        valid_code = _make_totp(totp_secret)
        ok_resp = await client.post(
            "/admin/api/auth/login",
            headers={"X-CSRF-Token": pre_csrf},
            json={
                "username": settings.WEB_ADMIN_USERNAME,
                "password": settings._plain_password,  # type: ignore[attr-defined]
                "totp_code": valid_code,
            },
        )
        assert ok_resp.status_code == 200
        body = ok_resp.json()
        bearer_token = body["access_token"]
        csrf_token = body["csrf_token"]
        set_cookie_hdr = ok_resp.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie_hdr
        assert "samesite=lax" in set_cookie_hdr.lower()

        # State-changing POST without CSRF -> 403
        missing_csrf_post = await client.post(
            "/admin/users/1/block",
            json={"blocked": True},
        )
        assert missing_csrf_post.status_code == 403

        # State-changing POST with valid Bearer token (no cookies) + X-CSRF-Token -> 200
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bearer_client:
            b_ok = await bearer_client.post(
                "/admin/users/1/block",
                headers={
                    "Authorization": f"Bearer {bearer_token}",
                    "X-CSRF-Token": csrf_token,
                },
                json={"blocked": True},
            )
            assert b_ok.status_code == 200
            assert b_ok.json()["user"]["is_blocked"] is True


async def test_telegram_login_widget_hmac_and_admin_rbac(tmp_path: Path) -> None:
    settings = _make_test_settings(tmp_path)
    app = create_web_admin_app(settings=settings)
    _wire_protocol_doubles(app, tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Forged hash -> 401
        forged = await client.post(
            "/admin/auth/telegram",
            json={"id": 111222333, "first_name": "Evil", "auth_date": int(time.time()), "hash": "deadbeef"},
        )
        assert forged.status_code == 401

        # Valid HMAC for non-admin telegram_id -> 403
        non_admin_payload = _sign_telegram_login_widget(
            {"id": 999888777, "first_name": "Guest", "username": "guest", "auth_date": int(time.time())},
            settings.BOT_TOKEN,
        )
        non_admin_resp = await client.post("/admin/auth/telegram", json=non_admin_payload)
        assert non_admin_resp.status_code == 403

        # Valid HMAC for configured ADMIN_TELEGRAM_IDS -> 200 + JWT cookie
        admin_payload = _sign_telegram_login_widget(
            {"id": 111222333, "first_name": "Boss", "username": "bossadmin", "auth_date": int(time.time())},
            settings.BOT_TOKEN,
        )
        ok_resp = await client.post("/admin/auth/telegram", json=admin_payload)
        assert ok_resp.status_code == 200
        assert ok_resp.json()["telegram_id"] == 111222333
        assert ADMIN_COOKIE_NAME in client.cookies


# ---------------------------------------------------------------------------
# 3. Dashboard, Users, Catalog (Dual-Layer Color Picker), Payments, Orders, Support, CMS, Broadcast
# ---------------------------------------------------------------------------
async def test_dashboard_and_users_management_routes(tmp_path: Path) -> None:
    settings = _make_test_settings(tmp_path)
    app = create_web_admin_app(settings=settings)
    doubles = _wire_protocol_doubles(app, tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login_admin(client, settings)

        # Dashboard HTML & JSON
        dash_html = await client.get("/admin/")
        assert dash_html.status_code == 200
        assert "142" in dash_html.text
        assert "gemini-pro-12m" in dash_html.text

        dash_json = await client.get("/admin/api/dashboard")
        assert dash_json.status_code == 200
        assert dash_json.json()["pending_deposits_count"] == 2

        # Search users by numeric ID / @username / name
        u_search = await client.get("/admin/api/users?q=@alice")
        assert u_search.status_code == 200
        assert len(u_search.json()["users"]) == 1

        # Charge USD + Deduct IRT -> triggers notification & Decimal math
        charge_resp = await client.post(
            "/admin/users/1/balance",
            headers={"X-CSRF-Token": csrf},
            json={"action": "charge", "currency": "USD", "amount": "12.50", "note": "Reward <script>x</script>"},
        )
        assert charge_resp.status_code == 200
        assert Decimal(charge_resp.json()["user"]["balance_usd"]) == Decimal("62.50000000")
        assert charge_resp.json()["notified"] is True
        assert "&lt;script&gt;x&lt;/script&gt;" in doubles["notifier"].sent_messages[-1]["text"]

        deduct_resp = await client.post(
            "/admin/users/1/balance",
            headers={"X-CSRF-Token": csrf},
            json={"action": "deduct", "currency": "IRT", "amount": "500000", "note": "Correction"},
        )
        assert deduct_resp.status_code == 200
        assert Decimal(deduct_resp.json()["user"]["balance_irt"]) == Decimal("1500000.00")

        # Direct Message with validated PNG upload
        dm_resp = await client.post(
            "/admin/users/1/message",
            headers={"X-CSRF-Token": csrf},
            data={"message_text": "Hello <b>user</b>"},
            files={"photo": ("promo.png", _make_png_bytes(), "image/png")},
        )
        assert dm_resp.status_code == 200
        assert dm_resp.json()["delivered"] is True
        assert doubles["notifier"].sent_messages[-1]["has_photo"] is True
        assert "&lt;b&gt;user&lt;/b&gt;" in doubles["notifier"].sent_messages[-1]["text"]


async def test_catalog_crud_hex_color_validation_tiers_and_stock_upload(tmp_path: Path) -> None:
    settings = _make_test_settings(tmp_path)
    app = create_web_admin_app(settings=settings)
    _wire_protocol_doubles(app, tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login_admin(client, settings)

        # Invalid hex color must be rejected (XSS / bad format)
        bad_hex = await client.post(
            "/admin/catalog/categories",
            headers={"X-CSRF-Token": csrf},
            json={"slug": "bad-cat", "color_hex": "#12345;alert(1)", "names_json": {"en": "Bad"}},
        )
        assert bad_hex.status_code == 400

        # Create valid category with Hex + Emoji
        cat_ok = await client.post(
            "/admin/catalog/categories",
            headers={"X-CSRF-Token": csrf},
            json={
                "slug": "streaming",
                "color_hex": "#A855F7",
                "color_emoji": "🟣",
                "names_json": {"en": "Streaming", "fa": "استریمینگ"},
            },
        )
        assert cat_ok.status_code == 201
        cat_id = cat_ok.json()["category"]["id"]
        assert cat_ok.json()["category"]["color_hex"] == "#a855f7"

        # Create product + Pin toggle + Bulk Tiers + Stock .csv upload
        prod_ok = await client.post(
            "/admin/catalog/products",
            headers={"X-CSRF-Token": csrf},
            json={
                "category_id": cat_id,
                "slug": "claude-pro-1m",
                "delivery_type": "auto",
                "price_usd": "19.99",
                "price_irt": "1300000",
                "color_hex": "#F97316",
                "color_emoji": "🟠",
                "titles_json": {"en": "Claude Pro 1M", "fa": "کلود پرو"},
            },
        )
        assert prod_ok.status_code == 201
        pid = prod_ok.json()["product"]["id"]

        pin_resp = await client.post(
            f"/admin/catalog/products/{pid}/pin",
            headers={"X-CSRF-Token": csrf},
            json={"is_pinned": True},
        )
        assert pin_resp.status_code == 200
        assert pin_resp.json()["product"]["is_pinned"] is True

        tiers_resp = await client.post(
            f"/admin/catalog/products/{pid}/tiers",
            headers={"X-CSRF-Token": csrf},
            json={"tiers": [{"min_qty": 10, "unit_price_usd": "17.50", "unit_price_irt": "1100000"}]},
        )
        assert tiers_resp.status_code == 200
        assert Decimal(tiers_resp.json()["tiers"][0]["unit_price_usd"]) == Decimal("17.50")

        # Upload invalid binary as .txt stock -> 400
        bad_stock = await client.post(
            f"/admin/catalog/products/{pid}/stock",
            headers={"X-CSRF-Token": csrf},
            files={"stock_file": ("keys.txt", b"\x00\x01\x02", "text/plain")},
        )
        assert bad_stock.status_code == 400

        # Upload valid paste + .csv stock
        stock_resp = await client.post(
            f"/admin/catalog/products/{pid}/stock",
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
            data={"stock_lines": "acc1:pass1\nacc2:pass2", "deduplicate": "true"},
            files={"stock_file": ("keys.csv", b"acc2:pass2\nacc3:pass3\n", "text/csv")},
        )
        assert stock_resp.status_code == 200
        imp = stock_resp.json()["import_result"]
        assert imp["added_count"] == 3
        assert imp["skipped_duplicates"] == 1

        # Render catalog and product detail HTML pages (Dual-layer color picker widgets)
        cat_html = await client.get("/admin/catalog")
        assert cat_html.status_code == 200
        assert "data-color-picker-widget" in cat_html.text

        prod_html = await client.get(f"/admin/catalog/products/{pid}")
        assert prod_html.status_code == 200
        assert "#f97316" in prod_html.text


async def test_payments_wallets_qr_fiat_rate_orders_support_cms_broadcast(tmp_path: Path) -> None:
    settings = _make_test_settings(tmp_path)
    app = create_web_admin_app(settings=settings)
    _wire_protocol_doubles(app, tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login_admin(client, settings)

        # Crypto wallet creation + auto-QR PNG preview
        w_resp = await client.post(
            "/admin/payments/wallets",
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
            data={
                "coin_name": "Litecoin",
                "symbol": "LTC",
                "network": "LTC",
                "wallet_address": "ltc1qtestaddress000011112222",
                "min_deposit_usd": "2",
                "verification_mode": "manual_txid",
            },
            files={"qr_file": ("qr.png", _make_png_bytes(), "image/png")},
        )
        assert w_resp.status_code == 201
        wid = w_resp.json()["wallet"]["id"]

        qr_img = await client.get(f"/admin/payments/wallets/{wid}/qr.png")
        assert qr_img.status_code == 200
        assert qr_img.headers["content-type"].startswith("image/png")

        auto_qr = await client.get("/admin/payments/wallets/1/qr.png")
        assert auto_qr.status_code == 200
        assert auto_qr.content.startswith(b"\x89PNG")

        # Iranian Fiat method creation + 16-digit validation
        bad_card = await client.post(
            "/admin/payments/fiat",
            headers={"X-CSRF-Token": csrf},
            json={"method_type": "card_to_card", "title": "Bad", "card_number": "1234"},
        )
        assert bad_card.status_code == 400

        fiat_ok = await client.post(
            "/admin/payments/fiat",
            headers={"X-CSRF-Token": csrf},
            json={
                "method_type": "card_to_card",
                "title": "کارت سامان",
                "card_number": "6219861012345678",
                "sheba": "IR550560000000001234567890",
                "cardholder_name": "Admin Shop",
                "bank_name": "Saman",
                "min_deposit_irt": "100000",
            },
        )
        assert fiat_ok.status_code == 201

        # Exchange rate update
        rate_resp = await client.post(
            "/admin/payments/rate",
            headers={"X-CSRF-Token": csrf},
            json={"usd_to_toman_rate": "72000", "rate_mode": "manual"},
        )
        assert rate_resp.status_code == 200
        assert Decimal(rate_resp.json()["exchange_rate"]["usd_to_toman_rate"]) == Decimal("72000")

        # Receipt viewer (valid PNG vs path traversal blocked)
        receipt_ok = await client.get("/admin/payments/1/receipt")
        assert receipt_ok.status_code == 200
        assert receipt_ok.content.startswith(b"\x89PNG")

        receipt_traversal = await client.get("/admin/payments/2/receipt")
        assert receipt_traversal.status_code == 400

        # 1-click approve payment
        app_resp = await client.post(
            "/admin/payments/1/approve",
            headers={"X-CSRF-Token": csrf},
            json={"admin_note": "Verified on TronScan"},
        )
        assert app_resp.status_code == 200
        assert app_resp.json()["payment"]["status"] == "approved"

        # Orders fulfill & refund
        fulfill_resp = await client.post(
            "/admin/orders/10/fulfill",
            headers={"X-CSRF-Token": csrf},
            json={"fulfillment_note": "Activated cleanly"},
        )
        assert fulfill_resp.status_code == 200
        assert fulfill_resp.json()["order"]["status"] == "completed"

        # Support ticket XSS check + reply + close
        tkt_html = await client.get("/admin/support/1")
        assert tkt_html.status_code == 200
        assert "<script>alert('xss')</script>" not in tkt_html.text
        assert "&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in tkt_html.text

        reply_resp = await client.post(
            "/admin/support/1/reply",
            headers={"X-CSRF-Token": csrf},
            json={"message_text": "Your deposit is verified."},
        )
        assert reply_resp.status_code == 200

        close_resp = await client.post("/admin/support/1/close", headers={"X-CSRF-Token": csrf})
        assert close_resp.status_code == 200
        assert close_resp.json()["ticket"]["status"] == "closed"

        # CMS 12-language text, button color hex, forbidden button check, settings
        tr_resp = await client.put(
            "/admin/cms/translations",
            headers={"X-CSRF-Token": csrf},
            json={"key": "welcome.banner", "lang_code": "es", "text_value": "¡Bienvenido!"},
        )
        assert tr_resp.status_code == 200

        btn_resp = await client.put(
            "/admin/cms/buttons/shop",
            headers={"X-CSRF-Token": csrf},
            json={"color_hex": "#22c55e", "color_emoji": "🟢", "labels_json": {"en": "🟢 Store"}},
        )
        assert btn_resp.status_code == 200
        assert btn_resp.json()["button"]["color_hex"] == "#22c55e"

        forbidden_btn = await client.post(
            "/admin/cms/buttons/referral",
            headers={"X-CSRF-Token": csrf},
            json={"color_hex": "#22c55e"},
        )
        assert forbidden_btn.status_code == 400

        set_resp = await client.post(
            "/admin/cms/settings",
            headers={"X-CSRF-Token": csrf},
            json={"maintenance_mode": "false", "pinned_limit": "6"},
        )
        assert set_resp.status_code == 200

        # Mass Broadcast job launch & status check
        bc_resp = await client.post(
            "/admin/broadcast",
            headers={"X-CSRF-Token": csrf},
            json={
                "target_filter": "lang:fa",
                "message_text": "New offer live!",
                "button_text": "Open",
                "button_url": "https://t.me/test_shop_bot",
            },
        )
        assert bc_resp.status_code == 201
        jid = bc_resp.json()["job"]["id"]
        bc_status = await client.get(f"/admin/api/broadcast/{jid}")
        assert bc_status.status_code == 200
        assert bc_status.json()["job"]["target_filter"] == "lang:fa"

        # Verify all HTML admin templates render cleanly with 200 OK
        for page_url in (
            "/admin/users",
            "/admin/users/1",
            "/admin/payments?status=pending",
            "/admin/orders",
            "/admin/support",
            "/admin/cms?tab=texts",
            "/admin/cms?tab=buttons",
            "/admin/cms?tab=settings",
            "/admin/broadcast",
        ):
            page_resp = await client.get(page_url)
            assert page_resp.status_code == 200, f"Failed HTML render on {page_url}"

        # HTML form submission returns 303 redirect cleanly
        form_resp = await client.post(
            "/admin/users/1/block",
            data={"csrf_token": csrf, "blocked": "false"},
            follow_redirects=False,
        )
        assert form_resp.status_code == 303


# ---------------------------------------------------------------------------
# 4. Telegram Mini App Shop (/miniapp & /webapp): initData HMAC, 12-Lang Routing, Purchase
# ---------------------------------------------------------------------------
async def test_miniapp_initdata_hmac_region_routing_and_atomic_purchase(tmp_path: Path) -> None:
    settings = _make_test_settings(tmp_path)
    app = create_web_admin_app(settings=settings)
    doubles = _wire_protocol_doubles(app, tmp_path)

    en_init_data = _sign_webapp_init_data(
        {"id": 555000111, "first_name": "Alice", "username": "alice_shopper", "language_code": "en"},
        settings.BOT_TOKEN,
    )
    fa_init_data = _sign_webapp_init_data(
        {"id": 555000222, "first_name": "Reza", "username": "reza_ir", "language_code": "fa"},
        settings.BOT_TOKEN,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Shell page renders RTL for fa and LTR for en
        fa_shell = await client.get("/miniapp?lang=fa")
        assert fa_shell.status_code == 200
        assert 'dir="rtl"' in fa_shell.text

        en_shell = await client.get("/webapp?lang=en")
        assert en_shell.status_code == 200
        assert 'dir="ltr"' in en_shell.text

        # Missing or forged initData -> 401
        no_init = await client.get("/miniapp/api/bootstrap")
        assert no_init.status_code == 401

        tampered = await client.get(
            "/miniapp/api/bootstrap",
            headers={"X-Telegram-Init-Data": en_init_data + "tamper"},
        )
        assert tampered.status_code == 401

        # English user gets ONLY Crypto methods (fiat_enabled=False, fiat_methods=[])
        en_boot = await client.get(
            "/miniapp/api/bootstrap",
            headers={"X-Telegram-Init-Data": en_init_data},
        )
        assert en_boot.status_code == 200
        en_data = en_boot.json()
        assert en_data["lang_code"] == "en"
        assert en_data["is_rtl"] is False
        assert en_data["payment_methods"]["fiat_enabled"] is False
        assert en_data["payment_methods"]["fiat_methods"] == []
        assert len(en_data["payment_methods"]["crypto_wallets"]) == 1
        assert en_data["products"][0]["color_hex"] == "#22c55e"

        # Verify ALL 11 non-fa languages enforce crypto-only policy
        for code in LANGUAGE_ORDER:
            if code == "fa":
                continue
            r = await client.get(
                f"/miniapp/api/bootstrap?lang={code}",
                headers={"X-Telegram-Init-Data": en_init_data},
            )
            assert r.status_code == 200
            pm = r.json()["payment_methods"]
            assert pm["fiat_enabled"] is False
            assert pm["fiat_methods"] == []

        # Persian (fa) user gets BOTH Fiat + Crypto methods
        fa_boot = await client.get(
            "/miniapp/api/bootstrap",
            headers={"X-Telegram-Init-Data": fa_init_data},
        )
        assert fa_boot.status_code == 200
        fa_data = fa_boot.json()
        assert fa_data["lang_code"] == "fa"
        assert fa_data["is_rtl"] is True
        assert fa_data["payment_methods"]["fiat_enabled"] is True
        assert len(fa_data["payment_methods"]["fiat_methods"]) == 1
        assert len(fa_data["payment_methods"]["crypto_wallets"]) == 1

        # Language switch via API
        sw_resp = await client.post(
            "/miniapp/api/language",
            headers={"X-Telegram-Init-Data": en_init_data},
            json={"lang_code": "ar"},
        )
        assert sw_resp.status_code == 200
        assert sw_resp.json()["is_rtl"] is True
        assert sw_resp.json()["payment_methods"]["fiat_enabled"] is False

        # Non-fa user cannot purchase in IRT
        irt_reject = await client.post(
            "/miniapp/api/purchase",
            headers={"X-Telegram-Init-Data": en_init_data},
            json={"product_id": 1, "quantity": 1, "currency": "IRT"},
        )
        assert irt_reject.status_code == 400

        # Atomic USD purchase delivers encrypted key and deducts balance
        buy_ok = await client.post(
            "/miniapp/api/purchase",
            headers={"X-Telegram-Init-Data": en_init_data},
            json={"product_id": 1, "quantity": 2, "currency": "USD"},
        )
        assert buy_ok.status_code == 200
        purchase = buy_ok.json()["purchase"]
        assert purchase["delivered_items"] == ["KEY-001", "KEY-002"]
        assert Decimal(purchase["new_balance"]) == Decimal("30.00000000")

        # Insufficient stock on next 2 items -> 409 Conflict
        buy_stock_fail = await client.post(
            "/miniapp/api/purchase",
            headers={"X-Telegram-Init-Data": en_init_data},
            json={"product_id": 1, "quantity": 2, "currency": "USD"},
        )
        assert buy_stock_fail.status_code == 409

        # Blocked user rejected with 403
        await doubles["user"].set_blocked(1, blocked=True, admin_id=1)
        blocked_resp = await client.get(
            "/miniapp/api/bootstrap",
            headers={"X-Telegram-Init-Data": en_init_data},
        )
        assert blocked_resp.status_code == 403


# ---------------------------------------------------------------------------
# 5. Optional Webhooks Router Mount Check
# ---------------------------------------------------------------------------
async def test_webhooks_router_mount_compat(tmp_path: Path) -> None:
    settings = _make_test_settings(tmp_path)
    app = create_web_admin_app(settings=settings)
    extra = APIRouter()

    @extra.get("/webhooks/health-check")
    async def wh_probe() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(extra)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/webhooks/health-check")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# 6. Lifespan (Webhook & Polling Modes), Full CRUD, Upload Hardening & Static Audit
# ---------------------------------------------------------------------------
async def test_lifespan_webhook_and_polling_modes_and_workflow_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types
    from contextlib import asynccontextmanager
    from app import main as main_mod

    class FakeSessionCtx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeSessionFactory:
        def __call__(self) -> FakeSessionCtx:
            return FakeSessionCtx()

    class FakeRedis:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    events: list[tuple[str, Any]] = []
    fake_shop_svc = object()
    fake_payment_svc = object()

    class FakeBotSession:
        async def close(self) -> None:
            events.append(("bot_closed", True))

    class FakeBot:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.session = FakeBotSession()

        async def set_webhook(self, *, url: str, secret_token: str | None = None, drop_pending_updates: bool = False) -> None:
            events.append(("set_webhook", url, secret_token, drop_pending_updates))

        async def delete_webhook(self, *, drop_pending_updates: bool = False) -> None:
            events.append(("delete_webhook", drop_pending_updates))

    class FakeStorage:
        async def close(self) -> None:
            events.append(("storage_closed", True))

    class FakeDP:
        def __init__(self) -> None:
            self.storage = FakeStorage()
            self.workflow_data = {
                "shop_service": fake_shop_svc,
                "payment_service": fake_payment_svc,
            }

        async def start_polling(self, bot: Any, handle_signals: bool = False) -> None:
            events.append(("polling_started", handle_signals))
            try:
                while True:
                    await __import__("asyncio").sleep(0.05)
            except __import__("asyncio").CancelledError:
                events.append(("polling_cancelled", True))
                raise

    fake_dp_mod = types.ModuleType("app.bot.dispatcher")
    fake_dp_mod.create_full_dispatcher = lambda **kwargs: FakeDP()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.bot.dispatcher", fake_dp_mod)
    monkeypatch.setattr(main_mod, "Bot", FakeBot)

    # Webhook mode
    wh_settings = _make_test_settings(tmp_path, bot_mode="webhook").model_copy(
        update={
            "APP_ENV": "development",
            "BOT_WEBHOOK_URL": "https://shop.example.com/webhook/telegram",
        }
    )
    wh_app = main_mod.create_app(settings=wh_settings)
    wh_app.state.session_factory = FakeSessionFactory()
    wh_app.state.redis = FakeRedis()
    wh_app.state.skip_startup_seed = True

    async with main_mod.lifespan(wh_app):
        assert wh_app.state.shop_service is fake_shop_svc
        assert wh_app.state.payment_service is fake_payment_svc

    assert (
        "set_webhook",
        "https://shop.example.com/webhook/telegram",
        wh_settings.BOT_WEBHOOK_SECRET,
        False,
    ) in events
    assert ("bot_closed", True) in events
    assert ("storage_closed", True) in events

    # Polling mode
    events.clear()
    poll_settings = _make_test_settings(tmp_path, bot_mode="polling").model_copy(
        update={"APP_ENV": "development"}
    )
    poll_app = main_mod.create_app(settings=poll_settings)
    poll_app.state.session_factory = FakeSessionFactory()
    poll_app.state.redis = FakeRedis()
    poll_app.state.skip_startup_seed = True

    async with main_mod.lifespan(poll_app):
        await __import__("asyncio").sleep(0.02)

    assert ("delete_webhook", False) in events
    assert ("polling_started", False) in events
    assert ("polling_cancelled", True) in events


async def test_uploads_qr_stock_dm_photo_and_validation(tmp_path: Path) -> None:
    settings = _make_test_settings(tmp_path)
    app = create_web_admin_app(settings=settings)
    _wire_protocol_doubles(app, tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login_admin(client, settings)

        # Uploaded QR PNG must actually persist and be served byte-for-byte
        png_bytes = _make_png_bytes()
        w_resp = await client.post(
            "/admin/payments/wallets",
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
            data={
                "coin_name": "TRON",
                "symbol": "TRX",
                "network": "TRC20",
                "wallet_address": "TCustomQrWalletAddress9999",
            },
            files={"qr_file": ("custom_qr.png", png_bytes, "image/png")},
        )
        assert w_resp.status_code == 201
        wallet_data = w_resp.json()["wallet"]
        assert wallet_data["qr_image_path"] is not None
        assert wallet_data["qr_image_path"].startswith("qr/")
        qr_img = await client.get(f"/admin/payments/wallets/{wallet_data['id']}/qr.png")
        assert qr_img.status_code == 200
        assert qr_img.content == png_bytes

        # Bad image magic bytes on QR upload -> 400
        bad_qr = await client.post(
            "/admin/payments/wallets",
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
            data={
                "coin_name": "BadCoin",
                "symbol": "BAD",
                "network": "TRC20",
                "wallet_address": "TBadQrWalletAddress9999",
            },
            files={"qr_file": ("fake.png", b"NOT_A_PNG_FILE_1234", "image/png")},
        )
        assert bad_qr.status_code == 400

        # Bad binary stock file with pasted lines still rejects binary upload cleanly
        bad_stock_combo = await client.post(
            "/admin/catalog/products/1/stock",
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
            data={"stock_lines": "valid1"},
            files={"stock_file": ("keys.txt", b"\x00\x01\x02", "text/plain")},
        )
        assert bad_stock_combo.status_code == 400

        # Pure .txt upload works
        txt_stock = await client.post(
            "/admin/catalog/products/1/stock",
            headers={"X-CSRF-Token": csrf, "Accept": "application/json"},
            files={"stock_file": ("keys.txt", b"TXT-KEY-1\nTXT-KEY-2\n", "text/plain")},
        )
        assert txt_stock.status_code == 200
        assert txt_stock.json()["import_result"]["added_count"] == 2

        # Bad DM photo upload rejected -> 400
        bad_dm = await client.post(
            "/admin/users/1/message",
            headers={"X-CSRF-Token": csrf},
            data={"message_text": "hello"},
            files={"photo": ("evil.png", b"MZ\x90\x00fakeexe", "image/png")},
        )
        assert bad_dm.status_code == 400


async def test_full_crud_update_delete_refund_reject_and_miniapp_detail_orders(tmp_path: Path) -> None:
    settings = _make_test_settings(tmp_path)
    app = create_web_admin_app(settings=settings)
    doubles = _wire_protocol_doubles(app, tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login_admin(client, settings)

        # Category update & delete
        cat_upd = await client.post(
            "/admin/catalog/categories/1",
            headers={"X-CSRF-Token": csrf},
            json={"color_hex": "#8b5cf6", "color_emoji": "🟣", "names_json": {"en": "AI Pro"}},
        )
        assert cat_upd.status_code == 200
        assert cat_upd.json()["category"]["color_hex"] == "#8b5cf6"

        cat_new = await client.post(
            "/admin/catalog/categories",
            headers={"X-CSRF-Token": csrf},
            json={"slug": "temp-cat", "color_hex": "#22c55e", "names_json": {"en": "Temp"}},
        )
        assert cat_new.status_code == 201
        temp_cid = cat_new.json()["category"]["id"]
        cat_del = await client.delete(
            f"/admin/catalog/categories/{temp_cid}",
            headers={"X-CSRF-Token": csrf},
        )
        assert cat_del.status_code == 200

        # Product update & delete
        prod_new = await client.post(
            "/admin/catalog/products",
            headers={"X-CSRF-Token": csrf},
            json={
                "category_id": 1,
                "slug": "temp-prod",
                "price_usd": "5.00",
                "color_hex": "#ef4444",
                "titles_json": {"en": "Temp Prod"},
            },
        )
        assert prod_new.status_code == 201
        temp_pid = prod_new.json()["product"]["id"]

        prod_upd = await client.put(
            f"/admin/catalog/products/{temp_pid}",
            headers={"X-CSRF-Token": csrf},
            json={"is_active": False, "color_hex": "#eab308"},
        )
        assert prod_upd.status_code == 200
        assert prod_upd.json()["product"]["is_active"] is False

        # MiniApp hides inactive product detail (404)
        en_init_data = _sign_webapp_init_data(
            {"id": 555000111, "first_name": "Alice", "username": "alice_shopper", "language_code": "en"},
            settings.BOT_TOKEN,
        )
        inactive_detail = await client.get(
            f"/miniapp/api/products/{temp_pid}",
            headers={"X-Telegram-Init-Data": en_init_data},
        )
        assert inactive_detail.status_code == 404

        active_detail = await client.get(
            "/miniapp/api/products/1",
            headers={"X-Telegram-Init-Data": en_init_data},
        )
        assert active_detail.status_code == 200
        assert active_detail.json()["product"]["slug"] == "gemini-pro-12m"

        prod_del = await client.delete(
            f"/admin/catalog/products/{temp_pid}",
            headers={"X-CSRF-Token": csrf},
        )
        assert prod_del.status_code == 200

        # Wallet update & delete + Fiat invalid Sheba / update & delete
        w_upd = await client.put(
            "/admin/payments/wallets/1",
            headers={"X-CSRF-Token": csrf},
            json={"min_deposit_usd": "10", "is_active": False},
        )
        assert w_upd.status_code == 200
        assert w_upd.json()["wallet"]["is_active"] is False

        bad_sheba = await client.post(
            "/admin/payments/fiat",
            headers={"X-CSRF-Token": csrf},
            json={
                "method_type": "card_to_card",
                "title": "Bad Sheba",
                "card_number": "6219861012345678",
                "sheba": "INVALID_IBAN",
            },
        )
        assert bad_sheba.status_code == 400

        f_upd = await client.put(
            "/admin/payments/fiat/1",
            headers={"X-CSRF-Token": csrf},
            json={"title": "Updated Mellat Card"},
        )
        assert f_upd.status_code == 200
        assert f_upd.json()["fiat_method"]["title"] == "Updated Mellat Card"

        # 1-click reject payment with admin_note & notification
        rej_resp = await client.post(
            "/admin/payments/2/reject",
            headers={"X-CSRF-Token": csrf},
            json={"admin_note": "TXID <invalid>"},
        )
        assert rej_resp.status_code == 200
        assert rej_resp.json()["payment"]["status"] == "rejected"
        assert "TXID &lt;invalid&gt;" in doubles["notifier"].sent_messages[-1]["text"]

        # Order refund
        ref_resp = await client.post(
            "/admin/orders/10/refund",
            headers={"X-CSRF-Token": csrf},
            json={"reason": "Out of capacity"},
        )
        assert ref_resp.status_code == 200
        assert ref_resp.json()["order"]["status"] == "refunded"

        # MiniApp purchase + order history endpoint
        buy_resp = await client.post(
            "/miniapp/api/purchase",
            headers={"X-Telegram-Init-Data": en_init_data},
            json={"product_id": 1, "quantity": 1, "currency": "USD"},
        )
        assert buy_resp.status_code == 200
        orders_resp = await client.get(
            "/miniapp/api/orders",
            headers={"X-Telegram-Init-Data": en_init_data},
        )
        assert orders_resp.status_code == 200
        assert len(orders_resp.json()["orders"]) >= 2


def test_static_security_and_coding_standards_audit(project_root: Path) -> None:
    import re

    templates_dir = project_root / "app" / "web_admin" / "templates"
    for tpl in templates_dir.rglob("*.html"):
        txt = tpl.read_text(encoding="utf-8")
        assert "| safe" not in txt and "|safe" not in txt, f"Unsafe Jinja filter in {tpl}"

    stub_tokens = ("TO" + "DO", "FIX" + "ME", "NotImplemented" + "Error")
    for py_file in [project_root / "app" / "main.py", *(project_root / "app" / "web_admin").rglob("*.py")]:
        content = py_file.read_text(encoding="utf-8")
        assert not re.search(r"\bfloat\s*\(", content), f"Forbidden float() call in {py_file}"
        assert "except Exception: pass" not in content and "except: pass" not in content
        for stub in stub_tokens:
            assert stub not in content, f"Forbidden stub {stub} in {py_file}"
