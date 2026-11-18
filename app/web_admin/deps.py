"""FastAPI dependencies, JWT/TOTP/Telegram authentication, CSRF validation, and service wiring."""

from __future__ import annotations

import hmac
import importlib
import inspect
import json
import os
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import Depends, Request, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.constants import (
    COLOR_EMOJI_CHOICES,
    COLOR_PALETTE,
    DEFAULT_LANGUAGE,
    LANGUAGE_ORDER,
    SUPPORTED_LANGUAGES,
    is_valid_hex_color,
    normalize_hex_color,
)
from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    UserBlockedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import (
    escape_html,
    generate_csrf_token,
    sanitize_username,
    verify_access_token,
    verify_csrf_token,
    verify_telegram_webapp_init_data,
)
from app.database.session import get_session_factory
from app.locales.i18n import TranslationService
from app.services.dtos import UserCreateOrUpdateDTO, UserDTO
from app.services.interfaces import (
    AnalyticsServiceProtocol,
    BroadcastServiceProtocol,
    CMSServiceProtocol,
    PaymentServiceProtocol,
    ShopServiceProtocol,
    SupportServiceProtocol,
    UserServiceProtocol,
)

logger = get_logger(__name__)

ADMIN_COOKIE_NAME = "shopbot_admin_token"
CSRF_COOKIE_NAME = "shopbot_csrf_sid"
CSRF_HEADER_NAME = "X-CSRF-Token"
MINIAPP_HEADER_NAME = "X-Telegram-Init-Data"

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class AdminPrincipal(BaseModel):
    """Authenticated Web Admin identity extracted from verified JWT claims."""

    sub: str
    username: str
    admin_id: int
    auth_method: str = "password"
    csrf_sid: str


class MiniAppPrincipal(BaseModel):
    """Authenticated Telegram Mini App customer identity."""

    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str = DEFAULT_LANGUAGE
    user: UserDTO | None = None


@runtime_checkable
class BotNotifierProtocol(Protocol):
    """Protocol for outbound Telegram user notifications triggered from Web Admin actions."""

    async def send_direct_message(
        self,
        telegram_id: int,
        text: str,
        *,
        photo_bytes: bytes | None = None,
        photo_filename: str | None = None,
    ) -> bool: ...


class DefaultBotNotifier:
    """Delivers Telegram messages via the runtime aiogram Bot instance when available."""

    def __init__(self, bot: Any | None) -> None:
        self._bot = bot

    async def send_direct_message(
        self,
        telegram_id: int,
        text: str,
        *,
        photo_bytes: bytes | None = None,
        photo_filename: str | None = None,
    ) -> bool:
        if self._bot is None:
            logger.info("Bot instance not active; skipping Telegram notification to %s", telegram_id)
            return False
        try:
            if photo_bytes is not None:
                from aiogram.types import BufferedInputFile

                input_file = BufferedInputFile(photo_bytes, filename=photo_filename or "image.jpg")
                await self._bot.send_photo(chat_id=telegram_id, photo=input_file, caption=text)
            else:
                await self._bot.send_message(chat_id=telegram_id, text=text)
            return True
        except Exception as exc:
            logger.warning("Failed to deliver Telegram message to %s: %s", telegram_id, exc)
            return False


# ---------------------------------------------------------------------------
# Jinja2 Auto-Escaped Template Environment & Formatting Helpers
# ---------------------------------------------------------------------------
def fmt_usd(value: Decimal | str | int | None) -> str:
    """Format a Decimal USD value cleanly without float conversion."""
    if value is None:
        return "0.00"
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
        quantized = dec.quantize(Decimal("0.01"))
        return f"{quantized:,.2f}"
    except (InvalidOperation, ValueError):
        return "0.00"


def fmt_irt(value: Decimal | str | int | None) -> str:
    """Format a Decimal Iranian Toman (IRT) value with comma separators."""
    if value is None:
        return "0"
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
        quantized = dec.quantize(Decimal("1"))
        return f"{int(quantized):,}"
    except (InvalidOperation, ValueError):
        return "0"


def contrast_text_color(hex_code: str | None) -> str:
    """Compute WCAG high-contrast text color (#ffffff or #111827) for a #RRGGBB background."""
    if not hex_code or not is_valid_hex_color(hex_code):
        return "#ffffff"
    cleaned = hex_code.strip().lstrip("#")
    r = int(cleaned[0:2], 16)
    g = int(cleaned[2:4], 16)
    b = int(cleaned[4:6], 16)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return "#111827" if luminance > 0.62 else "#ffffff"


def hex_to_rgb_string(hex_code: str | None) -> str:
    """Convert #RRGGBB hex string to rgb(r, g, b) notation."""
    if not hex_code or not is_valid_hex_color(hex_code):
        return "rgb(59, 130, 246)"
    cleaned = hex_code.strip().lstrip("#")
    r = int(cleaned[0:2], 16)
    g = int(cleaned[2:4], 16)
    b = int(cleaned[4:6], 16)
    return f"rgb({r}, {g}, {b})"


def create_jinja_templates() -> Jinja2Templates:
    """Create Jinja2Templates instance with strict autoescape enabled on all templates."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(
            enabled_extensions=("html", "htm", "xml", "j2"),
            default_for_string=True,
            default=True,
        ),
    )
    env.filters["escape_html"] = escape_html
    env.filters["sanitize_username"] = sanitize_username
    env.filters["fmt_usd"] = fmt_usd
    env.filters["fmt_irt"] = fmt_irt
    env.filters["contrast_text"] = contrast_text_color
    env.filters["hex_to_rgb"] = hex_to_rgb_string

    env.globals["SUPPORTED_LANGUAGES"] = SUPPORTED_LANGUAGES
    env.globals["LANGUAGE_ORDER"] = LANGUAGE_ORDER
    env.globals["COLOR_PALETTE"] = COLOR_PALETTE
    env.globals["COLOR_EMOJI_CHOICES"] = COLOR_EMOJI_CHOICES
    env.globals["contrast_text_color"] = contrast_text_color
    env.globals["hex_to_rgb_string"] = hex_to_rgb_string

    return Jinja2Templates(env=env)


templates = create_jinja_templates()


def is_request_https(request: Request) -> bool:
    """Return True if the request arrived over HTTPS directly or via reverse proxy."""
    if request.url.scheme == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return forwarded_proto.split(",")[0].strip().lower() == "https"


def prefers_json(request: Request) -> bool:
    """Return True when the client is an API caller or explicitly prefers JSON over HTML."""
    if (
        request.url.path.startswith("/admin/api/")
        or request.url.path.startswith("/miniapp/api/")
        or request.url.path.startswith("/webapp/api/")
    ):
        return True
    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return True
    if "text/html" not in accept and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if (
            request.headers.get(CSRF_HEADER_NAME)
            or request.headers.get("x-csrf-token")
            or request.headers.get("x-csrftoken")
            or request.headers.get("authorization", "").lower().startswith("bearer ")
        ):
            return True
    return False


def parse_decimal_strict(
    raw: str | Decimal | int | None,
    field_name: str,
    *,
    allow_zero: bool = False,
    allow_negative: bool = False,
) -> Decimal:
    """Parse a string or numeric value into a finite Decimal without float intermediates."""
    if raw is None or isinstance(raw, float):
        raise ValidationError(f"Invalid decimal value for {field_name}")
    cleaned = str(raw).strip().replace(",", "")
    if not cleaned:
        raise ValidationError(f"{field_name} is required")
    try:
        dec = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a valid decimal number") from exc
    if not dec.is_finite():
        raise ValidationError(f"{field_name} must be finite")
    if not allow_negative and dec < Decimal("0"):
        raise ValidationError(f"{field_name} cannot be negative")
    if not allow_zero and dec == Decimal("0"):
        raise ValidationError(f"{field_name} must be non-zero")
    return dec


def parse_color_hex_strict(raw: str | None, *, field_name: str = "color_hex") -> str:
    """Validate and normalize a #RRGGBB color hex string or raise ValidationError."""
    if raw is None or not str(raw).strip():
        raise ValidationError(f"{field_name} is required")
    try:
        return normalize_hex_color(str(raw).strip())
    except ValueError as exc:
        raise ValidationError(f"Invalid {field_name}: must match #RRGGBB format") from exc


def parse_color_emoji_strict(raw: str | None, *, default: str = "🔵") -> str:
    """Validate a color indicator emoji/badge string against injection characters."""
    if raw is None or not str(raw).strip():
        return default
    cleaned = str(raw).strip()
    if len(cleaned) > 16 or any(ch in cleaned for ch in ("<", ">", '"', "'", "&", ";")):
        raise ValidationError("Invalid color_emoji value")
    return cleaned


# ---------------------------------------------------------------------------
# Runtime Core & Domain Service Dependency Providers
# ---------------------------------------------------------------------------
def get_app_settings(request: Request) -> Settings:
    """Return runtime Settings attached to app.state or global cached settings."""
    state_settings = getattr(request.app.state, "settings", None)
    if isinstance(state_settings, Settings):
        return state_settings
    return get_settings()


def get_session_factory_dep(request: Request) -> async_sessionmaker[AsyncSession]:
    """Return the active async_sessionmaker from app.state or module session."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is not None:
        return factory
    return get_session_factory()


def get_redis_dep(request: Request) -> Redis | None:
    """Return the active async Redis client from app.state if available."""
    return getattr(request.app.state, "redis", None)


def get_translation_service(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory_dep),
    redis_client: Redis | None = Depends(get_redis_dep),
) -> TranslationService:
    """Return the TranslationService instance."""
    existing = getattr(request.app.state, "translation_service", None) or getattr(
        request.app.state, "i18n", None
    )
    if isinstance(existing, TranslationService):
        request.app.state.translation_service = existing
        request.app.state.i18n = existing
        return existing
    svc = TranslationService(session_factory=session_factory, redis_client=redis_client)
    request.app.state.translation_service = svc
    request.app.state.i18n = svc
    return svc


def get_bot_notifier(request: Request) -> BotNotifierProtocol:
    """Return BotNotifierProtocol bound to app.state.bot or custom notifier override."""
    override = getattr(request.app.state, "bot_notifier", None)
    if override is not None:
        return override
    bot = getattr(request.app.state, "bot", None)
    return DefaultBotNotifier(bot)


def _instantiate_domain_service(
    cls: type[Any],
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis | None,
    translation_service: TranslationService,
    bot: Any | None,
) -> Any:
    """Instantiate a Wave 2 domain service class by inspecting its constructor parameters."""
    sig = inspect.signature(cls.__init__)
    kwargs: dict[str, Any] = {}
    positional: list[Any] = []

    param_mapping: dict[str, Any] = {
        "session_factory": session_factory,
        "async_session_factory": session_factory,
        "session_maker": session_factory,
        "db_session_factory": session_factory,
        "redis": redis_client,
        "redis_client": redis_client,
        "settings": settings,
        "config": settings,
        "app_settings": settings,
        "encryption_key": settings.ENCRYPTION_KEY,
        "fernet_key": settings.ENCRYPTION_KEY,
        "translation_service": translation_service,
        "i18n": translation_service,
        "translations": translation_service,
        "bot": bot,
    }

    params = [p for name, p in sig.parameters.items() if name != "self"]
    has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)

    for idx, param in enumerate(params):
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if param.name in param_mapping:
            val = param_mapping[param.name]
            if val is not None or param.default is inspect.Parameter.empty:
                if param.kind == inspect.Parameter.POSITIONAL_ONLY:
                    positional.append(val)
                else:
                    kwargs[param.name] = val
        elif idx == 0 and param.default is inspect.Parameter.empty:
            if param.kind == inspect.Parameter.POSITIONAL_ONLY:
                positional.append(session_factory)
            else:
                kwargs[param.name] = session_factory
        elif has_var_keyword:
            continue

    return cls(*positional, **kwargs)


def _resolve_domain_service(
    request: Request,
    *,
    state_attr: str,
    module_path: str,
    class_name: str,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis | None,
    translation_service: TranslationService,
) -> Any:
    """Load and cache a Wave 2 service from app.state or import its implementation module."""
    existing = getattr(request.app.state, state_attr, None)
    if existing is not None:
        return existing

    services_bag = getattr(request.app.state, "services", None)
    if isinstance(services_bag, dict) and state_attr in services_bag:
        return services_bag[state_attr]

    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigurationError(
            f"Service '{class_name}' ({module_path}) is not wired or merged yet"
        ) from exc

    bot = getattr(request.app.state, "bot", None)
    instance = _instantiate_domain_service(
        cls,
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
        translation_service=translation_service,
        bot=bot,
    )
    setattr(request.app.state, state_attr, instance)
    return instance


def get_user_service(
    request: Request,
    settings: Settings = Depends(get_app_settings),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory_dep),
    redis_client: Redis | None = Depends(get_redis_dep),
    translation_service: TranslationService = Depends(get_translation_service),
) -> UserServiceProtocol:
    """Provide UserServiceProtocol implementation."""
    return _resolve_domain_service(
        request,
        state_attr="user_service",
        module_path="app.services.user_service",
        class_name="UserService",
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
        translation_service=translation_service,
    )


def get_shop_service(
    request: Request,
    settings: Settings = Depends(get_app_settings),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory_dep),
    redis_client: Redis | None = Depends(get_redis_dep),
    translation_service: TranslationService = Depends(get_translation_service),
) -> ShopServiceProtocol:
    """Provide ShopServiceProtocol implementation."""
    return _resolve_domain_service(
        request,
        state_attr="shop_service",
        module_path="app.services.shop_service",
        class_name="ShopService",
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
        translation_service=translation_service,
    )


def get_payment_service(
    request: Request,
    settings: Settings = Depends(get_app_settings),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory_dep),
    redis_client: Redis | None = Depends(get_redis_dep),
    translation_service: TranslationService = Depends(get_translation_service),
) -> PaymentServiceProtocol:
    """Provide PaymentServiceProtocol implementation."""
    return _resolve_domain_service(
        request,
        state_attr="payment_service",
        module_path="app.services.payment_service",
        class_name="PaymentService",
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
        translation_service=translation_service,
    )


def get_support_service(
    request: Request,
    settings: Settings = Depends(get_app_settings),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory_dep),
    redis_client: Redis | None = Depends(get_redis_dep),
    translation_service: TranslationService = Depends(get_translation_service),
) -> SupportServiceProtocol:
    """Provide SupportServiceProtocol implementation."""
    return _resolve_domain_service(
        request,
        state_attr="support_service",
        module_path="app.services.support_service",
        class_name="SupportService",
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
        translation_service=translation_service,
    )


def get_cms_service(
    request: Request,
    settings: Settings = Depends(get_app_settings),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory_dep),
    redis_client: Redis | None = Depends(get_redis_dep),
    translation_service: TranslationService = Depends(get_translation_service),
) -> CMSServiceProtocol:
    """Provide CMSServiceProtocol implementation."""
    return _resolve_domain_service(
        request,
        state_attr="cms_service",
        module_path="app.services.cms_service",
        class_name="CMSService",
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
        translation_service=translation_service,
    )


def get_broadcast_service(
    request: Request,
    settings: Settings = Depends(get_app_settings),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory_dep),
    redis_client: Redis | None = Depends(get_redis_dep),
    translation_service: TranslationService = Depends(get_translation_service),
) -> BroadcastServiceProtocol:
    """Provide BroadcastServiceProtocol implementation."""
    return _resolve_domain_service(
        request,
        state_attr="broadcast_service",
        module_path="app.services.broadcast_service",
        class_name="BroadcastService",
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
        translation_service=translation_service,
    )


def get_analytics_service(
    request: Request,
    settings: Settings = Depends(get_app_settings),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory_dep),
    redis_client: Redis | None = Depends(get_redis_dep),
    translation_service: TranslationService = Depends(get_translation_service),
) -> AnalyticsServiceProtocol:
    """Provide AnalyticsServiceProtocol implementation."""
    return _resolve_domain_service(
        request,
        state_attr="analytics_service",
        module_path="app.services.analytics_service",
        class_name="AnalyticsService",
        settings=settings,
        session_factory=session_factory,
        redis_client=redis_client,
        translation_service=translation_service,
    )


async def try_resolve_optional_service(
    request: Request,
    dep_fn: Any,
    settings: Settings | None = None,
) -> Any | None:
    """Resolve an optional dependency (respecting app.dependency_overrides) or return None if unmerged."""
    override = request.app.dependency_overrides.get(dep_fn)
    if override is not None:
        res = override()
        if inspect.isawaitable(res):
            return await res
        return res

    resolved_settings = settings or get_app_settings(request)
    session_factory = get_session_factory_dep(request)
    redis_client = get_redis_dep(request)
    i18n = get_translation_service(request, session_factory, redis_client)
    try:
        return dep_fn(
            request=request,
            settings=resolved_settings,
            session_factory=session_factory,
            redis_client=redis_client,
            translation_service=i18n,
        )
    except ConfigurationError:
        return None


# ---------------------------------------------------------------------------
# JWT Cookie + Bearer Auth & HMAC CSRF Protection
# ---------------------------------------------------------------------------
def extract_token_from_request(request: Request) -> tuple[str | None, str]:
    """Extract JWT from Authorization Bearer header or HttpOnly cookie."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token, "bearer"
    cookie_token = request.cookies.get(ADMIN_COOKIE_NAME)
    if cookie_token:
        return cookie_token.strip(), "cookie"
    return None, "none"


def try_get_current_admin(request: Request, settings: Settings) -> AdminPrincipal | None:
    """Attempt to authenticate the current admin without raising on unauthenticated requests."""
    cached = getattr(request.state, "admin", None)
    if isinstance(cached, AdminPrincipal):
        return cached
    token, source = extract_token_from_request(request)
    if not token:
        return None
    try:
        claims = verify_access_token(
            token,
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
    except AuthenticationError:
        return None
    if claims.get("role") != "admin":
        return None

    sub = str(claims["sub"])
    username = str(claims.get("username") or sub)
    default_admin_id = settings.ADMIN_TELEGRAM_IDS[0] if settings.ADMIN_TELEGRAM_IDS else 1
    admin_id = int(claims.get("admin_id") or default_admin_id)
    csrf_sid = str(claims.get("csrf_sid") or sub)
    principal = AdminPrincipal(
        sub=sub,
        username=username,
        admin_id=admin_id,
        auth_method=str(claims.get("auth_method") or source),
        csrf_sid=csrf_sid,
    )
    request.state.admin = principal
    return principal


async def get_current_admin(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> AdminPrincipal:
    """Require a valid JWT admin session via Bearer header or HttpOnly cookie."""
    principal = try_get_current_admin(request, settings)
    if principal is None:
        raise AuthenticationError("Admin authentication required")
    return principal


def resolve_totp_secret(settings: Settings) -> str:
    """Return configured TOTP secret from Settings or WEB_ADMIN_TOTP_SECRET env alias."""
    secret = (
        settings.TOTP_SECRET
        or getattr(settings, "WEB_ADMIN_TOTP_SECRET", "")
        or os.getenv("WEB_ADMIN_TOTP_SECRET", "")
    )
    return secret.strip()


def is_totp_required(settings: Settings) -> bool:
    """Return True when 2FA TOTP verification is active for Web Admin login."""
    return bool(settings.TWO_FACTOR_ENABLED or resolve_totp_secret(settings))


def get_or_init_csrf_sid(request: Request, admin: AdminPrincipal | None = None) -> str:
    """Resolve the session ID used to bind HMAC CSRF tokens."""
    if admin is not None and admin.csrf_sid:
        request.state.csrf_sid = admin.csrf_sid
        return admin.csrf_sid
    existing = getattr(request.state, "csrf_sid", None)
    if existing:
        return str(existing)
    cookie_sid = request.cookies.get(CSRF_COOKIE_NAME, "").strip()
    if cookie_sid and len(cookie_sid) >= 16:
        request.state.csrf_sid = cookie_sid
        return cookie_sid
    fresh_sid = secrets.token_hex(16)
    request.state.csrf_sid = fresh_sid
    request.state.csrf_sid_is_new = True
    return fresh_sid


def issue_csrf_token(request: Request, settings: Settings, admin: AdminPrincipal | None = None) -> str:
    """Generate an HMAC CSRF token bound to the current admin or pre-login session."""
    sid = get_or_init_csrf_sid(request, admin)
    return generate_csrf_token(settings.effective_csrf_secret, sid)


async def extract_submitted_csrf_token(request: Request) -> str:
    """Extract CSRF token from X-CSRF-Token header, form field, or JSON payload."""
    header_token = (
        request.headers.get(CSRF_HEADER_NAME)
        or request.headers.get("x-csrf-token")
        or request.headers.get("x-csrftoken")
    )
    if header_token and header_token.strip():
        return header_token.strip()

    content_type = request.headers.get("content-type", "").lower()
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        val = form.get("csrf_token")
        if isinstance(val, str) and val.strip():
            return val.strip()
    elif "application/json" in content_type:
        raw_body = await request.body()
        if raw_body:
            try:
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict) and isinstance(parsed.get("csrf_token"), str):
                    return parsed["csrf_token"].strip()
            except Exception as exc:
                logger.debug("Ignoring non-JSON body while checking CSRF field: %s", exc)
                return ""
    return ""


async def verify_request_csrf(
    request: Request,
    settings: Settings,
    admin: AdminPrincipal | None = None,
) -> None:
    """Validate HMAC CSRF token on state-changing HTTP methods."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return

    provided_token = await extract_submitted_csrf_token(request)
    if not provided_token:
        raise AuthorizationError("Missing CSRF token", code="csrf_missing")

    candidate_sids: list[str] = []
    if admin is not None and admin.csrf_sid:
        candidate_sids.append(admin.csrf_sid)
    cookie_sid = request.cookies.get(CSRF_COOKIE_NAME, "").strip()
    if cookie_sid and cookie_sid not in candidate_sids:
        candidate_sids.append(cookie_sid)
    if admin is not None and admin.sub not in candidate_sids:
        candidate_sids.append(admin.sub)

    if not candidate_sids:
        raise AuthorizationError("Missing CSRF session binding", code="csrf_session_missing")

    secret = settings.effective_csrf_secret
    if not any(verify_csrf_token(provided_token, secret, sid) for sid in candidate_sids):
        raise AuthorizationError("Invalid or expired CSRF token", code="csrf_invalid")


async def require_admin_csrf(
    request: Request,
    admin: AdminPrincipal = Depends(get_current_admin),
    settings: Settings = Depends(get_app_settings),
) -> AdminPrincipal:
    """Authenticate admin and verify HMAC CSRF token on state-changing requests."""
    await verify_request_csrf(request, settings, admin=admin)
    return admin


async def require_prelogin_csrf(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> None:
    """Verify HMAC CSRF token on unauthenticated login POST requests."""
    admin = try_get_current_admin(request, settings)
    await verify_request_csrf(request, settings, admin=admin)


def attach_auth_cookies(
    response: Response,
    request: Request,
    *,
    access_token: str,
    csrf_sid: str,
    expires_minutes: int,
) -> None:
    """Set HttpOnly SameSite JWT and CSRF session cookies on response."""
    secure = is_request_https(request)
    max_age = expires_minutes * 60
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=access_token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_sid,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def render_admin_template(
    request: Request,
    template_name: str,
    context: dict[str, Any],
    *,
    admin: AdminPrincipal | None = None,
    status_code: int = 200,
) -> Response:
    """Render an auto-escaped Jinja2 template with CSRF token and layout metadata."""
    settings = get_app_settings(request)
    resolved_admin = admin or try_get_current_admin(request, settings)
    csrf_sid = get_or_init_csrf_sid(request, resolved_admin)
    csrf_token = generate_csrf_token(settings.effective_csrf_secret, csrf_sid)

    full_context: dict[str, Any] = {
        "request": request,
        "admin": resolved_admin,
        "csrf_token": csrf_token,
        "app_name": settings.APP_NAME,
        "default_language": settings.DEFAULT_LANGUAGE,
        "active_nav": context.get("active_nav", "dashboard"),
        "flash_msg": request.query_params.get("msg", ""),
        "flash_err": request.query_params.get("err", ""),
    }
    full_context.update(context)

    response = templates.TemplateResponse(
        request=request,
        name=template_name,
        context=full_context,
        status_code=status_code,
    )
    if getattr(request.state, "csrf_sid_is_new", False) or CSRF_COOKIE_NAME not in request.cookies:
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_sid,
            max_age=settings.JWT_EXPIRE_MINUTES * 60,
            httponly=True,
            samesite="lax",
            secure=is_request_https(request),
            path="/",
        )
    return response


# ---------------------------------------------------------------------------
# Telegram Mini App WebApp.initData Authentication Dependency
# ---------------------------------------------------------------------------
async def get_miniapp_principal(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> MiniAppPrincipal:
    """Verify Telegram Mini App initData HMAC-SHA256 and upsert/validate customer user."""
    init_data = (
        request.headers.get(MINIAPP_HEADER_NAME)
        or request.headers.get("x-telegram-init-data")
        or request.query_params.get("init_data", "")
    ).strip()
    if not init_data:
        raise AuthenticationError("Missing Telegram WebApp initData")

    parsed = verify_telegram_webapp_init_data(init_data, settings.BOT_TOKEN)
    tg_user = parsed.get("user")
    if not isinstance(tg_user, dict) or "id" not in tg_user:
        raise AuthenticationError("Telegram WebApp initData missing user identity")

    try:
        telegram_id = int(tg_user["id"])
    except (TypeError, ValueError) as exc:
        raise AuthenticationError("Invalid Telegram user id in initData") from exc

    raw_lang = str(tg_user.get("language_code") or settings.DEFAULT_LANGUAGE)
    lang_code = TranslationService.normalize_lang(raw_lang)
    username = str(tg_user["username"]).strip() if tg_user.get("username") else None
    first_name = str(tg_user["first_name"]).strip() if tg_user.get("first_name") else None
    last_name = str(tg_user["last_name"]).strip() if tg_user.get("last_name") else None

    user_dto: UserDTO | None = None
    svc = await try_resolve_optional_service(request, get_user_service, settings=settings)
    if svc is not None:
        user_dto = await svc.upsert_telegram_user(
            UserCreateOrUpdateDTO(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                language_code=lang_code,
            )
        )

    if user_dto is not None:
        if user_dto.is_blocked:
            raise UserBlockedError("User account is blocked")
        lang_code = TranslationService.normalize_lang(user_dto.language_code)

    return MiniAppPrincipal(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        language_code=lang_code,
        user=user_dto,
    )
