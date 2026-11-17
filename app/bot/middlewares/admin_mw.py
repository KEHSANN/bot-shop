"""Admin router authorization middleware checking ADMIN_TELEGRAM_IDS and users.is_admin."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.middlewares.logging_mw import extract_telegram_user
from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.dtos import UserDTO
from app.services.interfaces import UserServiceProtocol

logger = get_logger(__name__)


class AdminAuthMiddleware(BaseMiddleware):
    """Strict RBAC guard for /admin routers verifying env ADMIN_TELEGRAM_IDS or DB is_admin."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        user_service: UserServiceProtocol | None = None,
    ) -> None:
        self._settings = settings
        self._user_service = user_service

    async def _reject(self, event: TelegramObject) -> None:
        message = "⛔ Access denied. Administrator privileges required."
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(message, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(message)
        except Exception as exc:
            logger.warning("Failed sending admin rejection notice: %s", exc)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings = self._settings or data.get("settings") or get_settings()
        tg_user = extract_telegram_user(event, data)
        if tg_user is None or getattr(tg_user, "is_bot", False):
            await self._reject(event)
            return None

        user: UserDTO | None = data.get("user")
        if user is None:
            svc = self._user_service or data.get("user_service")
            if svc is not None:
                user = await svc.get_by_telegram_id(tg_user.id)
                if user is not None:
                    data["user"] = user

        if user is not None and user.is_blocked:
            logger.warning("Blocked user telegram_id=%s attempted admin access", tg_user.id)
            return None

        is_env_admin = tg_user.id in settings.admin_ids
        is_db_admin = user is not None and user.is_admin
        if not (is_env_admin or is_db_admin):
            logger.warning("Unauthorized /admin access attempt by telegram_id=%s", tg_user.id)
            await self._reject(event)
            return None

        data["is_admin"] = True
        return await handler(event, data)
