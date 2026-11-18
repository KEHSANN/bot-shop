"""Two-Way Support Tickets viewer, reply, and close routes."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.constants import SenderRole, TicketStatus
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.core.security import escape_html
from app.locales.i18n import TranslationService
from app.services.dtos import UserDTO
from app.services.interfaces import SupportServiceProtocol
from app.web_admin.deps import (
    AdminPrincipal,
    BotNotifierProtocol,
    get_bot_notifier,
    get_support_service,
    get_translation_service,
    get_user_service,
    prefers_json,
    render_admin_template,
    require_admin_csrf,
    try_resolve_optional_service,
)

logger = get_logger(__name__)
router = APIRouter(tags=["admin-support"])


async def _try_load_ticket_user(request: Request, user_id: int) -> UserDTO | None:
    svc = await try_resolve_optional_service(request, get_user_service)
    if svc is None:
        return None
    try:
        return await svc.get_by_id(user_id)
    except Exception as exc:
        logger.warning("Unable to load ticket user %s: %s", user_id, exc)
        return None


@router.get("/admin/support")
@router.get("/admin/api/support")
async def support_tickets_list_view(
    request: Request,
    status: str = "all",
    limit: int = 50,
    offset: int = 0,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    support_service: SupportServiceProtocol = Depends(get_support_service),
) -> Response:
    """List support tickets with status filter (`open`, `answered`, `closed`)."""
    status_clean = status.strip().lower()
    parsed_status: TicketStatus | None = None
    if status_clean and status_clean != "all":
        if status_clean not in {s.value for s in TicketStatus}:
            raise ValidationError(f"Invalid ticket status filter '{status}'")
        parsed_status = TicketStatus(status_clean)

    tickets = await support_service.list_tickets(
        status=parsed_status,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )

    if prefers_json(request):
        return JSONResponse(
            {
                "status_filter": status_clean,
                "tickets": [t.model_dump(mode="json") for t in tickets],
            }
        )

    return render_admin_template(
        request,
        "support.html",
        {
            "active_nav": "support",
            "status_filter": status_clean,
            "tickets": tickets,
        },
        admin=admin,
    )


@router.get("/admin/support/{ticket_id}")
@router.get("/admin/api/support/{ticket_id}")
async def support_ticket_detail_view(
    request: Request,
    ticket_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    support_service: SupportServiceProtocol = Depends(get_support_service),
) -> Response:
    """View a support ticket conversation thread."""
    ticket = await support_service.get_ticket_detail(ticket_id)
    ticket_user = await _try_load_ticket_user(request, ticket.user_id)

    if prefers_json(request):
        return JSONResponse(
            {
                "ticket": ticket.model_dump(mode="json"),
                "user": ticket_user.model_dump(mode="json") if ticket_user else None,
            }
        )

    return render_admin_template(
        request,
        "ticket_detail.html",
        {
            "active_nav": "support",
            "ticket": ticket,
            "ticket_user": ticket_user,
        },
        admin=admin,
    )


@router.post("/admin/support/{ticket_id}/reply")
async def support_ticket_reply_route(
    request: Request,
    ticket_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    support_service: SupportServiceProtocol = Depends(get_support_service),
    translation_service: TranslationService = Depends(get_translation_service),
    bot_notifier: BotNotifierProtocol = Depends(get_bot_notifier),
) -> Response:
    """Append an admin reply to a support ticket and forward it to the user on Telegram."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)

    message_text = str(payload.get("message_text") or "").strip()
    if not message_text:
        raise ValidationError("Reply message_text must not be empty")

    msg = await support_service.add_message(
        ticket_id,
        SenderRole.ADMIN,
        admin.admin_id,
        message_text=message_text,
    )

    ticket = await support_service.get_ticket_detail(ticket_id)
    ticket_user = await _try_load_ticket_user(request, ticket.user_id)
    if ticket_user is not None:
        notify_text = await translation_service.get_text(
            "support.reply_from_admin",
            ticket_user.language_code,
            ticket_number=escape_html(ticket.ticket_number),
            message=escape_html(message_text),
        )
        await bot_notifier.send_direct_message(ticket_user.telegram_id, notify_text)

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "message": msg.model_dump(mode="json")})

    q = urlencode({"msg": "Reply sent to user"})
    return RedirectResponse(url=f"/admin/support/{ticket_id}?{q}", status_code=303)


@router.post("/admin/support/{ticket_id}/close")
async def support_ticket_close_route(
    request: Request,
    ticket_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    support_service: SupportServiceProtocol = Depends(get_support_service),
    translation_service: TranslationService = Depends(get_translation_service),
    bot_notifier: BotNotifierProtocol = Depends(get_bot_notifier),
) -> Response:
    """Close a support ticket and notify the customer."""
    closed = await support_service.close_ticket(ticket_id)
    ticket_user = await _try_load_ticket_user(request, closed.user_id)
    if ticket_user is not None:
        notify_text = await translation_service.get_text(
            "support.ticket_closed",
            ticket_user.language_code,
            ticket_number=escape_html(closed.ticket_number),
        )
        await bot_notifier.send_direct_message(ticket_user.telegram_id, notify_text)

    if prefers_json(request):
        return JSONResponse({"ok": True, "ticket": closed.model_dump(mode="json")})

    q = urlencode({"msg": f"Ticket {closed.ticket_number} closed"})
    return RedirectResponse(url=f"/admin/support/{ticket_id}?{q}", status_code=303)
