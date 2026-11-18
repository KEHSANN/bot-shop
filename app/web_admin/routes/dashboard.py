"""Dashboard KPI analytics and low-stock alert routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.services.interfaces import AnalyticsServiceProtocol
from app.web_admin.deps import (
    AdminPrincipal,
    get_analytics_service,
    prefers_json,
    render_admin_template,
    require_admin_csrf,
)

router = APIRouter(tags=["admin-dashboard"])


@router.get("/admin")
@router.get("/admin/")
async def dashboard_view(
    request: Request,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    analytics_service: AnalyticsServiceProtocol = Depends(get_analytics_service),
) -> Response:
    """Render main Web Admin Dashboard with real-time KPIs and low-stock alerts."""
    stats = await analytics_service.get_dashboard_stats()
    if prefers_json(request):
        return JSONResponse(stats.model_dump(mode="json"))

    return render_admin_template(
        request,
        "dashboard.html",
        {
            "active_nav": "dashboard",
            "stats": stats,
        },
        admin=admin,
    )


@router.get("/admin/api/dashboard")
async def dashboard_api(
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    analytics_service: AnalyticsServiceProtocol = Depends(get_analytics_service),
) -> JSONResponse:
    """Return DashboardStatsDTO as JSON."""
    stats = await analytics_service.get_dashboard_stats()
    return JSONResponse(stats.model_dump(mode="json"))
