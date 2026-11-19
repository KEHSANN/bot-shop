"""Wave 3 Cross-Subsystem End-to-End integration tests against live PostgreSQL 16 + Redis 7."""

from __future__ import annotations

import asyncio
import datetime as dt
import secrets
import time
from decimal import Decimal
from typing import Any

import pytest
from aiogram import Bot
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageCaption,
    EditMessageText,
    SendDocument,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import Chat, InlineKeyboardMarkup, Message, Update
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.dispatcher import create_full_dispatcher
from app.bot.keyboards.common_kb import AdmCB, CatCB, DepCB, LangCB, MenuCB, ProdCB, SupCB
from app.config import Settings
from app.core.constants import (
    Currency,
    DeliveryType,
    FiatMethodType,
    FORBIDDEN_MAIN_MENU_FEATURES,
    LANGUAGE_ORDER,
    OrderStatus,
    PaymentStatus,
    TicketStatus,
    WalletLedgerRefType,
)
from app.core.exceptions import (
    DuplicateTransactionError,
    InsufficientStockError,
    InvalidStateTransitionError,
)
from app.database.models import (
    Category,
    CryptoWallet,
    FiatPaymentMethod,
    Order,
    OrderItem,
    Payment,
    Product,
    ProductBulkTier,
    ProductStock,
    SupportMessage,
    SupportTicket,
    User,
    WalletLedger,
)
from app.services.dtos import (
    BalanceAdjustmentDTO,
    CategoryCreateDTO,
    CryptoWalletCreateDTO,
    FiatDepositCreateDTO,
    FiatMethodCreateDTO,
    ProductBulkTierDTO,
    ProductCreateDTO,
    PurchaseRequestDTO,
    UserCreateOrUpdateDTO,
)
from app.services.payment_service import PaymentService
from app.services.shop_service import ShopService
from app.services.support_service import SupportService
from app.services.user_service import UserService


def _unique_tg_id() -> int:
    return 7_000_000_000 + secrets.randbelow(800_000_000)


class RecordingBot(Bot):
    """Test Bot capturing all outgoing Bot API calls without network I/O."""

    def __init__(self) -> None:
        ephemeral = f"{100000 + secrets.randbelow(899999)}:ZZ{secrets.token_urlsafe(28)}"
        super().__init__(token=ephemeral)
        self.calls: list[TelegramMethod[Any]] = []
        self._msg_seq = 100

    async def __call__(
        self,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        now = dt.datetime.now(dt.UTC)
        if isinstance(
            method,
            (SendMessage, EditMessageText, EditMessageCaption, SendPhoto, SendDocument),
        ):
            self._msg_seq += 1
            chat_id = getattr(method, "chat_id", None) or 111111
            text = getattr(method, "text", None) or getattr(method, "caption", None) or ""
            return Message(
                message_id=self._msg_seq,
                date=now,
                chat=Chat(id=int(chat_id), type="private"),
                text=text,
            )
        return True

    def clear_calls(self) -> None:
        self.calls.clear()

    def last_markup_for(self, chat_id: int) -> InlineKeyboardMarkup | None:
        for call in reversed(self.calls):
            if isinstance(call, (SendMessage, EditMessageText, EditMessageCaption, SendPhoto)):
                c_id = getattr(call, "chat_id", None)
                if c_id is not None and int(c_id) == chat_id:
                    rm = getattr(call, "reply_markup", None)
                    if isinstance(rm, InlineKeyboardMarkup):
                        return rm
        return None

    def texts_for(self, chat_id: int) -> list[str]:
        out: list[str] = []
        for call in self.calls:
            if isinstance(call, (SendMessage, EditMessageText, EditMessageCaption, SendPhoto)):
                c_id = getattr(call, "chat_id", None)
                if c_id is not None and int(c_id) == chat_id:
                    out.append(getattr(call, "text", None) or getattr(call, "caption", None) or "")
        return out

    def last_answer_alert(self) -> AnswerCallbackQuery | None:
        for call in reversed(self.calls):
            if isinstance(call, AnswerCallbackQuery):
                return call
        return None


def _e2e_settings(base: Settings, *, admin_tg_id: int) -> Settings:
    return base.model_copy(
        update={
            "APP_ENV": "test",
            "ADMIN_TELEGRAM_IDS": [admin_tg_id],
            "RATE_LIMIT_MAX_REQUESTS": 200,
            "RATE_LIMIT_WINDOW_SECONDS": 3,
            "DEPOSIT_COOLDOWN_SECONDS": 0,
            "SUPPORT_COOLDOWN_SECONDS": 0,
        }
    )


def _msg_update(
    update_id: int,
    *,
    tg_id: int,
    text: str,
    username: str = "e2e_user",
    first_name: str = "E2E",
    lang_code: str = "en",
    msg_id: int = 1,
) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": msg_id,
                "date": int(time.time()),
                "chat": {"id": tg_id, "type": "private"},
                "from": {
                    "id": tg_id,
                    "is_bot": False,
                    "first_name": first_name,
                    "username": username,
                    "language_code": lang_code,
                },
                "text": text,
            },
        }
    )


def _cb_update(
    update_id: int,
    *,
    tg_id: int,
    data: str,
    username: str = "e2e_user",
    first_name: str = "E2E",
    lang_code: str = "en",
    msg_id: int = 50,
    prev_text: str = "Menu",
) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"cbq_{update_id}_{secrets.token_hex(4)}",
                "from": {
                    "id": tg_id,
                    "is_bot": False,
                    "first_name": first_name,
                    "username": username,
                    "language_code": lang_code,
                },
                "chat_instance": f"ci_{tg_id}",
                "message": {
                    "message_id": msg_id,
                    "date": int(time.time()),
                    "chat": {"id": tg_id, "type": "private"},
                    "text": prev_text,
                },
                "data": data,
            },
        }
    )


async def _cleanup_users_and_catalog(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
    *,
    tg_ids: list[int] | None = None,
    category_ids: list[int] | None = None,
    wallet_ids: list[int] | None = None,
    fiat_ids: list[int] | None = None,
) -> None:
    tg_ids = tg_ids or []
    category_ids = category_ids or []
    wallet_ids = wallet_ids or []
    fiat_ids = fiat_ids or []

    async with session_factory() as session:
        async with session.begin():
            if tg_ids:
                user_ids = list(
                    (
                        await session.execute(
                            select(User.id).where(User.telegram_id.in_(tg_ids))
                        )
                    ).scalars().all()
                )
                if user_ids:
                    order_ids = list(
                        (
                            await session.execute(
                                select(Order.id).where(Order.user_id.in_(user_ids))
                            )
                        ).scalars().all()
                    )
                    if order_ids:
                        await session.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
                        await session.execute(delete(ProductStock).where(ProductStock.order_id.in_(order_ids)))
                        await session.execute(delete(Order).where(Order.id.in_(order_ids)))

                    ticket_ids = list(
                        (
                            await session.execute(
                                select(SupportTicket.id).where(SupportTicket.user_id.in_(user_ids))
                            )
                        ).scalars().all()
                    )
                    if ticket_ids:
                        await session.execute(delete(SupportMessage).where(SupportMessage.ticket_id.in_(ticket_ids)))
                        await session.execute(delete(SupportTicket).where(SupportTicket.id.in_(ticket_ids)))

                    await session.execute(delete(WalletLedger).where(WalletLedger.user_id.in_(user_ids)))
                    await session.execute(delete(Payment).where(Payment.user_id.in_(user_ids)))
                    await session.execute(delete(User).where(User.id.in_(user_ids)))

            if category_ids:
                prod_ids = list(
                    (
                        await session.execute(
                            select(Product.id).where(Product.category_id.in_(category_ids))
                        )
                    ).scalars().all()
                )
                if prod_ids:
                    await session.execute(delete(OrderItem).where(OrderItem.product_id.in_(prod_ids)))
                    await session.execute(delete(Order).where(Order.product_id.in_(prod_ids)))
                    await session.execute(delete(ProductStock).where(ProductStock.product_id.in_(prod_ids)))
                    await session.execute(delete(ProductBulkTier).where(ProductBulkTier.product_id.in_(prod_ids)))
                    await session.execute(delete(Product).where(Product.id.in_(prod_ids)))
                await session.execute(delete(Category).where(Category.id.in_(category_ids)))

            if wallet_ids:
                await session.execute(delete(CryptoWallet).where(CryptoWallet.id.in_(wallet_ids)))
            if fiat_ids:
                await session.execute(delete(FiatPaymentMethod).where(FiatPaymentMethod.id.in_(fiat_ids)))

    for tg_id in tg_ids:
        await redis_client.delete(
            f"rl:win:{tg_id}",
            f"rl:alert:{tg_id}",
            f"cooldown:deposit:{tg_id}",
            f"rl:cd:support:{tg_id}",
        )


async def test_e2e_start_strict_5_buttons_and_pinned_products(
    app_settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    admin_id = _unique_tg_id()
    user_id = _unique_tg_id()
    settings = _e2e_settings(app_settings, admin_tg_id=admin_id)
    bot = RecordingBot()
    dp = create_full_dispatcher(
        settings=settings,
        session_factory=db_session_factory,
        redis_client=redis_client,
    )

    try:
        # First /start prompts the 12-language picker before entering Main Menu
        await dp.feed_update(bot, _msg_update(1, tg_id=user_id, text="/start", username="e2e_start"))
        lang_markup = bot.last_markup_for(user_id)
        assert lang_markup is not None
        lang_cbs = [btn.callback_data or "" for row in lang_markup.inline_keyboard for btn in row]
        assert [LangCB.unpack(cb).code for cb in lang_cbs] == list(LANGUAGE_ORDER)

        # Selecting a language persists language_selected=True and renders the strict 5-button Main Menu
        bot.clear_calls()
        await dp.feed_update(bot, _cb_update(2, tg_id=user_id, data=LangCB(code="en").pack()))
        markup = bot.last_markup_for(user_id)
        assert markup is not None

        all_callbacks = [btn.callback_data or "" for row in markup.inline_keyboard for btn in row]
        all_labels = " ".join((btn.text or "").lower() for row in markup.inline_keyboard for btn in row)

        menu_actions = [cb.split(":", 1)[1] for cb in all_callbacks if cb.startswith("menu:")]
        assert menu_actions == ["shop", "deposit", "balance", "support", "lang"]

        first_shop_row_idx = next(
            idx
            for idx, row in enumerate(markup.inline_keyboard)
            if any((b.callback_data or "") == "menu:shop" for b in row)
        )
        assert first_shop_row_idx >= 1, "Expected at least 1 pinned featured product row above Shop"
        pinned_cbs = [
            b.callback_data or ""
            for row in markup.inline_keyboard[:first_shop_row_idx]
            for b in row
        ]
        assert all(cb.startswith("prod:") for cb in pinned_cbs)

        core_rows = markup.inline_keyboard[first_shop_row_idx:]
        assert [len(r) for r in core_rows] == [1, 2, 2]

        for forbidden in FORBIDDEN_MAIN_MENU_FEATURES:
            assert forbidden.replace("_", " ") not in all_labels
            assert all(forbidden not in cb for cb in all_callbacks)

        # Second /start goes straight to Main Menu without re-asking language
        bot.clear_calls()
        await dp.feed_update(bot, _msg_update(3, tg_id=user_id, text="/start", username="e2e_start"))
        repeat_markup = bot.last_markup_for(user_id)
        assert repeat_markup is not None
        repeat_cbs = [btn.callback_data or "" for row in repeat_markup.inline_keyboard for btn in row]
        assert "menu:shop" in repeat_cbs
    finally:
        await dp.storage.close()
        await _cleanup_users_and_catalog(db_session_factory, redis_client, tg_ids=[user_id])


async def test_e2e_12_language_switching_and_region_aware_deposit(
    app_settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    admin_id = _unique_tg_id()
    user_id = _unique_tg_id()
    settings = _e2e_settings(app_settings, admin_tg_id=admin_id)
    payment_service = PaymentService(session_factory=db_session_factory, settings=settings)

    wallet = await payment_service.create_crypto_wallet(
        CryptoWalletCreateDTO(
            coin_name="Tether E2E",
            symbol="USDT",
            network="TRC20",
            wallet_address="TE2EWalletAddress1234567890",
            min_deposit_usd=Decimal("5.00"),
        )
    )
    fiat = await payment_service.create_fiat_method(
        FiatMethodCreateDTO(
            method_type=FiatMethodType.CARD_TO_CARD,
            title="E2E Melli Card",
            card_number="6037997100001111",
            sheba="IR120170000000000000000001",
            cardholder_name="Ehsan Test",
            bank_name="Melli",
            min_deposit_irt=Decimal("100000"),
        )
    )

    bot = RecordingBot()
    dp = create_full_dispatcher(
        settings=settings,
        session_factory=db_session_factory,
        redis_client=redis_client,
        payment_service=payment_service,
    )

    try:
        seq = 10
        await dp.feed_update(bot, _msg_update(seq, tg_id=user_id, text="/start"))
        user_service = UserService(db_session_factory, settings=settings)

        for code in LANGUAGE_ORDER:
            seq += 1
            bot.clear_calls()
            await dp.feed_update(bot, _cb_update(seq, tg_id=user_id, data=LangCB(code=code).pack()))
            db_user = await user_service.get_by_telegram_id(user_id)
            assert db_user is not None and db_user.language_code == code

            seq += 1
            bot.clear_calls()
            await dp.feed_update(bot, _cb_update(seq, tg_id=user_id, data=MenuCB(action="deposit").pack()))
            dep_kb = bot.last_markup_for(user_id)
            assert dep_kb is not None
            dep_cbs = [b.callback_data or "" for row in dep_kb.inline_keyboard for b in row]
            has_crypto = any(cb.startswith("dep:crypto:") for cb in dep_cbs)
            has_fiat = any(cb.startswith("dep:fiat:") for cb in dep_cbs)

            assert has_crypto, f"Crypto deposit must be available for {code}"
            if code == "fa":
                assert has_fiat, "Persian (fa) MUST expose Iranian Fiat methods alongside Crypto"
            else:
                assert not has_fiat, f"Non-Persian ({code}) MUST NOT expose Fiat methods: {dep_cbs}"

        seq += 1
        await dp.feed_update(bot, _cb_update(seq, tg_id=user_id, data=LangCB(code="en").pack()))
        seq += 1
        bot.clear_calls()
        await dp.feed_update(
            bot,
            _cb_update(seq, tg_id=user_id, data=DepCB(kind="fiat", id=fiat.id, action="select").pack()),
        )
        assert not any(
            isinstance(c, (SendMessage, EditMessageText)) and "6037997100001111" in (getattr(c, "text", "") or "")
            for c in bot.calls
        )
    finally:
        await dp.storage.close()
        await _cleanup_users_and_catalog(
            db_session_factory,
            redis_client,
            tg_ids=[user_id],
            wallet_ids=[wallet.id],
            fiat_ids=[fiat.id],
        )


async def test_e2e_deposit_replay_atomic_approval_and_double_approve_guard(
    app_settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    admin_id = _unique_tg_id()
    customer_id = _unique_tg_id()
    settings = _e2e_settings(app_settings, admin_tg_id=admin_id)
    payment_service = PaymentService(session_factory=db_session_factory, settings=settings)

    wallet = await payment_service.create_crypto_wallet(
        CryptoWalletCreateDTO(
            coin_name="USDT Replay",
            symbol="USDT",
            network="BEP20",
            wallet_address="0xE2EReplayWalletAddress",
            min_deposit_usd=Decimal("5.00"),
        )
    )
    fiat = await payment_service.create_fiat_method(
        FiatMethodCreateDTO(
            method_type=FiatMethodType.GATEWAY,
            title="E2E ZarinPal",
            gateway_provider="zarinpal",
            gateway_merchant_id="merchant-e2e-test-1234",
            min_deposit_irt=Decimal("50000"),
        )
    )

    bot = RecordingBot()
    dp = create_full_dispatcher(
        settings=settings,
        session_factory=db_session_factory,
        redis_client=redis_client,
        payment_service=payment_service,
    )

    try:
        await dp.feed_update(bot, _msg_update(1, tg_id=customer_id, text="/start", username="cust_dep"))
        await dp.feed_update(
            bot,
            _cb_update(2, tg_id=customer_id, data=DepCB(kind="crypto", id=wallet.id, action="select").pack()),
        )
        await dp.feed_update(bot, _msg_update(3, tg_id=customer_id, text="25.50"))

        tx_hash = f"0xE2E_TX_{secrets.token_hex(12)}"
        bot.clear_calls()
        await dp.feed_update(bot, _msg_update(4, tg_id=customer_id, text=tx_hash))

        admin_kb = bot.last_markup_for(admin_id)
        assert admin_kb is not None
        approve_cb = next(
            b.callback_data
            for row in admin_kb.inline_keyboard
            for b in row
            if (b.callback_data or "").startswith("pay:") and ":approve" in (b.callback_data or "")
        )
        payment_id = int(approve_cb.split(":")[1])

        await redis_client.delete(f"cooldown:deposit:{customer_id}")
        await dp.feed_update(
            bot,
            _cb_update(5, tg_id=customer_id, data=DepCB(kind="crypto", id=wallet.id, action="select").pack()),
        )
        await dp.feed_update(bot, _msg_update(6, tg_id=customer_id, text="25.50"))
        bot.clear_calls()
        await dp.feed_update(bot, _msg_update(7, tg_id=customer_id, text=tx_hash))
        cust_texts = "\n".join(bot.texts_for(customer_id)).lower()
        assert "already" in cust_texts or "duplicate" in cust_texts

        user_service = UserService(db_session_factory, settings=settings)
        cust_dto = await user_service.get_by_telegram_id(customer_id)
        assert cust_dto is not None
        auth_track = f"AUTH_E2E_{secrets.token_hex(6)}"
        await payment_service.create_fiat_deposit(
            FiatDepositCreateDTO(
                user_id=cust_dto.id,
                fiat_method_id=fiat.id,
                amount_irt=Decimal("500000"),
                authority_track_id=auth_track,
            )
        )
        with pytest.raises(DuplicateTransactionError):
            await payment_service.create_fiat_deposit(
                FiatDepositCreateDTO(
                    user_id=cust_dto.id,
                    fiat_method_id=fiat.id,
                    amount_irt=Decimal("500000"),
                    authority_track_id=auth_track,
                )
            )

        await dp.feed_update(bot, _msg_update(8, tg_id=admin_id, text="/start", username="admin_e2e"))
        bot.clear_calls()
        await asyncio.gather(
            dp.feed_update(bot, _cb_update(9, tg_id=admin_id, data=approve_cb)),
            dp.feed_update(bot, _cb_update(10, tg_id=admin_id, data=approve_cb)),
        )

        refreshed = await user_service.get_by_telegram_id(customer_id)
        assert refreshed is not None
        assert refreshed.balance_usd == Decimal("25.50000000")

        payment_dto = await payment_service.get_payment(payment_id)
        assert payment_dto.status == PaymentStatus.APPROVED

        async with db_session_factory() as session:
            ledgers = (
                await session.execute(
                    select(WalletLedger).where(
                        WalletLedger.user_id == refreshed.id,
                        WalletLedger.reference_type == WalletLedgerRefType.DEPOSIT_CRYPTO.value,
                    )
                )
            ).scalars().all()
            assert len(ledgers) == 1
            assert ledgers[0].before_balance == Decimal("0")
            assert ledgers[0].change_amount == Decimal("25.50000000")
            assert ledgers[0].after_balance == Decimal("25.50000000")
    finally:
        await dp.storage.close()
        await _cleanup_users_and_catalog(
            db_session_factory,
            redis_client,
            tg_ids=[customer_id, admin_id],
            wallet_ids=[wallet.id],
            fiat_ids=[fiat.id],
        )


async def test_e2e_shop_colored_card_bulk_tier_fernet_and_concurrent_last_stock(
    app_settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    admin_id = _unique_tg_id()
    buyer1_id = _unique_tg_id()
    buyer2_id = _unique_tg_id()
    settings = _e2e_settings(app_settings, admin_tg_id=admin_id)
    shop_service = ShopService(session_factory=db_session_factory, encryption_key=settings.ENCRYPTION_KEY)
    user_service = UserService(db_session_factory, settings=settings)

    cat = await shop_service.create_category(
        CategoryCreateDTO(
            slug=f"e2e-cat-{secrets.token_hex(4)}",
            color_emoji="🟣",
            color_hex="#a855f7",
            names_json={"en": "E2E Digital Keys", "fa": "کلیدهای دیجیتال"},
        )
    )
    prod = await shop_service.create_product(
        ProductCreateDTO(
            category_id=cat.id,
            slug=f"e2e-prod-{secrets.token_hex(4)}",
            delivery_type=DeliveryType.AUTO,
            price_usd=Decimal("5.00"),
            price_irt=Decimal("325000"),
            color_emoji="🟢",
            color_hex="#22c55e",
            titles_json={"en": "Gemini Pro E2E Key", "fa": "کلید جمینای"},
            descriptions_json={"en": "Automated license key", "fa": "لایسنس خودکار"},
            rules_json={"en": "Redeem within 24h", "fa": "تا ۲۴ ساعت فعال کنید"},
            bulk_tiers=[
                ProductBulkTierDTO(
                    min_qty=3,
                    unit_price_usd=Decimal("4.00"),
                    unit_price_irt=Decimal("260000"),
                )
            ],
        )
    )
    raw_secrets = [f"KEY-E2E-{i}-{secrets.token_hex(6)}" for i in range(4)]
    await shop_service.add_auto_stock(prod.id, raw_secrets)

    async with db_session_factory() as session:
        stored_payloads = (
            await session.execute(
                select(ProductStock.encrypted_payload).where(ProductStock.product_id == prod.id)
            )
        ).scalars().all()
        assert len(stored_payloads) == 4
        for enc, raw in zip(stored_payloads, raw_secrets):
            assert enc.startswith("gAAAAA")
            assert raw not in enc

    bot = RecordingBot()
    dp = create_full_dispatcher(
        settings=settings,
        session_factory=db_session_factory,
        redis_client=redis_client,
        shop_service=shop_service,
        user_service=user_service,
    )

    try:
        u1 = await user_service.upsert_telegram_user(UserCreateOrUpdateDTO(telegram_id=buyer1_id, username="b1", language_code="en"))
        u2 = await user_service.upsert_telegram_user(UserCreateOrUpdateDTO(telegram_id=buyer2_id, username="b2", language_code="en"))
        await user_service.adjust_balance(BalanceAdjustmentDTO(user_id=u1.id, currency=Currency.USD, amount=Decimal("20.00"), admin_id=admin_id, note="e2e"))
        await user_service.adjust_balance(BalanceAdjustmentDTO(user_id=u2.id, currency=Currency.USD, amount=Decimal("10.00"), admin_id=admin_id, note="e2e"))

        await dp.feed_update(bot, _cb_update(1, tg_id=buyer1_id, data=MenuCB(action="shop").pack()))
        await dp.feed_update(bot, _cb_update(2, tg_id=buyer1_id, data=CatCB(id=cat.id, page=0).pack()))
        prod_list_kb = bot.last_markup_for(buyer1_id)
        assert prod_list_kb is not None
        assert any("🟢" in (b.text or "") and "4" in (b.text or "") for row in prod_list_kb.inline_keyboard for b in row)

        bot.clear_calls()
        await dp.feed_update(bot, _cb_update(3, tg_id=buyer1_id, data=ProdCB(id=prod.id, action="view").pack()))
        card_text = "\n".join(bot.texts_for(buyer1_id))
        assert "4.00" in card_text and "3+" in card_text

        await dp.feed_update(bot, _cb_update(4, tg_id=buyer1_id, data=ProdCB(id=prod.id, action="custom_qty").pack()))
        await dp.feed_update(bot, _msg_update(5, tg_id=buyer1_id, text="3"))

        bot.clear_calls()
        await dp.feed_update(bot, _cb_update(6, tg_id=buyer1_id, data=ProdCB(id=prod.id, action="buy_usd").pack()))
        delivery_text = "\n".join(bot.texts_for(buyer1_id))
        assert "<pre>" in delivery_text
        delivered_count = sum(1 for key in raw_secrets if key in delivery_text)
        assert delivered_count == 3

        u1_after = await user_service.get_by_telegram_id(buyer1_id)
        assert u1_after is not None and u1_after.balance_usd == Decimal("8.00000000")

        bot.clear_calls()
        await asyncio.gather(
            dp.feed_update(bot, _cb_update(7, tg_id=buyer1_id, data=ProdCB(id=prod.id, action="buy_usd").pack())),
            dp.feed_update(bot, _cb_update(8, tg_id=buyer2_id, data=ProdCB(id=prod.id, action="buy_usd").pack())),
        )

        u1_final = await user_service.get_by_telegram_id(buyer1_id)
        u2_final = await user_service.get_by_telegram_id(buyer2_id)
        assert u1_final is not None and u2_final is not None
        balances = sorted([u1_final.balance_usd, u2_final.balance_usd])
        assert balances in (
            [Decimal("3.00000000"), Decimal("10.00000000")],
            [Decimal("5.00000000"), Decimal("8.00000000")],
        )

        with pytest.raises(InsufficientStockError):
            await shop_service.purchase_product(
                PurchaseRequestDTO(user_id=u2_final.id, product_id=prod.id, quantity=1, currency=Currency.USD)
            )
    finally:
        await dp.storage.close()
        await _cleanup_users_and_catalog(
            db_session_factory,
            redis_client,
            tg_ids=[buyer1_id, buyer2_id],
            category_ids=[cat.id],
        )


async def test_e2e_manual_order_fulfillment_refund_and_two_way_support(
    app_settings: Settings,
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    admin_id = _unique_tg_id()
    user_id = _unique_tg_id()
    settings = _e2e_settings(app_settings, admin_tg_id=admin_id)
    shop_service = ShopService(session_factory=db_session_factory, encryption_key=settings.ENCRYPTION_KEY)
    user_service = UserService(db_session_factory, settings=settings)
    support_service = SupportService(db_session_factory)

    cat = await shop_service.create_category(
        CategoryCreateDTO(
            slug=f"e2e-man-cat-{secrets.token_hex(4)}",
            color_emoji="🔵",
            color_hex="#3b82f6",
            names_json={"en": "Manual Services"},
        )
    )
    prod = await shop_service.create_product(
        ProductCreateDTO(
            category_id=cat.id,
            slug=f"e2e-man-prod-{secrets.token_hex(4)}",
            delivery_type=DeliveryType.MANUAL,
            manual_prompt_key="shop.manual_input_prompt",
            manual_stock_count=-1,
            price_usd=Decimal("12.00"),
            color_emoji="🟠",
            color_hex="#f97316",
            titles_json={"en": "Manual Activation E2E"},
        )
    )

    bot = RecordingBot()
    dp = create_full_dispatcher(
        settings=settings,
        session_factory=db_session_factory,
        redis_client=redis_client,
        shop_service=shop_service,
        user_service=user_service,
        support_service=support_service,
    )

    try:
        cust = await user_service.upsert_telegram_user(
            UserCreateOrUpdateDTO(telegram_id=user_id, username="manual_cust", language_code="en")
        )
        await user_service.upsert_telegram_user(
            UserCreateOrUpdateDTO(telegram_id=admin_id, username="manual_admin", language_code="en")
        )
        await user_service.adjust_balance(
            BalanceAdjustmentDTO(user_id=cust.id, currency=Currency.USD, amount=Decimal("30.00"), admin_id=admin_id, note="seed")
        )

        await dp.feed_update(bot, _cb_update(1, tg_id=user_id, data=ProdCB(id=prod.id, action="buy_usd").pack()))
        bot.clear_calls()
        await dp.feed_update(bot, _msg_update(2, tg_id=user_id, text="user_activate@example.com"))

        orders = await shop_service.list_user_orders(cust.id)
        assert len(orders) == 1
        order1 = orders[0]
        assert order1.status == OrderStatus.PENDING_FULFILLMENT
        assert order1.fulfillment_input == "user_activate@example.com"
        assert bot.last_markup_for(admin_id) is not None

        bot.clear_calls()
        await dp.feed_update(bot, _cb_update(3, tg_id=admin_id, data=AdmCB(sec="ord", act="fulfill", id=order1.id).pack()))
        await dp.feed_update(bot, _msg_update(4, tg_id=admin_id, text="Activated successfully!"))
        fulfilled = await shop_service.get_order_detail(order1.id)
        assert fulfilled.status == OrderStatus.COMPLETED
        assert any("Activated" in t or order1.order_number in t for t in bot.texts_for(user_id))

        await dp.feed_update(bot, _cb_update(5, tg_id=user_id, data=ProdCB(id=prod.id, action="buy_usd").pack()))
        await dp.feed_update(bot, _msg_update(6, tg_id=user_id, text="refund_me@example.com"))
        orders_after = await shop_service.list_user_orders(cust.id)
        order2 = next(o for o in orders_after if o.id != order1.id)
        assert order2.status == OrderStatus.PENDING_FULFILLMENT

        bot.clear_calls()
        await dp.feed_update(bot, _cb_update(7, tg_id=admin_id, data=AdmCB(sec="ord", act="refund", id=order2.id).pack()))
        refunded = await shop_service.get_order_detail(order2.id)
        assert refunded.status == OrderStatus.REFUNDED

        cust_after = await user_service.get_by_telegram_id(user_id)
        assert cust_after is not None and cust_after.balance_usd == Decimal("18.00000000")

        with pytest.raises(InvalidStateTransitionError):
            await shop_service.cancel_and_refund_order(order2.id, admin_id, reason="double refund")

        await dp.feed_update(bot, _cb_update(8, tg_id=user_id, data=MenuCB(action="support").pack()))
        await dp.feed_update(bot, _cb_update(9, tg_id=user_id, data=SupCB(action="cat", cat="other_issue").pack()))
        bot.clear_calls()
        await dp.feed_update(
            bot,
            _msg_update(10, tg_id=user_id, text='Help with <script>alert("xss")</script>'),
        )
        admin_sup_text = "\n".join(bot.texts_for(admin_id))
        assert "<script>" not in admin_sup_text
        assert "&lt;script&gt;" in admin_sup_text

        tickets = await support_service.list_user_tickets(cust.id)
        assert len(tickets) == 1
        ticket = tickets[0]

        bot.clear_calls()
        await dp.feed_update(bot, _cb_update(11, tg_id=admin_id, data=AdmCB(sec="sup", act="reply", id=ticket.id).pack()))
        await dp.feed_update(bot, _msg_update(12, tg_id=admin_id, text="Support team resolved your request."))
        assert any("Support team resolved your request." in t for t in bot.texts_for(user_id))

        await dp.feed_update(bot, _cb_update(13, tg_id=user_id, data=SupCB(action="close", id=ticket.id).pack()))
        closed_detail = await support_service.get_ticket_detail(ticket.id)
        assert closed_detail.status == TicketStatus.CLOSED
        assert len(closed_detail.messages) == 2
    finally:
        await dp.storage.close()
        await _cleanup_users_and_catalog(
            db_session_factory,
            redis_client,
            tg_ids=[user_id, admin_id],
            category_ids=[cat.id],
        )
