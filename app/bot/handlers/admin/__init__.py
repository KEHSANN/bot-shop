"""Modular in-bot /admin sub-routers."""

from app.bot.handlers.admin.analytics import router as analytics_router
from app.bot.handlers.admin.broadcast import router as broadcast_router
from app.bot.handlers.admin.catalog import router as catalog_router
from app.bot.handlers.admin.cms import router as cms_router
from app.bot.handlers.admin.payments import router as payments_router
from app.bot.handlers.admin.support import router as support_router
from app.bot.handlers.admin.users import router as users_router

__all__ = [
    "analytics_router",
    "broadcast_router",
    "catalog_router",
    "cms_router",
    "payments_router",
    "support_router",
    "users_router",
]
