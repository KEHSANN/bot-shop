"""Admin Support Tickets management & two-way reply handlers."""

from __future__ import annotations

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
    render_i18n_template,
    resolve_admin_actor_id,
    safe_answer_callback,
    safe_edit_or_send,
)
from app.bot.keyboards.admin_kb import (
    build_admin_back_kb,
    build_admin_cancel_kb,
    build_support_menu_kb,
    build_ticket_detail_kb,
    build_tickets_list_kb,
)
from app.bot.keyboards.common_kb import AdmCB
from app.bot.states import AdminStates
from app.core.constants import SenderRole, TicketStatus
from app.core.exceptions import ShopBotError
from app.core.logging import get_logger
from app.core.security import escape_html

logger = get_logger(__name__)
router = Router(name="admin_support")


async def _render_ticket_detail(
    target: CallbackQuery | Message,
    ticket_id: int,
    data: dict[str, Any],
) -> None:
    """Load and display full SupportTicket conversation thread."""
    try:
        support_service = get_service(data, "support_service")
        detail = await support_service.get_ticket_detail(ticket_id)
    except ShopBotError as exc:
        await safe_edit_or_send(
            target,
            f"⚠️ {escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(sec="sup", act="menu"),
        )
        return

    lines = [
        f"🆘 <b>Ticket {escape_html(detail.ticket_number)}</b>",
        f"👤 Internal User ID: <code>{detail.user_id}</code>",
        f"📂 Category: <code>{detail.category.value}</code> | Status: <b>{detail.status.value}</b>",
        f"🕒 Created: <code>{fmt_dt(detail.created_at)}</code>",
        "",
        "💬 <b>Conversation Thread:</b>",
    ]
    if not detail.messages:
        lines.append("<i>No messages yet.</i>")
    for m in detail.messages[-15:]:
        role_label = "👤 User" if m.sender_role == SenderRole.USER else "🛡 Admin"
        body = escape_html(m.message_text) if m.message_text else f"[Attachment: {escape_html(m.file_type or 'file')}]"
        lines.append(f"<b>{role_label}</b> ({fmt_dt(m.created_at)}):\n{body}\n")

    await safe_edit_or_send(
        target,
        "\n".join(lines)[:3900],
        reply_markup=build_ticket_detail_kb(detail),
    )


@router.callback_query(AdmCB.filter((F.sec == "sup") & (F.act == "menu")))
async def cb_support_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Display Support Tickets filter menu."""
    await state.clear()
    await safe_edit_or_send(
        callback,
        "🆘 <b>Support Tickets Console</b>\n\nFilter tickets by status:",
        reply_markup=build_support_menu_kb(),
    )


@router.callback_query(AdmCB.filter((F.sec == "sup") & (F.act.in_({"open", "answered", "closed", "all"}))))
async def cb_support_list(
    callback: CallbackQuery,
    callback_data: AdmCB,
    state: FSMContext,
    **data: Any,
) -> None:
    """List support tickets filtered by status."""
    await state.clear()
    status_filter: TicketStatus | None = None
    if callback_data.act != "all":
        status_filter = TicketStatus(callback_data.act)

    try:
        support_service = get_service(data, "support_service")
        tickets = await support_service.list_tickets(status=status_filter, limit=25)
    except ShopBotError as exc:
        await safe_edit_or_send(
            callback,
            f"⚠️ {escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(sec="sup", act="menu"),
        )
        return

    if not tickets:
        await safe_edit_or_send(
            callback,
            f"🆘 <b>Tickets ({callback_data.act})</b>\n\nNo tickets found.",
            reply_markup=build_support_menu_kb(),
        )
        return

    await safe_edit_or_send(
        callback,
        f"🆘 <b>Tickets ({callback_data.act})</b> — Select one to inspect:",
        reply_markup=build_tickets_list_kb(tickets),
    )


@router.callback_query(AdmCB.filter((F.sec == "sup") & (F.act == "view")))
async def cb_support_view(
    callback: CallbackQuery,
    callback_data: AdmCB,
    state: FSMContext,
    **data: Any,
) -> None:
    """View a single SupportTicket thread."""
    await state.clear()
    await _render_ticket_detail(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "sup") & (F.act == "reply")))
async def cb_support_reply_prompt(
    callback: CallbackQuery,
    callback_data: AdmCB,
    state: FSMContext,
) -> None:
    """Prompt admin for text, photo, or file reply to a ticket."""
    await state.set_state(AdminStates.waiting_support_reply)
    await state.update_data(reply_ticket_id=callback_data.id)
    await safe_edit_or_send(
        callback,
        f"✍️ <b>Reply to Ticket #{callback_data.id}</b>\n\n"
        f"Send your reply text, photo, or document:",
        reply_markup=build_admin_cancel_kb(sec="sup", act="view", target_id=callback_data.id),
    )


@router.message(AdminStates.waiting_support_reply)
async def msg_support_reply_send(
    message: Message,
    state: FSMContext,
    bot: Bot | None = None,
    **data: Any,
) -> None:
    """Record admin support reply and forward text/attachment to the ticket owner."""
    fsm_data = await state.get_data()
    ticket_id = int(fsm_data.get("reply_ticket_id", 0))

    file_id: str | None = None
    file_type: str | None = None
    reply_text: str | None = message.text

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
        reply_text = message.caption or ""
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
        reply_text = message.caption or ""

    if not reply_text and not file_id:
        await safe_edit_or_send(
            message,
            "❌ Please send text, a photo, or a document.",
            reply_markup=build_admin_cancel_kb(sec="sup", act="view", target_id=ticket_id),
        )
        return

    try:
        support_service = get_service(data, "support_service")
        detail = await support_service.get_ticket_detail(ticket_id)
        await support_service.add_message(
            ticket_id,
            SenderRole.ADMIN,
            resolve_admin_actor_id(message),
            message_text=reply_text,
            telegram_file_id=file_id,
            file_type=file_type,
        )
    except ShopBotError as exc:
        await state.clear()
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(sec="sup", act="menu"),
        )
        return

    await state.clear()

    active_bot = bot or message.bot
    user_service = get_optional_service(data, "user_service")
    if active_bot is not None and user_service is not None:
        try:
            user = await user_service.get_by_id(detail.user_id)
            notify_text = await render_i18n_template(
                data,
                "support.reply_from_admin",
                user.language_code,
                ticket_number=escape_html(detail.ticket_number),
                message=escape_html(reply_text or f"[{file_type}]"),
            )
            if file_type == "photo" and file_id:
                await active_bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=file_id,
                    caption=notify_text,
                    parse_mode=ParseMode.HTML,
                )
            elif file_type == "document" and file_id:
                await active_bot.send_document(
                    chat_id=user.telegram_id,
                    document=file_id,
                    caption=notify_text,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await notify_user_dm(active_bot, user.telegram_id, notify_text)
        except (ShopBotError, TelegramAPIError) as exc:
            logger.warning("Could not forward admin ticket reply to user_id=%s: %s", detail.user_id, exc)

    await _render_ticket_detail(message, ticket_id, data)


@router.callback_query(AdmCB.filter((F.sec == "sup") & (F.act == "close")))
async def cb_support_close(
    callback: CallbackQuery,
    callback_data: AdmCB,
    bot: Bot | None = None,
    **data: Any,
) -> None:
    """Close a support ticket and notify the customer."""
    try:
        support_service = get_service(data, "support_service")
        closed = await support_service.close_ticket(callback_data.id)
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return

    active_bot = bot or callback.bot
    user_service = get_optional_service(data, "user_service")
    if active_bot is not None and user_service is not None:
        try:
            user = await user_service.get_by_id(closed.user_id)
            notify_text = await render_i18n_template(
                data,
                "support.ticket_closed",
                user.language_code,
                ticket_number=escape_html(closed.ticket_number),
            )
            await notify_user_dm(active_bot, user.telegram_id, notify_text)
        except ShopBotError as exc:
            logger.warning("Could not notify user on ticket close: %s", exc)

    await safe_answer_callback(callback, "Ticket closed.")
    await _render_ticket_detail(callback, closed.id, data)
