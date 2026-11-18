"""Unified FastAPI + aiogram 3.x Bot entrypoint supporting both Webhook and Polling modes."""

from __future__ import annotations

import asyncio
import hmac
import importlib
import inspect
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import Any

import uvicorn
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.database.seed import run_seed
from app.database.session import close_db, get_session_factory, init_db
from app.locales.i18n import TranslationService
from app.web_admin.app import configure_web_admin
from app.web_admin.deps import get_app_settings

logger = get_logger(__name__)


def _is_usable_bot_token(token: str) -> bool:
    """Return True if BOT_TOKEN is non-empty and not an unconfigured template placeholder."""
    cleaned = (token or "").strip()
    if not cleaned or "CHANGE_ME" in cleaned or cleaned.startswith("0000000000:"):
        return False
    return ":" in cleaned


_SHARED_SERVICE_ATTRS: tuple[str, ...] = (
    "user_service",
    "shop_service",
    "payment_service",
    "support_service",
    "cms_service",
    "analytics_service",
    "broadcast_service",
)


def _load_dispatcher_factory() -> Any | None:
    """Import dispatcher factory from app.bot.dispatcher via sys.modules-aware importlib."""
    try:
        dp_mod = importlib.import_module("app.bot.dispatcher")

        for fn_name in ("create_full_dispatcher", "build_dispatcher"):
            fn = getattr(dp_mod, fn_name, None)
            if callable(fn):
                return fn
    except ImportError:
        logger.info("Bot dispatcher module not present yet; running Web Admin standalone")
    return None


def _sync_dispatcher_services_to_state(dp: Any, app_state: Any) -> None:
    """Mirror initialized domain services from Dispatcher workflow_data onto FastAPI app.state."""
    workflow_data = getattr(dp, "workflow_data", None)
    if not isinstance(workflow_data, dict):
        return
    for svc_attr in _SHARED_SERVICE_ATTRS:
        svc_val = workflow_data.get(svc_attr)
        if svc_val is not None and getattr(app_state, svc_attr, None) is None:
            setattr(app_state, svc_attr, svc_val)


def _invoke_dispatcher_factory(
    factory_fn: Any,
    *,
    settings: Settings,
    session_factory: Any,
    redis_client: Redis | None,
    translation_service: TranslationService,
    bot: Any | None = None,
    app_state: Any | None = None,
) -> Any:
    """Call create_full_dispatcher / build_dispatcher matching its declared parameter signature."""
    sig = inspect.signature(factory_fn)
    available: dict[str, Any] = {
        "settings": settings,
        "config": settings,
        "app_settings": settings,
        "session_factory": session_factory,
        "db_session_factory": session_factory,
        "redis": redis_client,
        "redis_client": redis_client,
        "translation_service": translation_service,
        "i18n": translation_service,
        "bot": bot,
    }
    if app_state is not None:
        services_bag = getattr(app_state, "services", None)
        for svc_attr in _SHARED_SERVICE_ATTRS:
            val = getattr(app_state, svc_attr, None)
            if val is None and isinstance(services_bag, dict):
                val = services_bag.get(svc_attr)
            if val is not None:
                available[svc_attr] = val

    kwargs: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if name in available and available[name] is not None:
            kwargs[name] = available[name]
    return factory_fn(**kwargs)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage PostgreSQL engine, Redis pool, idempotent seed, and aiogram Bot lifecycle."""
    settings: Settings = getattr(app.state, "settings", None) or get_settings()
    app.state.settings = settings
    configure_logging(settings.LOG_LEVEL)

    owns_db = False
    owns_redis = False
    owns_bot = False
    polling_task: asyncio.Task[Any] | None = None

    if getattr(app.state, "session_factory", None) is None:
        init_db(settings.DATABASE_URL, echo=settings.DEBUG)
        app.state.session_factory = get_session_factory()
        owns_db = True

    if getattr(app.state, "redis", None) is None and settings.REDIS_URL:
        redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        app.state.redis = redis_client
        owns_redis = True
    else:
        redis_client = getattr(app.state, "redis", None)

    session_factory = app.state.session_factory
    existing_i18n = getattr(app.state, "translation_service", None) or getattr(app.state, "i18n", None)
    if existing_i18n is None:
        existing_i18n = TranslationService(
            session_factory=session_factory,
            redis_client=redis_client,
        )
    app.state.translation_service = existing_i18n
    app.state.i18n = existing_i18n

    if not getattr(app.state, "skip_startup_seed", False):
        try:
            async with session_factory() as session:
                seed_summary = await run_seed(session)
                logger.info("Startup seed verification completed: %s", seed_summary)
        except Exception as exc:
            logger.warning("Startup seed check skipped or deferred: %s", exc)

    if not getattr(app.state, "skip_bot_startup", False):
        bot = getattr(app.state, "bot", None)
        dp = getattr(app.state, "dp", None)

        if bot is None and _is_usable_bot_token(settings.BOT_TOKEN):
            bot = Bot(
                token=settings.BOT_TOKEN,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            app.state.bot = bot
            owns_bot = True

        if dp is None and bot is not None:
            dp_factory = _load_dispatcher_factory()
            if dp_factory is not None:
                try:
                    dp = _invoke_dispatcher_factory(
                        dp_factory,
                        settings=settings,
                        session_factory=session_factory,
                        redis_client=redis_client,
                        translation_service=app.state.translation_service,
                        bot=bot,
                        app_state=app.state,
                    )
                    if inspect.isawaitable(dp):
                        dp = await dp
                    app.state.dp = dp
                except Exception as exc:
                    logger.warning("Failed to initialize aiogram Dispatcher: %s", exc)

        if dp is not None:
            _sync_dispatcher_services_to_state(dp, app.state)

        if bot is not None and dp is not None:
            if settings.BOT_MODE == "webhook":
                if settings.BOT_WEBHOOK_URL and settings.APP_ENV != "test":
                    try:
                        await bot.set_webhook(
                            url=settings.BOT_WEBHOOK_URL,
                            secret_token=settings.BOT_WEBHOOK_SECRET or None,
                            drop_pending_updates=False,
                        )
                        logger.info("Telegram webhook registered at %s", settings.BOT_WEBHOOK_URL)
                    except Exception as exc:
                        logger.warning("Telegram set_webhook warning: %s", exc)
            elif settings.BOT_MODE == "polling" and settings.APP_ENV != "test":
                try:
                    await bot.delete_webhook(drop_pending_updates=False)
                except Exception as exc:
                    logger.warning("Telegram delete_webhook warning: %s", exc)
                polling_task = asyncio.create_task(
                    dp.start_polling(bot, handle_signals=False),
                    name="telegram_bot_polling",
                )
                app.state.polling_task = polling_task
                logger.info("Telegram Bot background polling task started")

    try:
        yield
    finally:
        if polling_task is not None and not polling_task.done():
            polling_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await polling_task

        bot_instance = getattr(app.state, "bot", None)
        if owns_bot and bot_instance is not None:
            with suppress(Exception):
                await bot_instance.session.close()

        dp_instance = getattr(app.state, "dp", None)
        if dp_instance is not None and hasattr(dp_instance, "storage"):
            with suppress(Exception):
                await dp_instance.storage.close()

        if owns_redis and redis_client is not None:
            with suppress(Exception):
                await redis_client.aclose()

        if owns_db:
            with suppress(Exception):
                await close_db()


async def _run_healthcheck(request: Request) -> JSONResponse:
    """Verify PostgreSQL and Redis connectivity and report service health."""
    settings = get_app_settings(request)
    db_status = "error"
    redis_status = "error"

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        try:
            session_factory = get_session_factory()
        except Exception:
            session_factory = None

    if session_factory is not None:
        try:
            async with session_factory() as session:
                val = (await session.execute(text("SELECT 1"))).scalar_one()
                if val == 1:
                    db_status = "ok"
        except Exception as exc:
            logger.warning("Healthcheck database probe failed: %s", exc)

    redis_client: Redis | None = getattr(request.app.state, "redis", None)
    temp_redis: Redis | None = None
    try:
        if redis_client is None and settings.REDIS_URL:
            temp_redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
            redis_client = temp_redis
        if redis_client is not None:
            pong = await redis_client.ping()
            if pong:
                redis_status = "ok"
    except Exception as exc:
        logger.warning("Healthcheck Redis probe failed: %s", exc)
    finally:
        if temp_redis is not None:
            with suppress(Exception):
                await temp_redis.aclose()

    healthy = db_status == "ok" and redis_status == "ok"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "database": db_status,
            "redis": redis_status,
            "bot_mode": settings.BOT_MODE,
        },
    )


async def _handle_telegram_webhook(request: Request) -> Response:
    """Validate X-Telegram-Bot-Api-Secret-Token in constant time and feed Update to Dispatcher."""
    settings = get_app_settings(request)
    expected_secret = (settings.BOT_WEBHOOK_SECRET or "").strip()
    provided_secret = (request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()

    if not expected_secret or not provided_secret:
        return JSONResponse(
            status_code=403,
            content={"ok": False, "detail": "Missing Telegram webhook secret token"},
        )

    if not hmac.compare_digest(provided_secret.encode("utf-8"), expected_secret.encode("utf-8")):
        return JSONResponse(
            status_code=403,
            content={"ok": False, "detail": "Invalid Telegram webhook secret token"},
        )

    bot = getattr(request.app.state, "bot", None)
    dp = getattr(request.app.state, "dp", None)
    if bot is None or dp is None:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "detail": "Bot or Dispatcher is not initialized"},
        )

    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Webhook payload must be a JSON object")
        update = Update.model_validate(payload, context={"bot": bot})
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "detail": f"Malformed Telegram Update payload: {exc}"},
        )

    await dp.feed_update(bot=bot, update=update)
    return JSONResponse(status_code=200, content={"ok": True})


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the unified FastAPI application with lifespan, health probes, webhook endpoint, and Web Admin."""
    resolved_settings = settings or get_settings()
    application = FastAPI(
        title=resolved_settings.APP_NAME,
        debug=resolved_settings.DEBUG,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings

    @application.get("/healthz", tags=["health"])
    @application.get("/health", tags=["health"])
    async def healthcheck_endpoint(request: Request) -> JSONResponse:
        return await _run_healthcheck(request)

    webhook_path = (resolved_settings.BOT_WEBHOOK_PATH or "/webhook/telegram").strip()
    if not webhook_path.startswith("/"):
        webhook_path = "/" + webhook_path

    @application.post(webhook_path, tags=["telegram-webhook"])
    async def telegram_webhook_endpoint(request: Request) -> Response:
        return await _handle_telegram_webhook(request)

    if webhook_path != "/webhook/telegram":
        @application.post("/webhook/telegram", tags=["telegram-webhook"], include_in_schema=False)
        async def telegram_webhook_default_endpoint(request: Request) -> Response:
            return await _handle_telegram_webhook(request)

    configure_web_admin(application, settings=resolved_settings)
    return application


app = create_app()


def main() -> None:
    """Run the unified FastAPI + Bot server with uvicorn."""
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    uvicorn.run(
        "app.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        log_level=settings.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
