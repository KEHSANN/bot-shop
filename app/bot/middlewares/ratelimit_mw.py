"""Redis-backed sliding-window rate limiter and per-action cooldown helpers."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from redis.asyncio import Redis

from app.bot.middlewares.logging_mw import extract_telegram_user
from app.config import Settings, get_settings
from app.core.constants import DEFAULT_LANGUAGE
from app.core.logging import get_logger
from app.locales.i18n import TranslationService
from app.locales.seed_translations import TRANSLATIONS
from app.services.interfaces import UserServiceProtocol

logger = get_logger(__name__)

_SLIDING_WINDOW_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[3]) then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
redis.call('PEXPIRE', KEYS[1], ARGV[5])
return 1
"""


async def check_action_cooldown(
    redis_client: Redis | None,
    *,
    action: str,
    user_id: int,
    cooldown_seconds: int,
    fallback_store: dict[str, float] | None = None,
) -> bool:
    """Acquire an action cooldown lock; return True if allowed, False if still cooling down."""
    if cooldown_seconds <= 0:
        return True

    key = f"rl:cd:{action}:{user_id}"
    if redis_client is not None:
        try:
            acquired = await redis_client.set(key, "1", ex=cooldown_seconds, nx=True)
            return bool(acquired)
        except Exception as exc:
            logger.warning("Redis cooldown check failed for %s: %s", key, exc)

    if fallback_store is not None:
        now = time.monotonic()
        expires_at = fallback_store.get(key, 0.0)
        if now < expires_at:
            return False
        fallback_store[key] = now + cooldown_seconds
    return True


class RateLimitMiddleware(BaseMiddleware):
    """Outermost sliding-window rate limit middleware backed by Redis 7."""

    def __init__(
        self,
        *,
        redis_client: Redis | None = None,
        settings: Settings | None = None,
        translation_service: TranslationService | None = None,
        user_service: UserServiceProtocol | None = None,
        max_requests: int | None = None,
        window_seconds: int | None = None,
    ) -> None:
        self._redis = redis_client
        self._settings = settings or get_settings()
        self._i18n = translation_service
        self._user_service = user_service
        self._max_requests = max_requests if max_requests is not None else self._settings.RATE_LIMIT_MAX_REQUESTS
        self._window_seconds = (
            window_seconds if window_seconds is not None else self._settings.RATE_LIMIT_WINDOW_SECONDS
        )
        self._memory_windows: dict[int, deque[float]] = defaultdict(deque)
        self._memory_alerts: dict[int, float] = {}

    async def _is_allowed(self, telegram_id: int) -> bool:
        if self._max_requests <= 0 or self._window_seconds <= 0:
            return True

        if self._redis is not None:
            now_ms = int(time.time() * 1000)
            window_ms = self._window_seconds * 1000
            cutoff_ms = now_ms - window_ms
            member = f"{now_ms}:{secrets.token_hex(4)}"
            key = f"rl:win:{telegram_id}"
            try:
                allowed = await self._redis.eval(
                    _SLIDING_WINDOW_LUA,
                    1,
                    key,
                    cutoff_ms,
                    now_ms,
                    self._max_requests,
                    member,
                    window_ms + 1000,
                )
                return bool(int(allowed))
            except Exception as exc:
                logger.warning("Redis rate limit failed for telegram_id=%s: %s", telegram_id, exc)

        now = time.monotonic()
        bucket = self._memory_windows[telegram_id]
        cutoff = now - self._window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self._max_requests:
            return False
        bucket.append(now)
        return True

    async def _resolve_alert_text(self, tg_user: Any) -> str:
        lang = TranslationService.normalize_lang(getattr(tg_user, "language_code", None))
        if self._user_service is not None:
            try:
                db_user = await self._user_service.get_by_telegram_id(tg_user.id)
                if db_user is not None:
                    lang = TranslationService.normalize_lang(db_user.language_code)
            except Exception as exc:
                logger.warning("Failed to resolve user lang during rate limit alert: %s", exc)

        if self._i18n is not None:
            try:
                return await self._i18n.get_text("err.rate_limited", lang)
            except Exception as exc:
                logger.warning("i18n rate limit lookup failed: %s", exc)

        seed_map = TRANSLATIONS.get("err.rate_limited", {})
        return seed_map.get(lang) or seed_map.get(DEFAULT_LANGUAGE) or "⏳ Too many requests."

    async def _should_send_message_alert(self, telegram_id: int) -> bool:
        if self._redis is not None:
            try:
                return bool(await self._redis.set(f"rl:alert:{telegram_id}", "1", ex=2, nx=True))
            except Exception as exc:
                logger.warning("Redis alert throttle failed: %s", exc)
        now = time.monotonic()
        if now < self._memory_alerts.get(telegram_id, 0.0):
            return False
        self._memory_alerts[telegram_id] = now + 2.0
        return True

    async def _notify_rate_limited(self, event: TelegramObject, tg_user: Any) -> None:
        text = await self._resolve_alert_text(tg_user)
        cq: CallbackQuery | None = None
        msg: Message | None = None

        if isinstance(event, Update):
            cq = event.callback_query
            msg = event.message
        elif isinstance(event, CallbackQuery):
            cq = event
        elif isinstance(event, Message):
            msg = event

        try:
            if cq is not None:
                await cq.answer(text, show_alert=True)
            elif msg is not None and await self._should_send_message_alert(tg_user.id):
                await msg.answer(text)
        except Exception as exc:
            logger.warning("Failed sending rate-limit notification to %s: %s", tg_user.id, exc)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = extract_telegram_user(event, data)
        if tg_user is None or getattr(tg_user, "is_bot", False):
            return await handler(event, data)

        if not await self._is_allowed(tg_user.id):
            logger.info("Rate limit exceeded for telegram_id=%s", tg_user.id)
            await self._notify_rate_limited(event, tg_user)
            return None

        return await handler(event, data)
