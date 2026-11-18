"""Admin Mass Broadcast wizard (audience filter, media/text/button, live progress) & DM handlers."""

from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.common import (
    fmt_dt,
    get_service,
    resolve_admin_actor_id,
    safe_answer_callback,
    safe_edit_or_send,
)
from app.bot.keyboards.admin_kb import (
    build_admin_back_kb,
    build_admin_cancel_kb,
    build_broadcast_audience_kb,
    build_broadcast_confirm_kb,
    build_broadcast_jobs_kb,
    build_broadcast_menu_kb,
)
from app.bot.keyboards.common_kb import AdmCB
from app.bot.states import AdminStates
from app.core.constants import SUPPORTED_LANGUAGES
from app.core.exceptions import ShopBotError
from app.core.logging import get_logger
from app.core.security import escape_html
from app.services.broadcast_service import format_broadcast_progress_bar
from app.services.dtos import BroadcastCreateDTO, BroadcastJobDTO

logger = get_logger(__name__)
router = Router(name="admin_broadcast")


def _extract_broadcast_payload(message: Message) -> dict[str, Any] | None:
    """Extract text/caption, optional media file_id, and optional BTN: Label | URL line."""
    raw_text = (message.caption if (message.photo or message.video or message.document) else message.text) or ""
    lines = raw_text.splitlines()
    buttons: list[dict[str, str]] = []
    kept_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("BTN:") and "|" in stripped:
            btn_spec = stripped[4:].strip()
            b_text, b_url = [part.strip() for part in btn_spec.split("|", 1)]
            if b_text and b_url.startswith(("http://", "https://", "tg://")):
                buttons.append({"text": b_text, "url": b_url})
                continue
        kept_lines.append(line)

    cleaned_text = "\n".join(kept_lines).strip()
    payload: dict[str, Any] = {}

    if message.photo:
        payload["content_type"] = "photo"
        payload["photo_file_id"] = message.photo[-1].file_id
        payload["caption"] = cleaned_text
    elif message.video:
        payload["content_type"] = "video"
        payload["video_file_id"] = message.video.file_id
        payload["caption"] = cleaned_text
    elif message.document:
        payload["content_type"] = "document"
        payload["document_file_id"] = message.document.file_id
        payload["caption"] = cleaned_text
    elif cleaned_text:
        payload["content_type"] = "text"
        payload["text"] = cleaned_text
    else:
        return None

    if buttons:
        payload["buttons"] = buttons
    return payload


@router.callback_query(AdmCB.filter((F.sec == "bc") & (F.act == "menu")))
async def cb_broadcast_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_or_send(
        callback,
        "📢 <b>Messaging &amp; Mass Broadcast</b>\n\nChoose an action:",
        reply_markup=build_broadcast_menu_kb(),
    )


@router.callback_query(AdmCB.filter((F.sec == "bc") & (F.act == "dm")))
async def cb_broadcast_dm_target_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_broadcast_content)
    await state.update_data(bc_mode="dm_target")
    await safe_edit_or_send(
        callback,
        "✉️ <b>Single-User Direct Message</b>\n\n"
        "Send the target user's numeric <code>telegram_id</code> or <code>@username</code>:",
        reply_markup=build_admin_cancel_kb(sec="bc", act="menu"),
    )


@router.callback_query(AdmCB.filter((F.sec == "bc") & (F.act == "new")))
async def cb_broadcast_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_or_send(
        callback,
        "🚀 <b>Mass Broadcast Wizard (Step 1/3)</b>\n\nSelect the target audience:",
        reply_markup=build_broadcast_audience_kb(),
    )


@router.callback_query(AdmCB.filter((F.sec == "bc") & (F.act.startswith("aud_"))))
async def cb_broadcast_select_audience(
    callback: CallbackQuery,
    callback_data: AdmCB,
    state: FSMContext,
    **data: Any,
) -> None:
    suffix = callback_data.act.removeprefix("aud_")
    if suffix == "all":
        target_filter = "all"
    elif suffix == "bal":
        target_filter = "has_balance"
    elif suffix in SUPPORTED_LANGUAGES:
        target_filter = f"lang:{suffix}"
    else:
        await safe_answer_callback(callback, "Unsupported audience filter", show_alert=True)
        return

    try:
        broadcast_service = get_service(data, "broadcast_service")
        targets = await broadcast_service.resolve_target_telegram_ids(target_filter)
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="bc", act="menu"))
        return

    await state.set_state(AdminStates.waiting_broadcast_content)
    await state.update_data(bc_mode="mass", bc_filter=target_filter, bc_count=len(targets))
    await safe_edit_or_send(
        callback,
        f"🚀 <b>Mass Broadcast Wizard (Step 2/3)</b>\n\n"
        f"🎯 Audience: <code>{escape_html(target_filter)}</code> (<b>{len(targets)}</b> active recipients)\n\n"
        f"Send your broadcast message (text, photo, or video).\n"
        f"<i>Optional inline URL button:</i> add a line <code>BTN: Button Label | https://example.com</code>",
        reply_markup=build_admin_cancel_kb(sec="bc", act="menu"),
    )


@router.message(AdminStates.waiting_broadcast_content)
async def msg_broadcast_content(message: Message, state: FSMContext, **data: Any) -> None:
    fsm_data = await state.get_data()
    bc_mode = str(fsm_data.get("bc_mode", "mass"))

    if bc_mode == "dm_target":
        query = (message.text or "").strip()
        if not query:
            await safe_edit_or_send(
                message,
                "❌ Please enter a numeric Telegram ID or <code>@username</code>.",
                reply_markup=build_admin_cancel_kb(sec="bc", act="menu"),
            )
            return

        target_tg_id: int | None = None
        target_user_id = 0
        try:
            user_service = get_service(data, "user_service")
            if query.lstrip("-").isdigit():
                tg_num = int(query)
                found = await user_service.get_by_telegram_id(tg_num)
                if found is not None:
                    target_tg_id = found.telegram_id
                    target_user_id = found.id
                elif tg_num > 0:
                    target_tg_id = tg_num
            else:
                matches = await user_service.search_users(query, limit=1)
                if matches:
                    target_tg_id = matches[0].telegram_id
                    target_user_id = matches[0].id
        except ShopBotError:
            if query.isdigit():
                target_tg_id = int(query)

        if target_tg_id is None:
            await safe_edit_or_send(
                message,
                f"❌ No user found matching <code>{escape_html(query)}</code>.",
                reply_markup=build_admin_cancel_kb(sec="bc", act="menu"),
            )
            return

        await state.set_state(AdminStates.waiting_direct_message)
        await state.update_data(dm_user_id=target_user_id, dm_telegram_id=target_tg_id)
        await safe_edit_or_send(
            message,
            f"✉️ Target resolved: <code>{target_tg_id}</code>\n\nNow send text, photo, video, or document:",
            reply_markup=build_admin_cancel_kb(sec="bc", act="menu"),
        )
        return

    payload = _extract_broadcast_payload(message)
    if payload is None:
        await safe_edit_or_send(
            message,
            "❌ Please send text, photo, video, or document content.",
            reply_markup=build_admin_cancel_kb(sec="bc", act="menu"),
        )
        return

    target_filter = str(fsm_data.get("bc_filter", "all"))
    count = int(fsm_data.get("bc_count", 0))
    await state.update_data(bc_payload=payload)

    preview_body = escape_html(payload.get("text") or payload.get("caption") or "[Media Only]")
    btn_count = len(payload.get("buttons", []))
    await safe_edit_or_send(
        message,
        f"🚀 <b>Mass Broadcast Wizard (Step 3/3 — Confirm)</b>\n\n"
        f"🎯 Audience: <code>{escape_html(target_filter)}</code> (<b>{count}</b> recipients)\n"
        f"📦 Type: <code>{escape_html(payload.get('content_type', 'text'))}</code> | URL Buttons: <b>{btn_count}</b>\n\n"
        f"📝 Preview:\n{preview_body[:1000]}",
        reply_markup=build_broadcast_confirm_kb(),
    )


@router.callback_query(AdmCB.filter((F.sec == "bc") & (F.act == "confirm")))
async def cb_broadcast_launch(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot | None = None,
    **data: Any,
) -> None:
    """Create BroadcastJob and execute rate-limited delivery with live progress bar updates."""
    fsm_data = await state.get_data()
    target_filter = str(fsm_data.get("bc_filter", "all"))
    payload = fsm_data.get("bc_payload")
    if not isinstance(payload, dict) or not payload:
        await safe_answer_callback(callback, "Broadcast content missing. Please restart wizard.", show_alert=True)
        await state.clear()
        return

    active_bot = bot or callback.bot
    await state.clear()

    try:
        broadcast_service = get_service(data, "broadcast_service", bot=active_bot)
        job = await broadcast_service.create_job(
            BroadcastCreateDTO(
                admin_id=resolve_admin_actor_id(callback),
                target_filter=target_filter,
                message_payload=payload,
            )
        )
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="bc", act="menu"))
        return

    status_msg = await safe_edit_or_send(
        callback,
        f"📡 <b>Broadcast Job #{job.id} Running...</b>\n\n"
        f"🎯 Filter: <code>{escape_html(job.target_filter)}</code>\n"
        f"📊 {format_broadcast_progress_bar(job)}",
    )

    async def _edit_status(text: str, reply_markup: Any = None) -> None:
        if status_msg is None:
            return
        if active_bot is not None:
            await active_bot.edit_message_text(
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        else:
            await status_msg.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)

    async def _on_progress(progress_job: BroadcastJobDTO) -> None:
        try:
            await _edit_status(
                f"📡 <b>Broadcast Job #{progress_job.id} ({progress_job.status.value})</b>\n\n"
                f"🎯 Filter: <code>{escape_html(progress_job.target_filter)}</code>\n"
                f"📊 {format_broadcast_progress_bar(progress_job)}",
            )
        except TelegramAPIError as exc:
            logger.debug("Progress bar edit skipped: %s", exc)

    final_job = await broadcast_service.execute_job(
        job.id,
        bot=active_bot,
        progress_callback=_on_progress,
        progress_batch_size=10,
    )

    summary_text = (
        f"✅ <b>Broadcast Job #{final_job.id} {final_job.status.value.title()}</b>\n\n"
        f"🎯 Audience: <code>{escape_html(final_job.target_filter)}</code>\n"
        f"📊 {format_broadcast_progress_bar(final_job)}"
    )
    if status_msg is not None:
        try:
            await _edit_status(summary_text, reply_markup=build_admin_back_kb(sec="bc", act="menu"))
            return
        except TelegramAPIError:
            pass
    await safe_edit_or_send(callback, summary_text, reply_markup=build_admin_back_kb(sec="bc", act="menu"))


@router.callback_query(AdmCB.filter((F.sec == "bc") & (F.act == "list")))
async def cb_broadcast_history(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    try:
        broadcast_service = get_service(data, "broadcast_service")
        jobs = await broadcast_service.list_jobs(limit=15)
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="bc", act="menu"))
        return

    if not jobs:
        await safe_edit_or_send(callback, "📜 <b>Broadcast History</b>\n\nNo jobs recorded yet.", reply_markup=build_broadcast_menu_kb())
        return
    await safe_edit_or_send(callback, "📜 <b>Recent Broadcast Jobs</b>:", reply_markup=build_broadcast_jobs_kb(jobs))


@router.callback_query(AdmCB.filter((F.sec == "bc") & (F.act == "job")))
async def cb_broadcast_job_detail(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        broadcast_service = get_service(data, "broadcast_service")
        job = await broadcast_service.get_job(callback_data.id)
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="bc", act="list"))
        return

    text = (
        f"📡 <b>Broadcast Job #{job.id}</b>\n\n"
        f"🚦 Status: <b>{job.status.value}</b>\n"
        f"🎯 Filter: <code>{escape_html(job.target_filter)}</code>\n"
        f"📊 {format_broadcast_progress_bar(job)}\n"
        f"🕒 Created: <code>{fmt_dt(job.created_at)}</code>\n"
        f"🏁 Completed: <code>{fmt_dt(job.completed_at)}</code>"
    )
    await safe_edit_or_send(callback, text, reply_markup=build_admin_back_kb(sec="bc", act="list"))
