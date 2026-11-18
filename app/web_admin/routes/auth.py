"""Web Admin authentication routes: Username/Bcrypt, TOTP 2FA, Telegram Login Widget, and CSRF."""

from __future__ import annotations

import asyncio
import hmac
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import Settings
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    generate_csrf_token,
    verify_password,
    verify_telegram_login_widget,
    verify_totp_code,
)
from app.web_admin.deps import (
    ADMIN_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    AdminPrincipal,
    attach_auth_cookies,
    get_app_settings,
    get_or_init_csrf_sid,
    get_user_service,
    is_request_https,
    is_totp_required,
    prefers_json,
    render_admin_template,
    require_admin_csrf,
    require_prelogin_csrf,
    resolve_totp_secret,
    try_get_current_admin,
    try_resolve_optional_service,
)

logger = get_logger(__name__)

router = APIRouter(tags=["admin-auth"])


def _issue_admin_session(
    request: Request,
    settings: Settings,
    *,
    sub: str,
    username: str,
    admin_id: int,
    auth_method: str,
) -> tuple[str, str, str]:
    """Generate fresh CSRF session ID, signed JWT access token, and HMAC CSRF token."""
    csrf_sid = hmac.new(
        settings.effective_csrf_secret.encode("utf-8"),
        f"{sub}:{os_urandom_hex()}".encode("utf-8"),
        "sha256",
    ).hexdigest()[:32]
    access_token = create_access_token(
        subject=sub,
        secret_key=settings.JWT_SECRET_KEY,
        expires_minutes=settings.JWT_EXPIRE_MINUTES,
        algorithm=settings.JWT_ALGORITHM,
        extra_claims={
            "role": "admin",
            "username": username,
            "admin_id": admin_id,
            "auth_method": auth_method,
            "csrf_sid": csrf_sid,
        },
    )
    csrf_token = generate_csrf_token(settings.effective_csrf_secret, csrf_sid)
    return access_token, csrf_sid, csrf_token


def os_urandom_hex() -> str:
    import secrets

    return secrets.token_hex(16)


@router.get("/", include_in_schema=False)
async def root_redirect(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> RedirectResponse:
    """Redirect root URL to Admin Dashboard or Login screen."""
    admin = try_get_current_admin(request, settings)
    target = "/admin/" if admin is not None else "/admin/login"
    return RedirectResponse(url=target, status_code=303)


@router.get("/admin/auth/csrf")
async def get_csrf_token_endpoint(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> Response:
    """Issue a session-bound HMAC CSRF token for API clients and test harnesses."""
    admin = try_get_current_admin(request, settings)
    csrf_sid = get_or_init_csrf_sid(request, admin)
    csrf_token = generate_csrf_token(settings.effective_csrf_secret, csrf_sid)
    response = JSONResponse({"csrf_token": csrf_token})
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


@router.get("/admin/login")
async def login_page(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> Response:
    """Render Web Admin login page with CSRF protection, optional 2FA, and Telegram Login Widget."""
    admin = try_get_current_admin(request, settings)
    if admin is not None:
        return RedirectResponse(url="/admin/", status_code=303)

    return render_admin_template(
        request,
        "login.html",
        {
            "totp_required": is_totp_required(settings),
            "telegram_login_enabled": bool(settings.TELEGRAM_LOGIN_WIDGET_ENABLED),
            "bot_username": settings.TELEGRAM_BOT_USERNAME,
        },
    )


async def _authenticate_credentials(
    *,
    username: str,
    password: str,
    totp_code: str,
    settings: Settings,
) -> tuple[str, int]:
    """Validate admin username, bcrypt password hash, and optional TOTP 2FA code."""
    expected_user = settings.WEB_ADMIN_USERNAME.strip()
    provided_user = username.strip()
    user_ok = hmac.compare_digest(provided_user.encode("utf-8"), expected_user.encode("utf-8"))
    pwd_ok = await asyncio.to_thread(verify_password, password, settings.WEB_ADMIN_PASSWORD_HASH)

    if not (user_ok and pwd_ok):
        raise AuthenticationError("Invalid admin username or password")

    if is_totp_required(settings):
        totp_secret = resolve_totp_secret(settings)
        if not totp_secret or not verify_totp_code(totp_secret, totp_code.strip()):
            raise AuthenticationError("Invalid or missing 2FA TOTP verification code")

    default_admin_id = settings.ADMIN_TELEGRAM_IDS[0] if settings.ADMIN_TELEGRAM_IDS else 1
    return expected_user, default_admin_id


@router.post("/admin/login", dependencies=[Depends(require_prelogin_csrf)])
async def login_submit(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> Response:
    """Handle HTML form or JSON admin login submission."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        body = await request.json()
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        totp_code = str(body.get("totp_code") or "")
    else:
        form = await request.form()
        username = str(form.get("username") or "")
        password = str(form.get("password") or "")
        totp_code = str(form.get("totp_code") or "")

    try:
        verified_user, admin_id = await _authenticate_credentials(
            username=username,
            password=password,
            totp_code=totp_code,
            settings=settings,
        )
    except AuthenticationError as exc:
        if prefers_json(request) or "application/json" in content_type:
            raise
        return render_admin_template(
            request,
            "login.html",
            {
                "error": exc.message,
                "totp_required": is_totp_required(settings),
                "telegram_login_enabled": bool(settings.TELEGRAM_LOGIN_WIDGET_ENABLED),
                "bot_username": settings.TELEGRAM_BOT_USERNAME,
            },
            status_code=401,
        )

    access_token, csrf_sid, csrf_token = _issue_admin_session(
        request,
        settings,
        sub=f"admin:{verified_user}",
        username=verified_user,
        admin_id=admin_id,
        auth_method="password",
    )

    if prefers_json(request) or "application/json" in content_type:
        resp: Response = JSONResponse(
            {
                "ok": True,
                "access_token": access_token,
                "token_type": "bearer",
                "csrf_token": csrf_token,
                "username": verified_user,
            }
        )
    else:
        resp = RedirectResponse(url="/admin/", status_code=303)

    attach_auth_cookies(
        resp,
        request,
        access_token=access_token,
        csrf_sid=csrf_sid,
        expires_minutes=settings.JWT_EXPIRE_MINUTES,
    )
    return resp


@router.post("/admin/api/auth/login", dependencies=[Depends(require_prelogin_csrf)])
async def api_login_submit(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> Response:
    """JSON API login endpoint for programmatic admin clients."""
    return await login_submit(request=request, settings=settings)


async def _verify_telegram_admin_payload(
    request: Request,
    auth_data: dict[str, Any],
    settings: Settings,
) -> tuple[int, str]:
    """Validate Telegram Login Widget HMAC and verify admin authorization."""
    if not settings.TELEGRAM_LOGIN_WIDGET_ENABLED:
        raise AuthorizationError("Telegram Login Widget authentication is disabled")
    if not verify_telegram_login_widget(auth_data, settings.BOT_TOKEN):
        raise AuthenticationError("Invalid or expired Telegram Login Widget signature")

    try:
        telegram_id = int(auth_data["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("Invalid Telegram user ID in widget payload") from exc

    is_authorized = telegram_id in settings.ADMIN_TELEGRAM_IDS
    if not is_authorized:
        user_svc = await try_resolve_optional_service(request, get_user_service, settings=settings)
        if user_svc is not None:
            db_user = await user_svc.get_by_telegram_id(telegram_id)
            if db_user is not None and db_user.is_admin and not db_user.is_blocked:
                is_authorized = True

    if not is_authorized:
        raise AuthorizationError("Telegram account is not authorized as an administrator")

    username = str(auth_data.get("username") or auth_data.get("first_name") or f"tg_{telegram_id}")
    return telegram_id, username


@router.get("/admin/auth/telegram")
async def telegram_login_widget_get(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> Response:
    """Handle Telegram Login Widget GET redirect callback."""
    auth_data = dict(request.query_params)
    try:
        telegram_id, username = await _verify_telegram_admin_payload(request, auth_data, settings)
    except (AuthenticationError, AuthorizationError) as exc:
        if prefers_json(request):
            raise
        query = urlencode({"err": exc.message})
        return RedirectResponse(url=f"/admin/login?{query}", status_code=303)

    access_token, csrf_sid, csrf_token = _issue_admin_session(
        request,
        settings,
        sub=f"tg:{telegram_id}",
        username=username,
        admin_id=telegram_id,
        auth_method="telegram_widget",
    )

    if prefers_json(request):
        resp: Response = JSONResponse(
            {
                "ok": True,
                "access_token": access_token,
                "token_type": "bearer",
                "csrf_token": csrf_token,
                "telegram_id": telegram_id,
                "username": username,
            }
        )
    else:
        resp = RedirectResponse(url="/admin/", status_code=303)

    attach_auth_cookies(
        resp,
        request,
        access_token=access_token,
        csrf_sid=csrf_sid,
        expires_minutes=settings.JWT_EXPIRE_MINUTES,
    )
    return resp


@router.post("/admin/auth/telegram")
async def telegram_login_widget_post(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> Response:
    """Handle Telegram Login Widget POST callback."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        raw = await request.json()
        auth_data = dict(raw) if isinstance(raw, dict) else {}
    else:
        form = await request.form()
        auth_data = {k: str(v) for k, v in form.items()}

    telegram_id, username = await _verify_telegram_admin_payload(request, auth_data, settings)
    access_token, csrf_sid, csrf_token = _issue_admin_session(
        request,
        settings,
        sub=f"tg:{telegram_id}",
        username=username,
        admin_id=telegram_id,
        auth_method="telegram_widget",
    )

    resp: Response
    if prefers_json(request) or "application/json" in content_type:
        resp = JSONResponse(
            {
                "ok": True,
                "access_token": access_token,
                "token_type": "bearer",
                "csrf_token": csrf_token,
                "telegram_id": telegram_id,
                "username": username,
            }
        )
    else:
        resp = RedirectResponse(url="/admin/", status_code=303)

    attach_auth_cookies(
        resp,
        request,
        access_token=access_token,
        csrf_sid=csrf_sid,
        expires_minutes=settings.JWT_EXPIRE_MINUTES,
    )
    return resp


@router.post("/admin/logout")
async def logout_submit(
    request: Request,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
) -> Response:
    """Clear admin JWT and CSRF cookies on logout."""
    if prefers_json(request):
        resp: Response = JSONResponse({"ok": True})
    else:
        resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(key=ADMIN_COOKIE_NAME, path="/")
    resp.delete_cookie(key=CSRF_COOKIE_NAME, path="/")
    return resp
