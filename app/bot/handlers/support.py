"""Two-way customer support tickets and live attachment forwarding handlers."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis

from app.bot.handlers import safe_edit_or_send
from app.bot.keyboards.common_kb import MenuCB, SupCB, build_back_button, build_cancel_kb
from app.bot.middlewares.ratelimit_mw import check_action_cooldown
from app.bot.states import SupportStates
from app.config import Settings, get_settings
from app.core.constants import DEFAULT_LANGUAGE, SenderRole, TicketCategory, TicketStatus
from app.core.exceptions import InvalidStateTransitionError, NotFoundError
from app.core.logging import get_logger
from app.core.security import escape_html, sanitize_username
from app.locales.i18n import TranslationService
from app.services.dtos import SupportTicketDetailDTO, UserDTO
from app.services.interfaces import SupportServiceProtocol, UserServiceProtocol

logger = get_logger(__name__)
router = Router(name="customer_support")

_FALLBACK_COOLDOWNS: dict[str, float] = {}

_STATUS_ICONS: dict[TicketStatus, str] = {
    TicketStatus.OPEN: "🟢",
    TicketStatus.ANSWERED: "💬",
    TicketStatus.CLOSED: "🔒",
}


def _extract_message_payload(message: Message) -> tuple[str | None, str | None, str | None]:
    """Extract (message_text, telegram_file_id, file_type) from a Telegram Message."""
    if message.photo:
        return message.caption, message.photo[-1].file_id, "photo"
    if message.document:
        return message.caption, message.document.file_id, "document"
    if message.video:
        return message.caption, message.video.file_id, "video"
    if message.voice:
        return message.caption, message.voice.file_id, "voice"
    if message.text:
        return message.text, None, None
    return None, None, None


async def _send_with_optional_media(
    bot: Bot,
    *,
    chat_id: int,
    text: str,
    file_id: str | None,
    file_type: str | None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if file_id and file_type == "photo":
        await bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    elif file_id and file_type == "document":
        await bot.send_document(
            chat_id=chat_id,
            document=file_id,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    elif file_id and file_type == "video":
        await bot.send_video(
            chat_id=chat_id,
            video=file_id,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    elif file_id and file_type == "voice":
        await bot.send_voice(
            chat_id=chat_id,
            voice=file_id,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )


async def _build_support_home_kb(i18n: TranslationService, lang: str) -> InlineKeyboardMarkup:
    dep_text = await i18n.get_text("support.btn_deposit_issue", lang)
    other_text = await i18n.get_text("support.btn_other_issue", lang)
    my_tickets_raw = await i18n.get_text("support.my_tickets", lang)
    my_tickets_label = my_tickets_raw.replace("<b>", "").replace("</b>", "")
    back_text = await i18n.get_text("btn.back", lang)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=dep_text,
                    callback_data=SupCB(
                        action="cat",
                        cat=TicketCategory.DEPOSIT_ISSUE.value,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=other_text,
                    callback_data=SupCB(
                        action="cat",
                        cat=TicketCategory.OTHER_ISSUE.value,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=my_tickets_label,
                    callback_data=SupCB(action="list").pack(),
                )
            ],
            [build_back_button(text=back_text, callback_data=MenuCB(action="main"))],
        ]
    )


async def _notify_admins_of_support_message(
    bot: Bot | None,
    *,
    settings: Settings,
    user: UserDTO,
    ticket: SupportTicketDetailDTO,
    message_text: str | None,
    file_id: str | None,
    file_type: str | None,
    is_new_ticket: bool,
) -> None:
    if bot is None or not settings.admin_ids:
        return

    heading = "🆘 <b>New Support Ticket</b>" if is_new_ticket else "💬 <b>New Customer Support Reply</b>"
    safe_first = escape_html(user.first_name or "-")
    safe_user = sanitize_username(user.username)
    safe_body = escape_html(message_text) if message_text else "<i>[Attachment]</i>"
    admin_caption = (
        f"{heading}\n\n"
        f"🎫 Ticket: <code>{escape_html(ticket.ticket_number)}</code>\n"
        f"📂 Category: <b>{escape_html(ticket.category.value)}</b>\n"
        f"👤 User: {safe_first} ({safe_user}) [<code>{user.telegram_id}</code>]\n\n"
        f"📝 Message:\n{safe_body}"
    )
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Reply",
                    callback_data=SupCB(action="reply", id=ticket.id).pack(),
                ),
                InlineKeyboardButton(
                    text="🔒 Close",
                    callback_data=SupCB(action="close", id=ticket.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👁 View Thread",
                    callback_data=SupCB(action="view", id=ticket.id).pack(),
                )
            ],
        ]
    )

    for admin_id in settings.admin_ids:
        try:
            await _send_with_optional_media(
                bot,
                chat_id=admin_id,
                text=admin_caption,
                file_id=file_id,
                file_type=file_type,
                reply_markup=admin_kb,
            )
        except Exception as exc:
            logger.warning("Failed forwarding support ticket %s to admin %s: %s", ticket.ticket_number, admin_id, exc)


@router.message(Command("support"))
async def cmd_support(
    message: Message,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    state: FSMContext | None = None,
) -> None:
    """Open Customer Support category menu via command."""
    if state is not None:
        await state.clear()
    active_lang = i18n.normalize_lang(lang)
    prompt = await i18n.get_text("support.choose_category", active_lang)
    kb = await _build_support_home_kb(i18n, active_lang)
    await safe_edit_or_send(message, prompt, reply_markup=kb)


@router.callback_query(MenuCB.filter(F.action == "support"))
@router.callback_query(SupCB.filter(F.action == "new"))
async def cb_support_home(
    callback: CallbackQuery,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    state: FSMContext | None = None,
) -> None:
    """Open Customer Support category menu in-place."""
    if state is not None:
        await state.clear()
    active_lang = i18n.normalize_lang(lang)
    prompt = await i18n.get_text("support.choose_category", active_lang)
    kb = await _build_support_home_kb(i18n, active_lang)
    await safe_edit_or_send(callback, prompt, reply_markup=kb)


@router.callback_query(SupCB.filter(F.action == "cat"))
async def cb_select_support_category(
    callback: CallbackQuery,
    callback_data: SupCB,
    i18n: TranslationService,
    state: FSMContext,
    lang: str = DEFAULT_LANGUAGE,
) -> None:
    """Prompt customer to send support ticket text/photo/document for selected category."""
    active_lang = i18n.normalize_lang(lang)
    raw_cat = callback_data.cat.strip()
    valid_cats = {TicketCategory.DEPOSIT_ISSUE.value, TicketCategory.OTHER_ISSUE.value}
    cat_value = raw_cat if raw_cat in valid_cats else TicketCategory.OTHER_ISSUE.value

    await state.update_data(support_category=cat_value)
    await state.set_state(SupportStates.waiting_ticket_message)

    enter_prompt = await i18n.get_text("support.enter_message", active_lang)
    back_text = await i18n.get_text("btn.back", active_lang)
    kb = build_cancel_kb(text=back_text, callback_data=SupCB(action="new"))
    await safe_edit_or_send(callback, enter_prompt, reply_markup=kb)


@router.message(SupportStates.waiting_ticket_message)
async def msg_create_support_ticket(
    message: Message,
    user: UserDTO,
    support_service: SupportServiceProtocol,
    i18n: TranslationService,
    state: FSMContext,
    lang: str = DEFAULT_LANGUAGE,
    settings: Settings | None = None,
    redis_client: Redis | None = None,
    bot: Bot | None = None,
) -> None:
    """Create support ticket from user message/media, enforce cooldown, and notify admins."""
    active_lang = i18n.normalize_lang(lang or user.language_code)
    runtime_settings = settings or get_settings()

    msg_text, file_id, file_type = _extract_message_payload(message)
    if not (msg_text and msg_text.strip()) and not file_id:
        prompt = await i18n.get_text("support.enter_message", active_lang)
        back_text = await i18n.get_text("btn.back", active_lang)
        await message.answer(
            prompt,
            reply_markup=build_cancel_kb(text=back_text, callback_data=SupCB(action="new")),
            parse_mode=ParseMode.HTML,
        )
        return

    allowed = await check_action_cooldown(
        redis_client,
        action="support",
        user_id=user.telegram_id,
        cooldown_seconds=runtime_settings.SUPPORT_COOLDOWN_SECONDS,
        fallback_store=_FALLBACK_COOLDOWNS,
    )
    if not allowed:
        rl_text = await i18n.get_text("err.rate_limited", active_lang)
        await message.answer(rl_text, parse_mode=ParseMode.HTML)
        return

    state_data = await state.get_data()
    raw_cat = str(state_data.get("support_category", TicketCategory.OTHER_ISSUE.value))
    category = (
        TicketCategory(raw_cat)
        if raw_cat in {TicketCategory.DEPOSIT_ISSUE.value, TicketCategory.OTHER_ISSUE.value}
        else TicketCategory.OTHER_ISSUE
    )

    ticket = await support_service.create_ticket(
        user.id,
        category,
        subject=category.value,
        message_text=msg_text,
        telegram_file_id=file_id,
        file_type=file_type,
    )
    await state.clear()

    created_text = await i18n.get_text(
        "support.ticket_created",
        active_lang,
        ticket_number=escape_html(ticket.ticket_number),
    )
    back_text = await i18n.get_text("btn.back", active_lang)
    my_tickets_raw = await i18n.get_text("support.my_tickets", active_lang)
    my_tickets_label = my_tickets_raw.replace("<b>", "").replace("</b>", "")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🎫 {ticket.ticket_number}",
                    callback_data=SupCB(action="view", id=ticket.id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=my_tickets_label,
                    callback_data=SupCB(action="list").pack(),
                )
            ],
            [build_back_button(text=back_text, callback_data=MenuCB(action="main"))],
        ]
    )
    await message.answer(created_text, reply_markup=kb, parse_mode=ParseMode.HTML)

    await _notify_admins_of_support_message(
        bot or message.bot,
        settings=runtime_settings,
        user=user,
        ticket=ticket,
        message_text=msg_text,
        file_id=file_id,
        file_type=file_type,
        is_new_ticket=True,
    )


@router.callback_query(SupCB.filter(F.action == "list"))
async def cb_list_user_tickets(
    callback: CallbackQuery,
    user: UserDTO,
    support_service: SupportServiceProtocol,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    state: FSMContext | None = None,
) -> None:
    """List user's support tickets with status badges."""
    if state is not None:
        await state.clear()
    active_lang = i18n.normalize_lang(lang or user.language_code)
    heading = await i18n.get_text("support.my_tickets", active_lang)
    tickets = await support_service.list_user_tickets(user.id, limit=15)

    rows: list[list[InlineKeyboardButton]] = []
    if tickets:
        body = heading
        for t in tickets:
            icon = _STATUS_ICONS.get(t.status, "🎫")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {t.ticket_number} ({t.status.value})",
                        callback_data=SupCB(action="view", id=t.id).pack(),
                    )
                ]
            )
    else:
        empty_msg = await i18n.get_text("support.no_tickets", active_lang)
        body = f"{heading}\n\n{empty_msg}"

    back_text = await i18n.get_text("btn.back", active_lang)
    rows.append([build_back_button(text=back_text, callback_data=SupCB(action="new"))])
    await safe_edit_or_send(
        callback,
        body,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _format_ticket_thread(detail: SupportTicketDetailDTO) -> str:
    icon = _STATUS_ICONS.get(detail.status, "🎫")
    lines: list[str] = [
        f"{icon} <b>{escape_html(detail.ticket_number)}</b> "
        f"(<code>{escape_html(detail.category.value)}</code> — <b>{escape_html(detail.status.value)}</b>)",
        "",
    ]
    for msg in detail.messages[-10:]:
        role_badge = "🛡 Admin" if msg.sender_role == SenderRole.ADMIN else "👤 You"
        stamp = msg.created_at.strftime("%m-%d %H:%M")
        content = escape_html(msg.message_text) if msg.message_text else ""
        media_tag = f" 📎[<i>{escape_html(msg.file_type or 'file')}</i>]" if msg.telegram_file_id else ""
        lines.append(f"<b>{role_badge}</b> (<code>{stamp}</code>):{media_tag}\n{content or '<i>[Attachment]</i>'}")
    return "\n\n".join(lines).strip()


@router.callback_query(SupCB.filter(F.action == "view"))
async def cb_view_ticket(
    callback: CallbackQuery,
    callback_data: SupCB,
    user: UserDTO,
    support_service: SupportServiceProtocol,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    is_admin: bool = False,
    state: FSMContext | None = None,
) -> None:
    """View a support ticket conversation thread in-place."""
    if state is not None:
        await state.clear()
    active_lang = i18n.normalize_lang(lang or user.language_code)

    try:
        detail = await support_service.get_ticket_detail(callback_data.id)
    except NotFoundError:
        await callback.answer("Ticket not found", show_alert=True)
        return

    if detail.user_id != user.id and not is_admin:
        await callback.answer("⛔ Access denied", show_alert=True)
        return

    text = _format_ticket_thread(detail)
    back_text = await i18n.get_text("btn.back", active_lang)
    rows: list[list[InlineKeyboardButton]] = []
    if detail.status != TicketStatus.CLOSED:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💬 Reply",
                    callback_data=SupCB(action="reply", id=detail.id).pack(),
                ),
                InlineKeyboardButton(
                    text="🔒 Close",
                    callback_data=SupCB(action="close", id=detail.id).pack(),
                ),
            ]
        )
    rows.append([build_back_button(text=back_text, callback_data=SupCB(action="list"))])
    await safe_edit_or_send(
        callback,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(SupCB.filter(F.action == "reply"))
async def cb_reply_ticket_prompt(
    callback: CallbackQuery,
    callback_data: SupCB,
    user: UserDTO,
    support_service: SupportServiceProtocol,
    i18n: TranslationService,
    state: FSMContext,
    lang: str = DEFAULT_LANGUAGE,
    is_admin: bool = False,
) -> None:
    """Enter FSM state to reply to an open support ticket (as customer or admin)."""
    active_lang = i18n.normalize_lang(lang or user.language_code)
    try:
        detail = await support_service.get_ticket_detail(callback_data.id)
    except NotFoundError:
        await callback.answer("Ticket not found", show_alert=True)
        return

    if detail.user_id != user.id and not is_admin:
        await callback.answer("⛔ Access denied", show_alert=True)
        return

    if detail.status == TicketStatus.CLOSED:
        closed_msg = await i18n.get_text(
            "support.ticket_closed",
            active_lang,
            ticket_number=detail.ticket_number,
        )
        await callback.answer(closed_msg, show_alert=True)
        return

    as_admin = bool(is_admin and detail.user_id != user.id)
    await state.update_data(support_ticket_id=detail.id, support_as_admin=as_admin)
    await state.set_state(SupportStates.waiting_reply_message)

    prompt = await i18n.get_text("support.enter_message", active_lang)
    cancel_text = await i18n.get_text("btn.cancel", active_lang)
    kb = build_cancel_kb(
        text=cancel_text,
        callback_data=SupCB(action="view", id=detail.id),
    )
    await safe_edit_or_send(callback, prompt, reply_markup=kb)


@router.message(SupportStates.waiting_reply_message)
async def msg_reply_support_ticket(
    message: Message,
    user: UserDTO,
    user_service: UserServiceProtocol,
    support_service: SupportServiceProtocol,
    i18n: TranslationService,
    state: FSMContext,
    lang: str = DEFAULT_LANGUAGE,
    is_admin: bool = False,
    settings: Settings | None = None,
    redis_client: Redis | None = None,
    bot: Bot | None = None,
) -> None:
    """Append reply message to support ticket and route notification to admin or customer."""
    active_lang = i18n.normalize_lang(lang or user.language_code)
    runtime_settings = settings or get_settings()

    msg_text, file_id, file_type = _extract_message_payload(message)
    if not (msg_text and msg_text.strip()) and not file_id:
        prompt = await i18n.get_text("support.enter_message", active_lang)
        await message.answer(prompt, parse_mode=ParseMode.HTML)
        return

    state_data = await state.get_data()
    ticket_id = int(state_data.get("support_ticket_id", 0))
    as_admin = bool(state_data.get("support_as_admin", False) and is_admin)

    if not as_admin:
        allowed = await check_action_cooldown(
            redis_client,
            action="support",
            user_id=user.telegram_id,
            cooldown_seconds=runtime_settings.SUPPORT_COOLDOWN_SECONDS,
            fallback_store=_FALLBACK_COOLDOWNS,
        )
        if not allowed:
            rl_text = await i18n.get_text("err.rate_limited", active_lang)
            await message.answer(rl_text, parse_mode=ParseMode.HTML)
            return

    detail = await support_service.get_ticket_detail(ticket_id)
    if detail.user_id != user.id and not is_admin:
        await state.clear()
        return

    sender_role = SenderRole.ADMIN if as_admin else SenderRole.USER
    try:
        await support_service.add_message(
            ticket_id,
            sender_role,
            user.telegram_id,
            message_text=msg_text,
            telegram_file_id=file_id,
            file_type=file_type,
        )
    except InvalidStateTransitionError:
        await state.clear()
        closed_msg = await i18n.get_text(
            "support.ticket_closed",
            active_lang,
            ticket_number=escape_html(detail.ticket_number),
        )
        await message.answer(closed_msg, parse_mode=ParseMode.HTML)
        return

    await state.clear()
    updated_detail = await support_service.get_ticket_detail(ticket_id)
    active_bot = bot or message.bot

    if as_admin:
        customer = await user_service.get_by_id(updated_detail.user_id)
        cust_lang = i18n.normalize_lang(customer.language_code)
        reply_notify = await i18n.get_text(
            "support.reply_from_admin",
            cust_lang,
            ticket_number=escape_html(updated_detail.ticket_number),
            message=escape_html(msg_text or "[Attachment]"),
        )
        cust_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Reply",
                        callback_data=SupCB(action="reply", id=updated_detail.id).pack(),
                    ),
                    InlineKeyboardButton(
                        text="👁 View",
                        callback_data=SupCB(action="view", id=updated_detail.id).pack(),
                    ),
                ]
            ]
        )
        if active_bot is not None:
            try:
                await _send_with_optional_media(
                    active_bot,
                    chat_id=customer.telegram_id,
                    text=reply_notify,
                    file_id=file_id,
                    file_type=file_type,
                    reply_markup=cust_kb,
                )
            except Exception as exc:
                logger.warning("Failed sending admin support reply to user %s: %s", customer.telegram_id, exc)

        await message.answer(
            f"✅ Reply sent to user <code>{customer.telegram_id}</code> on ticket <code>{escape_html(updated_detail.ticket_number)}</code>.",
            parse_mode=ParseMode.HTML,
        )
    else:
        back_text = await i18n.get_text("btn.back", active_lang)
        sent_text = await i18n.get_text(
            "support.ticket_created",
            active_lang,
            ticket_number=escape_html(updated_detail.ticket_number),
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🎫 {updated_detail.ticket_number}",
                        callback_data=SupCB(action="view", id=updated_detail.id).pack(),
                    )
                ],
                [build_back_button(text=back_text, callback_data=MenuCB(action="main"))],
            ]
        )
        await message.answer(sent_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        await _notify_admins_of_support_message(
            active_bot,
            settings=runtime_settings,
            user=user,
            ticket=updated_detail,
            message_text=msg_text,
            file_id=file_id,
            file_type=file_type,
            is_new_ticket=False,
        )


@router.callback_query(SupCB.filter(F.action == "close"))
async def cb_close_ticket(
    callback: CallbackQuery,
    callback_data: SupCB,
    user: UserDTO,
    support_service: SupportServiceProtocol,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    is_admin: bool = False,
    state: FSMContext | None = None,
) -> None:
    """Close a support ticket and update message in-place."""
    if state is not None:
        await state.clear()
    active_lang = i18n.normalize_lang(lang or user.language_code)

    try:
        detail = await support_service.get_ticket_detail(callback_data.id)
    except NotFoundError:
        await callback.answer("Ticket not found", show_alert=True)
        return

    if detail.user_id != user.id and not is_admin:
        await callback.answer("⛔ Access denied", show_alert=True)
        return

    closed = await support_service.close_ticket(detail.id)
    closed_text = await i18n.get_text(
        "support.ticket_closed",
        active_lang,
        ticket_number=escape_html(closed.ticket_number),
    )
    back_text = await i18n.get_text("btn.back", active_lang)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [build_back_button(text=back_text, callback_data=SupCB(action="list"))]
        ]
    )
    await safe_edit_or_send(callback, closed_text, reply_markup=kb)
