"""Admin home console and Dashboard & Analytics handlers."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.common import get_service, safe_edit_or_send
from app.bot.keyboards.admin_kb import (
    build_admin_back_kb,
    build_admin_home_kb,
    build_dashboard_kb,
)
from app.bot.keyboards.common_kb import AdmCB
from app.core.exceptions import ShopBotError
from app.core.logging import get_logger
from app.core.security import escape_html

logger = get_logger(__name__)
router = Router(name="admin_analytics")


def _format_admin_home_text(user: Any) -> str:
    name = escape_html(getattr(user, "first_name", None) or getattr(user, "username", None) or "Admin")
    return (
        f"🛡 <b>In-Bot Admin Console</b>\n\n"
        f"👤 Operator: <b>{name}</b> (<code>{getattr(user, 'id', 0)}</code>)\n"
        f"Select a management module below:"
    )


@router.message(Command("admin"))
async def cmd_admin_panel(message: Message, state: FSMContext) -> None:
    """Entry command /admin displaying the inline admin SPA console."""
    await state.clear()
    await safe_edit_or_send(
        message,
        _format_admin_home_text(message.from_user),
        reply_markup=build_admin_home_kb(),
    )


@router.callback_query(AdmCB.filter(F.sec == "home"))
async def cb_admin_home(callback: CallbackQuery, state: FSMContext) -> None:
    """Return to the root /admin console and clear active admin FSM state."""
    await state.clear()
    await safe_edit_or_send(
        callback,
        _format_admin_home_text(callback.from_user),
        reply_markup=build_admin_home_kb(),
    )


@router.callback_query(AdmCB.filter((F.sec == "dash") & (F.act == "view")))
async def cb_admin_dashboard(
    callback: CallbackQuery,
    state: FSMContext,
    **data: Any,
) -> None:
    """Render live Dashboard KPIs and low-stock alerts from AnalyticsService."""
    await state.clear()
    try:
        analytics_service = get_service(data, "analytics_service")
        stats = await analytics_service.get_dashboard_stats()
    except ShopBotError as exc:
        await safe_edit_or_send(
            callback,
            f"⚠️ <b>Dashboard Unavailable</b>\n\n{escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(),
        )
        return

    lines = [
        "📊 <b>Dashboard &amp; Analytics</b>",
        "",
        f"👥 Total Users: <b>{stats.total_users}</b>",
        f"⚡ Active Users: 24h: <b>{stats.active_users_24h}</b> | 7d: <b>{stats.active_users_7d}</b> | 30d: <b>{stats.active_users_30d}</b>",
        "",
        f"💵 Total USD Revenue: <b>${stats.total_revenue_usd:,.2f}</b>",
        f"💳 Total IRT Revenue: <b>{stats.total_revenue_irt:,.0f} تومان</b>",
        "",
        f"⏳ Pending Deposits: <b>{stats.pending_deposits_count}</b>",
        f"📋 Pending Manual Orders: <b>{stats.pending_manual_orders_count}</b>",
        f"🆘 Open Support Tickets: <b>{stats.open_tickets_count}</b>",
    ]

    if stats.low_stock_alerts:
        lines.append("")
        lines.append("⚠️ <b>Low-Stock Alerts:</b>")
        for alert in stats.low_stock_alerts[:15]:
            lines.append(
                f"• <b>{escape_html(alert.title)}</b> (<code>{escape_html(alert.slug)}</code>) — "
                f"Avail: <b>{alert.available_stock}</b> / Thr: {alert.threshold}"
            )
    else:
        lines.append("")
        lines.append("✅ All active products are above low-stock thresholds.")

    await safe_edit_or_send(
        callback,
        "\n".join(lines),
        reply_markup=build_dashboard_kb(),
    )
