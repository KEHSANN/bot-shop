"""Comprehensive unit, integration, concurrency, gateway, webhook, and bot handler tests for Payment Engine."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI
from httpx import ASGITransport
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers.deposit import (
    cb_admin_review_deposit_notification,
    cb_open_deposit_menu,
    cb_select_crypto_wallet,
    cb_select_fiat_method,
    msg_enter_crypto_amount,
    msg_enter_fiat_amount,
    msg_submit_crypto_proof,
    msg_submit_fiat_receipt,
)
from app.bot.keyboards.common_kb import DepCB, PayCB
from app.bot.keyboards.deposit_kb import build_deposit_methods_kb
from app.bot.states import DepositStates
from app.config import Settings
from app.core.constants import (
    CryptoVerificationMode,
    Currency,
    FiatMethodType,
    LANGUAGE_ORDER,
    PaymentCategory,
    PaymentStatus,
    WalletLedgerRefType,
)
from app.core.exceptions import (
    DuplicateTransactionError,
    FileValidationError,
    InvalidStateTransitionError,
    ValidationError,
)
from app.core.security import decrypt_secret, detect_image_mime
from app.database.models import (
    CryptoWallet,
    FiatPaymentMethod,
    Payment,
    User,
    WalletLedger,
)
from app.locales.i18n import TranslationService
from app.services.dtos import (
    CryptoDepositCreateDTO,
    CryptoWalletCreateDTO,
    CryptoWalletUpdateDTO,
    FiatDepositCreateDTO,
    FiatMethodCreateDTO,
    FiatMethodUpdateDTO,
    PaymentDTO,
    PaymentReviewDTO,
)
from app.services.gateways import (
    BscScanExplorer,
    IDPayGateway,
    NOWPaymentsGateway,
    NextPayGateway,
    OxapayGateway,
    TronGridExplorer,
    ZarinPalGateway,
    ZibalGateway,
)
from app.services.interfaces import PaymentServiceProtocol
from app.services.payment_service import PaymentService
from app.web_admin.webhooks import get_payment_service, router as webhooks_router


async def _cleanup_payment_test_data(session_factory: async_sessionmaker[AsyncSession], tg_ids: list[int]) -> None:
    """Clean up isolated test users, ledgers, payments, wallets, and fiat methods."""
    async with session_factory() as session:
        async with session.begin():
            user_ids = (
                await session.execute(select(User.id).where(User.telegram_id.in_(tg_ids)))
            ).scalars().all()
            if user_ids:
                await session.execute(delete(WalletLedger).where(WalletLedger.user_id.in_(user_ids)))
                await session.execute(delete(Payment).where(Payment.user_id.in_(user_ids)))
                await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.execute(
                delete(CryptoWallet).where(CryptoWallet.coin_name.like("TEST_COIN_%"))
            )
            await session.execute(
                delete(FiatPaymentMethod).where(FiatPaymentMethod.title.like("TEST_FIAT_%"))
            )


async def _create_test_user(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    *,
    lang_code: str = "en",
    is_admin: bool = False,
) -> User:
    async with session_factory() as session:
        async with session.begin():
            user = User(
                telegram_id=telegram_id,
                username=f"pay_user_{telegram_id}",
                first_name="PayTest",
                language_code=lang_code,
                balance_usd=Decimal("0.00000000"),
                balance_irt=Decimal("0.00"),
                is_admin=is_admin,
            )
            session.add(user)
            await session.flush()
            await session.refresh(user)
            return user


# ---------------------------------------------------------------------------
# 1. Protocol Conformance & All 12-Language Region Routing Tests
# ---------------------------------------------------------------------------
async def test_protocol_conformance_and_12_language_region_routing(
    db_session_factory: async_sessionmaker[AsyncSession],
    app_settings: Settings,
) -> None:
    service = PaymentService(session_factory=db_session_factory, settings=app_settings)
    assert isinstance(service, PaymentServiceProtocol)

    try:
        wallet = await service.create_crypto_wallet(
            CryptoWalletCreateDTO(
                coin_name="TEST_COIN_USDT",
                symbol="USDT",
                network="TRC20",
                wallet_address="T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
                min_deposit_usd=Decimal("5.00"),
                fee_note="1 USDT network fee",
                instructions="Send TRC20 USDT only.",
                is_active=True,
                sort_order=1,
            )
        )
        fiat_card = await service.create_fiat_method(
            FiatMethodCreateDTO(
                method_type=FiatMethodType.CARD_TO_CARD,
                title="TEST_FIAT_MELLAT",
                card_number="6104-3377-8899-0011",
                sheba="IR120120000000009876543210",
                cardholder_name="علی محمدی",
                bank_name="بانک ملت",
                min_deposit_irt=Decimal("100000"),
                is_active=True,
                sort_order=1,
            )
        )

        assert len(LANGUAGE_ORDER) == 12
        for lang in LANGUAGE_ORDER:
            avail = await service.get_available_methods_for_language(lang)
            kb = build_deposit_methods_kb(avail)
            packed_cbs = [
                btn.callback_data
                for row in kb.inline_keyboard
                for btn in row
                if btn.callback_data
            ]

            assert any(w.id == wallet.id for w in avail.crypto_wallets)
            assert any(cb == DepCB(kind="crypto", id=wallet.id, action="select").pack() for cb in packed_cbs)

            if lang == "fa":
                assert avail.fiat_enabled is True
                assert any(m.id == fiat_card.id for m in avail.fiat_methods)
                assert any(
                    cb == DepCB(kind="fiat", id=fiat_card.id, action="select").pack()
                    for cb in packed_cbs
                )
            else:
                assert avail.fiat_enabled is False, f"Fiat must be disabled for {lang}"
                assert avail.fiat_methods == [], f"Fiat methods list must be empty for {lang}"
                assert not any("dep:fiat:" in cb for cb in packed_cbs), (
                    f"Keyboard for {lang} must never render fiat buttons"
                )
    finally:
        await _cleanup_payment_test_data(db_session_factory, [])


# ---------------------------------------------------------------------------
# 2. CryptoWallet & FiatPaymentMethod CRUD + QR Generation + Fernet Encryption
# ---------------------------------------------------------------------------
async def test_crypto_and_fiat_crud_qr_and_encryption(
    db_session_factory: async_sessionmaker[AsyncSession],
    app_settings: Settings,
    tmp_path: Path,
) -> None:
    custom_settings = app_settings.model_copy(update={"UPLOAD_DIR": str(tmp_path)})
    service = PaymentService(session_factory=db_session_factory, settings=custom_settings)

    try:
        w1 = await service.create_crypto_wallet(
            CryptoWalletCreateDTO(
                coin_name="TEST_COIN_TON",
                symbol="ton",
                network="TON",
                wallet_address="EQD4FPq-PRDieyQKkizFTRtSDyucUIqrj0v_zXJmqaDp6_0t",
                min_deposit_usd=Decimal("2.5"),
                verification_mode=CryptoVerificationMode.MANUAL_TXID,
            )
        )
        assert w1.symbol == "TON"
        assert w1.min_deposit_usd == Decimal("2.50000000")

        # Auto-generated QR PNG bytes check
        qr_bytes = await service.get_wallet_qr_bytes(w1.id)
        assert detect_image_mime(qr_bytes) == "image/png"

        # Custom QR upload validation (reject bad magic bytes, accept valid PNG)
        with pytest.raises(FileValidationError):
            await service.save_wallet_qr_image(w1.id, b"not-an-image", filename="bad.png")

        w1_with_qr = await service.save_wallet_qr_image(
            w1.id,
            qr_bytes,
            filename="custom_qr.png",
            content_type="image/png",
        )
        assert w1_with_qr.qr_image_path is not None
        assert await service.get_wallet_qr_bytes(w1.id) == qr_bytes

        # Update & delete crypto wallet
        w1_upd = await service.update_crypto_wallet(
            w1.id,
            CryptoWalletUpdateDTO(
                wallet_address="EQ_UPDATED_WALLET_ADDRESS_12345",
                min_deposit_usd=Decimal("10"),
                is_active=False,
            ),
        )
        assert w1_upd.wallet_address == "EQ_UPDATED_WALLET_ADDRESS_12345"
        assert w1_upd.min_deposit_usd == Decimal("10.00000000")
        assert w1_upd.is_active is False
        await service.delete_crypto_wallet(w1.id)

        # Fiat gateway merchant ID encryption at rest check
        secret_merchant = f"merchant-{secrets.token_hex(8)}"
        gw_method = await service.create_fiat_method(
            FiatMethodCreateDTO(
                method_type=FiatMethodType.GATEWAY,
                title="TEST_FIAT_ZARINPAL",
                gateway_provider="zarinpal",
                gateway_merchant_id=secret_merchant,
                min_deposit_irt=Decimal("50000"),
            )
        )
        async with db_session_factory() as session:
            db_row = await session.get(FiatPaymentMethod, gw_method.id)
            assert db_row is not None
            assert db_row.gateway_merchant_id_encrypted is not None
            assert db_row.gateway_merchant_id_encrypted != secret_merchant
            assert db_row.gateway_merchant_id_encrypted.startswith("gAAAAA")
            assert decrypt_secret(db_row.gateway_merchant_id_encrypted, custom_settings.ENCRYPTION_KEY) == secret_merchant

        # Invalid card number / Sheba validation
        with pytest.raises(ValidationError):
            await service.create_fiat_method(
                FiatMethodCreateDTO(
                    method_type=FiatMethodType.CARD_TO_CARD,
                    title="TEST_FIAT_BAD_CARD",
                    card_number="12345",
                )
            )
        with pytest.raises(ValidationError):
            await service.update_fiat_method(
                gw_method.id,
                FiatMethodUpdateDTO(sheba="INVALID_SHEBA"),
            )

        await service.delete_fiat_method(gw_method.id)
    finally:
        await _cleanup_payment_test_data(db_session_factory, [])


# ---------------------------------------------------------------------------
# 3. Exchange Rate & Replay Protection (Duplicate TXID / Authority)
# ---------------------------------------------------------------------------
async def test_exchange_rate_and_duplicate_replay_rejection(
    db_session_factory: async_sessionmaker[AsyncSession],
    app_settings: Settings,
) -> None:
    tg_id = 8800112201
    await _cleanup_payment_test_data(db_session_factory, [tg_id])
    service = PaymentService(session_factory=db_session_factory, settings=app_settings)
    original_rate = await service.get_exchange_rate()

    try:
        updated_rate = await service.update_exchange_rate(Decimal("70000"), rate_mode="manual")
        assert updated_rate.usd_to_toman_rate == Decimal("70000.0000")

        user = await _create_test_user(db_session_factory, tg_id, lang_code="fa")
        wallet = await service.create_crypto_wallet(
            CryptoWalletCreateDTO(
                coin_name="TEST_COIN_USDT_REPLAY",
                symbol="USDT",
                network="TRC20",
                wallet_address="T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
                min_deposit_usd=Decimal("10"),
            )
        )
        fiat = await service.create_fiat_method(
            FiatMethodCreateDTO(
                method_type=FiatMethodType.CARD_TO_CARD,
                title="TEST_FIAT_REPLAY",
                card_number="6037991122334455",
                min_deposit_irt=Decimal("70000"),
            )
        )

        # Below minimum deposit rejected
        with pytest.raises(ValidationError):
            await service.create_crypto_deposit(
                CryptoDepositCreateDTO(
                    user_id=user.id,
                    crypto_wallet_id=wallet.id,
                    amount_usd=Decimal("5"),
                    tx_hash="0xTOO_SMALL",
                )
            )

        tx_id = f"0xTX_{secrets.token_hex(12)}"
        dep1 = await service.create_crypto_deposit(
            CryptoDepositCreateDTO(
                user_id=user.id,
                crypto_wallet_id=wallet.id,
                amount_usd=Decimal("15.25"),
                tx_hash=tx_id,
            )
        )
        assert dep1.status == PaymentStatus.PENDING
        assert dep1.amount_usd == Decimal("15.25000000")

        # Duplicate tx_hash rejected with DuplicateTransactionError
        with pytest.raises(DuplicateTransactionError):
            await service.create_crypto_deposit(
                CryptoDepositCreateDTO(
                    user_id=user.id,
                    crypto_wallet_id=wallet.id,
                    amount_usd=Decimal("15.25"),
                    tx_hash=tx_id,
                )
            )

        # Fiat deposit with authority_track_id and conversion rate check
        track_id = f"TRACK_{secrets.token_hex(8)}"
        fdep1 = await service.create_fiat_deposit(
            FiatDepositCreateDTO(
                user_id=user.id,
                fiat_method_id=fiat.id,
                amount_irt=Decimal("140000"),
                authority_track_id=track_id,
            )
        )
        assert fdep1.amount_irt == Decimal("140000.00")
        assert fdep1.exchange_rate_used == Decimal("70000.0000")
        assert fdep1.amount_usd == Decimal("2.00000000")

        # Duplicate authority_track_id rejected with DuplicateTransactionError
        with pytest.raises(DuplicateTransactionError):
            await service.create_fiat_deposit(
                FiatDepositCreateDTO(
                    user_id=user.id,
                    fiat_method_id=fiat.id,
                    amount_irt=Decimal("140000"),
                    authority_track_id=track_id,
                )
            )
    finally:
        await service.update_exchange_rate(
            original_rate.usd_to_toman_rate,
            rate_mode=original_rate.rate_mode,
        )
        await _cleanup_payment_test_data(db_session_factory, [tg_id])


# ---------------------------------------------------------------------------
# 4. Concurrent review_payment Race Test (PostgreSQL SELECT ... FOR UPDATE)
# ---------------------------------------------------------------------------
async def test_concurrent_review_payment_approves_and_credits_only_once(
    db_session_factory: async_sessionmaker[AsyncSession],
    app_settings: Settings,
) -> None:
    tg_id = 8800112202
    await _cleanup_payment_test_data(db_session_factory, [tg_id])
    service = PaymentService(session_factory=db_session_factory, settings=app_settings)

    try:
        user = await _create_test_user(db_session_factory, tg_id, lang_code="fa")
        wallet = await service.create_crypto_wallet(
            CryptoWalletCreateDTO(
                coin_name="TEST_COIN_RACE",
                symbol="USDT",
                network="BEP20",
                wallet_address="0x71c7656ec7ab88b098defb751b7401b5f6d8976f",
                min_deposit_usd=Decimal("1"),
            )
        )
        payment = await service.create_crypto_deposit(
            CryptoDepositCreateDTO(
                user_id=user.id,
                crypto_wallet_id=wallet.id,
                amount_usd=Decimal("50.125"),
                tx_hash=f"0xRACE_{secrets.token_hex(12)}",
            )
        )

        review_req = PaymentReviewDTO(
            payment_id=payment.id,
            admin_id=9000001,
            approve=True,
            admin_note="Concurrent approval test",
        )

        # Fire 5 concurrent approvals against PostgreSQL
        results = await asyncio.gather(
            *[service.review_payment(review_req) for _ in range(5)],
            return_exceptions=True,
        )

        successes = [r for r in results if isinstance(r, PaymentDTO)]
        conflicts = [r for r in results if isinstance(r, InvalidStateTransitionError)]

        assert len(successes) == 1, f"Expected exactly 1 approval success, got {results}"
        assert len(conflicts) == 4, f"Expected 4 InvalidStateTransitionError conflicts, got {results}"
        assert successes[0].status == PaymentStatus.APPROVED

        async with db_session_factory() as session:
            refreshed_user = await session.get(User, user.id)
            assert refreshed_user is not None
            assert refreshed_user.balance_usd == Decimal("50.12500000")

            ledgers = (
                await session.execute(
                    select(WalletLedger).where(
                        WalletLedger.user_id == user.id,
                        WalletLedger.reference_id == payment.id,
                    )
                )
            ).scalars().all()
            assert len(ledgers) == 1
            assert ledgers[0].before_balance == Decimal("0.00000000")
            assert ledgers[0].change_amount == Decimal("50.12500000")
            assert ledgers[0].after_balance == Decimal("50.12500000")
            assert ledgers[0].reference_type == WalletLedgerRefType.DEPOSIT_CRYPTO.value
    finally:
        await _cleanup_payment_test_data(db_session_factory, [tg_id])


# ---------------------------------------------------------------------------
# 5. Mocked Blockchain Explorers & Iranian Gateways Tests
# ---------------------------------------------------------------------------
async def test_crypto_explorers_and_iranian_gateways_mocked(
    db_session_factory: async_sessionmaker[AsyncSession],
    app_settings: Settings,
) -> None:
    tg_id = 8800112203
    await _cleanup_payment_test_data(db_session_factory, [tg_id])

    target_tron_base58 = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
    target_bsc_addr = "0x71c7656ec7ab88b098defb751b7401b5f6d8976f"

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "trongrid.io/v1/transactions/" in url:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "event_name": "Transfer",
                            "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                            "result": {
                                "from": "TMuA6YqfCeX8EhbfYEg5y7S4DqzSJireY9",
                                "to": target_tron_base58,
                                "value": "25000000",  # 25 USDT (6 decimals)
                            },
                        }
                    ]
                },
            )
        if "bscscan.com/api" in url:
            padded_to = "0x" + ("0" * 24) + target_bsc_addr[2:]
            padded_from = "0x" + ("0" * 24) + ("1" * 40)
            amount_wei_hex = hex(int(Decimal("30") * (Decimal(10) ** 18)))
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "result": {
                        "status": "0x1",
                        "logs": [
                            {
                                "address": "0x55d398326f99059ff775485246999027b3197955",
                                "topics": [
                                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                                    padded_from,
                                    padded_to,
                                ],
                                "data": amount_wei_hex,
                            }
                        ],
                    },
                },
            )
        if "zarinpal.com/pg/v4/payment/request.json" in url:
            return httpx.Response(200, json={"data": {"code": 100, "authority": "A0000000000000000000000000012345"}})
        if "zarinpal.com/pg/v4/payment/verify.json" in url:
            return httpx.Response(200, json={"data": {"code": 100, "ref_id": 987654321, "card_pan": "6037****1234"}})
        if "zibal.ir/v1/request" in url:
            return httpx.Response(200, json={"result": 100, "trackId": 55667788})
        if "zibal.ir/v1/verify" in url:
            return httpx.Response(200, json={"result": 100, "refNumber": "ZB-1122", "cardNumber": "6219****9999"})
        if "nextpay.org/nx/gateway/token" in url:
            return httpx.Response(200, json={"code": -1, "trans_id": "NP-TRANS-001"})
        if "nextpay.org/nx/gateway/verify" in url:
            return httpx.Response(200, json={"code": 0, "Shaparak_Ref_Id": "SH-9988"})
        if "idpay.ir/v1.1/payment/verify" in url:
            return httpx.Response(200, json={"status": 100, "track_id": "IDP-8877", "payment": {"card_no": "6104****0000"}})
        if "idpay.ir/v1.1/payment" in url:
            return httpx.Response(200, json={"id": "IDP-ID-001", "link": "https://idpay.ir/p/IDP-ID-001"})
        return httpx.Response(404, json={"error": "not mocked"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as mock_client:
        # 1. TronGrid & BscScan adapters
        trongrid = TronGridExplorer(api_key="test-key", http_client=mock_client)
        trc_res = await trongrid.verify_transaction(
            tx_hash="0xtrc20tx",
            expected_address=target_tron_base58,
            min_amount=Decimal("20"),
        )
        assert trc_res.verified is True
        assert trc_res.amount == Decimal("25")

        bscscan = BscScanExplorer(api_key="test-key", http_client=mock_client)
        bsc_res = await bscscan.verify_transaction(
            tx_hash="0xbep20tx",
            expected_address=target_bsc_addr,
            min_amount=Decimal("30"),
        )
        assert bsc_res.verified is True
        assert bsc_res.amount == Decimal("30")

        # 2. All 4 Iranian gateways request + verify
        zp = ZarinPalGateway(merchant_id="test-merchant", sandbox=True, http_client=mock_client)
        zp_req = await zp.request_payment(
            amount_irt=Decimal("250000"),
            callback_url="https://example.com/webhooks/payment/zarinpal",
            description="Test",
            order_id="PAY-1",
        )
        assert zp_req.authority_track_id == "A0000000000000000000000000012345"
        zp_ver = await zp.verify_payment(
            authority_track_id=zp_req.authority_track_id,
            amount_irt=Decimal("250000"),
            params={"Status": "OK"},
        )
        assert zp_ver.verified is True
        assert zp_ver.ref_id == "987654321"

        zb = ZibalGateway(merchant="zibal", http_client=mock_client)
        zb_req = await zb.request_payment(
            amount_irt=Decimal("100000"),
            callback_url="https://example.com/cb",
            description="Test",
            order_id="PAY-2",
        )
        assert zb_req.authority_track_id == "55667788"
        zb_ver = await zb.verify_payment(
            authority_track_id="55667788",
            amount_irt=Decimal("100000"),
            params={"success": "1"},
        )
        assert zb_ver.verified is True

        np_gw = NextPayGateway(api_key="np-key", http_client=mock_client)
        np_req = await np_gw.request_payment(
            amount_irt=Decimal("150000"),
            callback_url="https://example.com/cb",
            description="Test",
            order_id="PAY-3",
        )
        assert np_req.authority_track_id == "NP-TRANS-001"
        np_ver = await np_gw.verify_payment(
            authority_track_id="NP-TRANS-001",
            amount_irt=Decimal("150000"),
        )
        assert np_ver.verified is True

        idp = IDPayGateway(api_key="idp-key", sandbox=True, http_client=mock_client)
        idp_req = await idp.request_payment(
            amount_irt=Decimal("180000"),
            callback_url="https://example.com/cb",
            description="Test",
            order_id="PAY-4",
        )
        assert idp_req.authority_track_id == "IDP-ID-001"
        idp_ver = await idp.verify_payment(
            authority_track_id="IDP-ID-001",
            amount_irt=Decimal("180000"),
            params={"status": "10", "order_id": "PAY-4"},
        )
        assert idp_ver.verified is True

        # 3. PaymentService on-chain verification integration
        try:
            user = await _create_test_user(db_session_factory, tg_id, lang_code="en")
            service = PaymentService(
                session_factory=db_session_factory,
                settings=app_settings,
                http_client=mock_client,
            )
            wallet = await service.create_crypto_wallet(
                CryptoWalletCreateDTO(
                    coin_name="TEST_COIN_TRONGRID",
                    symbol="USDT",
                    network="TRC20",
                    wallet_address=target_tron_base58,
                    min_deposit_usd=Decimal("10"),
                    verification_mode=CryptoVerificationMode.TRONGRID,
                )
            )
            dep = await service.create_crypto_deposit(
                CryptoDepositCreateDTO(
                    user_id=user.id,
                    crypto_wallet_id=wallet.id,
                    amount_usd=Decimal("25"),
                    tx_hash=f"0xTRON_{secrets.token_hex(10)}",
                )
            )
            verified_dep = await service.verify_crypto_deposit_onchain(dep.id)
            assert verified_dep.status == PaymentStatus.APPROVED

            async with db_session_factory() as session:
                refreshed = await session.get(User, user.id)
                assert refreshed is not None
                assert refreshed.balance_usd == Decimal("25.00000000")
        finally:
            await _cleanup_payment_test_data(db_session_factory, [tg_id])


# ---------------------------------------------------------------------------
# 6. FastAPI Webhook Callback Routes Tests (/webhooks/payment & /webhooks/crypto)
# ---------------------------------------------------------------------------
async def test_webhook_callback_routes_and_hmac_verification(
    db_session_factory: async_sessionmaker[AsyncSession],
    app_settings: Settings,
) -> None:
    tg_id = 8800112204
    await _cleanup_payment_test_data(db_session_factory, [tg_id])

    ipn_secret = f"ipn_{secrets.token_hex(16)}"
    oxa_secret = f"oxa_{secrets.token_hex(16)}"
    custom_settings = app_settings.model_copy(
        update={
            "ZARINPAL_MERCHANT_ID": f"zp_{secrets.token_hex(8)}",
            "NOWPAYMENTS_API_KEY": f"np_{secrets.token_hex(8)}",
            "NOWPAYMENTS_IPN_SECRET": ipn_secret,
            "OXAPAY_MERCHANT_API_KEY": oxa_secret,
        }
    )

    def _gw_mock(request: httpx.Request) -> httpx.Response:
        if "zarinpal.com/pg/v4/payment/verify.json" in str(request.url):
            return httpx.Response(200, json={"data": {"code": 100, "ref_id": 777888}})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_gw_mock)) as gw_http_client:
        service = PaymentService(
            session_factory=db_session_factory,
            settings=custom_settings,
            http_client=gw_http_client,
        )

        app = FastAPI()
        app.include_router(webhooks_router)
        app.dependency_overrides[get_payment_service] = lambda: service

        try:
            user = await _create_test_user(db_session_factory, tg_id, lang_code="fa")
            fiat_gw = await service.create_fiat_method(
                FiatMethodCreateDTO(
                    method_type=FiatMethodType.GATEWAY,
                    title="TEST_FIAT_WEBHOOK_ZP",
                    gateway_provider="zarinpal",
                    min_deposit_irt=Decimal("50000"),
                )
            )
            wallet_np = await service.create_crypto_wallet(
                CryptoWalletCreateDTO(
                    coin_name="TEST_COIN_NP",
                    symbol="USDT",
                    network="TRC20",
                    wallet_address="T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
                    min_deposit_usd=Decimal("5"),
                    verification_mode=CryptoVerificationMode.NOWPAYMENTS,
                )
            )

            auth_code = f"A0000_{secrets.token_hex(8)}"
            fiat_payment = await service.create_fiat_deposit(
                FiatDepositCreateDTO(
                    user_id=user.id,
                    fiat_method_id=fiat_gw.id,
                    amount_irt=Decimal("300000"),
                    payment_category=PaymentCategory.FIAT_GATEWAY,
                    authority_track_id=auth_code,
                )
            )

            crypto_payment = await service.create_crypto_deposit(
                CryptoDepositCreateDTO(
                    user_id=user.id,
                    crypto_wallet_id=wallet_np.id,
                    amount_usd=Decimal("12.50"),
                )
            )

            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as test_client:
                # 1. ZarinPal callback success -> 200 OK & credits IRT once
                r1 = await test_client.get(
                    "/webhooks/payment/zarinpal",
                    params={"Authority": auth_code, "Status": "OK"},
                )
                assert r1.status_code == 200
                assert r1.json()["ok"] is True
                assert r1.json()["payment_id"] == fiat_payment.id

                # 2. Replay of ZarinPal callback -> 409 Conflict, zero double credit
                r2 = await test_client.get(
                    "/webhooks/payment/zarinpal",
                    params={"Authority": auth_code, "Status": "OK"},
                )
                assert r2.status_code == 409

                # 3. NOWPayments webhook with invalid HMAC -> 401 Unauthorized
                np_payload = {
                    "order_id": f"PAY-{crypto_payment.id}",
                    "payment_status": "finished",
                    "payin_hash": f"0xNP_{secrets.token_hex(10)}",
                    "price_amount": "12.50",
                }
                raw_np = json.dumps(np_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

                r_bad_sig = await test_client.post(
                    "/webhooks/crypto/nowpayments",
                    content=raw_np,
                    headers={"x-nowpayments-sig": "deadbeef", "content-type": "application/json"},
                )
                assert r_bad_sig.status_code == 401

                # 4. NOWPayments webhook with valid HMAC -> 200 OK & credits USD once
                valid_sig = hmac.new(ipn_secret.encode("utf-8"), raw_np, hashlib.sha512).hexdigest()
                r_good_sig = await test_client.post(
                    "/webhooks/crypto/nowpayments",
                    content=raw_np,
                    headers={"x-nowpayments-sig": valid_sig, "content-type": "application/json"},
                )
                assert r_good_sig.status_code == 200
                assert r_good_sig.json()["ok"] is True

                # 5. Replay of NOWPayments webhook -> 409 Conflict
                r_replay = await test_client.post(
                    "/webhooks/crypto/nowpayments",
                    content=raw_np,
                    headers={"x-nowpayments-sig": valid_sig, "content-type": "application/json"},
                )
                assert r_replay.status_code == 409

            async with db_session_factory() as session:
                refreshed_user = await session.get(User, user.id)
                assert refreshed_user is not None
                assert refreshed_user.balance_irt == Decimal("300000.00")
                assert refreshed_user.balance_usd == Decimal("12.50000000")
        finally:
            await _cleanup_payment_test_data(db_session_factory, [tg_id])


# ---------------------------------------------------------------------------
# 7. Deposit Bot Flow Handlers & Admin Review Tests
# ---------------------------------------------------------------------------
async def test_deposit_bot_handlers_and_admin_review_flow(
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
    app_settings: Settings,
) -> None:
    user_en_tg = 8800112205
    user_fa_tg = 8800112206
    admin_tg = 8800112207
    await _cleanup_payment_test_data(db_session_factory, [user_en_tg, user_fa_tg, admin_tg])

    custom_settings = app_settings.model_copy(update={"ADMIN_TELEGRAM_IDS": [admin_tg]})
    service = PaymentService(session_factory=db_session_factory, settings=custom_settings)
    i18n = TranslationService(db_session_factory, redis_client)
    storage = MemoryStorage()

    try:
        user_en = await _create_test_user(db_session_factory, user_en_tg, lang_code="en")
        user_fa = await _create_test_user(db_session_factory, user_fa_tg, lang_code="fa")
        await _create_test_user(db_session_factory, admin_tg, lang_code="fa", is_admin=True)

        wallet = await service.create_crypto_wallet(
            CryptoWalletCreateDTO(
                coin_name="TEST_COIN_BOT",
                symbol="USDT",
                network="TRC20",
                wallet_address="T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
                min_deposit_usd=Decimal("10"),
            )
        )
        fiat_card = await service.create_fiat_method(
            FiatMethodCreateDTO(
                method_type=FiatMethodType.CARD_TO_CARD,
                title="TEST_FIAT_BOT_CARD",
                card_number="6037991155667788",
                sheba="IR120170000000123456789012",
                cardholder_name="رضا رضایی",
                bank_name="بانک ملی",
                min_deposit_irt=Decimal("100000"),
            )
        )

        # Helper to construct mock CallbackQuery & Message
        mock_bot = SimpleNamespace(
            send_message=AsyncMock(),
            send_photo=AsyncMock(),
        )

        def _make_state(uid: int) -> FSMContext:
            return FSMContext(
                storage=storage,
                key=StorageKey(bot_id=1, chat_id=uid, user_id=uid),
            )

        deps = {
            "session_factory": db_session_factory,
            "settings": custom_settings,
            "payment_service": service,
            "i18n": i18n,
            "redis": redis_client,
        }

        # 0. Open Deposit Menu in-place (en: crypto-only; fa: fiat + crypto)
        en_state = _make_state(user_en_tg)
        en_cb_msg = SimpleNamespace(
            photo=None,
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        )
        en_cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_en_tg, username="en_u", language_code="en"),
            message=en_cb_msg,
            bot=mock_bot,
            answer=AsyncMock(),
        )
        await cb_open_deposit_menu(en_cb, en_state, **deps)
        en_markup = en_cb_msg.edit_text.call_args.kwargs["reply_markup"]
        en_cbs = [b.callback_data for r in en_markup.inline_keyboard for b in r if b.callback_data]
        assert not any("dep:fiat:" in cb for cb in en_cbs)

        fa_state = _make_state(user_fa_tg)
        fa_cb_msg = SimpleNamespace(
            photo=None,
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        )
        fa_cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_fa_tg, username="fa_u", language_code="fa"),
            message=fa_cb_msg,
            bot=mock_bot,
            answer=AsyncMock(),
        )
        await cb_open_deposit_menu(fa_cb, fa_state, **deps)
        fa_markup = fa_cb_msg.edit_text.call_args.kwargs["reply_markup"]
        fa_cbs = [b.callback_data for r in fa_markup.inline_keyboard for b in r if b.callback_data]
        assert any("dep:fiat:" in cb for cb in fa_cbs)

        # 1. English user cannot select fiat method even if crafting callback manually
        await cb_select_fiat_method(
            en_cb,
            DepCB(kind="fiat", id=fiat_card.id, action="select"),
            en_state,
            **deps,
        )
        assert await en_state.get_state() is None

        # 2. English user crypto deposit flow -> select wallet -> enter amount -> submit TXID
        await cb_select_crypto_wallet(
            en_cb,
            DepCB(kind="crypto", id=wallet.id, action="select"),
            en_state,
            **deps,
        )
        assert await en_state.get_state() == DepositStates.waiting_crypto_amount.state

        en_msg_amt = SimpleNamespace(
            from_user=SimpleNamespace(id=user_en_tg, username="en_u", language_code="en"),
            text="25.5",
            bot=mock_bot,
            answer=AsyncMock(),
            answer_photo=AsyncMock(),
        )
        await msg_enter_crypto_amount(en_msg_amt, en_state, **deps)
        assert await en_state.get_state() == DepositStates.waiting_crypto_txid_or_receipt.state
        assert en_msg_amt.answer_photo.called

        tx_hash_bot = f"0xBOT_TX_{secrets.token_hex(10)}"
        en_msg_tx = SimpleNamespace(
            from_user=SimpleNamespace(id=user_en_tg, username="en_u", language_code="en"),
            text=tx_hash_bot,
            photo=None,
            document=None,
            bot=mock_bot,
            answer=AsyncMock(),
        )
        await redis_client.delete(f"cooldown:deposit:{user_en_tg}")
        await msg_submit_crypto_proof(en_msg_tx, en_state, **deps)
        assert await en_state.get_state() is None
        assert mock_bot.send_message.called  # Admin notification sent

        # Verify payment created and approve via Admin 1-click PayCB handler
        payments = await service.list_payments(user_id=user_en.id)
        assert len(payments) == 1
        created_payment = payments[0]
        assert created_payment.status == PaymentStatus.PENDING

        # Non-admin attempting PayCB approve is rejected
        non_admin_cb = SimpleNamespace(
            from_user=SimpleNamespace(id=user_en_tg, username="en_u", language_code="en"),
            message=SimpleNamespace(edit_reply_markup=AsyncMock()),
            bot=mock_bot,
            answer=AsyncMock(),
        )
        await cb_admin_review_deposit_notification(
            non_admin_cb,
            PayCB(id=created_payment.id, action="approve"),
            **deps,
        )
        assert (await service.get_payment(created_payment.id)).status == PaymentStatus.PENDING

        # Admin approves via PayCB
        admin_cb = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_tg, username="admin_u", language_code="fa"),
            message=SimpleNamespace(edit_reply_markup=AsyncMock()),
            bot=mock_bot,
            answer=AsyncMock(),
        )
        await cb_admin_review_deposit_notification(
            admin_cb,
            PayCB(id=created_payment.id, action="approve"),
            **deps,
        )
        assert (await service.get_payment(created_payment.id)).status == PaymentStatus.APPROVED

        # 3. Persian ('fa') user Card-to-Card flow with Persian numerals
        await cb_select_fiat_method(
            fa_cb,
            DepCB(kind="fiat", id=fiat_card.id, action="select"),
            fa_state,
            **deps,
        )
        assert await fa_state.get_state() == DepositStates.waiting_fiat_amount.state

        fa_msg_amt = SimpleNamespace(
            from_user=SimpleNamespace(id=user_fa_tg, username="fa_u", language_code="fa"),
            text="۲۵۰,۰۰۰",
            bot=mock_bot,
            answer=AsyncMock(),
        )
        await msg_enter_fiat_amount(fa_msg_amt, fa_state, **deps)
        assert await fa_state.get_state() == DepositStates.waiting_fiat_receipt.state

        await redis_client.delete(f"cooldown:deposit:{user_fa_tg}")
        fa_msg_receipt = SimpleNamespace(
            from_user=SimpleNamespace(id=user_fa_tg, username="fa_u", language_code="fa"),
            text=f"PEYGIRI_{secrets.token_hex(6)}",
            photo=None,
            document=None,
            bot=mock_bot,
            answer=AsyncMock(),
        )
        await msg_submit_fiat_receipt(fa_msg_receipt, fa_state, **deps)
        assert await fa_state.get_state() is None

        fa_payments = await service.list_payments(user_id=user_fa.id)
        assert len(fa_payments) == 1
        assert fa_payments[0].amount_irt == Decimal("250000.00")
    finally:
        await redis_client.delete(f"cooldown:deposit:{user_en_tg}", f"cooldown:deposit:{user_fa_tg}")
        await _cleanup_payment_test_data(db_session_factory, [user_en_tg, user_fa_tg, admin_tg])


# ---------------------------------------------------------------------------
# 8. Oxapay Webhook, Gateway Cancellation, Live Rate Fallback & Receipt Validation
# ---------------------------------------------------------------------------
async def test_oxapay_webhook_rejection_live_rate_and_receipt_validation(
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
    app_settings: Settings,
    tmp_path: Path,
) -> None:
    tg_id = 8800112208
    await _cleanup_payment_test_data(db_session_factory, [tg_id])
    oxa_secret = f"oxa_{secrets.token_hex(16)}"
    custom_settings = app_settings.model_copy(
        update={
            "UPLOAD_DIR": str(tmp_path),
            "OXAPAY_MERCHANT_API_KEY": oxa_secret,
            "ZARINPAL_MERCHANT_ID": f"zp_{secrets.token_hex(8)}",
        }
    )

    def _mock_http(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tetherland.com" in url:
            return httpx.Response(200, json={"data": {"currencies": {"USDT": {"price": "71250"}}}})
        if "zarinpal.com/pg/v4/payment/request.json" in url:
            return httpx.Response(200, json={"data": {"code": 100, "authority": "A000CANCEL001"}})
        return httpx.Response(500, json={"error": "boom"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_http)) as http_client:
        service = PaymentService(
            session_factory=db_session_factory,
            settings=custom_settings,
            http_client=http_client,
        )
        app = FastAPI()
        app.include_router(webhooks_router)
        app.dependency_overrides[get_payment_service] = lambda: service

        try:
            user = await _create_test_user(db_session_factory, tg_id, lang_code="fa")

            # Live rate Decimal fetch + safe error fallback
            live_rate = await service.fetch_live_usd_to_toman_rate()
            assert live_rate == Decimal("71250.0000")
            fallback_rate = await service.fetch_live_usd_to_toman_rate(
                api_url="https://unavailable.invalid/rate"
            )
            assert fallback_rate == (await service.get_exchange_rate()).usd_to_toman_rate

            # Gateway initiate + Status=NOK cancellation -> rejected without balance credit
            gw = await service.create_fiat_method(
                FiatMethodCreateDTO(
                    method_type=FiatMethodType.GATEWAY,
                    title="TEST_FIAT_CANCEL_ZP",
                    gateway_provider="zarinpal",
                    min_deposit_irt=Decimal("50000"),
                )
            )
            gw_payment, pay_url = await service.initiate_fiat_gateway_deposit(
                user_id=user.id,
                fiat_method_id=gw.id,
                amount_irt=Decimal("200000"),
                callback_base_url="https://shop.example.com",
            )
            assert "A000CANCEL001" in pay_url

            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                cancel_resp = await client.get(
                    "/webhooks/payment/zarinpal",
                    params={"Authority": "A000CANCEL001", "Status": "NOK"},
                )
                assert cancel_resp.status_code == 200
                assert cancel_resp.json()["ok"] is False
                assert cancel_resp.json()["status"] == "rejected"

                # Subsequent approval on rejected payment must raise InvalidStateTransitionError
                with pytest.raises(InvalidStateTransitionError):
                    await service.review_payment(
                        PaymentReviewDTO(payment_id=gw_payment.id, admin_id=1, approve=True)
                    )

                # Oxapay webhook HMAC verification
                wallet_oxa = await service.create_crypto_wallet(
                    CryptoWalletCreateDTO(
                        coin_name="TEST_COIN_OXA",
                        symbol="USDT",
                        network="TRC20",
                        wallet_address="T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
                        min_deposit_usd=Decimal("5"),
                        verification_mode=CryptoVerificationMode.OXAPAY,
                    )
                )
                oxa_payment = await service.create_crypto_deposit(
                    CryptoDepositCreateDTO(
                        user_id=user.id,
                        crypto_wallet_id=wallet_oxa.id,
                        amount_usd=Decimal("19.75"),
                    )
                )
                oxa_body = json.dumps(
                    {
                        "trackId": "OXA-TRACK-999",
                        "orderId": f"PAY-{oxa_payment.id}",
                        "status": "Paid",
                        "amount": "19.75",
                        "txID": f"0xOXA_{secrets.token_hex(8)}",
                    }
                ).encode("utf-8")
                oxa_sig = hmac.new(oxa_secret.encode("utf-8"), oxa_body, hashlib.sha512).hexdigest()

                bad_oxa = await client.post(
                    "/webhooks/crypto/oxapay",
                    content=oxa_body,
                    headers={"hmac": "invalid"},
                )
                assert bad_oxa.status_code == 401

                good_oxa = await client.post(
                    "/webhooks/crypto/oxapay",
                    content=oxa_body,
                    headers={"hmac": oxa_sig},
                )
                assert good_oxa.status_code == 200
                assert good_oxa.json()["ok"] is True

            # Receipt image upload in deposit handler: reject fake image, accept valid PNG, enforce cooldown
            storage = MemoryStorage()
            state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=tg_id, user_id=tg_id))
            wallet_manual = await service.create_crypto_wallet(
                CryptoWalletCreateDTO(
                    coin_name="TEST_COIN_RECEIPT",
                    symbol="USDT",
                    network="TRC20",
                    wallet_address="T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
                    min_deposit_usd=Decimal("5"),
                )
            )
            valid_png = await service.generate_wallet_qr_bytes("receipt-png-bytes")
            await state.update_data(crypto_wallet_id=wallet_manual.id, amount_usd="15")
            await state.set_state(DepositStates.waiting_crypto_txid_or_receipt)

            async def _fake_download_bad(path: str, destination: Any) -> None:
                destination.write(b"MZ_FAKE_EXECUTABLE")

            async def _fake_download_good(path: str, destination: Any) -> None:
                destination.write(valid_png)

            bot_bad = SimpleNamespace(
                get_file=AsyncMock(return_value=SimpleNamespace(file_path="photos/bad.jpg")),
                download_file=_fake_download_bad,
                send_message=AsyncMock(),
                send_photo=AsyncMock(),
            )
            msg_bad_photo = SimpleNamespace(
                from_user=SimpleNamespace(id=tg_id, username="u", language_code="fa"),
                photo=[SimpleNamespace(file_id="file_bad_1", file_size=20)],
                document=None,
                text=None,
                caption=None,
                bot=bot_bad,
                answer=AsyncMock(),
            )
            deps = {
                "session_factory": db_session_factory,
                "settings": custom_settings,
                "payment_service": service,
                "i18n": TranslationService(db_session_factory, redis_client),
                "redis": redis_client,
            }
            await redis_client.delete(f"cooldown:deposit:{tg_id}")
            await msg_submit_crypto_proof(msg_bad_photo, state, **deps)
            assert await state.get_state() == DepositStates.waiting_crypto_txid_or_receipt.state

            bot_good = SimpleNamespace(
                get_file=AsyncMock(return_value=SimpleNamespace(file_path="photos/good.png")),
                download_file=_fake_download_good,
                send_message=AsyncMock(),
                send_photo=AsyncMock(),
            )
            msg_good_photo = SimpleNamespace(
                from_user=SimpleNamespace(id=tg_id, username="u", language_code="fa"),
                photo=[SimpleNamespace(file_id="file_good_1", file_size=len(valid_png))],
                document=None,
                text=None,
                caption="Receipt screenshot",
                bot=bot_good,
                answer=AsyncMock(),
            )
            await msg_submit_crypto_proof(msg_good_photo, state, **deps)
            assert await state.get_state() is None
            assert await redis_client.exists(f"cooldown:deposit:{tg_id}") == 1
        finally:
            await redis_client.delete(f"cooldown:deposit:{tg_id}")
            await _cleanup_payment_test_data(db_session_factory, [tg_id])


# ---------------------------------------------------------------------------
# 9. Static Security, Async I/O & Decimal Purity Audit
# ---------------------------------------------------------------------------
def test_static_security_and_decimal_purity(project_root: Path) -> None:
    owned_files = [
        project_root / "app/services/payment_service.py",
        project_root / "app/services/gateways/__init__.py",
        project_root / "app/services/gateways/base.py",
        project_root / "app/services/gateways/crypto_explorers.py",
        project_root / "app/services/gateways/iranian_gateways.py",
        project_root / "app/bot/keyboards/deposit_kb.py",
        project_root / "app/bot/handlers/deposit.py",
        project_root / "app/web_admin/webhooks.py",
    ]
    forbidden_patterns = (
        "TO" + "DO",
        "FIX" + "ME",
        "NotImplemented" + "Error",
        "import requests",
        "time.sleep(",
    )
    for path in owned_files:
        content = path.read_text(encoding="utf-8")
        for pat in forbidden_patterns:
            assert pat not in content, f"Forbidden pattern '{pat}' found in {path}"
        assert "float(" not in content, f"float() must not be used in {path}"
