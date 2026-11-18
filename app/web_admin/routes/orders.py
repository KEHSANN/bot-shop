"""Admin Orders & Manual Fulfillment Queue routes."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.constants import OrderStatus
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.core.security import escape_html
from app.locales.i18n import TranslationService
from app.services.interfaces import ShopServiceProtocol
from app.web_admin.deps import (
    AdminPrincipal,
    BotNotifierProtocol,
    get_bot_notifier,
    get_shop_service,
    get_translation_service,
    get_user_service,
    prefers_json,
    render_admin_template,
    require_admin_csrf,
    try_resolve_optional_service,
)

logger = get_logger(__name__)
router = APIRouter(tags=["admin-orders"])


@router.get("/admin/orders")
@router.get("/admin/api/orders")
async def orders_list_view(
    request: Request,
    status: str = "all",
    limit: int = 50,
    offset: int = 0,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """List customer orders with filtering for pending manual fulfillment."""
    status_clean = status.strip().lower()
    parsed_status: OrderStatus | None = None
    if status_clean and status_clean != "all":
        if status_clean not in {s.value for s in OrderStatus}:
            raise ValidationError(f"Invalid order status filter '{status}'")
        parsed_status = OrderStatus(status_clean)

    orders = await shop_service.list_orders(
        status=parsed_status,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )

    if prefers_json(request):
        return JSONResponse(
            {
                "status_filter": status_clean,
                "orders": [o.model_dump(mode="json") for o in orders],
            }
        )

    return render_admin_template(
        request,
        "orders.html",
        {
            "active_nav": "orders",
            "status_filter": status_clean,
            "orders": orders,
            "order_statuses": [s.value for s in OrderStatus],
        },
        admin=admin,
    )


@router.get("/admin/orders/{order_id}")
@router.get("/admin/api/orders/{order_id}")
async def order_detail_view(
    request: Request,
    order_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Inspect a single order."""
    order = await shop_service.get_order_detail(order_id, decrypt_items=False)
    return JSONResponse({"order": order.model_dump(mode="json")})


@router.post("/admin/orders/{order_id}/fulfill")
async def fulfill_manual_order_route(
    request: Request,
    order_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
    translation_service: TranslationService = Depends(get_translation_service),
    bot_notifier: BotNotifierProtocol = Depends(get_bot_notifier),
) -> Response:
    """Mark a pending manual fulfillment order as completed and notify the customer."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)

    fulfillment_note = str(payload.get("fulfillment_note") or payload.get("note") or "").strip() or None
    fulfilled_order = await shop_service.fulfill_manual_order(
        order_id,
        admin.admin_id,
        fulfillment_note=fulfillment_note,
    )

    user_svc = await try_resolve_optional_service(request, get_user_service)
    if user_svc is not None:
        try:
            user = await user_svc.get_by_id(fulfilled_order.user_id)
            notify_text = await translation_service.get_text(
                "shop.order_fulfilled_notify",
                user.language_code,
                order_number=escape_html(fulfilled_order.order_number),
                note=escape_html(fulfillment_note or "-"),
            )
            await bot_notifier.send_direct_message(user.telegram_id, notify_text)
        except Exception as exc:
            logger.warning("Order fulfillment notification skipped for order %s: %s", order_id, exc)

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "order": fulfilled_order.model_dump(mode="json")})

    q = urlencode({"msg": f"Order {fulfilled_order.order_number} fulfilled"})
    return RedirectResponse(url=f"/admin/orders?{q}", status_code=303)


@router.post("/admin/orders/{order_id}/refund")
async def refund_order_route(
    request: Request,
    order_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Atomically cancel an order and refund the customer's wallet balance."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)

    reason = str(payload.get("reason") or payload.get("note") or "").strip() or None
    refunded_order = await shop_service.cancel_and_refund_order(
        order_id,
        admin.admin_id,
        reason=reason,
    )

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "order": refunded_order.model_dump(mode="json")})

    q = urlencode({"msg": f"Order {refunded_order.order_number} refunded"})
    return RedirectResponse(url=f"/admin/orders?{q}", status_code=303)
