"""12-language switcher handlers with in-place menu editing."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import safe_edit_or_send
from app.bot.handlers.start import render_main_menu
from app.bot.keyboards.common_kb import LangCB, MenuCB, build_language_kb
from app.bot.middlewares.force_join_mw import check_user_force_join, send_force_join_prompt
from app.core.constants import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES
from app.locales.i18n import TranslationService
from app.services.dtos import UserDTO
from app.services.interfaces import UserServiceProtocol

router = Router(name="customer_language")


async def _show_language_picker(
    target: Message | CallbackQuery,
    *,
    i18n: TranslationService,
    lang: str,
) -> None:
    active_lang = i18n.normalize_lang(lang)
    prompt = await i18n.get_text("lang.select_prompt", active_lang)
    back_text = await i18n.get_text("btn.back", active_lang)
    kb = build_language_kb(
        current_lang=active_lang,
        include_back=True,
        back_text=back_text,
        back_callback=MenuCB(action="main"),
    )
    await safe_edit_or_send(target, prompt, reply_markup=kb)


@router.message(Command("language", "lang"))
async def cmd_language(
    message: Message,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    state: FSMContext | None = None,
) -> None:
    """Open the 12-language 2-column selector via command."""
    if state is not None:
        await state.clear()
    await _show_language_picker(message, i18n=i18n, lang=lang)


@router.callback_query(MenuCB.filter(F.action == "lang"))
async def cb_open_language(
    callback: CallbackQuery,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    state: FSMContext | None = None,
) -> None:
    """Open the 12-language 2-column selector in-place."""
    if state is not None:
        await state.clear()
    await _show_language_picker(callback, i18n=i18n, lang=lang)


@router.callback_query(LangCB.filter())
async def cb_select_language(
    callback: CallbackQuery,
    callback_data: LangCB,
    user: UserDTO,
    user_service: UserServiceProtocol,
    i18n: TranslationService,
    state: FSMContext | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    shop_service: Any | None = None,
    cms_service: Any | None = None,
    is_admin: bool = False,
    redis_client: Any | None = None,
) -> None:
    """Persist selected language and refresh Main Menu in-place in the new locale."""
    if state is not None:
        await state.clear()

    code = callback_data.code.strip().lower()
    if code not in SUPPORTED_LANGUAGES:
        await callback.answer("Unsupported language", show_alert=True)
        return

    updated_user = await user_service.set_language(user.telegram_id, code)
    new_lang = updated_user.language_code
    changed_text = await i18n.get_text("lang.changed", new_lang)

    if not is_admin and callback.bot is not None:
        missing = await check_user_force_join(
            bot=callback.bot,
            user_telegram_id=updated_user.telegram_id,
            cms_service=cms_service,
            session_factory=session_factory,
            redis_client=redis_client,
        )
        if missing:
            await send_force_join_prompt(
                callback,
                missing_channels=missing,
                i18n=i18n,
                lang=new_lang,
                prefix_text=changed_text,
                toast_text=changed_text,
            )
            return

    await render_main_menu(
        callback,
        user=updated_user,
        i18n=i18n,
        lang=new_lang,
        session_factory=session_factory,
        shop_service=shop_service,
        cms_service=cms_service,
        prefix_text=changed_text,
        toast_text=changed_text,
    )
