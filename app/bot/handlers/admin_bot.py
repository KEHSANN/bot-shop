"""Root /admin router protected by RBAC middleware, assembling all admin sub-modules."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Router
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.handlers.admin import (
    analytics_router,
    broadcast_router,
    catalog_router,
    cms_router,
    payments_router,
    support_router,
    users_router,
)
from app.bot.handlers.admin.common import get_optional_service, is_admin_telegram_id
from app.core.logging import get_logger

logger = get_logger(__name__)


class AdminAccessMiddleware(BaseMiddleware):
    """Router-level RBAC guard enforcing ADMIN_TELEGRAM_IDS or user.is_admin == True."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        telegram_id = int(from_user.id) if from_user is not None else 0
        db_user = data.get("db_user") or data.get("user")

        if db_user is None and telegram_id > 0:
            user_service = get_optional_service(data, "user_service")
            if user_service is not None and hasattr(user_service, "get_by_telegram_id"):
                try:
                    db_user = await user_service.get_by_telegram_id(telegram_id)
                except Exception as exc:
                    logger.debug("AdminAccessMiddleware user lookup failed: %s", exc)

        if telegram_id > 0 and (data.get("is_admin") is True or is_admin_telegram_id(telegram_id, db_user)):
            return await handler(event, data)

        logger.warning("Unauthorized /admin access attempt by telegram_id=%s", telegram_id)
        if isinstance(event, CallbackQuery):
            await event.answer("⛔ Access denied. Admin privileges required.", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("⛔ Access denied. Admin privileges required.")
        return None


def _build_admin_auth_middleware() -> BaseMiddleware:
    """Use shared AdminAuthMiddleware from bot-customer if available, else AdminAccessMiddleware."""
    try:
        from app.bot.middlewares.admin_mw import AdminAuthMiddleware

        sig = inspect.signature(AdminAuthMiddleware.__init__)
        required = [
            p
            for name, p in sig.parameters.items()
            if name != "self" and p.default is inspect.Parameter.empty and p.kind not in {
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }
        ]
        if not required:
            return AdminAuthMiddleware()
    except ImportError:
        pass
    return AdminAccessMiddleware()


admin_router = Router(name="admin_bot")
_admin_mw = _build_admin_auth_middleware()
admin_router.message.middleware(_admin_mw)
admin_router.callback_query.middleware(_admin_mw)

admin_router.include_router(analytics_router)
admin_router.include_router(users_router)
admin_router.include_router(catalog_router)
admin_router.include_router(payments_router)
admin_router.include_router(support_router)
admin_router.include_router(cms_router)
admin_router.include_router(broadcast_router)

router = admin_router

__all__ = [
    "AdminAccessMiddleware",
    "admin_router",
    "router",
]
