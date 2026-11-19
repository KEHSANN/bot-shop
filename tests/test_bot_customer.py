"""Comprehensive unit and integration tests for Wave 2 bot-customer subsystem."""

from __future__ import annotations

import asyncio
import datetime as dt
import secrets
from decimal import Decimal
from typing import Any

import pytest
from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageCaption,
    EditMessageText,
    GetChatMember,
    SendDocument,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
    User as TgUser,
)
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.dispatcher import (
    build_dispatcher,
    create_full_dispatcher,
)
from app.bot.handlers import format_irt, format_usd
from app.bot.keyboards import (
    AdmCB,
    CatCB,
    DepCB,
    LangCB,
    MenuCB,
    OrderCB,
    PayCB,
    ProdCB,
    QtyCB,
    SupCB,
    build_back_kb,
    build_cancel_kb,
    build_language_kb,
    build_main_menu_kb,
)
from app.bot.middlewares import (
    AdminAuthMiddleware,
    AuthContextMiddleware,
    BanCheckMiddleware,
    ForceJoinMiddleware,
    I18nMiddleware,
    LoggingMiddleware,
    RateLimitMiddleware,
    check_action_cooldown,
)
from app.bot.middlewares.force_join_mw import (
    parse_force_join_channels,
    validate_force_join_channels_raw,
)
from app.bot.states import AdminStates, DepositStates, ShopStates, SupportStates
from app.config import Settings
from app.core.constants import (
    Currency,
    FORBIDDEN_MAIN_MENU_FEATURES,
    LANGUAGE_ORDER,
    SenderRole,
    SettingKey,
    TicketCategory,
    TicketStatus,
    WalletLedgerRefType,
)
from app.core.exceptions import (
    InsufficientBalanceError,
    InvalidStateTransitionError,
    ValidationError,
)
from app.database.models import AppSetting, SupportTicket, User, WalletLedger
from app.database.seed import run_seed
from app.locales.i18n import TranslationService
from app.services.dtos import (
    BalanceAdjustmentDTO,
    ProductDTO,
    UserCreateOrUpdateDTO,
)
from app.services.interfaces import SupportServiceProtocol, UserServiceProtocol
from app.services.support_service import SupportService
from app.services.user_service import UserService


def _unique_tg_id() -> int:
    # Intentionally > 2^31-1 to exercise int64 Telegram IDs in PostgreSQL
    return 6_500_000_000 + secrets.randbelow(500_000_000)


class RecordingBot(Bot):
    """Test Bot subclass intercepting outgoing Telegram API calls without network I/O."""

    def __init__(self, token: str | None = None) -> None:
        ephemeral = token or f"{100000 + secrets.randbelow(900000)}:ZZ{secrets.token_urlsafe(28)}"
        super().__init__(token=ephemeral)
        self.calls: list[TelegramMethod[Any]] = []
        self.chat_members: dict[tuple[str | int, int], Any] = {}

    async def __call__(
        self,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        now = dt.datetime.now(dt.UTC)
        if isinstance(method, GetChatMember):
            entry = self.chat_members.get((method.chat_id, int(method.user_id)))
            if isinstance(entry, Exception):
                raise entry
            if entry is not None:
                return entry
            return ChatMemberMember(
                user=TgUser(id=int(method.user_id), is_bot=False, first_name="Member"),
            )
        if isinstance(method, (SendMessage, EditMessageText, EditMessageCaption, SendPhoto, SendDocument)):
            chat_id = getattr(method, "chat_id", None) or 111111
            text = getattr(method, "text", None) or getattr(method, "caption", None) or ""
            return Message(
                message_id=len(self.calls) + 100,
                date=now,
                chat=Chat(id=int(chat_id), type="private"),
                text=text,
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True


def _make_message_update(
    *,
    tg_id: int,
    text: str | None = "/start",
    username: str | None = "alice",
    first_name: str = "Alice",
    lang_code: str = "en",
    photo_file_id: str | None = None,
    caption: str | None = None,
) -> Update:
    now = dt.datetime.now(dt.UTC)
    tg_user = TgUser(
        id=tg_id,
        is_bot=False,
        first_name=first_name,
        username=username,
        language_code=lang_code,
    )
    photos = (
        [PhotoSize(file_id=photo_file_id, file_unique_id="uniq1", width=800, height=600)]
        if photo_file_id
        else None
    )
    msg = Message(
        message_id=secrets.randbelow(100_000) + 1,
        date=now,
        chat=Chat(id=tg_id, type="private"),
        from_user=tg_user,
        text=text if not photo_file_id else None,
        caption=caption,
        photo=photos,
    )
    return Update(update_id=secrets.randbelow(1_000_000) + 1, message=msg)


def _make_callback_update(
    *,
    tg_id: int,
    data: str,
    username: str | None = "alice",
    first_name: str = "Alice",
    lang_code: str = "en",
    message_text: str | None = "Previous menu text",
    message_caption: str | None = None,
) -> Update:
    now = dt.datetime.now(dt.UTC)
    tg_user = TgUser(
        id=tg_id,
        is_bot=False,
        first_name=first_name,
        username=username,
        language_code=lang_code,
    )
    bot_user = TgUser(id=123456, is_bot=True, first_name="ShopBot", username="shop_bot")
    msg = Message(
        message_id=500,
        date=now,
        chat=Chat(id=tg_id, type="private"),
        from_user=bot_user,
        text=message_text,
        caption=message_caption,
    )
    cq = CallbackQuery(
        id=f"cq_{secrets.token_hex(6)}",
        from_user=tg_user,
        chat_instance="ci_1",
        message=msg,
        data=data,
    )
    return Update(update_id=secrets.randbelow(1_000_000) + 1, callback_query=cq)


async def _cleanup_user_by_tg_id(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_ids: list[int],
) -> None:
    if not telegram_ids:
        return
    async with session_factory() as session:
        async with session.begin():
            users = (
                await session.execute(
                    User.__table__.select().where(User.telegram_id.in_(telegram_ids))
                )
            ).all()
            user_ids = [u.id for u in users]
            if user_ids:
                await session.execute(
                    delete(SupportTicket).where(SupportTicket.user_id.in_(user_ids))
                )
                await session.execute(
                    delete(WalletLedger).where(WalletLedger.user_id.in_(user_ids))
                )
                await session.execute(delete(User).where(User.id.in_(user_ids)))


# ---------------------------------------------------------------------------
# 1. Shared Bot Contracts, FSM States & Keyboards Tests
# ---------------------------------------------------------------------------
def test_fsm_states_and_callback_data_contracts() -> None:
    assert hasattr(ShopStates, "waiting_custom_qty")
    assert hasattr(ShopStates, "waiting_manual_input")
    assert hasattr(DepositStates, "waiting_crypto_amount")
    assert hasattr(DepositStates, "waiting_crypto_txid_or_receipt")
    assert hasattr(DepositStates, "waiting_fiat_amount")
    assert hasattr(DepositStates, "waiting_fiat_receipt")
    assert hasattr(SupportStates, "waiting_ticket_message")
    assert hasattr(SupportStates, "waiting_reply_message")

    expected_admin_states = {
        "waiting_user_search",
        "waiting_balance_amount",
        "waiting_balance_note",
        "waiting_direct_message",
        "waiting_reject_reason",
        "waiting_fulfill_note",
        "waiting_category_input",
        "waiting_product_input",
        "waiting_stock_upload",
        "waiting_wallet_input",
        "waiting_fiat_input",
        "waiting_cms_text",
        "waiting_broadcast_content",
        "waiting_support_reply",
        "waiting_exchange_rate",
    }
    for attr in expected_admin_states:
        assert hasattr(AdminStates, attr), f"AdminStates missing {attr}"

    callbacks = [
        MenuCB(action="main"),
        MenuCB(action="shop"),
        MenuCB(action="deposit"),
        MenuCB(action="balance"),
        MenuCB(action="support"),
        MenuCB(action="lang"),
        LangCB(code="fa"),
        CatCB(id=12345, page=3),
        ProdCB(id=12345, action="buy_usd"),
        QtyCB(prod_id=12345, qty=100),
        OrderCB(id=12345, action="view"),
        OrderCB(action="list"),
        DepCB(kind="crypto", id=55, action="select"),
        PayCB(id=999, action="approve"),
        SupCB(action="new"),
        SupCB(action="cat", cat="deposit_issue"),
        SupCB(action="reply", id=777, cat="other_issue"),
        AdmCB(sec="users", act="charge", id=888, page=2),
    ]
    for cb in callbacks:
        packed = cb.pack()
        assert len(packed.encode("utf-8")) <= 64
        unpacked = type(cb).unpack(packed)
        assert unpacked == cb


def test_main_menu_and_language_keyboards_strict_layout() -> None:
    now = dt.datetime.now(dt.UTC)
    pinned = [
        ProductDTO(
            id=10,
            category_id=1,
            slug="gemini-pro",
            delivery_type="manual",
            price_usd=Decimal("9.99"),
            price_irt=Decimal("650000"),
            color_emoji="🟢",
            color_hex="#22c55e",
            is_pinned=True,
            is_active=True,
            sort_order=1,
            manual_stock_count=-1,
            low_stock_threshold=5,
            sold_count=12,
            titles_json={"en": "🚀 GEMINI PRO", "fa": "🚀 جمینای پرو"},
            descriptions_json={},
            rules_json={},
            manual_prompts_json={},
            created_at=now,
            updated_at=now,
        )
    ]
    kb = build_main_menu_kb(lang_code="en", pinned_products=pinned, pinned_limit=4)
    assert len(kb.inline_keyboard) == 4
    assert kb.inline_keyboard[0][0].text == "🟢 🚀 GEMINI PRO"
    assert kb.inline_keyboard[0][0].callback_data == ProdCB(id=10, action="view").pack()

    core_buttons = [btn for row in kb.inline_keyboard[1:] for btn in row]
    assert len(core_buttons) == 5
    core_callbacks = [b.callback_data for b in core_buttons]
    assert core_callbacks == [
        MenuCB(action="shop").pack(),
        MenuCB(action="deposit").pack(),
        MenuCB(action="balance").pack(),
        MenuCB(action="support").pack(),
        MenuCB(action="lang").pack(),
    ]
    all_labels_lower = " ".join(b.text.lower() for b in core_buttons)
    for forbidden in FORBIDDEN_MAIN_MENU_FEATURES:
        assert forbidden.replace("_", " ") not in all_labels_lower

    lang_kb = build_language_kb(current_lang="fa", include_back=True)
    assert len(lang_kb.inline_keyboard) == 7
    lang_buttons = [b for row in lang_kb.inline_keyboard[:6] for b in row]
    assert len(lang_buttons) == 12
    assert [LangCB.unpack(b.callback_data).code for b in lang_buttons] == list(LANGUAGE_ORDER)
    assert lang_buttons[0].text.startswith("✅ ")

    back_kb = build_back_kb("🔙 Back", MenuCB(action="main"), main_menu_text="🏠 Main")
    assert len(back_kb.inline_keyboard[0]) == 2
    cancel_kb = build_cancel_kb("❌ Cancel")
    assert cancel_kb.inline_keyboard[0][0].callback_data == MenuCB(action="main").pack()


# ---------------------------------------------------------------------------
# 2. UserService Tests (Upsert, 64-bit Search, Block, Atomic Balance & Concurrency)
# ---------------------------------------------------------------------------
async def test_user_service_crud_search_block_and_atomic_balance(
    db_session_factory: async_sessionmaker[AsyncSession],
    app_settings: Settings,
) -> None:
    svc = UserService(db_session_factory, settings=app_settings)
    assert isinstance(svc, UserServiceProtocol)

    tg_id = _unique_tg_id()
    admin_tg_id = _unique_tg_id()
    try:
        created = await svc.upsert_telegram_user(
            UserCreateOrUpdateDTO(
                telegram_id=tg_id,
                username="@TestUser_Alpha",
                first_name="Ali",
                last_name="Rezaei",
                language_code="fa-IR",
            )
        )
        assert created.telegram_id == tg_id
        assert created.username == "TestUser_Alpha"
        assert created.language_code == "fa"
        assert created.balance_usd == Decimal("0")
        assert created.balance_irt == Decimal("0")

        switched = await svc.set_language(tg_id, "de")
        assert switched.language_code == "de"

        refreshed = await svc.upsert_telegram_user(
            UserCreateOrUpdateDTO(
                telegram_id=tg_id,
                username="TestUser_Updated",
                first_name="Ali",
                last_name="Rezaei",
                language_code="en",
            )
        )
        assert refreshed.language_code == "de"
        assert refreshed.username == "TestUser_Updated"

        with pytest.raises(ValidationError):
            await svc.set_language(tg_id, "invalid_lang")

        # Search by 64-bit numeric telegram_id (> int32 max!), @username, and full name
        by_num = await svc.search_users(str(tg_id))
        assert any(u.id == created.id for u in by_num)

        by_handle = await svc.search_users("@TestUser_Updated")
        assert any(u.id == created.id for u in by_handle)

        by_name = await svc.search_users("Ali Rezaei")
        assert any(u.id == created.id for u in by_name)

        with pytest.raises(ValidationError):
            await svc.set_blocked(created.id, blocked=True, admin_id=tg_id)

        blocked_u = await svc.set_blocked(created.id, blocked=True, admin_id=admin_tg_id)
        assert blocked_u.is_blocked is True
        blocked_list = await svc.list_users(blocked_only=True)
        assert any(u.id == created.id for u in blocked_list)

        unblocked_u = await svc.set_blocked(created.id, blocked=False, admin_id=admin_tg_id)
        assert unblocked_u.is_blocked is False

        u1, l1 = await svc.adjust_balance(
            BalanceAdjustmentDTO(
                user_id=created.id,
                currency=Currency.USD,
                amount=Decimal("25.50"),
                admin_id=admin_tg_id,
                note="Initial bonus",
            )
        )
        assert u1.balance_usd == Decimal("25.50000000")
        assert l1.before_balance == Decimal("0")
        assert l1.change_amount == Decimal("25.50000000")
        assert l1.after_balance == Decimal("25.50000000")
        assert l1.reference_type == WalletLedgerRefType.ADMIN_CHARGE

        u2, l2 = await svc.adjust_balance(
            BalanceAdjustmentDTO(
                user_id=created.id,
                currency=Currency.USD,
                amount=Decimal("-5.25"),
                admin_id=admin_tg_id,
                note="Manual adjustment",
            )
        )
        assert u2.balance_usd == Decimal("20.25000000")
        assert l2.reference_type == WalletLedgerRefType.ADMIN_DEDUCT

        u3, l3 = await svc.adjust_balance(
            BalanceAdjustmentDTO(
                user_id=created.id,
                currency=Currency.IRT,
                amount=Decimal("650000"),
                admin_id=admin_tg_id,
                note="Toman credit",
            )
        )
        assert u3.balance_irt == Decimal("650000.00")
        assert l3.currency == Currency.IRT

        with pytest.raises(InsufficientBalanceError):
            await svc.adjust_balance(
                BalanceAdjustmentDTO(
                    user_id=created.id,
                    currency=Currency.USD,
                    amount=Decimal("-100.00"),
                    admin_id=admin_tg_id,
                )
            )

        results = await asyncio.gather(
            svc.adjust_balance(
                BalanceAdjustmentDTO(
                    user_id=created.id,
                    currency=Currency.USD,
                    amount=Decimal("-15.00"),
                    admin_id=admin_tg_id,
                    note="Race 1",
                )
            ),
            svc.adjust_balance(
                BalanceAdjustmentDTO(
                    user_id=created.id,
                    currency=Currency.USD,
                    amount=Decimal("-15.00"),
                    admin_id=admin_tg_id,
                    note="Race 2",
                )
            ),
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, InsufficientBalanceError)]
        assert len(successes) == 1
        assert len(failures) == 1

        final_user = await svc.get_by_id(created.id)
        assert final_user.balance_usd == Decimal("5.25000000")

        history = await svc.get_wallet_history(created.id)
        assert len(history) == 4
    finally:
        await _cleanup_user_by_tg_id(db_session_factory, [tg_id])


# ---------------------------------------------------------------------------
# 3. SupportService Tests
# ---------------------------------------------------------------------------
async def test_support_service_lifecycle(
    db_session_factory: async_sessionmaker[AsyncSession],
    app_settings: Settings,
) -> None:
    user_svc = UserService(db_session_factory, settings=app_settings)
    sup_svc = SupportService(db_session_factory)
    assert isinstance(sup_svc, SupportServiceProtocol)

    tg_id = _unique_tg_id()
    admin_tg_id = _unique_tg_id()
    try:
        user = await user_svc.upsert_telegram_user(
            UserCreateOrUpdateDTO(telegram_id=tg_id, username="sup_tester", first_name="Sara")
        )
        ticket = await sup_svc.create_ticket(
            user.id,
            TicketCategory.DEPOSIT_ISSUE,
            subject="Missing USDT deposit",
            message_text="I sent 50 USDT TRC20",
            telegram_file_id="photo_file_123",
            file_type="photo",
        )
        assert ticket.ticket_number.startswith("SUP-")
        assert ticket.status == TicketStatus.OPEN
        assert len(ticket.messages) == 1
        assert ticket.messages[0].sender_role == SenderRole.USER
        assert ticket.messages[0].telegram_file_id == "photo_file_123"

        reply_msg = await sup_svc.add_message(
            ticket.id,
            SenderRole.ADMIN,
            admin_tg_id,
            message_text="We verified and credited your deposit.",
        )
        assert reply_msg.sender_role == SenderRole.ADMIN

        detail = await sup_svc.get_ticket_detail(ticket.id)
        assert detail.status == TicketStatus.ANSWERED
        assert len(detail.messages) == 2

        await sup_svc.add_message(
            ticket.id,
            SenderRole.USER,
            tg_id,
            message_text="Thank you!",
        )
        detail2 = await sup_svc.get_ticket_detail(ticket.id)
        assert detail2.status == TicketStatus.OPEN
        assert len(detail2.messages) == 3

        user_tickets = await sup_svc.list_user_tickets(user.id)
        assert len(user_tickets) == 1

        open_tickets = await sup_svc.list_tickets(status=TicketStatus.OPEN)
        assert any(t.id == ticket.id for t in open_tickets)

        closed = await sup_svc.close_ticket(ticket.id)
        assert closed.status == TicketStatus.CLOSED

        with pytest.raises(InvalidStateTransitionError):
            await sup_svc.add_message(
                ticket.id,
                SenderRole.USER,
                tg_id,
                message_text="Cannot reply after close",
            )
    finally:
        await _cleanup_user_by_tg_id(db_session_factory, [tg_id])


# ---------------------------------------------------------------------------
# 4. Middlewares & End-to-End Customer Bot Dispatcher Flow Tests
# ---------------------------------------------------------------------------
async def test_middlewares_and_customer_handlers_end_to_end(
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
    app_settings: Settings,
) -> None:
    async with db_session_factory() as session:
        await run_seed(session)

    cust_tg_id = _unique_tg_id()
    other_tg_id = _unique_tg_id()
    blocked_tg_id = _unique_tg_id()
    admin_tg_id = _unique_tg_id()
    db_admin_tg_id = _unique_tg_id()
    rl_tg_id = _unique_tg_id()

    test_settings = app_settings.model_copy(
        update={
            "ADMIN_TELEGRAM_IDS": [admin_tg_id],
            "SUPPORT_COOLDOWN_SECONDS": 2,
        }
    )

    user_svc = UserService(db_session_factory, settings=test_settings)
    sup_svc = SupportService(db_session_factory)
    i18n = TranslationService(db_session_factory, redis_client=redis_client)

    admin_test_router = Router(name="test_admin_router")
    admin_test_router.message.middleware(
        AdminAuthMiddleware(settings=test_settings, user_service=user_svc)
    )

    @admin_test_router.message(Command("admin"))
    async def _cmd_admin(message: Message) -> None:
        await message.answer("ADMIN_PANEL_OK")

    dp = build_dispatcher(
        extra_routers=(admin_test_router,),
        settings=test_settings,
        session_factory=db_session_factory,
        redis_client=redis_client,
        user_service=user_svc,
        support_service=sup_svc,
        translation_service=i18n,
        rate_limit_max_requests=40,
        rate_limit_window_seconds=3,
    )
    assert isinstance(dp.storage, RedisStorage)
    outer_types = [type(m) for m in dp.update.outer_middleware]
    assert LoggingMiddleware in outer_types
    assert BanCheckMiddleware in outer_types
    assert RateLimitMiddleware in outer_types
    assert outer_types.index(LoggingMiddleware) < outer_types.index(BanCheckMiddleware) < outer_types.index(RateLimitMiddleware)
    inner_types = [type(m) for m in dp.update.middleware]
    assert AuthContextMiddleware in inner_types
    assert I18nMiddleware in inner_types
    assert inner_types.index(AuthContextMiddleware) < inner_types.index(I18nMiddleware)

    bot = RecordingBot()

    try:
        # 1a. First /start for new user prompts 12-language selector (no Back button)
        await dp.feed_update(
            bot,
            _make_message_update(
                tg_id=cust_tg_id,
                text="/start",
                username="bad<script>user",
                first_name="<b>Eve</b>",
                lang_code="en",
            ),
        )
        assert len(bot.calls) == 1
        first_call = bot.calls[-1]
        assert isinstance(first_call, SendMessage)
        assert isinstance(first_call.reply_markup, InlineKeyboardMarkup)
        first_cbs = [b.callback_data or "" for row in first_call.reply_markup.inline_keyboard for b in row]
        assert [LangCB.unpack(cb).code for cb in first_cbs] == list(LANGUAGE_ORDER)

        # 1b. Selecting 'en' marks language_selected=True & edits in-place to strict 5-button Main Menu (XSS escaped)
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(
                tg_id=cust_tg_id,
                data=LangCB(code="en").pack(),
                username="bad<script>user",
                first_name="<b>Eve</b>",
            ),
        )
        start_call = next(c for c in bot.calls if isinstance(c, EditMessageText))
        assert "&lt;script&gt;" in start_call.text
        assert "<script>" not in start_call.text
        assert "$" in start_call.text
        assert "تومان" not in start_call.text
        assert isinstance(start_call.reply_markup, InlineKeyboardMarkup)

        # 1c. Subsequent /start skips language prompt and opens Main Menu directly
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_message_update(
                tg_id=cust_tg_id,
                text="/start",
                username="bad<script>user",
                first_name="<b>Eve</b>",
                lang_code="en",
            ),
        )
        repeat_call = bot.calls[-1]
        assert isinstance(repeat_call, SendMessage)
        assert "$" in repeat_call.text
        repeat_cbs = [b.callback_data or "" for row in repeat_call.reply_markup.inline_keyboard for b in row]
        assert MenuCB(action="shop").pack() in repeat_cbs

        # 2. Language switch: open language menu in-place -> select Persian ('fa') -> edits in-place with IRT balance
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(
                tg_id=cust_tg_id,
                data=MenuCB(action="lang").pack(),
            ),
        )
        assert any(isinstance(c, EditMessageText) for c in bot.calls)

        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(
                tg_id=cust_tg_id,
                data=LangCB(code="fa").pack(),
            ),
        )
        edit_calls = [c for c in bot.calls if isinstance(c, EditMessageText)]
        assert len(edit_calls) == 1
        assert "تومان" in edit_calls[0].text
        db_cust = await user_svc.get_by_telegram_id(cust_tg_id)
        assert db_cust is not None and db_cust.language_code == "fa"

        # 3. Credit user balance & open Balance screen in-place (plus edit_caption from media message)
        await user_svc.adjust_balance(
            BalanceAdjustmentDTO(
                user_id=db_cust.id,
                currency=Currency.USD,
                amount=Decimal("42.50"),
                admin_id=admin_tg_id,
                note="Credit <tag>",
            )
        )
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(
                tg_id=cust_tg_id,
                data=MenuCB(action="balance").pack(),
                lang_code="fa",
                message_text=None,
                message_caption="Media caption",
            ),
        )
        caption_edits = [c for c in bot.calls if isinstance(c, EditMessageCaption)]
        assert len(caption_edits) == 1
        assert "42.50" in caption_edits[0].caption
        assert "&lt;tag&gt;" in caption_edits[0].caption
        assert "<tag>" not in caption_edits[0].caption

        # 4. Support ticket creation + photo forwarding to admin + list/view/IDOR + admin reply + close
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(
                tg_id=cust_tg_id,
                data=SupCB(action="cat", cat=TicketCategory.DEPOSIT_ISSUE.value).pack(),
                lang_code="fa",
            ),
        )
        assert any(isinstance(c, EditMessageText) for c in bot.calls)

        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_message_update(
                tg_id=cust_tg_id,
                photo_file_id="AgACAgQAAxkBAAIB_test_photo_id",
                caption="Need help with TXID <b>123</b>",
                username="bad<script>user",
                first_name="Alice<img src=x>",
                lang_code="fa",
            ),
        )
        cust_msg_calls = [
            c for c in bot.calls if isinstance(c, SendMessage) and c.chat_id == cust_tg_id
        ]
        admin_photo_calls = [
            c for c in bot.calls if isinstance(c, SendPhoto) and c.chat_id == admin_tg_id
        ]
        assert len(cust_msg_calls) == 1
        assert len(admin_photo_calls) == 1
        assert "&lt;b&gt;123&lt;/b&gt;" in (admin_photo_calls[0].caption or "")
        assert "<img" not in (admin_photo_calls[0].caption or "")

        tickets = await sup_svc.list_user_tickets(db_cust.id)
        assert len(tickets) == 1
        ticket_id = tickets[0].id

        # Ticket list & detail view
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(tg_id=cust_tg_id, data=SupCB(action="list").pack()),
        )
        assert any(isinstance(c, EditMessageText) for c in bot.calls)

        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(tg_id=cust_tg_id, data=SupCB(action="view", id=ticket_id).pack()),
        )
        view_edits = [c for c in bot.calls if isinstance(c, EditMessageText)]
        assert len(view_edits) == 1
        assert tickets[0].ticket_number in view_edits[0].text

        # IDOR protection: another user must be denied viewing/closing someone else's ticket
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(tg_id=other_tg_id, data=SupCB(action="view", id=ticket_id).pack()),
        )
        assert any(isinstance(c, AnswerCallbackQuery) and c.show_alert for c in bot.calls)
        assert not any(isinstance(c, EditMessageText) for c in bot.calls)

        # Admin replies via SupCB(action="reply", id=ticket_id)
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(
                tg_id=admin_tg_id,
                data=SupCB(action="reply", id=ticket_id).pack(),
                username="root_admin",
                first_name="Admin",
            ),
        )
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_message_update(
                tg_id=admin_tg_id,
                text="Resolved your deposit <ok>",
                username="root_admin",
                first_name="Admin",
            ),
        )
        sent_to_cust = [
            c for c in bot.calls if isinstance(c, SendMessage) and c.chat_id == cust_tg_id
        ]
        assert len(sent_to_cust) == 1
        assert "&lt;ok&gt;" in sent_to_cust[0].text

        # Close ticket via SupCB(action="close", id=ticket_id)
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(
                tg_id=cust_tg_id,
                data=SupCB(action="close", id=ticket_id).pack(),
            ),
        )
        detail_after_close = await sup_svc.get_ticket_detail(ticket_id)
        assert detail_after_close.status == TicketStatus.CLOSED

        # /cancel clears FSM state and shows Main Menu
        bot.calls.clear()
        await dp.feed_update(bot, _make_message_update(tg_id=cust_tg_id, text="/cancel"))
        assert any(isinstance(c, SendMessage) for c in bot.calls)

        # 5. AdminAuthMiddleware RBAC check: customer rejected, env admin & DB is_admin allowed
        bot.calls.clear()
        await dp.feed_update(bot, _make_message_update(tg_id=cust_tg_id, text="/admin"))
        assert len(bot.calls) == 1
        assert "Access denied" in getattr(bot.calls[0], "text", "")

        bot.calls.clear()
        await dp.feed_update(bot, _make_message_update(tg_id=admin_tg_id, text="/admin"))
        assert any(getattr(c, "text", "") == "ADMIN_PANEL_OK" for c in bot.calls)

        await user_svc.upsert_telegram_user(UserCreateOrUpdateDTO(telegram_id=db_admin_tg_id))
        async with db_session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(User).where(User.telegram_id == db_admin_tg_id).values(is_admin=True)
                )
        bot.calls.clear()
        await dp.feed_update(bot, _make_message_update(tg_id=db_admin_tg_id, text="/admin"))
        assert any(getattr(c, "text", "") == "ADMIN_PANEL_OK" for c in bot.calls)

        # 6. BanCheckMiddleware: blocked user updates dropped completely at outer layer
        blocked_dto = await user_svc.upsert_telegram_user(
            UserCreateOrUpdateDTO(telegram_id=blocked_tg_id, username="banned_guy")
        )
        await user_svc.set_blocked(blocked_dto.id, blocked=True, admin_id=admin_tg_id)

        bot.calls.clear()
        await dp.feed_update(bot, _make_message_update(tg_id=blocked_tg_id, text="/start"))
        assert len(bot.calls) == 0

        # 7. RateLimitMiddleware sliding window test + cooldown check
        rl_dp = build_dispatcher(
            settings=test_settings,
            session_factory=db_session_factory,
            redis_client=redis_client,
            user_service=user_svc,
            support_service=sup_svc,
            translation_service=i18n,
            rate_limit_max_requests=2,
            rate_limit_window_seconds=5,
        )
        await redis_client.delete(f"rl:win:{rl_tg_id}", f"rl:alert:{rl_tg_id}")
        bot.calls.clear()
        for _ in range(3):
            await rl_dp.feed_update(
                bot,
                _make_callback_update(
                    tg_id=rl_tg_id,
                    data=MenuCB(action="main").pack(),
                ),
            )
        alert_calls = [
            c for c in bot.calls if isinstance(c, AnswerCallbackQuery) and c.show_alert is True
        ]
        assert len(alert_calls) == 1

        cd_uid = _unique_tg_id()
        assert await check_action_cooldown(
            redis_client, action="deposit", user_id=cd_uid, cooldown_seconds=2
        ) is True
        assert await check_action_cooldown(
            redis_client, action="deposit", user_id=cd_uid, cooldown_seconds=2
        ) is False

        # 8. Maintenance mode test: non-admin receives maintenance notice, admin still passes
        async with db_session_factory() as session:
            orig_maint = (
                await session.execute(
                    select(AppSetting.value).where(AppSetting.key == SettingKey.MAINTENANCE_MODE.value)
                )
            ).scalar_one_or_none()

        try:
            async with db_session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(AppSetting)
                        .where(AppSetting.key == SettingKey.MAINTENANCE_MODE.value)
                        .values(value="true")
                    )

            bot.calls.clear()
            await dp.feed_update(bot, _make_message_update(tg_id=cust_tg_id, text="/start"))
            assert len(bot.calls) == 1
            assert bot.calls[0].text == await i18n.get_text("err.maintenance", "fa")

            bot.calls.clear()
            await dp.feed_update(bot, _make_message_update(tg_id=admin_tg_id, text="/start"))
            assert len(bot.calls) == 1
            assert bot.calls[0].text != await i18n.get_text("err.maintenance", "en")
            bot.calls.clear()
            await dp.feed_update(
                bot,
                _make_callback_update(tg_id=admin_tg_id, data=LangCB(code="en").pack()),
            )
            assert any(isinstance(c, EditMessageText) and "$" in c.text for c in bot.calls)
        finally:
            if orig_maint is not None:
                async with db_session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            update(AppSetting)
                            .where(AppSetting.key == SettingKey.MAINTENANCE_MODE.value)
                            .values(value=orig_maint)
                        )

        # 9. create_full_dispatcher smoke test
        full_dp = create_full_dispatcher(
            settings=test_settings,
            session_factory=db_session_factory,
            redis_client=redis_client,
        )
        assert full_dp is not None
        assert format_usd(Decimal("10.5")) == "10.50"
        assert format_irt(Decimal("650000")) == "650,000"
    finally:
        await _cleanup_user_by_tg_id(
            db_session_factory,
            [cust_tg_id, other_tg_id, blocked_tg_id, admin_tg_id, db_admin_tg_id, rl_tg_id],
        )


def test_force_join_channel_parser_and_validator() -> None:
    parsed = parse_force_join_channels(
        "@shop_news\n"
        "https://t.me/promo_channel|Promo Club\n"
        "-1001234567890|https://t.me/+SecretInvite|VIP Channel\n"
        "@SHOP_NEWS",
        strict=True,
    )
    assert len(parsed) == 3
    assert parsed[0].chat_id == "@shop_news"
    assert parsed[0].url == "https://t.me/shop_news"
    assert parsed[0].title is None

    assert parsed[1].chat_id == "@promo_channel"
    assert parsed[1].url == "https://t.me/promo_channel"
    assert parsed[1].title == "Promo Club"

    assert parsed[2].chat_id == -1001234567890
    assert parsed[2].url == "https://t.me/+SecretInvite"
    assert parsed[2].title == "VIP Channel"

    canonical = validate_force_join_channels_raw(
        "https://t.me/alpha_chan\n-1009876543210|https://t.me/+InvCode|Private VIP"
    )
    assert canonical == "@alpha_chan\n-1009876543210|https://t.me/+InvCode|Private VIP"
    assert validate_force_join_channels_raw("   ") == ""

    with pytest.raises(ValidationError):
        parse_force_join_channels("https://t.me/+OnlyInviteLink", strict=True)
    with pytest.raises(ValidationError):
        parse_force_join_channels("-1001234567890", strict=True)
    with pytest.raises(ValidationError):
        parse_force_join_channels("bad!", strict=True)


async def test_force_join_channel_enforcement_and_verification(
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
    app_settings: Settings,
) -> None:
    async with db_session_factory() as session:
        await run_seed(session)

    existing_tg_id = _unique_tg_id()
    new_user_tg_id = _unique_tg_id()
    admin_tg_id = _unique_tg_id()

    test_settings = app_settings.model_copy(update={"ADMIN_TELEGRAM_IDS": [admin_tg_id]})
    user_svc = UserService(db_session_factory, settings=test_settings)
    sup_svc = SupportService(db_session_factory)
    i18n = TranslationService(db_session_factory, redis_client=redis_client)

    dp = build_dispatcher(
        settings=test_settings,
        session_factory=db_session_factory,
        redis_client=redis_client,
        user_service=user_svc,
        support_service=sup_svc,
        translation_service=i18n,
        rate_limit_max_requests=50,
        rate_limit_window_seconds=3,
    )
    assert ForceJoinMiddleware in [type(m) for m in dp.update.middleware]

    async def _set_setting(key: str, value: str) -> None:
        async with db_session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(select(AppSetting).where(AppSetting.key == key))
                ).scalar_one_or_none()
                if row is None:
                    session.add(AppSetting(key=key, value=value))
                else:
                    row.value = value

    async with db_session_factory() as session:
        orig_enabled = (
            await session.execute(
                select(AppSetting.value).where(AppSetting.key == SettingKey.FORCE_JOIN_ENABLED.value)
            )
        ).scalar_one_or_none()
        orig_channels = (
            await session.execute(
                select(AppSetting.value).where(AppSetting.key == SettingKey.FORCE_JOIN_CHANNELS.value)
            )
        ).scalar_one_or_none()

    bot = RecordingBot()
    try:
        await user_svc.upsert_telegram_user(
            UserCreateOrUpdateDTO(telegram_id=existing_tg_id, username="existing_fj", language_code="en")
        )
        await user_svc.set_language(existing_tg_id, "en")

        await _set_setting(SettingKey.FORCE_JOIN_ENABLED.value, "true")
        await _set_setting(SettingKey.FORCE_JOIN_CHANNELS.value, "@shop_news")

        # 1. Existing unjoined user sends /start -> blocked with Join + Verify buttons
        bot.chat_members[("@shop_news", existing_tg_id)] = ChatMemberLeft(
            user=TgUser(id=existing_tg_id, is_bot=False, first_name="Existing")
        )
        bot.calls.clear()
        await dp.feed_update(bot, _make_message_update(tg_id=existing_tg_id, text="/start"))
        send_calls = [c for c in bot.calls if isinstance(c, SendMessage)]
        assert len(send_calls) == 1
        assert "Channel Subscription Required" in send_calls[0].text
        rows = send_calls[0].reply_markup.inline_keyboard
        assert rows[0][0].url == "https://t.me/shop_news"
        assert rows[1][0].callback_data == MenuCB(action="check_join").pack()

        # 2. Clicking Verify while still not joined triggers alert callback
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(tg_id=existing_tg_id, data=MenuCB(action="check_join").pack()),
        )
        alerts = [c for c in bot.calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]
        assert len(alerts) == 1
        assert "not joined" in (alerts[0].text or "").lower()

        # 3. After joining, clicking Verify opens Main Menu with verified toast
        bot.chat_members[("@shop_news", existing_tg_id)] = ChatMemberMember(
            user=TgUser(id=existing_tg_id, is_bot=False, first_name="Existing")
        )
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(tg_id=existing_tg_id, data=MenuCB(action="check_join").pack()),
        )
        edits = [c for c in bot.calls if isinstance(c, EditMessageText)]
        answers = [c for c in bot.calls if isinstance(c, AnswerCallbackQuery)]
        assert len(edits) == 1 and "$" in edits[0].text
        assert any("verified" in (a.text or "").lower() for a in answers)

        # 4. Brand-new user picks language first, then sees force-join in chosen locale ("fa")
        bot.chat_members[("@shop_news", new_user_tg_id)] = ChatMemberLeft(
            user=TgUser(id=new_user_tg_id, is_bot=False, first_name="NewUser")
        )
        bot.calls.clear()
        await dp.feed_update(bot, _make_message_update(tg_id=new_user_tg_id, text="/start"))
        first_sends = [c for c in bot.calls if isinstance(c, SendMessage)]
        assert len(first_sends) == 1
        assert any(
            btn.callback_data and btn.callback_data.startswith("lang:")
            for row in first_sends[0].reply_markup.inline_keyboard
            for btn in row
        )

        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(tg_id=new_user_tg_id, data=LangCB(code="fa").pack()),
        )
        lang_edits = [c for c in bot.calls if isinstance(c, EditMessageText)]
        assert len(lang_edits) == 1
        assert "عضویت اجباری" in lang_edits[0].text
        assert "$" not in lang_edits[0].text

        bot.chat_members[("@shop_news", new_user_tg_id)] = ChatMemberMember(
            user=TgUser(id=new_user_tg_id, is_bot=False, first_name="NewUser")
        )
        bot.calls.clear()
        await dp.feed_update(
            bot,
            _make_callback_update(tg_id=new_user_tg_id, data=MenuCB(action="check_join").pack()),
        )
        fa_menu_edits = [c for c in bot.calls if isinstance(c, EditMessageText)]
        assert len(fa_menu_edits) == 1
        assert "خوش آمدید" in fa_menu_edits[0].text

        # 5. Admin bypasses force-join even if ChatMemberLeft
        bot.chat_members[("@shop_news", admin_tg_id)] = ChatMemberLeft(
            user=TgUser(id=admin_tg_id, is_bot=False, first_name="Admin")
        )
        bot.calls.clear()
        await dp.feed_update(bot, _make_message_update(tg_id=admin_tg_id, text="/start"))
        assert any(isinstance(c, SendMessage) and "preferred language" in c.text for c in bot.calls)

        # 6. Misconfigured channel (e.g. bot not admin / chat not found) fails open gracefully
        await _set_setting(SettingKey.FORCE_JOIN_CHANNELS.value, "@missing_perm_ch")
        bot.chat_members[("@missing_perm_ch", existing_tg_id)] = TelegramBadRequest(
            method=GetChatMember(chat_id="@missing_perm_ch", user_id=existing_tg_id),
            message="Bad Request: chat not found",
        )
        bot.calls.clear()
        await dp.feed_update(bot, _make_message_update(tg_id=existing_tg_id, text="/start"))
        assert any(isinstance(c, SendMessage) and "$" in c.text for c in bot.calls)
    finally:
        await _set_setting(
            SettingKey.FORCE_JOIN_ENABLED.value,
            orig_enabled if orig_enabled is not None else "false",
        )
        await _set_setting(
            SettingKey.FORCE_JOIN_CHANNELS.value,
            orig_channels if orig_channels is not None else "",
        )
        await _cleanup_user_by_tg_id(
            db_session_factory,
            [existing_tg_id, new_user_tg_id, admin_tg_id],
        )
        await bot.session.close()
