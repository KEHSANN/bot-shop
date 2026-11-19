"""Admin Dynamic CMS (12-Language Texts), UI Buttons/Colors & AppSettings handlers."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.common import (
    get_service,
    safe_answer_callback,
    safe_edit_or_send,
)
from app.bot.keyboards.admin_kb import (
    build_admin_back_kb,
    build_admin_cancel_kb,
    build_cms_languages_kb,
    build_cms_menu_kb,
    build_cms_translations_list_kb,
    build_dual_color_picker_kb,
    build_settings_kb,
    build_ui_button_detail_kb,
    build_ui_buttons_list_kb,
)
from app.bot.keyboards.common_kb import AdmCB
from app.bot.middlewares.force_join_mw import validate_force_join_channels_raw
from app.bot.states import AdminStates
from app.core.constants import COLOR_PALETTE, SUPPORTED_LANGUAGES, SettingKey, is_valid_hex_color
from app.core.exceptions import ShopBotError, ValidationError
from app.core.logging import get_logger
from app.core.security import escape_html

logger = get_logger(__name__)
router = Router(name="admin_cms")

_CMS_PAGE_SIZE = 10


async def _render_ui_button_card(target: CallbackQuery | Message, btn_id: int, data: dict[str, Any]) -> None:
    try:
        cms_service = get_service(data, "cms_service")
        buttons = await cms_service.list_buttons()
        btn = next((b for b in buttons if b.id == btn_id), None)
        if btn is None:
            await safe_edit_or_send(target, "❌ UI Button not found.", reply_markup=build_admin_back_kb(sec="btn", act="list"))
            return
    except ShopBotError as exc:
        await safe_edit_or_send(target, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="cms", act="menu"))
        return

    labels_preview = "\n".join(
        f"• <code>{escape_html(code)}</code>: {escape_html(val)}"
        for code, val in sorted(btn.labels_json.items())
    )
    text = (
        f"🎛 <b>UI Button: <code>{escape_html(btn.button_key)}</code></b>\n\n"
        f"📍 Scope: <code>{btn.menu_scope.value}</code> (Row {btn.row_index}, Col {btn.col_index})\n"
        f"🎨 Dual Color: {btn.color_emoji} <code>{btn.color_hex}</code>\n"
        f"🚦 Active: <b>{'Yes' if btn.is_active else 'No'}</b>\n\n"
        f"🌐 <b>Labels:</b>\n{labels_preview or '-'}"
    )
    await safe_edit_or_send(target, text[:3900], reply_markup=build_ui_button_detail_kb(btn))


async def _render_settings_view(target: CallbackQuery | Message, data: dict[str, Any]) -> None:
    try:
        cms_service = get_service(data, "cms_service")
        maint = await cms_service.get_setting(SettingKey.MAINTENANCE_MODE.value, "false")
        pin_lim = await cms_service.get_setting(SettingKey.PINNED_LIMIT.value, "4")
        low_stk = await cms_service.get_setting(SettingKey.LOW_STOCK_DEFAULT_THRESHOLD.value, "5")
        guide_url = await cms_service.get_setting(SettingKey.GUIDE_URL.value, "")
        fjoin_en = await cms_service.get_setting(SettingKey.FORCE_JOIN_ENABLED.value, "false")
        fjoin_ch = await cms_service.get_setting(SettingKey.FORCE_JOIN_CHANNELS.value, "")
    except ShopBotError as exc:
        await safe_edit_or_send(target, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="cms", act="menu"))
        return

    maint_on = str(maint).strip().lower() in {"true", "1", "yes", "on"}
    fjoin_on = str(fjoin_en).strip().lower() in {"true", "1", "yes", "on"}
    guide_display = escape_html(guide_url) if guide_url else "Not set"
    fjoin_ch_display = escape_html(fjoin_ch) if fjoin_ch else "Not set"
    text = (
        f"⚙️ <b>Application Settings</b>\n\n"
        f"🛠 Maintenance Mode: <b>{'ON' if maint_on else 'OFF'}</b>\n"
        f"📢 Force Channel Join: <b>{'ON' if fjoin_on else 'OFF'}</b>\n"
        f"📣 Required Channel(s): <code>{fjoin_ch_display}</code>\n"
        f"📌 Pinned Products Limit: <b>{escape_html(pin_lim or '4')}</b>\n"
        f"⚠️ Default Low-Stock Threshold: <b>{escape_html(low_stk or '5')}</b>\n"
        f"📖 Guide URL: <code>{guide_display}</code>"
    )
    await safe_edit_or_send(
        target,
        text,
        reply_markup=build_settings_kb(maintenance_on=maint_on, force_join_on=fjoin_on),
    )


@router.callback_query(AdmCB.filter((F.sec == "cms") & (F.act == "menu")))
async def cb_cms_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_or_send(
        callback,
        "🎨 <b>Dynamic CMS, UI Buttons &amp; App Settings</b>\n\nSelect an option:",
        reply_markup=build_cms_menu_kb(),
    )


@router.callback_query(AdmCB.filter((F.sec == "cms") & (F.act == "langs")))
async def cb_cms_languages(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_or_send(
        callback,
        "🌐 <b>Select Language to Edit UI Translations</b>:",
        reply_markup=build_cms_languages_kb(),
    )


@router.callback_query(AdmCB.filter((F.sec == "cms") & (F.act.startswith("lang_"))))
async def cb_cms_lang_keys(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    lang_code = callback_data.act.removeprefix("lang_")
    if lang_code not in SUPPORTED_LANGUAGES:
        await safe_answer_callback(callback, "Unsupported language", show_alert=True)
        return
    page = max(0, callback_data.page)
    try:
        cms_service = get_service(data, "cms_service")
        rows = await cms_service.list_translations(
            lang_code=lang_code,
            limit=_CMS_PAGE_SIZE + 1,
            offset=page * _CMS_PAGE_SIZE,
        )
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="cms", act="langs"))
        return

    has_next = len(rows) > _CMS_PAGE_SIZE
    visible = rows[:_CMS_PAGE_SIZE]
    await state.update_data(cms_page_keys=[r.key for r in visible], cms_page_lang=lang_code)
    info = SUPPORTED_LANGUAGES[lang_code]
    await safe_edit_or_send(
        callback,
        f"{info.flag} <b>{escape_html(info.native_name)} ({lang_code}) Translations</b> — Page {page + 1}:",
        reply_markup=build_cms_translations_list_kb(visible, lang_code=lang_code, page=page, has_next=has_next),
    )


@router.callback_query(AdmCB.filter((F.sec == "cms") & (F.act.startswith("ed_"))))
async def cb_cms_edit_key_prompt(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    lang_code = callback_data.act.removeprefix("ed_")
    page = max(0, callback_data.page)
    idx = callback_data.id
    fsm_data = await state.get_data()
    keys = fsm_data.get("cms_page_keys") if fsm_data.get("cms_page_lang") == lang_code else None

    key_name: str | None = None
    if isinstance(keys, list) and 0 <= idx < len(keys):
        key_name = str(keys[idx])
    else:
        try:
            cms_service = get_service(data, "cms_service")
            rows = await cms_service.list_translations(
                lang_code=lang_code,
                limit=_CMS_PAGE_SIZE,
                offset=page * _CMS_PAGE_SIZE,
            )
            if 0 <= idx < len(rows):
                key_name = rows[idx].key
        except ShopBotError:
            key_name = None

    if not key_name:
        await safe_answer_callback(callback, "Translation key not found on page.", show_alert=True)
        return

    try:
        cms_service = get_service(data, "cms_service")
        current_val = await cms_service.get_translation(key_name, lang_code)
    except ShopBotError:
        current_val = ""

    await state.set_state(AdminStates.waiting_cms_text)
    await state.update_data(cms_mode="key", cms_key=key_name, cms_lang=lang_code)
    await safe_edit_or_send(
        callback,
        f"✏️ <b>Edit Translation</b>\n\n"
        f"🔑 Key: <code>{escape_html(key_name)}</code> | 🌐 Lang: <code>{escape_html(lang_code)}</code>\n"
        f"📄 Current Value:\n<pre>{escape_html(current_val)}</pre>\n\n"
        f"Send the new text value:",
        reply_markup=build_admin_cancel_kb(sec="cms", act=f"lang_{lang_code}", page=page),
    )


@router.callback_query(AdmCB.filter((F.sec == "cms") & (F.act == "upsert")))
async def cb_cms_upsert_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_cms_text)
    await state.update_data(cms_mode="upsert")
    await safe_edit_or_send(
        callback,
        "✏️ <b>Upsert Any Translation Key</b>\n\n"
        "Send: <code>key | lang_code | new_text</code>\n"
        "Example: <code>welcome.banner | en | Welcome to our Shop!</code>",
        reply_markup=build_admin_cancel_kb(sec="cms", act="menu"),
    )


@router.message(AdminStates.waiting_cms_text)
async def msg_cms_text_save(message: Message, state: FSMContext, **data: Any) -> None:
    fsm_data = await state.get_data()
    mode = str(fsm_data.get("cms_mode", "key"))
    raw = (message.text or "").strip()

    try:
        cms_service = get_service(data, "cms_service")
        if mode == "btn_hex":
            btn_id = int(fsm_data.get("cms_btn_id", 0))
            btn_key = str(fsm_data.get("cms_btn_key", ""))
            parts = [p.strip() for p in raw.split("|")]
            if not is_valid_hex_color(parts[0]):
                await safe_edit_or_send(
                    message,
                    "❌ Invalid Hex color. Expected <code>#RRGGBB</code>.",
                    reply_markup=build_admin_cancel_kb(sec="btn", act="view", target_id=btn_id),
                )
                return
            emoji_val = parts[1] if len(parts) > 1 and parts[1] else None
            await cms_service.update_button(btn_key, color_hex=parts[0].lower(), color_emoji=emoji_val)
            await state.clear()
            await _render_ui_button_card(message, btn_id, data)
            return

        if mode == "btn_label":
            btn_id = int(fsm_data.get("cms_btn_id", 0))
            btn_key = str(fsm_data.get("cms_btn_key", ""))
            parts = [p.strip() for p in raw.split("|", 1)]
            if len(parts) != 2 or parts[0].lower() not in SUPPORTED_LANGUAGES or not parts[1]:
                await safe_edit_or_send(
                    message,
                    "❌ Format: <code>lang_code | new_label</code> (e.g. <code>fa | 🛒 فروشگاه</code>)",
                    reply_markup=build_admin_cancel_kb(sec="btn", act="view", target_id=btn_id),
                )
                return
            buttons = await cms_service.list_buttons()
            existing = next((b for b in buttons if b.button_key == btn_key), None)
            merged = dict(existing.labels_json) if existing else {}
            merged[parts[0].lower()] = parts[1]
            await cms_service.update_button(btn_key, labels_json=merged)
            await state.clear()
            await _render_ui_button_card(message, btn_id, data)
            return

        if mode == "guide_url":
            new_url = "" if raw.lower() in {"clear", "none", "-"} else raw
            if new_url and (
                len(new_url) > 500
                or any(ch.isspace() for ch in new_url)
                or not new_url.lower().startswith(("https://", "http://"))
            ):
                await safe_edit_or_send(
                    message,
                    "❌ Invalid URL. Send an <code>https://</code> or <code>http://</code> link, or <code>clear</code>.",
                    reply_markup=build_admin_cancel_kb(sec="set", act="list"),
                )
                return
            await cms_service.set_setting(SettingKey.GUIDE_URL.value, new_url)
            await state.clear()
            await _render_settings_view(message, data)
            return

        if mode == "force_join_channels":
            if raw.lower() in {"clear", "none", "-"}:
                cleaned = ""
            else:
                try:
                    cleaned = validate_force_join_channels_raw(raw)
                except ValidationError as exc:
                    await safe_edit_or_send(
                        message,
                        f"❌ {escape_html(exc.message)}",
                        reply_markup=build_admin_cancel_kb(sec="set", act="list"),
                    )
                    return
            await cms_service.set_setting(SettingKey.FORCE_JOIN_CHANNELS.value, cleaned)
            await state.clear()
            await _render_settings_view(message, data)
            return

        if mode == "upsert":
            parts = [p.strip() for p in raw.split("|", 2)]
            if len(parts) != 3 or not parts[0] or not parts[2]:
                raise ValueError("Expected format: key | lang_code | new_text")
            key_name, lang_code, new_text = parts[0], parts[1].lower(), parts[2]
        else:
            key_name = str(fsm_data.get("cms_key", ""))
            lang_code = str(fsm_data.get("cms_lang", "en"))
            new_text = raw
            if not new_text:
                raise ValueError("Translation text cannot be empty")

        updated = await cms_service.upsert_translation(key_name, lang_code, new_text)
        await state.clear()
        await safe_edit_or_send(
            message,
            f"✅ <b>Translation Saved &amp; Cache Invalidated</b>\n\n"
            f"🔑 Key: <code>{escape_html(updated.key)}</code>\n"
            f"🌐 Lang: <code>{escape_html(updated.lang_code)}</code>\n"
            f"📝 Value:\n<pre>{escape_html(updated.text_value)}</pre>",
            reply_markup=build_admin_back_kb(sec="cms", act=f"lang_{updated.lang_code}", label="🔙 Back to Language Keys"),
        )
    except (ShopBotError, ValueError) as exc:
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(str(exc))}",
            reply_markup=build_admin_cancel_kb(sec="cms", act="menu"),
        )


@router.callback_query(AdmCB.filter((F.sec == "btn") & (F.act == "list")))
async def cb_buttons_list(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    try:
        cms_service = get_service(data, "cms_service")
        buttons = await cms_service.list_buttons()
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="cms", act="menu"))
        return
    await safe_edit_or_send(
        callback,
        "🎛 <b>UI Buttons &amp; Colors</b> (click to edit):",
        reply_markup=build_ui_buttons_list_kb(buttons),
    )


@router.callback_query(AdmCB.filter((F.sec == "btn") & (F.act == "view")))
async def cb_button_view(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await _render_ui_button_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "btn") & (F.act == "toggle")))
async def cb_button_toggle(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        cms_service = get_service(data, "cms_service")
        buttons = await cms_service.list_buttons()
        btn = next((b for b in buttons if b.id == callback_data.id), None)
        if btn is None:
            await safe_answer_callback(callback, "Button not found", show_alert=True)
            return
        await cms_service.update_button(btn.button_key, is_active=not btn.is_active)
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_ui_button_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "btn") & (F.act == "color")))
async def cb_button_color_picker(callback: CallbackQuery, callback_data: AdmCB) -> None:
    await safe_edit_or_send(
        callback,
        f"🎨 <b>UI Button #{callback_data.id} Dual-Layer Color Picker</b>:",
        reply_markup=build_dual_color_picker_kb("btn", callback_data.id),
    )


@router.callback_query(AdmCB.filter((F.sec == "btn") & (F.act.startswith("col_"))))
async def cb_button_color_preset(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    key = callback_data.act.removeprefix("col_")
    preset = COLOR_PALETTE.get(key)
    if preset is None:
        await safe_answer_callback(callback, "Unknown preset", show_alert=True)
        return
    try:
        cms_service = get_service(data, "cms_service")
        buttons = await cms_service.list_buttons()
        btn = next((b for b in buttons if b.id == callback_data.id), None)
        if btn is None:
            await safe_answer_callback(callback, "Button not found", show_alert=True)
            return
        await cms_service.update_button(btn.button_key, color_emoji=preset.emoji, color_hex=preset.hex_code)
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_ui_button_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "btn") & (F.act.in_({"hex", "label"}))))
async def cb_button_fsm_prompt(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    try:
        cms_service = get_service(data, "cms_service")
        buttons = await cms_service.list_buttons()
        btn = next((b for b in buttons if b.id == callback_data.id), None)
        if btn is None:
            await safe_answer_callback(callback, "Button not found", show_alert=True)
            return
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return

    await state.set_state(AdminStates.waiting_cms_text)
    if callback_data.act == "hex":
        await state.update_data(cms_mode="btn_hex", cms_btn_id=btn.id, cms_btn_key=btn.button_key)
        prompt = "🎨 Send custom Hex color (e.g. <code>#3b82f6</code>) or <code>#RRGGBB | 🔵</code>:"
    else:
        await state.update_data(cms_mode="btn_label", cms_btn_id=btn.id, cms_btn_key=btn.button_key)
        prompt = "🌐 Send: <code>lang_code | New Button Label</code>\nExample: <code>fa | 🛒 فروشگاه دیجیتال</code>"
    await safe_edit_or_send(callback, prompt, reply_markup=build_admin_cancel_kb(sec="btn", act="view", target_id=btn.id))


@router.callback_query(AdmCB.filter((F.sec == "set") & (F.act == "list")))
async def cb_settings_view(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await _render_settings_view(callback, data)


@router.callback_query(AdmCB.filter((F.sec == "set") & (F.act == "tog_maint")))
async def cb_settings_toggle_maint(callback: CallbackQuery, **data: Any) -> None:
    try:
        cms_service = get_service(data, "cms_service")
        curr = await cms_service.get_setting(SettingKey.MAINTENANCE_MODE.value, "false")
        is_on = str(curr).strip().lower() in {"true", "1", "yes", "on"}
        await cms_service.set_setting(SettingKey.MAINTENANCE_MODE.value, "false" if is_on else "true")
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_settings_view(callback, data)


@router.callback_query(AdmCB.filter((F.sec == "set") & (F.act == "tog_fjoin")))
async def cb_settings_toggle_force_join(callback: CallbackQuery, **data: Any) -> None:
    try:
        cms_service = get_service(data, "cms_service")
        curr = await cms_service.get_setting(SettingKey.FORCE_JOIN_ENABLED.value, "false")
        is_on = str(curr).strip().lower() in {"true", "1", "yes", "on"}
        await cms_service.set_setting(SettingKey.FORCE_JOIN_ENABLED.value, "false" if is_on else "true")
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_settings_view(callback, data)


@router.callback_query(AdmCB.filter((F.sec == "set") & (F.act == "fjoin_ch")))
async def cb_settings_force_join_channels_prompt(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    try:
        cms_service = get_service(data, "cms_service")
        curr = await cms_service.get_setting(SettingKey.FORCE_JOIN_CHANNELS.value, "")
    except ShopBotError:
        curr = ""
    await state.set_state(AdminStates.waiting_cms_text)
    await state.update_data(cms_mode="force_join_channels")
    await safe_edit_or_send(
        callback,
        f"📣 <b>Set Force-Join Required Channel(s)</b>\n\n"
        f"Current:\n<code>{escape_html(curr or 'Not set')}</code>\n\n"
        f"Send channel(s) one per line (or comma-separated):\n"
        f"• Public: <code>@YourChannel</code> or <code>https://t.me/YourChannel</code>\n"
        f"• Private: <code>-1001234567890|https://t.me/+InviteLink</code>\n"
        f"• Optional label: append <code>|Channel Title</code>\n\n"
        f"⚠️ Make sure the bot is an <b>Administrator</b> in every required channel.\n"
        f"Send <code>clear</code> to remove all channels.",
        reply_markup=build_admin_cancel_kb(sec="set", act="list"),
    )


@router.callback_query(AdmCB.filter((F.sec == "set") & (F.act.in_({"pin_lim", "low_stk"}))))
async def cb_settings_numeric(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    key = (
        SettingKey.PINNED_LIMIT.value
        if callback_data.act == "pin_lim"
        else SettingKey.LOW_STOCK_DEFAULT_THRESHOLD.value
    )
    try:
        cms_service = get_service(data, "cms_service")
        await cms_service.set_setting(key, str(max(1, callback_data.id)))
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_settings_view(callback, data)


@router.callback_query(AdmCB.filter((F.sec == "set") & (F.act == "guide_url")))
async def cb_settings_guide_url_prompt(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    try:
        cms_service = get_service(data, "cms_service")
        curr = await cms_service.get_setting(SettingKey.GUIDE_URL.value, "")
    except ShopBotError:
        curr = ""
    await state.set_state(AdminStates.waiting_cms_text)
    await state.update_data(cms_mode="guide_url")
    await safe_edit_or_send(
        callback,
        f"📖 <b>Set Guide URL for Manual Product Prompts</b>\n\n"
        f"Current: <code>{escape_html(curr or 'Not set')}</code>\n\n"
        f"Send a valid <code>https://...</code> URL (or <code>clear</code> to remove):",
        reply_markup=build_admin_cancel_kb(sec="set", act="list"),
    )
