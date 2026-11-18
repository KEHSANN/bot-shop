"""Member search, user profile inspection, balance adjustment, ban/unban, and DM routes."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.constants import Currency
from app.core.exceptions import ValidationError
from app.core.security import escape_html, validate_receipt_image
from app.locales.i18n import TranslationService
from app.services.dtos import BalanceAdjustmentDTO, OrderDTO, PaymentDTO
from app.services.interfaces import UserServiceProtocol
from app.web_admin.deps import (
    AdminPrincipal,
    BotNotifierProtocol,
    get_app_settings,
    get_bot_notifier,
    get_payment_service,
    get_shop_service,
    get_translation_service,
    get_user_service,
    parse_decimal_strict,
    prefers_json,
    render_admin_template,
    require_admin_csrf,
    try_resolve_optional_service,
)

router = APIRouter(tags=["admin-users"])


async def _try_load_user_payments(request: Request, user_id: int) -> list[PaymentDTO]:
    svc = await try_resolve_optional_service(request, get_payment_service)
    if svc is None:
        return []
    return await svc.list_payments(user_id=user_id, limit=50)


async def _try_load_user_orders(request: Request, user_id: int) -> list[OrderDTO]:
    svc = await try_resolve_optional_service(request, get_shop_service)
    if svc is None:
        return []
    return await svc.list_user_orders(user_id, limit=50)


@router.get("/admin/users")
@router.get("/admin/api/users")
async def users_list_view(
    request: Request,
    q: str = "",
    blocked_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    user_service: UserServiceProtocol = Depends(get_user_service),
) -> Response:
    """List or search members by numeric Telegram ID, @username, or first/last name."""
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)
    query_str = q.strip()
    if query_str:
        users = await user_service.search_users(query_str, limit=safe_limit, offset=safe_offset)
    else:
        users = await user_service.list_users(
            limit=safe_limit,
            offset=safe_offset,
            blocked_only=blocked_only,
        )

    if prefers_json(request):
        return JSONResponse(
            {
                "query": query_str,
                "blocked_only": blocked_only,
                "users": [u.model_dump(mode="json") for u in users],
            }
        )

    return render_admin_template(
        request,
        "users.html",
        {
            "active_nav": "users",
            "users": users,
            "search_query": query_str,
            "blocked_only": blocked_only,
            "limit": safe_limit,
            "offset": safe_offset,
        },
        admin=admin,
    )


@router.get("/admin/users/{user_id}")
@router.get("/admin/api/users/{user_id}")
async def user_detail_view(
    request: Request,
    user_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    user_service: UserServiceProtocol = Depends(get_user_service),
) -> Response:
    """Inspect full user profile, balances, ledger history, deposits, and orders."""
    user = await user_service.get_by_id(user_id)
    ledger = await user_service.get_wallet_history(user_id, limit=50)
    payments = await _try_load_user_payments(request, user_id)
    orders = await _try_load_user_orders(request, user_id)

    if prefers_json(request):
        return JSONResponse(
            {
                "user": user.model_dump(mode="json"),
                "ledger": [entry.model_dump(mode="json") for entry in ledger],
                "payments": [p.model_dump(mode="json") for p in payments],
                "orders": [o.model_dump(mode="json") for o in orders],
            }
        )

    return render_admin_template(
        request,
        "user_detail.html",
        {
            "active_nav": "users",
            "member": user,
            "ledger": ledger,
            "payments": payments,
            "orders": orders,
        },
        admin=admin,
    )


@router.post("/admin/users/{user_id}/balance")
async def user_adjust_balance(
    request: Request,
    user_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    user_service: UserServiceProtocol = Depends(get_user_service),
    translation_service: TranslationService = Depends(get_translation_service),
    bot_notifier: BotNotifierProtocol = Depends(get_bot_notifier),
) -> Response:
    """Atomically charge or deduct a user's USD or IRT balance and notify the user."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)

    raw_currency = str(payload.get("currency") or "USD").strip().upper()
    if raw_currency not in {"USD", "IRT"}:
        raise ValidationError("Currency must be USD or IRT")
    currency = Currency(raw_currency)

    action = str(payload.get("action") or "charge").strip().lower()
    raw_amount = payload.get("amount")
    parsed_amount = parse_decimal_strict(
        raw_amount,
        "amount",
        allow_zero=False,
        allow_negative=True,
    )

    if action == "deduct":
        signed_amount = -abs(parsed_amount)
    elif action == "charge":
        if parsed_amount < Decimal("0"):
            signed_amount = parsed_amount
        else:
            signed_amount = abs(parsed_amount)
    else:
        raise ValidationError("Balance action must be 'charge' or 'deduct'")

    note_raw = str(payload.get("note") or "").strip()
    note = note_raw if note_raw else None

    updated_user, ledger_entry = await user_service.adjust_balance(
        BalanceAdjustmentDTO(
            user_id=user_id,
            currency=currency,
            amount=signed_amount,
            admin_id=admin.admin_id,
            note=note,
        )
    )

    msg_key = "balance.admin_credited" if signed_amount > Decimal("0") else "balance.admin_deducted"
    new_balance_val = updated_user.balance_usd if currency == Currency.USD else updated_user.balance_irt
    notify_text = await translation_service.get_text(
        msg_key,
        updated_user.language_code,
        amount=f"{abs(signed_amount)} {currency.value}",
        currency=currency.value,
        new_balance=str(new_balance_val),
        note=escape_html(note or "-"),
    )
    notified = await bot_notifier.send_direct_message(updated_user.telegram_id, notify_text)

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse(
            {
                "ok": True,
                "user": updated_user.model_dump(mode="json"),
                "ledger_entry": ledger_entry.model_dump(mode="json"),
                "notified": notified,
            }
        )

    q = urlencode({"msg": f"Balance updated ({currency.value} {signed_amount:+})"})
    return RedirectResponse(url=f"/admin/users/{user_id}?{q}", status_code=303)


@router.post("/admin/users/{user_id}/block")
async def user_toggle_block(
    request: Request,
    user_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    user_service: UserServiceProtocol = Depends(get_user_service),
) -> Response:
    """Block or unblock a specific user immediately."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)

    if "blocked" in payload:
        raw_blocked = payload["blocked"]
        if isinstance(raw_blocked, bool):
            blocked = raw_blocked
        else:
            blocked = str(raw_blocked).strip().lower() in {"1", "true", "yes", "on", "block"}
    else:
        action = str(payload.get("action") or "block").strip().lower()
        blocked = action != "unblock"

    updated_user = await user_service.set_blocked(
        user_id,
        blocked=blocked,
        admin_id=admin.admin_id,
    )

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "user": updated_user.model_dump(mode="json")})

    state_label = "blocked" if updated_user.is_blocked else "unblocked"
    q = urlencode({"msg": f"User {updated_user.telegram_id} {state_label}"})
    return RedirectResponse(url=f"/admin/users/{user_id}?{q}", status_code=303)


@router.post("/admin/users/{user_id}/message")
async def user_send_direct_message(
    request: Request,
    user_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    user_service: UserServiceProtocol = Depends(get_user_service),
    bot_notifier: BotNotifierProtocol = Depends(get_bot_notifier),
) -> Response:
    """Send a sanitized direct message (and optional validated image) to a specific user."""
    settings = get_app_settings(request)
    content_type = request.headers.get("content-type", "").lower()
    photo_bytes: bytes | None = None
    photo_filename: str | None = None

    if "application/json" in content_type:
        payload = await request.json()
        message_text = str(payload.get("message_text") or "").strip()
    else:
        form = await request.form()
        message_text = str(form.get("message_text") or "").strip()
        upload = form.get("photo")
        if isinstance(upload, (StarletteUploadFile, UploadFile)) and upload.filename:
            raw_bytes = await upload.read()
            if raw_bytes:
                validate_receipt_image(
                    raw_bytes,
                    filename=upload.filename,
                    content_type=upload.content_type,
                    max_bytes=settings.MAX_UPLOAD_SIZE_BYTES,
                )
                photo_bytes = raw_bytes
                photo_filename = upload.filename

    if not message_text and photo_bytes is None:
        raise ValidationError("Message text or photo attachment is required")

    user = await user_service.get_by_id(user_id)
    safe_html_text = escape_html(message_text)
    delivered = await bot_notifier.send_direct_message(
        user.telegram_id,
        safe_html_text,
        photo_bytes=photo_bytes,
        photo_filename=photo_filename,
    )

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse(
            {
                "ok": True,
                "user_id": user.id,
                "telegram_id": user.telegram_id,
                "delivered": delivered,
            }
        )

    q = urlencode({"msg": "Direct message sent to user"})
    return RedirectResponse(url=f"/admin/users/{user_id}?{q}", status_code=303)
