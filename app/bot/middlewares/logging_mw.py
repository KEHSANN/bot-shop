"""Outermost update correlation and sanitized logging middleware."""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.core.logging import get_logger

logger = get_logger(__name__)


def extract_telegram_user(event: TelegramObject, data: dict[str, Any]) -> Any:
    """Safely resolve Telegram User object from middleware data or Update payload."""
    from_user = data.get("event_from_user")
    if from_user is not None:
        return from_user
    if isinstance(event, Update):
        for attr in (
            "message",
            "edited_message",
            "callback_query",
            "inline_query",
            "chosen_inline_result",
            "shipping_query",
            "pre_checkout_query",
            "my_chat_member",
            "chat_member",
        ):
            inner = getattr(event, attr, None)
            if inner is not None and getattr(inner, "from_user", None) is not None:
                return inner.from_user
    return getattr(event, "from_user", None)


class LoggingMiddleware(BaseMiddleware):
    """Assign correlation ID and log non-sensitive Telegram update metadata and timing."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        correlation_id = secrets.token_hex(6)
        data["correlation_id"] = correlation_id

        tg_user = extract_telegram_user(event, data)
        telegram_id = getattr(tg_user, "id", None)
        update_id = getattr(event, "update_id", None)
        event_type = getattr(event, "event_type", type(event).__name__)

        started = time.monotonic()
        logger.debug(
            "cid=%s update_id=%s type=%s telegram_id=%s started",
            correlation_id,
            update_id,
            event_type,
            telegram_id,
        )
        try:
            result = await handler(event, data)
        except Exception:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            logger.exception(
                "cid=%s update_id=%s type=%s telegram_id=%s failed in %.1fms",
                correlation_id,
                update_id,
                event_type,
                telegram_id,
                elapsed_ms,
            )
            raise

        elapsed_ms = (time.monotonic() - started) * 1000.0
        logger.debug(
            "cid=%s update_id=%s type=%s telegram_id=%s completed in %.1fms",
            correlation_id,
            update_id,
            event_type,
            telegram_id,
            elapsed_ms,
        )
        return result
