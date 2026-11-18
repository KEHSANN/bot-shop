"""Admin Member Search & User Management handlers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.common import (
    fmt_dt,
    get_optional_service,
    get_service,
    notify_user_dm,
    parse_positive_decimal,
    render_i18n_template,
    resolve_admin_actor_id,
    safe_answer_callback,
    safe_edit_or_send,
)
from app.bot.keyboards.admin_kb import (
    build_admin_back_kb,
    build_admin_cancel_kb,
    build_user_inspection_kb,
    build_user_list_kb,
    build_users_menu_kb,
)
from app.bot.keyboards.common_kb import AdmCB
from app.bot.states import AdminStates
from app.core.constants import Currency
from app.core.exceptions import ShopBotError
from app.core.logging import get_logger
from app.core.security import escape_html, sanitize_username
from app.services.dtos import BalanceAdjustmentDTO, UserDTO

logger = get_logger(__name__)
router = Router(name="admin_users")

_PAGE_SIZE = 10


async def _render_user_card(
    target: CallbackQuery | Message,
    user: UserDTO,
    data: dict[str, Any],
) -> None:
    """Format and display the User Profile Inspection & Actions Card."""
    payment_service = get_optional_service(data, "payment_service")
    shop_service = get_optional_service(data, "shop_service")

    total_deposits = 0
    total_orders = 0
    if payment_service is not None:
        try:
            payments = await payment_service.list_payments(user_id=user.id, limit=200)
            total_deposits = len(payments)
        except Exception as exc:
            logger.debug("Could not fetch user deposits count: %s", exc)
    if shop_service is not None:
        try:
            orders = await shop_service.list_user_orders(user.id, limit=200)
            total_orders = len(orders)
        except Exception as exc:
            logger.debug("Could not fetch user orders count: %s", exc)

    full_name = " ".join(part for part in (user.first_name, user.last_name) if part) or "-"
    status_badge = "🚫 <b>Blocked</b>" if user.is_blocked else "🟢 <b>Active</b>"
    admin_badge = " | 🛡 Admin" if user.is_admin else ""

    text = (
        f"👤 <b>User Inspection Card</b>\n\n"
        f"🆔 Internal ID: <code>{user.id}</code>\n"
        f"📲 Telegram ID: <code>{user.telegram_id}</code>\n"
        f"🔗 Username: {sanitize_username(user.username)}\n"
        f"📛 Name: <b>{escape_html(full_name)}</b>\n"
        f"🌐 Language: <code>{escape_html(user.language_code)}</code>\n"
        f"📅 Registered: <code>{fmt_dt(user.created_at)}</code>\n"
        f"🚦 Status: {status_badge}{admin_badge}\n\n"
        f"💵 USD Balance: <b>${user.balance_usd:,.2f}</b>\n"
        f"💳 IRT Balance: <b>{user.balance_irt:,.0f} تومان</b>\n"
        f"📥 Total Deposits: <b>{total_deposits}</b> | 🛒 Total Orders: <b>{total_orders}</b>"
    )
    await safe_edit_or_send(target, text, reply_markup=build_user_inspection_kb(user))


@router.callback_query(AdmCB.filter((F.sec == "usr") & (F.act == "menu")))
async def cb_users_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Display the Member Search & User Management menu."""
    await state.clear()
    await safe_edit_or_send(
        callback,
        "👥 <b>Member Search &amp; User Management</b>\n\nChoose an action below:",
        reply_markup=build_users_menu_kb(),
    )


@router.callback_query(AdmCB.filter((F.sec == "usr") & (F.act.in_({"list", "blocked"}))))
async def cb_users_list(
    callback: CallbackQuery,
    callback_data: AdmCB,
    state: FSMContext,
    **data: Any,
) -> None:
    """List all users or blocked users with pagination."""
    await state.clear()
    blocked_only = callback_data.act == "blocked"
    page = max(0, callback_data.page)
    try:
        user_service = get_service(data, "user_service")
        users = await user_service.list_users(
            limit=_PAGE_SIZE + 1,
            offset=page * _PAGE_SIZE,
            blocked_only=blocked_only,
        )
    except ShopBotError as exc:
        await safe_edit_or_send(
            callback,
            f"⚠️ {escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(sec="usr", act="menu"),
        )
        return

    has_next = len(users) > _PAGE_SIZE
    visible = users[:_PAGE_SIZE]
    title = "🚫 <b>Blocked Users</b>" if blocked_only else "👥 <b>Registered Users</b>"
    if not visible:
        await safe_edit_or_send(
            callback,
            f"{title}\n\nNo matching users found.",
            reply_markup=build_admin_back_kb(sec="usr", act="menu"),
        )
        return

    await safe_edit_or_send(
        callback,
        f"{title} (Page {page + 1}):",
        reply_markup=build_user_list_kb(visible, act=callback_data.act, page=page, has_next=has_next),
    )


@router.callback_query(AdmCB.filter((F.sec == "usr") & (F.act == "search")))
async def cb_users_search_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt admin for Telegram ID, @username, or name query."""
    await state.set_state(AdminStates.waiting_user_search)
    await safe_edit_or_send(
        callback,
        "🔍 <b>Search Member</b>\n\n"
        "Send numeric <code>telegram_id</code>, <code>@username</code>, or first/last name:",
        reply_markup=build_admin_cancel_kb(sec="usr", act="menu"),
    )


@router.message(AdminStates.waiting_user_search)
async def msg_users_search_execute(
    message: Message,
    state: FSMContext,
    **data: Any,
) -> None:
    """Execute user search by numeric telegram_id, @username, or name."""
    query = (message.text or "").strip()
    if not query:
        await safe_edit_or_send(
            message,
            "❌ Please send a valid search term (Telegram ID, @username, or name).",
            reply_markup=build_admin_cancel_kb(sec="usr", act="menu"),
        )
        return

    try:
        user_service = get_service(data, "user_service")
        matches: list[UserDTO] = []
        if query.lstrip("-").isdigit():
            by_tg = await user_service.get_by_telegram_id(int(query))
            if by_tg is not None:
                matches.append(by_tg)
        if not matches:
            matches = await user_service.search_users(query, limit=15)
    except ShopBotError as exc:
        await state.clear()
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(sec="usr", act="menu"),
        )
        return

    await state.clear()
    if not matches:
        await safe_edit_or_send(
            message,
            f"🔍 No users found matching <code>{escape_html(query)}</code>.",
            reply_markup=build_users_menu_kb(),
        )
        return

    if len(matches) == 1:
        await _render_user_card(message, matches[0], data)
        return

    await safe_edit_or_send(
        message,
        f"🔍 <b>Search Results for</b> <code>{escape_html(query)}</code> ({len(matches)}):",
        reply_markup=build_user_list_kb(matches, act="list", page=0, has_next=False),
    )


@router.callback_query(AdmCB.filter((F.sec == "usr") & (F.act == "view")))
async def cb_user_view(
    callback: CallbackQuery,
    callback_data: AdmCB,
    state: FSMContext,
    **data: Any,
) -> None:
    """Inspect a specific user's profile card."""
    await state.clear()
    try:
        user_service = get_service(data, "user_service")
        user = await user_service.get_by_id(callback_data.id)
    except ShopBotError as exc:
        await safe_edit_or_send(
            callback,
            f"⚠️ {escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(sec="usr", act="menu"),
        )
        return

    await _render_user_card(callback, user, data)


@router.callback_query(AdmCB.filter((F.sec == "usr") & (F.act == "toggle_ban")))
async def cb_user_toggle_ban(
    callback: CallbackQuery,
    callback_data: AdmCB,
    **data: Any,
) -> None:
    """Block or unblock the inspected user immediately."""
    try:
        user_service = get_service(data, "user_service")
        current = await user_service.get_by_id(callback_data.id)
        updated = await user_service.set_blocked(
            current.id,
            blocked=not current.is_blocked,
            admin_id=resolve_admin_actor_id(callback),
        )
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return

    status_txt = "User blocked." if updated.is_blocked else "User unblocked."
    await safe_answer_callback(callback, status_txt, show_alert=False)
    await _render_user_card(callback, updated, data)


@router.callback_query(AdmCB.filter((F.sec == "usr") & (F.act.in_({"chg_usd", "ded_usd", "chg_irt", "ded_irt"}))))
async def cb_user_balance_prompt(
    callback: CallbackQuery,
    callback_data: AdmCB,
    state: FSMContext,
) -> None:
    """Start balance charge/deduct FSM flow for USD or IRT."""
    is_usd = callback_data.act.endswith("_usd")
    is_charge = callback_data.act.startswith("chg_")
    currency = Currency.USD.value if is_usd else Currency.IRT.value
    action_label = "Charge (+)" if is_charge else "Deduct (-)"

    await state.set_state(AdminStates.waiting_balance_amount)
    await state.update_data(
        bal_user_id=callback_data.id,
        bal_currency=currency,
        bal_sign=1 if is_charge else -1,
    )
    await safe_edit_or_send(
        callback,
        f"💰 <b>{action_label} {currency} Balance</b>\n\n"
        f"Enter the positive {currency} amount:",
        reply_markup=build_admin_cancel_kb(sec="usr", act="view", target_id=callback_data.id),
    )


@router.message(AdminStates.waiting_balance_amount)
async def msg_user_balance_amount(message: Message, state: FSMContext) -> None:
    """Validate Decimal balance adjustment amount and ask for optional note."""
    fsm_data = await state.get_data()
    target_user_id = int(fsm_data.get("bal_user_id", 0))
    amount = parse_positive_decimal(message.text or "")
    if amount is None:
        await safe_edit_or_send(
            message,
            "❌ Invalid amount. Please enter a strictly positive number (e.g. <code>10.50</code>):",
            reply_markup=build_admin_cancel_kb(sec="usr", act="view", target_id=target_user_id),
        )
        return

    await state.update_data(bal_amount=str(amount))
    await state.set_state(AdminStates.waiting_balance_note)
    await safe_edit_or_send(
        message,
        "📝 Send an optional reason note for the ledger & user notification (or send <code>-</code> to skip):",
        reply_markup=build_admin_cancel_kb(sec="usr", act="view", target_id=target_user_id),
    )


@router.message(AdminStates.waiting_balance_note)
async def msg_user_balance_commit(
    message: Message,
    state: FSMContext,
    bot: Bot | None = None,
    **data: Any,
) -> None:
    """Execute atomic wallet adjustment and notify customer."""
    fsm_data = await state.get_data()
    target_user_id = int(fsm_data.get("bal_user_id", 0))
    currency = Currency(str(fsm_data.get("bal_currency", "USD")))
    sign = int(fsm_data.get("bal_sign", 1))
    amount = Decimal(str(fsm_data.get("bal_amount", "0")))
    signed_amount = amount if sign > 0 else -amount

    raw_note = (message.text or "").strip()
    note = None if raw_note in {"", "-"} else raw_note

    try:
        user_service = get_service(data, "user_service")
        updated_user, _ledger = await user_service.adjust_balance(
            BalanceAdjustmentDTO(
                user_id=target_user_id,
                currency=currency,
                amount=signed_amount,
                admin_id=resolve_admin_actor_id(message),
                note=note,
            )
        )
    except ShopBotError as exc:
        await state.clear()
        await safe_edit_or_send(
            message,
            f"⚠️ <b>Balance Adjustment Failed:</b> {escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(sec="usr", act="view", target_id=target_user_id),
        )
        return

    await state.clear()
    active_bot = bot or message.bot
    if active_bot is not None:
        amt_str = f"${amount:,.2f} USD" if currency == Currency.USD else f"{amount:,.0f} IRT"
        tpl_key = "balance.admin_credited" if sign > 0 else "balance.admin_deducted"
        notify_text = await render_i18n_template(
            data,
            tpl_key,
            updated_user.language_code,
            amount=amt_str,
            note=escape_html(note or "-"),
        )
        await notify_user_dm(active_bot, updated_user.telegram_id, notify_text)

    await _render_user_card(message, updated_user, data)


@router.callback_query(AdmCB.filter((F.sec == "usr") & (F.act == "dm")))
async def cb_user_dm_prompt(
    callback: CallbackQuery,
    callback_data: AdmCB,
    state: FSMContext,
    **data: Any,
) -> None:
    """Start direct message flow from User Inspection Card."""
    try:
        user_service = get_service(data, "user_service")
        user = await user_service.get_by_id(callback_data.id)
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return

    await state.set_state(AdminStates.waiting_direct_message)
    await state.update_data(dm_user_id=user.id, dm_telegram_id=user.telegram_id)
    await safe_edit_or_send(
        callback,
        f"✉️ <b>Direct Message to</b> <code>{user.telegram_id}</code> ({sanitize_username(user.username)})\n\n"
        f"Send text, photo, video, or document:",
        reply_markup=build_admin_cancel_kb(sec="usr", act="view", target_id=user.id),
    )


@router.message(AdminStates.waiting_direct_message)
async def msg_user_dm_send(
    message: Message,
    state: FSMContext,
    bot: Bot | None = None,
    **data: Any,
) -> None:
    """Deliver admin direct message (text/photo/video/document) to target user."""
    fsm_data = await state.get_data()
    target_user_id = int(fsm_data.get("dm_user_id", 0))
    target_tg_id = int(fsm_data.get("dm_telegram_id", 0))
    active_bot = bot or message.bot

    payload: dict[str, Any] = {}
    if message.photo:
        payload["content_type"] = "photo"
        payload["photo_file_id"] = message.photo[-1].file_id
        payload["caption"] = escape_html(message.caption) if message.caption else ""
    elif message.video:
        payload["content_type"] = "video"
        payload["video_file_id"] = message.video.file_id
        payload["caption"] = escape_html(message.caption) if message.caption else ""
    elif message.document:
        payload["content_type"] = "document"
        payload["document_file_id"] = message.document.file_id
        payload["caption"] = escape_html(message.caption) if message.caption else ""
    elif message.text:
        payload["content_type"] = "text"
        payload["text"] = escape_html(message.text)
    else:
        await safe_edit_or_send(
            message,
            "❌ Unsupported content type. Send text, photo, video, or document.",
            reply_markup=build_admin_cancel_kb(sec="usr", act="menu"),
        )
        return

    try:
        broadcast_service = get_service(data, "broadcast_service", bot=active_bot)
        ok = await broadcast_service.send_direct_message(target_tg_id, payload, bot=active_bot)
    except ShopBotError as exc:
        ok = False
        logger.warning("DM send failed: %s", exc)

    await state.clear()
    back_sec = "usr" if target_user_id else "bc"
    back_act = "view" if target_user_id else "menu"
    status_msg = (
        f"✅ Direct message delivered to <code>{target_tg_id}</code>."
        if ok
        else f"❌ Failed to deliver message to <code>{target_tg_id}</code> (user may have blocked the bot)."
    )
    await safe_edit_or_send(
        message,
        status_msg,
        reply_markup=build_admin_back_kb(sec=back_sec, act=back_act, target_id=target_user_id),
    )


@router.callback_query(AdmCB.filter((F.sec == "usr") & (F.act.in_({"deposits", "orders", "ledger"}))))
async def cb_user_history(
    callback: CallbackQuery,
    callback_data: AdmCB,
    **data: Any,
) -> None:
    """View Deposit History, Order History, or Wallet Ledger for an inspected user."""
    user_id = callback_data.id
    kind = callback_data.act
    lines: list[str] = []

    try:
        if kind == "deposits":
            payment_service = get_service(data, "payment_service")
            payments = await payment_service.list_payments(user_id=user_id, limit=15)
            lines.append(f"💳 <b>Deposit History (User #{user_id})</b>\n")
            if not payments:
                lines.append("No deposits found.")
            for p in payments:
                amt = f"${p.amount_usd:,.2f}" if p.amount_usd > 0 else f"{p.amount_irt:,.0f} IRT"
                lines.append(
                    f"• #{p.id} | {p.payment_category.value} | <b>{amt}</b> | "
                    f"<code>{p.status.value}</code> | {fmt_dt(p.created_at)}"
                )
        elif kind == "orders":
            shop_service = get_service(data, "shop_service")
            orders = await shop_service.list_user_orders(user_id, limit=15)
            lines.append(f"📜 <b>Order History (User #{user_id})</b>\n")
            if not orders:
                lines.append("No orders found.")
            for o in orders:
                lines.append(
                    f"• <code>{escape_html(o.order_number)}</code> | x{o.quantity} | "
                    f"<b>{o.total_amount:,.2f} {o.currency.value}</b> | {o.status.value} | {fmt_dt(o.created_at)}"
                )
        else:
            user_service = get_service(data, "user_service")
            entries = await user_service.get_wallet_history(user_id, limit=15)
            lines.append(f"📒 <b>Wallet Ledger (User #{user_id})</b>\n")
            if not entries:
                lines.append("No ledger entries found.")
            for e in entries:
                sign = "+" if e.change_amount > 0 else ""
                lines.append(
                    f"• {e.currency.value}: <b>{sign}{e.change_amount:,.2f}</b> "
                    f"({e.before_balance:,.2f} → {e.after_balance:,.2f}) | {e.reference_type.value} | {escape_html(e.note or '-')}"
                )
    except ShopBotError as exc:
        lines = [f"⚠️ {escape_html(exc.message)}"]

    await safe_edit_or_send(
        callback,
        "\n".join(lines),
        reply_markup=build_admin_back_kb(sec="usr", act="view", target_id=user_id, label="🔙 Back to User Card"),
    )
