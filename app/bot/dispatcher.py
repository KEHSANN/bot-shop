"""Aiogram 3 Dispatcher factory wiring Redis FSM storage, ordered middlewares, and routers."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
from collections.abc import Sequence
from typing import Any

from aiogram import Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from aiogram.types import ErrorEvent
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import balance as balance_handler
from app.bot.handlers import language as language_handler
from app.bot.handlers import start as start_handler
from app.bot.handlers import support as support_handler
from app.bot.middlewares import (
    AdminAuthMiddleware,
    AuthContextMiddleware,
    BanCheckMiddleware,
    ForceJoinMiddleware,
    I18nMiddleware,
    LoggingMiddleware,
    RateLimitMiddleware,
)
from app.config import Settings, get_settings
from app.core.constants import DEFAULT_LANGUAGE
from app.core.exceptions import (
    DuplicateTransactionError,
    FileValidationError,
    InsufficientBalanceError,
    InsufficientStockError,
    RateLimitExceededError,
    ShopBotError,
    UserBlockedError,
)
from app.core.logging import get_logger
from app.core.security import escape_html
from app.database.session import get_session_factory
from app.locales.i18n import TranslationService
from app.services.interfaces import SupportServiceProtocol, UserServiceProtocol
from app.services.support_service import SupportService
from app.services.user_service import UserService

logger = get_logger(__name__)


def create_redis_storage(
    redis_url: str | None = None,
    *,
    redis_client: Redis | None = None,
) -> RedisStorage:
    """Create RedisStorage with key isolation per (bot_id, chat_id, user_id)."""
    key_builder = DefaultKeyBuilder(with_bot_id=True, with_destiny=True)
    if redis_client is not None:
        return RedisStorage(redis=redis_client, key_builder=key_builder)
    url = redis_url or get_settings().REDIS_URL
    return RedisStorage.from_url(url, key_builder=key_builder)


def get_customer_routers() -> tuple[Router, ...]:
    """Return customer routers detached cleanly for isolated dispatcher construction."""
    for r in (
        start_handler.router,
        language_handler.router,
        balance_handler.router,
        support_handler.router,
    ):
        if r.parent_router is not None:
            r._parent_router = None
    return (
        start_handler.router,
        language_handler.router,
        balance_handler.router,
        support_handler.router,
    )


def _instantiate_optional_service(
    module_name: str,
    class_name: str,
    *,
    available_deps: dict[str, Any],
) -> Any | None:
    """Import and instantiate a Wave 2 domain service when its module exists on disk."""
    if importlib.util.find_spec(module_name) is None:
        return None
    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_name, None)
    if cls is None:
        return None
    try:
        sig = inspect.signature(cls.__init__)
        kwargs: dict[str, Any] = {}
        for name, _param in list(sig.parameters.items())[1:]:
            if name in available_deps and available_deps[name] is not None:
                kwargs[name] = available_deps[name]
        return cls(**kwargs)
    except Exception as exc:
        logger.warning("Could not auto-instantiate %s.%s: %s", module_name, class_name, exc)
        return None


def _load_optional_router(module_name: str) -> Router | None:
    """Import a router module when present in the codebase and detach stale parent for test reuse."""
    if importlib.util.find_spec(module_name) is None:
        return None
    mod = importlib.import_module(module_name)
    router = getattr(mod, "router", None)
    if isinstance(router, Router):
        if router.parent_router is not None:
            router._parent_router = None
        return router
    return None


def _ensure_admin_middleware(
    admin_router: Router,
    *,
    settings: Settings,
    user_service: UserServiceProtocol,
) -> None:
    """Ensure AdminAuthMiddleware is attached to the admin router's message and callback_query observers."""
    for observer in (admin_router.message, admin_router.callback_query):
        already_attached = any(
            isinstance(mw, AdminAuthMiddleware) for mw in observer.middleware
        )
        if not already_attached:
            observer.middleware(
                AdminAuthMiddleware(settings=settings, user_service=user_service)
            )


def _register_error_handler(
    dp: Dispatcher,
    *,
    translation_service: TranslationService,
) -> None:
    @dp.error()
    async def _handle_domain_errors(event: ErrorEvent) -> bool:
        exc = event.exception
        update = event.update

        if isinstance(exc, UserBlockedError):
            return True

        lang = DEFAULT_LANGUAGE
        from_user = (
            (update.message and update.message.from_user)
            or (update.callback_query and update.callback_query.from_user)
        )
        if from_user is not None and getattr(from_user, "language_code", None):
            lang = translation_service.normalize_lang(from_user.language_code)

        if isinstance(exc, RateLimitExceededError):
            msg_text = await translation_service.get_text("err.rate_limited", lang)
        elif isinstance(exc, InsufficientBalanceError):
            msg_text = await translation_service.get_text("shop.insufficient_balance", lang)
        elif isinstance(exc, InsufficientStockError):
            available = exc.details.get("available", 0) if isinstance(exc.details, dict) else 0
            msg_text = await translation_service.get_text("shop.insufficient_stock", lang, available=available)
        elif isinstance(exc, DuplicateTransactionError):
            msg_text = await translation_service.get_text("deposit.duplicate_txid", lang)
        elif isinstance(exc, FileValidationError):
            msg_text = await translation_service.get_text("err.invalid_file", lang)
        elif isinstance(exc, ShopBotError):
            msg_text = escape_html(exc.message)
        else:
            logger.exception("Unhandled exception during Telegram update processing", exc_info=exc)
            msg_text = await translation_service.get_text("err.unexpected", lang)

        try:
            if update.callback_query is not None:
                await update.callback_query.answer(msg_text, show_alert=True)
            elif update.message is not None:
                await update.message.answer(msg_text, parse_mode=ParseMode.HTML)
        except Exception as send_exc:
            logger.warning("Failed sending error response to user: %s", send_exc)
        return True


def build_dispatcher(
    extra_routers: Sequence[Router] = (),
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis_client: Redis | None = None,
    storage: BaseStorage | None = None,
    user_service: UserServiceProtocol | None = None,
    support_service: SupportServiceProtocol | None = None,
    translation_service: TranslationService | None = None,
    shop_service: Any | None = None,
    payment_service: Any | None = None,
    cms_service: Any | None = None,
    analytics_service: Any | None = None,
    broadcast_service: Any | None = None,
    include_customer_routers: bool = True,
    rate_limit_max_requests: int | None = None,
    rate_limit_window_seconds: int | None = None,
) -> Dispatcher:
    """Build an aiogram Dispatcher with exact middleware ordering and customer routers."""
    runtime_settings = settings or get_settings()
    runtime_session_factory = session_factory or get_session_factory()

    owned_redis: Redis | None = None
    active_redis = redis_client
    if active_redis is None and storage is None and runtime_settings.REDIS_URL:
        owned_redis = Redis.from_url(runtime_settings.REDIS_URL, decode_responses=True)
        active_redis = owned_redis

    if storage is None:
        if active_redis is not None:
            storage = create_redis_storage(redis_client=active_redis)
        else:
            storage = MemoryStorage()

    resolved_user_service = user_service or UserService(
        runtime_session_factory,
        settings=runtime_settings,
    )
    resolved_support_service = support_service or SupportService(runtime_session_factory)
    resolved_i18n = translation_service or TranslationService(
        runtime_session_factory,
        redis_client=active_redis,
    )

    dp = Dispatcher(
        storage=storage,
        settings=runtime_settings,
        session_factory=runtime_session_factory,
        redis_client=active_redis,
        redis=active_redis,
        user_service=resolved_user_service,
        support_service=resolved_support_service,
        translation_service=resolved_i18n,
        i18n=resolved_i18n,
    )

    if owned_redis is not None:
        @dp.shutdown()
        async def _close_owned_redis() -> None:
            await owned_redis.aclose()

    optional_services = {
        "shop_service": shop_service,
        "payment_service": payment_service,
        "cms_service": cms_service,
        "analytics_service": analytics_service,
        "broadcast_service": broadcast_service,
    }
    for key, svc in optional_services.items():
        if svc is not None:
            dp.workflow_data[key] = svc

    # Exact middleware order mandated by docs/ARCHITECTURE_CONTRACTS.md Section 6:
    # 1. LoggingMiddleware (outer)
    # 2. BanCheckMiddleware (outer)
    # 3. RateLimitMiddleware (outer)
    # 4. AuthContextMiddleware / DbSessionMiddleware (inner)
    # 5. I18nMiddleware (inner)
    dp.update.outer_middleware(LoggingMiddleware())
    dp.update.outer_middleware(BanCheckMiddleware(resolved_user_service))
    dp.update.outer_middleware(
        RateLimitMiddleware(
            redis_client=active_redis,
            settings=runtime_settings,
            translation_service=resolved_i18n,
            user_service=resolved_user_service,
            max_requests=rate_limit_max_requests,
            window_seconds=rate_limit_window_seconds,
        )
    )
    dp.update.middleware(
        AuthContextMiddleware(
            resolved_user_service,
            settings=runtime_settings,
            session_factory=runtime_session_factory,
            translation_service=resolved_i18n,
        )
    )
    dp.update.middleware(I18nMiddleware(resolved_i18n))
    dp.update.middleware(
        ForceJoinMiddleware(
            session_factory=runtime_session_factory,
            redis_client=active_redis,
            translation_service=resolved_i18n,
        )
    )

    _register_error_handler(dp, translation_service=resolved_i18n)

    if include_customer_routers:
        for customer_router in get_customer_routers():
            dp.include_router(customer_router)

    for router in extra_routers:
        if router.parent_router is not None:
            router._parent_router = None
        dp.include_router(router)

    return dp


def create_full_dispatcher(
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis_client: Redis | None = None,
    storage: BaseStorage | None = None,
    user_service: UserServiceProtocol | None = None,
    support_service: SupportServiceProtocol | None = None,
    translation_service: TranslationService | None = None,
    shop_service: Any | None = None,
    payment_service: Any | None = None,
    cms_service: Any | None = None,
    analytics_service: Any | None = None,
    broadcast_service: Any | None = None,
    extra_routers: Sequence[Router] = (),
) -> Dispatcher:
    """Create full Dispatcher wiring customer + shop + deposit + admin services and routers."""
    runtime_settings = settings or get_settings()
    runtime_session_factory = session_factory or get_session_factory()

    owned_redis: Redis | None = None
    active_redis = redis_client
    if active_redis is None and storage is None and runtime_settings.REDIS_URL:
        owned_redis = Redis.from_url(runtime_settings.REDIS_URL, decode_responses=True)
        active_redis = owned_redis

    resolved_user_service = user_service or UserService(
        runtime_session_factory,
        settings=runtime_settings,
    )
    resolved_support_service = support_service or SupportService(runtime_session_factory)
    resolved_i18n = translation_service or TranslationService(
        runtime_session_factory,
        redis_client=active_redis,
    )

    deps: dict[str, Any] = {
        "session_factory": runtime_session_factory,
        "settings": runtime_settings,
        "redis_client": active_redis,
        "redis": active_redis,
        "translation_service": resolved_i18n,
        "i18n": resolved_i18n,
        "encryption_key": runtime_settings.ENCRYPTION_KEY,
        "user_service": resolved_user_service,
        "support_service": resolved_support_service,
    }

    if shop_service is None:
        shop_service = _instantiate_optional_service(
            "app.services.shop_service", "ShopService", available_deps=deps
        )
    if cms_service is None:
        cms_service = _instantiate_optional_service(
            "app.services.cms_service", "CMSService", available_deps=deps
        )
    if analytics_service is None:
        analytics_service = _instantiate_optional_service(
            "app.services.analytics_service", "AnalyticsService", available_deps=deps
        )
    if payment_service is None:
        payment_service = _instantiate_optional_service(
            "app.services.payment_service", "PaymentService", available_deps=deps
        )
    if broadcast_service is None:
        broadcast_service = _instantiate_optional_service(
            "app.services.broadcast_service", "BroadcastService", available_deps=deps
        )

    discovered_routers: list[Router] = []
    for mod_path in (
        "app.bot.handlers.shop",
        "app.bot.handlers.deposit",
        "app.bot.handlers.admin_bot",
    ):
        router = _load_optional_router(mod_path)
        if router is not None:
            if mod_path.endswith("admin_bot"):
                _ensure_admin_middleware(
                    router,
                    settings=runtime_settings,
                    user_service=resolved_user_service,
                )
            discovered_routers.append(router)

    discovered_routers.extend(extra_routers)

    dp = build_dispatcher(
        extra_routers=discovered_routers,
        settings=runtime_settings,
        session_factory=runtime_session_factory,
        redis_client=active_redis,
        storage=storage,
        user_service=resolved_user_service,
        support_service=resolved_support_service,
        translation_service=resolved_i18n,
        shop_service=shop_service,
        payment_service=payment_service,
        cms_service=cms_service,
        analytics_service=analytics_service,
        broadcast_service=broadcast_service,
        include_customer_routers=True,
    )
    if owned_redis is not None:
        @dp.shutdown()
        async def _close_full_dp_redis() -> None:
            await owned_redis.aclose()
    return dp


__all__ = [
    "build_dispatcher",
    "create_full_dispatcher",
    "create_redis_storage",
    "get_customer_routers",
]
