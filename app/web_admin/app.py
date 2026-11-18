"""FastAPI Web Admin application wiring, exception handlers, static assets, and router mounting."""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, get_settings
from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    DuplicateTransactionError,
    ExternalGatewayError,
    FileValidationError,
    InsufficientBalanceError,
    InsufficientStockError,
    InvalidStateTransitionError,
    NotFoundError,
    RateLimitExceededError,
    ShopBotError,
    UserBlockedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.web_admin.deps import STATIC_DIR, prefers_json
from app.web_admin.routes import (
    auth,
    broadcast,
    catalog,
    cms,
    dashboard,
    miniapp,
    orders,
    payments,
    support,
    users,
)

logger = get_logger(__name__)

_EXCEPTION_STATUS_MAP: tuple[tuple[type[ShopBotError], int], ...] = (
    (AuthenticationError, 401),
    (AuthorizationError, 403),
    (UserBlockedError, 403),
    (NotFoundError, 404),
    (DuplicateTransactionError, 409),
    (InvalidStateTransitionError, 409),
    (InsufficientStockError, 409),
    (RateLimitExceededError, 429),
    (ValidationError, 400),
    (FileValidationError, 400),
    (InsufficientBalanceError, 400),
    (ExternalGatewayError, 502),
    (ConfigurationError, 503),
)


def _resolve_status_code(exc: ShopBotError) -> int:
    for cls, code in _EXCEPTION_STATUS_MAP:
        if isinstance(exc, cls):
            return code
    return 400


class SecurityHeadersMiddleware:
    """Pure ASGI middleware attaching hardening headers without buffering request streams."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        is_miniapp = path.startswith("/miniapp") or path.startswith("/webapp")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw_headers = list(message.get("headers", []))
                existing_names = {k.lower() for k, _ in raw_headers}
                if b"x-content-type-options" not in existing_names:
                    raw_headers.append((b"x-content-type-options", b"nosniff"))
                if b"referrer-policy" not in existing_names:
                    raw_headers.append((b"referrer-policy", b"strict-origin-when-cross-origin"))
                if not is_miniapp and b"x-frame-options" not in existing_names:
                    raw_headers.append((b"x-frame-options", b"SAMEORIGIN"))
                if path.startswith("/admin") and b"cache-control" not in existing_names:
                    raw_headers.append((b"cache-control", b"no-store"))
                message["headers"] = raw_headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def configure_web_admin(app: FastAPI, settings: Settings | None = None) -> FastAPI:
    """Attach Web Admin routes, Mini App routes, optional payment webhooks, static files, and error handlers."""
    resolved_settings = settings or getattr(app.state, "settings", None) or get_settings()
    app.state.settings = resolved_settings

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    if not any(getattr(r, "name", None) == "static" for r in app.routes):
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(ShopBotError)
    async def handle_shop_bot_error(request: Request, exc: ShopBotError) -> Response:
        status_code = _resolve_status_code(exc)
        accept = request.headers.get("accept", "")
        if (
            isinstance(exc, AuthenticationError)
            and request.method == "GET"
            and "text/html" in accept
            and not prefers_json(request)
            and request.url.path.startswith("/admin")
            and request.url.path != "/admin/login"
        ):
            return RedirectResponse(url="/admin/login", status_code=303)

        return JSONResponse(
            status_code=status_code,
            content={
                "ok": False,
                "error": exc.code,
                "detail": exc.message,
                "details": exc.details,
            },
        )

    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(users.router)
    app.include_router(catalog.router)
    app.include_router(payments.router)
    app.include_router(orders.router)
    app.include_router(support.router)
    app.include_router(cms.router)
    app.include_router(broadcast.router)
    app.include_router(miniapp.router)

    try:
        from app.web_admin.webhooks import router as webhooks_router

        app.include_router(webhooks_router)
    except ImportError:
        logger.debug("Optional payment webhooks module not present yet; skipping mount")

    return app


def create_web_admin_app(settings: Settings | None = None) -> FastAPI:
    """Create a standalone FastAPI instance configured with Web Admin and Mini App routes."""
    resolved_settings = settings or get_settings()
    app = FastAPI(
        title=resolved_settings.APP_NAME,
        debug=resolved_settings.DEBUG,
    )
    return configure_web_admin(app, settings=resolved_settings)
