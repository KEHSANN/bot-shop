"""Forced Telegram channel membership verification middleware and channel config helpers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    Update,
)
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import safe_edit_or_send
from app.bot.keyboards.common_kb import MenuCB
from app.core.constants import DEFAULT_LANGUAGE, SettingKey
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.database.models import AppSetting
from app.locales.i18n import TranslationService
from app.locales.seed_translations import TRANSLATIONS
from app.services.dtos import UserDTO

logger = get_logger(__name__)

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_NUMERIC_CHAT_ID_RE = re.compile(r"^-\d{5,20}$")
MAX_FORCE_JOIN_CHANNELS = 10
FORCE_JOIN_CACHE_TTL = 60

_BOOL_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})


@dataclass(frozen=True, slots=True)
class RequiredChannel:
    """Validated required Telegram channel entry for force-join verification."""

    chat_id: int | str
    url: str
    title: str | None = None


def _is_safe_http_url(url: str | None) -> bool:
    if not url:
        return False
    cleaned = url.strip()
    if not cleaned or len(cleaned) > 500 or any(ch.isspace() for ch in cleaned):
        return False
    parsed = urlparse(cleaned)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_force_join_channels(
    raw: str | None,
    *,
    strict: bool = False,
) -> list[RequiredChannel]:
    """Parse configured channels from multiline or comma-separated setting text."""
    if not raw or not raw.strip():
        return []

    candidates: list[str] = []
    for line in raw.replace("\r", "\n").split("\n"):
        stripped_line = line.strip()
        if not stripped_line:
            continue
        if "|" in stripped_line:
            candidates.append(stripped_line)
        else:
            candidates.extend(stripped_line.split(","))

    channels: list[RequiredChannel] = []
    seen_ids: set[str] = set()

    for token in candidates:
        entry = token.strip()
        if not entry:
            continue

        parts = [p.strip() for p in entry.split("|")]
        head = parts[0]
        if not head:
            if strict:
                raise ValidationError(f"Invalid channel entry: '{entry}'")
            continue

        chat_id: int | str
        url: str
        title: str | None = None

        if _NUMERIC_CHAT_ID_RE.fullmatch(head):
            chat_id = int(head)
            if len(parts) < 2 or not _is_safe_http_url(parts[1]):
                if strict:
                    raise ValidationError(
                        "Private numeric channel ID requires an invite URL: -1001234567890|https://t.me/+InviteLink"
                    )
                continue
            url = parts[1]
            title = parts[2] if len(parts) >= 3 and parts[2] else None
        elif head.lower().startswith(
            ("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/", "http://telegram.me/", "telegram.me/")
        ):
            normalized_url = head if head.lower().startswith(("https://", "http://")) else f"https://{head}"
            parsed_url = urlparse(normalized_url)
            path = parsed_url.path.strip("/")
            first_segment = path.split("/", 1)[0] if path else ""
            if not first_segment or first_segment.startswith("+") or first_segment.lower() in {"joinchat", "c"}:
                if strict:
                    raise ValidationError(
                        "Private channel invite links require the channel ID prefix: -1001234567890|https://t.me/+invite"
                    )
                continue
            username = first_segment.lstrip("@")
            if not _USERNAME_RE.fullmatch(username):
                if strict:
                    raise ValidationError(f"Invalid Telegram channel link: '{head}'")
                continue
            chat_id = f"@{username}"
            url = f"https://t.me/{username}"
            if len(parts) >= 2 and parts[1]:
                if _is_safe_http_url(parts[1]):
                    url = parts[1]
                    title = parts[2] if len(parts) >= 3 and parts[2] else None
                else:
                    title = parts[1]
        else:
            username = head.lstrip("@")
            if not _USERNAME_RE.fullmatch(username):
                if strict:
                    raise ValidationError(
                        f"Invalid channel identifier '{head}'. Expected @username, https://t.me/username, or -100chatid|https://t.me/+invite"
                    )
                continue
            chat_id = f"@{username}"
            url = f"https://t.me/{username}"
            if len(parts) >= 2 and parts[1]:
                if _is_safe_http_url(parts[1]):
                    url = parts[1]
                    title = parts[2] if len(parts) >= 3 and parts[2] else None
                else:
                    title = parts[1]

        dedup_key = str(chat_id).lower()
        if dedup_key in seen_ids:
            continue
        seen_ids.add(dedup_key)

        if len(channels) >= MAX_FORCE_JOIN_CHANNELS:
            if strict:
                raise ValidationError(
                    f"At most {MAX_FORCE_JOIN_CHANNELS} force-join channels can be configured"
                )
            break

        channels.append(RequiredChannel(chat_id=chat_id, url=url, title=title))

    return channels


def validate_force_join_channels_raw(raw: str) -> str:
    """Validate and canonicalize multiline channel configuration for storage."""
    if not (raw or "").strip():
        return ""
    parsed = parse_force_join_channels(raw, strict=True)
    formatted_lines: list[str] = []
    for ch in parsed:
        if isinstance(ch.chat_id, int):
            if ch.title:
                formatted_lines.append(f"{ch.chat_id}|{ch.url}|{ch.title}")
            else:
                formatted_lines.append(f"{ch.chat_id}|{ch.url}")
        else:
            default_url = f"https://t.me/{ch.chat_id.lstrip('@')}"
            if ch.url != default_url:
                if ch.title:
                    formatted_lines.append(f"{ch.chat_id}|{ch.url}|{ch.title}")
                else:
                    formatted_lines.append(f"{ch.chat_id}|{ch.url}")
            elif ch.title:
                formatted_lines.append(f"{ch.chat_id}|{ch.title}")
            else:
                formatted_lines.append(ch.chat_id)
    return "\n".join(formatted_lines)


async def load_force_join_config(
    *,
    cms_service: Any | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> tuple[bool, list[RequiredChannel]]:
    """Load force-join toggle and parsed channel list from CMSService or DB."""
    raw_enabled: str | None = None
    raw_channels: str | None = None

    try:
        if cms_service is not None:
            raw_enabled = await cms_service.get_setting(SettingKey.FORCE_JOIN_ENABLED.value, "false")
            raw_channels = await cms_service.get_setting(SettingKey.FORCE_JOIN_CHANNELS.value, "")
        elif session_factory is not None:
            async with session_factory() as session:
                rows = (
                    await session.execute(
                        select(AppSetting.key, AppSetting.value).where(
                            AppSetting.key.in_(
                                [
                                    SettingKey.FORCE_JOIN_ENABLED.value,
                                    SettingKey.FORCE_JOIN_CHANNELS.value,
                                ]
                            )
                        )
                    )
                ).all()
                values = {row.key: row.value for row in rows}
                raw_enabled = values.get(SettingKey.FORCE_JOIN_ENABLED.value, "false")
                raw_channels = values.get(SettingKey.FORCE_JOIN_CHANNELS.value, "")
    except Exception as exc:
        logger.warning("Failed loading force-join settings: %s", exc)
        return False, []

    enabled = str(raw_enabled or "").strip().lower() in _BOOL_TRUE_VALUES
    if not enabled:
        return False, []
    channels = parse_force_join_channels(raw_channels, strict=False)
    return bool(channels), channels


def _is_member_status(member: Any) -> bool:
    status = getattr(member, "status", "")
    status_str = str(getattr(status, "value", status)).strip().lower()
    if status_str in {"creator", "administrator", "member"}:
        return True
    if status_str == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def get_unjoined_channels(
    *,
    bot: Any,
    user_telegram_id: int,
    channels: Sequence[RequiredChannel],
    redis_client: Redis | None = None,
    bypass_cache: bool = False,
    cache_ttl: int = FORCE_JOIN_CACHE_TTL,
) -> list[RequiredChannel]:
    """Return subset of required channels the user has not joined yet (with short Redis positive cache)."""
    if not channels:
        return []

    channels_sig = hashlib.sha256(
        "|".join(f"{str(c.chat_id).lower()}:{c.url}" for c in channels).encode("utf-8")
    ).hexdigest()[:16]
    cache_key = f"shopbot:fjoin:{channels_sig}:{user_telegram_id}"

    if not bypass_cache and redis_client is not None:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return []
        except Exception as exc:
            logger.debug("Force-join Redis read failed for user %s: %s", user_telegram_id, exc)

    missing: list[RequiredChannel] = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch.chat_id, user_id=user_telegram_id)
            if not _is_member_status(member):
                missing.append(ch)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            err = str(exc).lower()
            if (
                "user_not_participant" in err
                or "participant_id_invalid" in err
                or "user not found" in err
            ):
                missing.append(ch)
            else:
                logger.warning(
                    "ForceJoinMiddleware cannot check channel %s (ensure bot is admin in channel): %s",
                    ch.chat_id,
                    exc,
                )
        except Exception as exc:
            logger.warning(
                "ForceJoinMiddleware unexpected error checking channel %s: %s",
                ch.chat_id,
                exc,
            )

    if not missing and redis_client is not None:
        try:
            await redis_client.set(cache_key, "1", ex=cache_ttl)
        except Exception as exc:
            logger.debug("Force-join Redis write failed for user %s: %s", user_telegram_id, exc)

    return missing


def build_force_join_kb(
    missing_channels: Sequence[RequiredChannel],
    *,
    join_btn_label: str,
    check_btn_label: str,
) -> InlineKeyboardMarkup:
    """Build inline keyboard with channel invite URL buttons and a Verify button."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, ch in enumerate(missing_channels, start=1):
        if ch.title:
            btn_text = ch.title if ch.title.startswith("📢") else f"📢 {ch.title}"
        elif isinstance(ch.chat_id, str) and ch.chat_id.startswith("@"):
            btn_text = f"{join_btn_label} ({ch.chat_id})"
        else:
            btn_text = f"{join_btn_label} #{idx}" if len(missing_channels) > 1 else join_btn_label
        rows.append([InlineKeyboardButton(text=btn_text[:64], url=ch.url)])

    rows.append(
        [
            InlineKeyboardButton(
                text=check_btn_label[:64],
                callback_data=MenuCB(action="check_join").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _resolve_i18n_text(
    i18n: TranslationService | None,
    key: str,
    lang: str,
) -> str:
    if i18n is not None:
        return await i18n.get_text(key, lang)
    entry = TRANSLATIONS.get(key, {})
    return entry.get(lang) or entry.get(DEFAULT_LANGUAGE) or key


async def send_force_join_prompt(
    target: Message | CallbackQuery,
    *,
    missing_channels: Sequence[RequiredChannel],
    i18n: TranslationService | None,
    lang: str,
    prefix_text: str | None = None,
    toast_text: str | None = None,
    show_alert: bool = False,
) -> Message | None:
    """Send or edit the localized forced channel membership prompt."""
    prompt_text = await _resolve_i18n_text(i18n, "force_join.prompt", lang)
    join_label = await _resolve_i18n_text(i18n, "force_join.join_btn", lang)
    check_label = await _resolve_i18n_text(i18n, "force_join.check_btn", lang)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    kb = build_force_join_kb(
        missing_channels,
        join_btn_label=join_label,
        check_btn_label=check_label,
    )
    return await safe_edit_or_send(
        target,
        prompt_text,
        reply_markup=kb,
        toast_text=toast_text,
        show_alert=show_alert,
    )


async def check_user_force_join(
    *,
    bot: Any,
    user_telegram_id: int,
    cms_service: Any | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis_client: Redis | None = None,
    bypass_cache: bool = False,
) -> list[RequiredChannel]:
    """Load config and return any required channels the user has not joined yet."""
    enabled, channels = await load_force_join_config(
        cms_service=cms_service,
        session_factory=session_factory,
    )
    if not enabled or not channels:
        return []
    return await get_unjoined_channels(
        bot=bot,
        user_telegram_id=user_telegram_id,
        channels=channels,
        redis_client=redis_client,
        bypass_cache=bypass_cache,
    )


class ForceJoinMiddleware(BaseMiddleware):
    """Enforce required Telegram channel membership for non-admin users."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        redis_client: Redis | None = None,
        translation_service: TranslationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._i18n = translation_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if data.get("is_admin"):
            return await handler(event, data)

        user: UserDTO | None = data.get("user")
        bot = data.get("bot") or getattr(event, "bot", None)
        if user is None or bot is None:
            return await handler(event, data)

        cq: CallbackQuery | None = (
            event.callback_query
            if isinstance(event, Update)
            else (event if isinstance(event, CallbackQuery) else None)
        )
        msg: Message | None = (
            event.message
            if isinstance(event, Update)
            else (event if isinstance(event, Message) else None)
        )
        if cq is None and msg is None:
            return await handler(event, data)

        enabled, channels = await load_force_join_config(
            cms_service=data.get("cms_service"),
            session_factory=data.get("session_factory") or self._session_factory,
        )
        if not enabled or not channels:
            return await handler(event, data)

        # Let brand-new users pick their language before gating them behind force-join
        if not user.language_selected:
            if msg is not None and (msg.text or "").strip().split(maxsplit=1)[0].lower().startswith("/start"):
                return await handler(event, data)
            if cq is not None and (cq.data or "").startswith("lang:"):
                return await handler(event, data)

        check_join_cb = MenuCB(action="check_join").pack()
        is_check_join = cq is not None and (cq.data or "") == check_join_cb

        missing = await get_unjoined_channels(
            bot=bot,
            user_telegram_id=user.telegram_id,
            channels=channels,
            redis_client=data.get("redis_client") or self._redis,
            bypass_cache=is_check_join,
        )
        if not missing:
            return await handler(event, data)

        lang = str(data.get("lang") or user.language_code or DEFAULT_LANGUAGE)
        i18n: TranslationService | None = data.get("i18n") or self._i18n

        if is_check_join and cq is not None:
            alert_text = await _resolve_i18n_text(i18n, "force_join.not_joined_alert", lang)
            await send_force_join_prompt(
                cq,
                missing_channels=missing,
                i18n=i18n,
                lang=lang,
                toast_text=alert_text,
                show_alert=True,
            )
        else:
            target = cq if cq is not None else msg
            assert target is not None
            await send_force_join_prompt(
                target,
                missing_channels=missing,
                i18n=i18n,
                lang=lang,
            )
        return None


__all__ = [
    "FORCE_JOIN_CACHE_TTL",
    "ForceJoinMiddleware",
    "MAX_FORCE_JOIN_CHANNELS",
    "RequiredChannel",
    "build_force_join_kb",
    "check_user_force_join",
    "get_unjoined_channels",
    "load_force_join_config",
    "parse_force_join_channels",
    "send_force_join_prompt",
    "validate_force_join_channels_raw",
]
