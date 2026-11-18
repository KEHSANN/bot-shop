"""Shared helpers, service resolution, and HTML-safe UI helpers for /admin handlers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.config import get_settings
from app.core.constants import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES
from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger
from app.core.security import escape_html
from app.database.session import get_session_factory
from app.locales.seed_translations import TRANSLATIONS
from app.services.broadcast_service import BroadcastService

logger = get_logger(__name__)


def resolve_admin_actor_id(event: CallbackQuery | Message) -> int:
    """Return the Telegram user ID of the acting administrator."""
    return int(event.from_user.id) if event.from_user else 0


async def safe_edit_or_send(
    target: CallbackQuery | Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Edit an inline callback message in-place or send a new message cleanly."""
    if isinstance(target, CallbackQuery):
        msg = target.message
        if msg is not None and isinstance(msg, Message):
            try:
                edited = await msg.edit_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
                await safe_answer_callback(target)
                return edited if isinstance(edited, Message) else msg
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    await safe_answer_callback(target)
                    return msg
                sent = await msg.answer(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
                await safe_answer_callback(target)
                return sent
        await safe_answer_callback(target)
        return None

    return await target.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


async def safe_answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    """Acknowledge a CallbackQuery without failing if the query has expired."""
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramAPIError as exc:
        logger.debug("Callback answer ignored: %s", exc)


def get_service(data: dict[str, Any], name: str, *, bot: Bot | None = None) -> Any:
    """Resolve an injected service from handler data or instantiate Wave 2/3 implementation lazily."""
    existing = data.get(name)
    if existing is not None:
        return existing

    session_factory = data.get("session_factory") or get_session_factory()
    redis_client = data.get("redis") or data.get("redis_client")

    if name == "broadcast_service":
        return BroadcastService(session_factory=session_factory, bot=bot)

    if name == "user_service":
        try:
            from app.services.user_service import UserService

            return UserService(session_factory)
        except ImportError as exc:
            raise ConfigurationError("UserService is not available") from exc

    if name == "support_service":
        try:
            from app.services.support_service import SupportService

            return SupportService(session_factory)
        except ImportError as exc:
            raise ConfigurationError("SupportService is not available") from exc

    if name == "shop_service":
        try:
            from app.services.shop_service import ShopService

            return ShopService(session_factory)
        except ImportError as exc:
            raise ConfigurationError("ShopService is not available") from exc

    if name == "cms_service":
        try:
            from app.services.cms_service import CMSService

            return CMSService(session_factory, redis_client)
        except ImportError as exc:
            raise ConfigurationError("CMSService is not available") from exc

    if name == "analytics_service":
        try:
            from app.services.analytics_service import AnalyticsService

            return AnalyticsService(session_factory)
        except ImportError as exc:
            raise ConfigurationError("AnalyticsService is not available") from exc

    if name == "payment_service":
        try:
            from app.services.payment_service import PaymentService

            return PaymentService(session_factory)
        except ImportError as exc:
            raise ConfigurationError("PaymentService is not available") from exc

    raise ConfigurationError(f"Unknown service requested: {name}")


def get_optional_service(data: dict[str, Any], name: str, *, bot: Bot | None = None) -> Any | None:
    """Resolve a service if available, returning None when an optional module is not yet merged."""
    try:
        return get_service(data, name, bot=bot)
    except ConfigurationError:
        return None


async def render_i18n_template(
    data: dict[str, Any],
    key: str,
    lang_code: str,
    **kwargs: Any,
) -> str:
    """Render a localized notification string using TranslationService or seed fallback."""
    lang = lang_code if lang_code in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    i18n = data.get("translation_service") or data.get("i18n")
    if i18n is not None and hasattr(i18n, "get_text"):
        return await i18n.get_text(key, lang, **kwargs)

    cms = get_optional_service(data, "cms_service")
    template: str | None = None
    if cms is not None and hasattr(cms, "get_translation"):
        try:
            template = await cms.get_translation(key, lang)
        except Exception as exc:
            logger.debug("CMS translation lookup fallback for %s/%s: %s", key, lang, exc)

    if not template or template == key:
        entry = TRANSLATIONS.get(key, {})
        template = entry.get(lang) or entry.get(DEFAULT_LANGUAGE) or key

    try:
        return template.format(**kwargs) if kwargs else template
    except Exception:
        return template


async def notify_user_dm(bot: Bot | None, telegram_id: int, html_text: str) -> bool:
    """Send an HTML notification DM to a customer without failing the admin transaction."""
    if bot is None or telegram_id <= 0:
        return False
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=html_text,
            parse_mode=ParseMode.HTML,
        )
        return True
    except TelegramAPIError as exc:
        logger.warning("Failed to send notification DM to telegram_id=%s: %s", telegram_id, exc)
        return False


def parse_positive_decimal(raw: str) -> Decimal | None:
    """Parse a positive finite Decimal string, returning None if invalid."""
    cleaned = raw.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        val = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not val.is_finite() or val <= Decimal("0"):
        return None
    return val


def fmt_dt(dt: datetime | None) -> str:
    """Format a datetime concisely in UTC for admin views."""
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def is_admin_telegram_id(telegram_id: int, user_obj: Any = None) -> bool:
    """Check whether telegram_id is listed in ADMIN_TELEGRAM_IDS or flagged is_admin in DB."""
    if telegram_id in get_settings().admin_ids:
        return True
    if user_obj is not None and bool(getattr(user_obj, "is_admin", False)):
        return True
    return False


def safe_str(val: Any, default: str = "-") -> str:
    """Return HTML-escaped string or default placeholder."""
    if val is None or str(val) == "":
        return default
    return escape_html(str(val))
