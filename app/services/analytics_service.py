"""Real PostgreSQL aggregation service for Admin Dashboard analytics and low-stock alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.constants import (
    Currency,
    DEFAULT_LANGUAGE,
    DeliveryType,
    OrderStatus,
    PaymentStatus,
    StockStatus,
    TicketStatus,
)
from app.database.models import Order, Payment, Product, ProductStock, SupportTicket, User
from app.services.dtos import DashboardStatsDTO, LowStockAlertDTO

_USD_QUANT = Decimal("0.00000001")
_IRT_QUANT = Decimal("0.01")


class AnalyticsService:
    """Implements AnalyticsServiceProtocol using real SQL aggregates."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_dashboard_stats(self) -> DashboardStatsDTO:
        """Compute live user activity, revenue, pending queue counts, and low-stock alerts."""
        now = datetime.now(UTC)
        cutoff_24h = now - timedelta(hours=24)
        cutoff_7d = now - timedelta(days=7)
        cutoff_30d = now - timedelta(days=30)

        paid_order_statuses = (
            OrderStatus.COMPLETED.value,
            OrderStatus.PENDING_FULFILLMENT.value,
            OrderStatus.PENDING_INPUT.value,
        )
        pending_manual_statuses = (
            OrderStatus.PENDING_INPUT.value,
            OrderStatus.PENDING_FULFILLMENT.value,
        )

        async with self._session_factory() as session:
            user_row = (
                await session.execute(
                    select(
                        func.count(User.id),
                        func.count(User.id).filter(User.updated_at >= cutoff_24h),
                        func.count(User.id).filter(User.updated_at >= cutoff_7d),
                        func.count(User.id).filter(User.updated_at >= cutoff_30d),
                    )
                )
            ).one()

            revenue_row = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(Order.total_amount).filter(
                                Order.currency == Currency.USD.value
                            ),
                            Decimal("0"),
                        ),
                        func.coalesce(
                            func.sum(Order.total_amount).filter(
                                Order.currency == Currency.IRT.value
                            ),
                            Decimal("0"),
                        ),
                    ).where(Order.status.in_(paid_order_statuses))
                )
            ).one()

            pending_deposits_count = int(
                (
                    await session.execute(
                        select(func.count(Payment.id)).where(
                            Payment.status == PaymentStatus.PENDING.value
                        )
                    )
                ).scalar_one()
            )

            open_tickets_count = int(
                (
                    await session.execute(
                        select(func.count(SupportTicket.id)).where(
                            SupportTicket.status == TicketStatus.OPEN.value
                        )
                    )
                ).scalar_one()
            )

            pending_manual_orders_count = int(
                (
                    await session.execute(
                        select(func.count(Order.id)).where(
                            Order.status.in_(pending_manual_statuses)
                        )
                    )
                ).scalar_one()
            )

            active_products = (
                await session.execute(
                    select(Product)
                    .where(Product.is_active.is_(True))
                    .order_by(Product.id.asc())
                )
            ).scalars().all()

            auto_ids = [
                p.id for p in active_products if p.delivery_type == DeliveryType.AUTO.value
            ]
            stock_map: dict[int, int] = {}
            if auto_ids:
                stock_rows = (
                    await session.execute(
                        select(ProductStock.product_id, func.count(ProductStock.id))
                        .where(
                            ProductStock.product_id.in_(auto_ids),
                            ProductStock.status == StockStatus.AVAILABLE.value,
                        )
                        .group_by(ProductStock.product_id)
                    )
                ).all()
                stock_map = {int(pid): int(cnt) for pid, cnt in stock_rows}

            low_stock_alerts: list[LowStockAlertDTO] = []
            for prod in active_products:
                if prod.delivery_type == DeliveryType.AUTO.value:
                    available_stock = stock_map.get(prod.id, 0)
                else:
                    if prod.manual_stock_count < 0:
                        continue
                    available_stock = int(prod.manual_stock_count)

                if available_stock <= prod.low_stock_threshold:
                    title = (
                        (prod.titles_json or {}).get(DEFAULT_LANGUAGE)
                        or next(iter((prod.titles_json or {}).values()), None)
                        or prod.slug
                    )
                    low_stock_alerts.append(
                        LowStockAlertDTO(
                            product_id=prod.id,
                            slug=prod.slug,
                            title=str(title),
                            delivery_type=DeliveryType(prod.delivery_type),
                            available_stock=available_stock,
                            threshold=prod.low_stock_threshold,
                        )
                    )

            low_stock_alerts.sort(key=lambda a: (a.available_stock, a.product_id))

            return DashboardStatsDTO(
                total_users=int(user_row[0]),
                active_users_24h=int(user_row[1]),
                active_users_7d=int(user_row[2]),
                active_users_30d=int(user_row[3]),
                total_revenue_usd=Decimal(revenue_row[0]).quantize(
                    _USD_QUANT, rounding=ROUND_HALF_UP
                ),
                total_revenue_irt=Decimal(revenue_row[1]).quantize(
                    _IRT_QUANT, rounding=ROUND_HALF_UP
                ),
                pending_deposits_count=pending_deposits_count,
                open_tickets_count=open_tickets_count,
                pending_manual_orders_count=pending_manual_orders_count,
                low_stock_alerts=low_stock_alerts,
            )
