"""/start and /cancel commands plus strictly scoped 5-button Main Menu handler."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import format_irt, format_usd, safe_edit_or_send
from app.bot.keyboards.common_kb import MenuCB, build_language_kb, build_main_menu_kb
from app.core.constants import DEFAULT_LANGUAGE, SettingKey
from app.core.logging import get_logger
from app.core.security import sanitize_username
from app.database.models import AppSetting, Product
from app.locales.i18n import TranslationService
from app.services.dtos import ProductDTO, UserDTO

logger = get_logger(__name__)
router = Router(name="customer_start")


async def _load_pinned_products_and_limit(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    shop_service: Any | None = None,
    cms_service: Any | None = None,
) -> tuple[list[ProductDTO], int]:
    pinned_limit = 4
    if cms_service is not None:
        try:
            raw_limit = await cms_service.get_setting(SettingKey.PINNED_LIMIT.value, default="4")
            if raw_limit is not None and str(raw_limit).strip().isdigit():
                pinned_limit = int(str(raw_limit).strip())
        except Exception as exc:
            logger.warning("Failed reading pinned_limit from cms_service: %s", exc)
    elif session_factory is not None:
        try:
            async with session_factory() as session:
                raw_limit = (
                    await session.execute(
                        select(AppSetting.value).where(AppSetting.key == SettingKey.PINNED_LIMIT.value)
                    )
                ).scalar_one_or_none()
                if raw_limit is not None and str(raw_limit).strip().isdigit():
                    pinned_limit = int(str(raw_limit).strip())
        except Exception as exc:
            logger.warning("Failed reading pinned_limit from DB: %s", exc)

    if pinned_limit <= 0:
        return [], 0

    if shop_service is not None:
        try:
            pinned = await shop_service.list_products(active_only=True, pinned_only=True)
            return list(pinned), pinned_limit
        except Exception as exc:
            logger.warning("Failed reading pinned products from shop_service: %s", exc)

    if session_factory is not None:
        try:
            async with session_factory() as session:
                rows = (
                    await session.execute(
                        select(Product)
                        .where(Product.is_pinned.is_(True), Product.is_active.is_(True))
                        .order_by(Product.sort_order.asc(), Product.id.asc())
                        .limit(pinned_limit)
                    )
                ).scalars().all()
                return [ProductDTO.model_validate(r) for r in rows], pinned_limit
        except Exception as exc:
            logger.warning("Failed reading pinned products from DB: %s", exc)

    return [], pinned_limit


async def render_main_menu(
    target: Message | CallbackQuery,
    *,
    user: UserDTO,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    shop_service: Any | None = None,
    cms_service: Any | None = None,
    prefix_text: str | None = None,
    toast_text: str | None = None,
) -> Message | None:
    """Render the localized Welcome Banner and strict 5-button (+ pinned products) Main Menu."""
    active_lang = i18n.normalize_lang(lang or user.language_code)
    buttons = await i18n.get_main_menu_buttons(active_lang)
    lang_map = await i18n.get_language_map(active_lang)
    pinned_products, pinned_limit = await _load_pinned_products_and_limit(
        session_factory=session_factory,
        shop_service=shop_service,
        cms_service=cms_service,
    )

    balance_irt_line = ""
    if active_lang == "fa":
        balance_irt_line = await i18n.get_text(
            "welcome.balance_irt_line",
            active_lang,
            balance_irt=format_irt(user.balance_irt),
        )

    banner_text = await i18n.get_text(
        "welcome.banner",
        active_lang,
        user_id=user.telegram_id,
        username=sanitize_username(user.username),
        balance_usd=format_usd(user.balance_usd),
        balance_irt_line=balance_irt_line,
    )
    if prefix_text:
        banner_text = f"{prefix_text}\n\n{banner_text}"

    kb = build_main_menu_kb(
        lang_code=active_lang,
        buttons=buttons,
        pinned_products=pinned_products,
        labels=lang_map,
        pinned_limit=pinned_limit,
    )
    return await safe_edit_or_send(
        target,
        banner_text,
        reply_markup=kb,
        toast_text=toast_text,
    )


@router.message(CommandStart())
@router.message(Command("cancel"))
async def cmd_start(
    message: Message,
    user: UserDTO,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    state: FSMContext | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    shop_service: Any | None = None,
    cms_service: Any | None = None,
) -> None:
    """Handle /start or /cancel by clearing active FSM state and displaying language picker or Main Menu."""
    if state is not None:
        await state.clear()
    raw_cmd = (message.text or "").strip().split(maxsplit=1)[0].lower()
    if raw_cmd.startswith("/start") and not user.language_selected:
        active_lang = i18n.normalize_lang(lang or user.language_code)
        prompt = await i18n.get_text("lang.select_prompt", active_lang)
        if active_lang != DEFAULT_LANGUAGE:
            en_prompt = await i18n.get_text("lang.select_prompt", DEFAULT_LANGUAGE)
            if en_prompt and en_prompt != prompt:
                prompt = f"{prompt}\n{en_prompt}"
        await safe_edit_or_send(
            message,
            prompt,
            reply_markup=build_language_kb(current_lang=None, include_back=False),
        )
        return
    await render_main_menu(
        message,
        user=user,
        i18n=i18n,
        lang=lang,
        session_factory=session_factory,
        shop_service=shop_service,
        cms_service=cms_service,
    )


@router.callback_query(MenuCB.filter(F.action == "main"))
async def cb_main_menu(
    callback: CallbackQuery,
    user: UserDTO,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    state: FSMContext | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    shop_service: Any | None = None,
    cms_service: Any | None = None,
) -> None:
    """Handle return to Main Menu via in-place message edit."""
    if state is not None:
        await state.clear()
    await render_main_menu(
        callback,
        user=user,
        i18n=i18n,
        lang=lang,
        session_factory=session_factory,
        shop_service=shop_service,
        cms_service=cms_service,
    )


@router.callback_query(MenuCB.filter(F.action == "check_join"))
async def cb_check_join(
    callback: CallbackQuery,
    user: UserDTO,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    state: FSMContext | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    shop_service: Any | None = None,
    cms_service: Any | None = None,
) -> None:
    """Enter Main Menu (or language picker) after ForceJoinMiddleware verifies channel membership."""
    if state is not None:
        await state.clear()
    active_lang = i18n.normalize_lang(lang or user.language_code)
    verified_toast = await i18n.get_text("force_join.verified_toast", active_lang)
    if not user.language_selected:
        prompt = await i18n.get_text("lang.select_prompt", active_lang)
        if active_lang != DEFAULT_LANGUAGE:
            en_prompt = await i18n.get_text("lang.select_prompt", DEFAULT_LANGUAGE)
            if en_prompt and en_prompt != prompt:
                prompt = f"{prompt}\n{en_prompt}"
        await safe_edit_or_send(
            callback,
            prompt,
            reply_markup=build_language_kb(current_lang=None, include_back=False),
            toast_text=verified_toast,
        )
        return
    await render_main_menu(
        callback,
        user=user,
        i18n=i18n,
        lang=active_lang,
        session_factory=session_factory,
        shop_service=shop_service,
        cms_service=cms_service,
        toast_text=verified_toast,
    )


@router.callback_query(MenuCB.filter(F.action == "noop"))
async def cb_noop(callback: CallbackQuery) -> None:
    """Acknowledge informational / non-interactive inline buttons."""
    await callback.answer()
