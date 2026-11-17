"""Outermost blocked-user guard dropping all updates from banned accounts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.bot.middlewares.logging_mw import extract_telegram_user
from app.core.logging import get_logger
from app.services.interfaces import UserServiceProtocol

logger = get_logger(__name__)


class BanCheckMiddleware(BaseMiddleware):
    """Reject and drop all updates from users marked is_blocked=True at the outermost layer."""

    def __init__(self, user_service: UserServiceProtocol) -> None:
        self._user_service = user_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = extract_telegram_user(event, data)
        if tg_user is None:
            return await handler(event, data)

        existing = await self._user_service.get_by_telegram_id(tg_user.id)
        if existing is not None and existing.is_blocked:
            logger.info("Dropped update from blocked user telegram_id=%s", tg_user.id)
            return None

        return await handler(event, data)
