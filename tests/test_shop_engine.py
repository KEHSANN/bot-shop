"""Comprehensive integration, concurrency, CMS, analytics, and bot handler tests for Wave 2 shop-engine."""

from __future__ import annotations

import asyncio
import secrets
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.keyboards.common_kb import AdmCB, CatCB, MenuCB, OrderCB, ProdCB, QtyCB
from app.bot.keyboards.shop_kb import (
    build_categories_kb,
    build_product_detail_kb,
    build_products_kb,
    format_irt,
    format_usd,
)
from app.bot.handlers.shop import (
    cb_category_products,
    cb_custom_qty_prompt,
    cb_order_detail,
    cb_order_history,
    cb_product_detail,
    cb_purchase_product,
    cb_select_qty,
    cb_shop_categories,
    msg_custom_qty_input,
    msg_manual_fulfillment_input,
    router as shop_router,
)
from app.bot.states import ShopStates
from app.config import Settings
from app.core.constants import (
    Currency,
    DeliveryType,
    OrderStatus,
    PaymentCategory,
    PaymentStatus,
    StockStatus,
    TicketCategory,
    TicketStatus,
    WalletLedgerRefType,
)
from app.core.exceptions import (
    AuthorizationError,
    InsufficientBalanceError,
    InsufficientStockError,
    InvalidStateTransitionError,
    NotFoundError,
    UserBlockedError,
    ValidationError,
)
from app.core.security import decrypt_secret, generate_fernet_key
from app.database.models import (
    AppSetting,
    Category,
    Order,
    OrderItem,
    Payment,
    Product,
    ProductBulkTier,
    ProductStock,
    SupportMessage,
    SupportTicket,
    UIButton,
    UITranslation,
    User,
    WalletLedger,
)
from app.locales.i18n import TranslationService
from app.services.analytics_service import AnalyticsService
from app.services.cms_service import CMSService
from app.services.dtos import (
    CategoryCreateDTO,
    CategoryUpdateDTO,
    ProductBulkTierDTO,
    ProductCreateDTO,
    ProductUpdateDTO,
    PurchaseRequestDTO,
    UserDTO,
)
from app.services.interfaces import (
    AnalyticsServiceProtocol,
    CMSServiceProtocol,
    ShopServiceProtocol,
)
from app.services.shop_service import ShopService


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------
def _unique_slug(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def _unique_tg_id() -> int:
    return 8_000_000_000 + int.from_bytes(secrets.token_bytes(4), "big")


async def _create_test_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    balance_usd: Decimal = Decimal("0"),
    balance_irt: Decimal = Decimal("0"),
    lang: str = "en",
    is_blocked: bool = False,
    username: str | None = None,
) -> User:
    async with session_factory() as session:
        async with session.begin():
            user = User(
                telegram_id=_unique_tg_id(),
                username=username or f"shoptest_{secrets.token_hex(3)}",
                first_name="Shop<Tester>",
                language_code=lang,
                balance_usd=balance_usd,
                balance_irt=balance_irt,
                is_blocked=is_blocked,
            )
            session.add(user)
            await session.flush()
            await session.refresh(user)
            return user


async def _cleanup_entities(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_ids: list[int] | None = None,
    category_ids: list[int] | None = None,
    product_ids: list[int] | None = None,
    translation_keys: list[str] | None = None,
    setting_keys: list[str] | None = None,
) -> None:
    uids = list(user_ids or [])
    cids = list(category_ids or [])
    pids = list(product_ids or [])
    tkeys = list(translation_keys or [])
    skeys = list(setting_keys or [])

    async with session_factory() as session:
        async with session.begin():
            if cids:
                cat_pids = list(
                    (
                        await session.execute(
                            select(Product.id).where(Product.category_id.in_(cids))
                        )
                    ).scalars().all()
                )
                pids = list(set(pids + cat_pids))

            if uids or pids:
                order_conds = []
                if uids:
                    order_conds.append(Order.user_id.in_(uids))
                if pids:
                    order_conds.append(Order.product_id.in_(pids))
                from sqlalchemy import or_

                order_ids = list(
                    (
                        await session.execute(select(Order.id).where(or_(*order_conds)))
                    ).scalars().all()
                )
                if order_ids:
                    await session.execute(
                        delete(OrderItem).where(OrderItem.order_id.in_(order_ids))
                    )
                if pids:
                    await session.execute(
                        delete(ProductStock).where(ProductStock.product_id.in_(pids))
                    )
                if order_ids:
                    await session.execute(delete(Order).where(Order.id.in_(order_ids)))

            if uids:
                await session.execute(delete(WalletLedger).where(WalletLedger.user_id.in_(uids)))
                await session.execute(delete(Payment).where(Payment.user_id.in_(uids)))
                ticket_ids = list(
                    (
                        await session.execute(
                            select(SupportTicket.id).where(SupportTicket.user_id.in_(uids))
                        )
                    ).scalars().all()
                )
                if ticket_ids:
                    await session.execute(
                        delete(SupportMessage).where(SupportMessage.ticket_id.in_(ticket_ids))
                    )
                    await session.execute(
                        delete(SupportTicket).where(SupportTicket.id.in_(ticket_ids))
                    )
                await session.execute(delete(User).where(User.id.in_(uids)))

            if pids:
                await session.execute(
                    delete(ProductBulkTier).where(ProductBulkTier.product_id.in_(pids))
                )
                await session.execute(delete(Product).where(Product.id.in_(pids)))

            if cids:
                await session.execute(delete(Category).where(Category.id.in_(cids)))

            if tkeys:
                await session.execute(
                    delete(UITranslation).where(UITranslation.key.in_(tkeys))
                )
            if skeys:
                await session.execute(delete(AppSetting).where(AppSetting.key.in_(skeys)))


@pytest.fixture
def shop_service(db_session_factory: async_sessionmaker[AsyncSession]) -> ShopService:
    return ShopService(db_session_factory, encryption_key=generate_fernet_key())


# ---------------------------------------------------------------------------
# 1. Protocol Compliance & CRUD + Dual Color Validation
# ---------------------------------------------------------------------------
def test_service_protocol_compliance(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert isinstance(ShopService(db_session_factory, generate_fernet_key()), ShopServiceProtocol)
    assert isinstance(CMSService(db_session_factory), CMSServiceProtocol)
    assert isinstance(AnalyticsService(db_session_factory), AnalyticsServiceProtocol)
    assert isinstance(shop_router, Router)


async def test_category_and_product_crud_and_color_validation(
    db_session_factory: async_sessionmaker[AsyncSession],
    shop_service: ShopService,
) -> None:
    cat_ids: list[int] = []
    prod_ids: list[int] = []
    try:
        with pytest.raises(PydanticValidationError):
            CategoryCreateDTO(slug=_unique_slug("badhex"), color_hex="#GGGGGG")

        with pytest.raises(PydanticValidationError):
            ProductCreateDTO(
                category_id=1,
                slug=_unique_slug("badp"),
                price_usd=Decimal("5.00"),
                color_hex="blue",
            )

        cat = await shop_service.create_category(
            CategoryCreateDTO(
                slug=_unique_slug("cat"),
                sort_order=5,
                is_active=True,
                color_emoji="🟣",
                color_hex="#A855F7",
                names_json={"en": "Streaming", "fa": "استریم"},
            )
        )
        cat_ids.append(cat.id)
        assert cat.color_hex == "#a855f7"
        assert cat.color_emoji == "🟣"

        with pytest.raises(ValidationError):
            await shop_service.create_category(
                CategoryCreateDTO(slug=cat.slug, names_json={"en": "Dup"})
            )

        updated_cat = await shop_service.update_category(
            cat.id,
            CategoryUpdateDTO(color_emoji="🟠", color_hex="#F97316", is_active=False),
        )
        assert updated_cat.color_emoji == "🟠"
        assert updated_cat.color_hex == "#f97316"
        assert updated_cat.is_active is False

        active_cats = await shop_service.list_categories(active_only=True)
        assert all(c.id != cat.id for c in active_cats)
        all_cats = await shop_service.list_categories(active_only=False)
        assert any(c.id == cat.id for c in all_cats)

        await shop_service.update_category(cat.id, CategoryUpdateDTO(is_active=True))

        prod = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("prod"),
                delivery_type=DeliveryType.AUTO,
                price_usd=Decimal("12.50"),
                price_irt=Decimal("812500"),
                color_emoji="🟢",
                color_hex="#22C55E",
                is_pinned=True,
                titles_json={"en": "Pro Plan", "fa": "اکانت پرو"},
                descriptions_json={"en": "1 month"},
                rules_json={"en": "Rule 1"},
            )
        )
        prod_ids.append(prod.id)
        assert prod.color_hex == "#22c55e"
        assert prod.is_pinned is True
        assert prod.available_stock == 0

        pinned = await shop_service.list_products(pinned_only=True, active_only=True)
        assert any(p.id == prod.id for p in pinned)

        updated_prod = await shop_service.update_product(
            prod.id,
            ProductUpdateDTO(price_irt=None, is_pinned=False, color_hex="#EF4444"),
        )
        assert updated_prod.price_irt is None
        assert updated_prod.is_pinned is False
        assert updated_prod.color_hex == "#ef4444"

        await shop_service.delete_product(prod.id)
        prod_ids.remove(prod.id)
        with pytest.raises(NotFoundError):
            await shop_service.get_product_detail(prod.id)

        await shop_service.delete_category(cat.id)
        cat_ids.remove(cat.id)
    finally:
        await _cleanup_entities(db_session_factory, category_ids=cat_ids, product_ids=prod_ids)


# ---------------------------------------------------------------------------
# 2. Bulk Tiers & Decimal Pricing
# ---------------------------------------------------------------------------
async def test_bulk_tiers_and_decimal_unit_price_calculation(
    db_session_factory: async_sessionmaker[AsyncSession],
    shop_service: ShopService,
) -> None:
    cat_ids: list[int] = []
    prod_ids: list[int] = []
    try:
        cat = await shop_service.create_category(
            CategoryCreateDTO(slug=_unique_slug("bulkcat"), names_json={"en": "Bulk"})
        )
        cat_ids.append(cat.id)

        prod = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("bulkprod"),
                price_usd=Decimal("2.00"),
                price_irt=Decimal("130000"),
                titles_json={"en": "Key"},
            )
        )
        prod_ids.append(prod.id)

        with pytest.raises(ValidationError):
            await shop_service.set_bulk_tiers(
                prod.id,
                [
                    ProductBulkTierDTO(min_qty=10, unit_price_usd=Decimal("1.75")),
                    ProductBulkTierDTO(min_qty=10, unit_price_usd=Decimal("1.60")),
                ],
            )

        tiers = await shop_service.set_bulk_tiers(
            prod.id,
            [
                ProductBulkTierDTO(
                    min_qty=30,
                    unit_price_usd=Decimal("1.70"),
                    unit_price_irt=Decimal("110500"),
                ),
                ProductBulkTierDTO(
                    min_qty=10,
                    unit_price_usd=Decimal("1.75"),
                    unit_price_irt=Decimal("113750"),
                ),
                ProductBulkTierDTO(
                    min_qty=100,
                    unit_price_usd=Decimal("1.65"),
                    unit_price_irt=Decimal("107250"),
                ),
            ],
        )
        assert [t.min_qty for t in tiers] == [10, 30, 100]

        for qty, expected_usd, expected_irt in [
            (1, Decimal("2.00000000"), Decimal("130000.00")),
            (9, Decimal("2.00000000"), Decimal("130000.00")),
            (10, Decimal("1.75000000"), Decimal("113750.00")),
            (29, Decimal("1.75000000"), Decimal("113750.00")),
            (30, Decimal("1.70000000"), Decimal("110500.00")),
            (99, Decimal("1.70000000"), Decimal("110500.00")),
            (100, Decimal("1.65000000"), Decimal("107250.00")),
            (250, Decimal("1.65000000"), Decimal("107250.00")),
        ]:
            u_usd = await shop_service.calculate_unit_price(prod.id, qty, Currency.USD)
            u_irt = await shop_service.calculate_unit_price(prod.id, qty, Currency.IRT)
            assert isinstance(u_usd, Decimal) and u_usd == expected_usd
            assert isinstance(u_irt, Decimal) and u_irt == expected_irt

        # Product without price_irt must reject IRT calculation
        usd_only = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("usdonly"),
                price_usd=Decimal("5.00"),
                price_irt=None,
                titles_json={"en": "USD Only"},
            )
        )
        prod_ids.append(usd_only.id)
        with pytest.raises(ValidationError):
            await shop_service.calculate_unit_price(usd_only.id, 1, Currency.IRT)
    finally:
        await _cleanup_entities(db_session_factory, category_ids=cat_ids, product_ids=prod_ids)


# ---------------------------------------------------------------------------
# 3. Auto-Stock Fernet Encryption & Deduplication
# ---------------------------------------------------------------------------
async def test_add_auto_stock_fernet_encryption_and_deduplication(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = generate_fernet_key()
    service = ShopService(db_session_factory, encryption_key=key)
    cat_ids: list[int] = []
    prod_ids: list[int] = []
    try:
        cat = await service.create_category(
            CategoryCreateDTO(slug=_unique_slug("stockcat"), names_json={"en": "Keys"})
        )
        cat_ids.append(cat.id)
        prod = await service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("stockprod"),
                price_usd=Decimal("3.00"),
                titles_json={"en": "Key Pack"},
            )
        )
        prod_ids.append(prod.id)

        res1 = await service.add_auto_stock(
            prod.id,
            ["KEY-AAA-111", "  KEY-BBB-222  ", "", "KEY-AAA-111"],
            deduplicate=True,
        )
        assert res1.added_count == 2
        assert res1.skipped_duplicates == 1
        assert res1.total_available == 2

        res2 = await service.add_auto_stock(
            prod.id,
            ["KEY-BBB-222", "KEY-CCC-333"],
            deduplicate=True,
        )
        assert res2.added_count == 1
        assert res2.skipped_duplicates == 1
        assert res2.total_available == 3

        res3 = await service.add_auto_stock(
            prod.id,
            ["KEY-AAA-111"],
            deduplicate=False,
        )
        assert res3.added_count == 1
        assert res3.total_available == 4

        async with db_session_factory() as session:
            rows = (
                await session.execute(
                    select(ProductStock)
                    .where(ProductStock.product_id == prod.id)
                    .order_by(ProductStock.id.asc())
                )
            ).scalars().all()
            assert len(rows) == 4
            for row in rows:
                assert row.encrypted_payload.startswith("gAAAAA")
                assert "KEY-" not in row.encrypted_payload
                assert len(row.payload_hash or "") == 64
            assert [decrypt_secret(r.encrypted_payload, key) for r in rows] == [
                "KEY-AAA-111",
                "KEY-BBB-222",
                "KEY-CCC-333",
                "KEY-AAA-111",
            ]
    finally:
        await _cleanup_entities(db_session_factory, category_ids=cat_ids, product_ids=prod_ids)


# ---------------------------------------------------------------------------
# 4. Atomic Purchase, Ledger, Error Rollback & Manual Fulfillment/Refund
# ---------------------------------------------------------------------------
async def test_atomic_auto_purchase_usd_and_irt_and_ledger_and_errors(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = generate_fernet_key()
    service = ShopService(db_session_factory, encryption_key=key)
    user_ids: list[int] = []
    cat_ids: list[int] = []
    prod_ids: list[int] = []
    try:
        u1 = await _create_test_user(
            db_session_factory,
            balance_usd=Decimal("50.00"),
            balance_irt=Decimal("500000.00"),
        )
        u_blocked = await _create_test_user(
            db_session_factory,
            balance_usd=Decimal("100.00"),
            is_blocked=True,
        )
        user_ids.extend([u1.id, u_blocked.id])

        cat = await service.create_category(
            CategoryCreateDTO(slug=_unique_slug("buycat"), names_json={"en": "Accounts"})
        )
        cat_ids.append(cat.id)

        prod = await service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("buyprod"),
                price_usd=Decimal("5.00"),
                price_irt=Decimal("300000.00"),
                titles_json={"en": "GPT Account"},
                bulk_tiers=[
                    ProductBulkTierDTO(
                        min_qty=2,
                        unit_price_usd=Decimal("4.00"),
                        unit_price_irt=Decimal("250000.00"),
                    )
                ],
            )
        )
        prod_ids.append(prod.id)
        await service.add_auto_stock(prod.id, ["ACC-1", "ACC-2", "ACC-3", "ACC-4"])

        # Blocked user rejected
        with pytest.raises(UserBlockedError):
            await service.purchase_product(
                PurchaseRequestDTO(
                    user_id=u_blocked.id, product_id=prod.id, quantity=1, currency=Currency.USD
                )
            )

        # Insufficient balance rejected with clean rollback
        with pytest.raises(InsufficientBalanceError):
            await service.purchase_product(
                PurchaseRequestDTO(
                    user_id=u1.id, product_id=prod.id, quantity=20, currency=Currency.USD
                )
            )

        # Bulk purchase 2 units with USD -> unit_price = 4.00, total = 8.00
        res_usd = await service.purchase_product(
            PurchaseRequestDTO(
                user_id=u1.id, product_id=prod.id, quantity=2, currency=Currency.USD
            )
        )
        assert res_usd.order.status == OrderStatus.COMPLETED
        assert res_usd.order.unit_price == Decimal("4.00000000")
        assert res_usd.order.total_amount == Decimal("8.00000000")
        assert res_usd.new_balance == Decimal("42.00000000")
        assert res_usd.delivered_items == ["ACC-1", "ACC-2"]

        # Purchase 1 unit with IRT -> unit_price = 300000.00
        res_irt = await service.purchase_product(
            PurchaseRequestDTO(
                user_id=u1.id, product_id=prod.id, quantity=1, currency=Currency.IRT
            )
        )
        assert res_irt.order.currency == Currency.IRT
        assert res_irt.order.total_amount == Decimal("300000.00000000")
        assert res_irt.new_balance == Decimal("200000.00")
        assert res_irt.delivered_items == ["ACC-3"]

        # Verify get_order_detail hides payload unless decrypt_items=True
        masked = await service.get_order_detail(res_usd.order.id, decrypt_items=False)
        assert all(item.decrypted_payload is None for item in masked.items)
        decrypted = await service.get_order_detail(res_usd.order.id, decrypt_items=True)
        assert [i.decrypted_payload for i in decrypted.items] == ["ACC-1", "ACC-2"]

        # Requesting 2 units when only 1 ("ACC-4") remains -> InsufficientStockError + rollback
        with pytest.raises(InsufficientStockError) as exc_info:
            await service.purchase_product(
                PurchaseRequestDTO(
                    user_id=u1.id, product_id=prod.id, quantity=2, currency=Currency.USD
                )
            )
        assert exc_info.value.details.get("available") == 1

        async with db_session_factory() as session:
            db_user = (await session.execute(select(User).where(User.id == u1.id))).scalar_one()
            assert db_user.balance_usd == Decimal("42.00000000")
            assert db_user.balance_irt == Decimal("200000.00")

            ledgers = (
                await session.execute(
                    select(WalletLedger)
                    .where(WalletLedger.user_id == u1.id)
                    .order_by(WalletLedger.id.asc())
                )
            ).scalars().all()
            assert len(ledgers) == 2
            assert ledgers[0].reference_type == WalletLedgerRefType.ORDER_PURCHASE.value
            assert ledgers[0].change_amount == Decimal("-8.00000000")
            assert ledgers[1].change_amount == Decimal("-300000.00000000")
    finally:
        await _cleanup_entities(
            db_session_factory, user_ids=user_ids, category_ids=cat_ids, product_ids=prod_ids
        )


async def test_manual_fulfillment_lifecycle_and_atomic_refund_idempotency(
    db_session_factory: async_sessionmaker[AsyncSession],
    shop_service: ShopService,
) -> None:
    user_ids: list[int] = []
    cat_ids: list[int] = []
    prod_ids: list[int] = []
    try:
        u1 = await _create_test_user(db_session_factory, balance_usd=Decimal("30.00"))
        u_other = await _create_test_user(db_session_factory, balance_usd=Decimal("10.00"))
        user_ids.extend([u1.id, u_other.id])

        cat = await shop_service.create_category(
            CategoryCreateDTO(slug=_unique_slug("mancat"), names_json={"en": "Manual"})
        )
        cat_ids.append(cat.id)
        prod = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("manprod"),
                delivery_type=DeliveryType.MANUAL,
                manual_prompt_key="email",
                manual_prompts_json={"en": "Send your Google email"},
                price_usd=Decimal("10.00"),
                manual_stock_count=2,
                titles_json={"en": "Gemini Pro Activation"},
            )
        )
        prod_ids.append(prod.id)

        p1 = await shop_service.purchase_product(
            PurchaseRequestDTO(
                user_id=u1.id, product_id=prod.id, quantity=1, currency=Currency.USD
            )
        )
        assert p1.order.status == OrderStatus.PENDING_INPUT
        assert p1.new_balance == Decimal("20.00000000")

        # IDOR check: another user cannot submit input
        with pytest.raises(AuthorizationError):
            await shop_service.submit_manual_fulfillment_input(
                p1.order.id, u_other.id, "evil@example.com"
            )

        submitted = await shop_service.submit_manual_fulfillment_input(
            p1.order.id, u1.id, "customer@example.com"
        )
        assert submitted.status == OrderStatus.PENDING_FULFILLMENT
        assert submitted.fulfillment_input == "customer@example.com"

        fulfilled = await shop_service.fulfill_manual_order(
            p1.order.id, admin_id=999, fulfillment_note="Activated for 12 months"
        )
        assert fulfilled.status == OrderStatus.COMPLETED
        assert fulfilled.fulfilled_by_admin_id == 999
        assert fulfilled.fulfillment_note == "Activated for 12 months"

        # Second purchase -> cancel and refund
        p2 = await shop_service.purchase_product(
            PurchaseRequestDTO(
                user_id=u1.id,
                product_id=prod.id,
                quantity=1,
                currency=Currency.USD,
                fulfillment_input="second@example.com",
            )
        )
        assert p2.order.status == OrderStatus.PENDING_FULFILLMENT
        assert p2.new_balance == Decimal("10.00000000")

        detail_after_p2 = await shop_service.get_product_detail(prod.id)
        assert detail_after_p2.manual_stock_count == 0
        assert detail_after_p2.sold_count == 2

        refunded = await shop_service.cancel_and_refund_order(
            p2.order.id, admin_id=999, reason="Cannot activate region"
        )
        assert refunded.status == OrderStatus.REFUNDED

        # Double-refund must fail
        with pytest.raises(InvalidStateTransitionError):
            await shop_service.cancel_and_refund_order(p2.order.id, admin_id=999)

        detail_after_refund = await shop_service.get_product_detail(prod.id)
        assert detail_after_refund.manual_stock_count == 1
        assert detail_after_refund.sold_count == 1

        async with db_session_factory() as session:
            db_user = (await session.execute(select(User).where(User.id == u1.id))).scalar_one()
            assert db_user.balance_usd == Decimal("20.00000000")

        user_orders = await shop_service.list_user_orders(u1.id)
        assert len(user_orders) == 2
        refunded_list = await shop_service.list_orders(status=OrderStatus.REFUNDED)
        assert any(o.id == p2.order.id for o in refunded_list)
    finally:
        await _cleanup_entities(
            db_session_factory, user_ids=user_ids, category_ids=cat_ids, product_ids=prod_ids
        )


# ---------------------------------------------------------------------------
# 5. REAL PostgreSQL Concurrency Race Tests (SKIP LOCKED & FOR UPDATE)
# ---------------------------------------------------------------------------
async def test_real_postgres_concurrency_race_last_stock_item(
    db_session_factory: async_sessionmaker[AsyncSession],
    shop_service: ShopService,
) -> None:
    """Two users race concurrently for the last 1 stock item -> exactly 1 wins, 1 gets InsufficientStockError."""
    user_ids: list[int] = []
    cat_ids: list[int] = []
    prod_ids: list[int] = []
    try:
        u1 = await _create_test_user(db_session_factory, balance_usd=Decimal("50.00"))
        u2 = await _create_test_user(db_session_factory, balance_usd=Decimal("50.00"))
        user_ids.extend([u1.id, u2.id])

        cat = await shop_service.create_category(
            CategoryCreateDTO(slug=_unique_slug("racecat"), names_json={"en": "Race"})
        )
        cat_ids.append(cat.id)
        prod = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("raceprod"),
                price_usd=Decimal("15.00"),
                titles_json={"en": "Last Key"},
            )
        )
        prod_ids.append(prod.id)
        await shop_service.add_auto_stock(prod.id, ["ONLY-ONE-KEY-999"])

        r1, r2 = await asyncio.gather(
            shop_service.purchase_product(
                PurchaseRequestDTO(
                    user_id=u1.id, product_id=prod.id, quantity=1, currency=Currency.USD
                )
            ),
            shop_service.purchase_product(
                PurchaseRequestDTO(
                    user_id=u2.id, product_id=prod.id, quantity=1, currency=Currency.USD
                )
            ),
            return_exceptions=True,
        )

        successes = [r for r in (r1, r2) if not isinstance(r, BaseException)]
        failures = [r for r in (r1, r2) if isinstance(r, BaseException)]
        assert len(successes) == 1, f"Expected 1 winner, got results={(r1, r2)}"
        assert len(failures) == 1
        assert isinstance(failures[0], InsufficientStockError)
        assert successes[0].delivered_items == ["ONLY-ONE-KEY-999"]

        detail = await shop_service.get_product_detail(prod.id)
        assert detail.available_stock == 0
        assert detail.sold_count == 1

        async with db_session_factory() as session:
            balances = (
                await session.execute(
                    select(User.id, User.balance_usd).where(User.id.in_([u1.id, u2.id]))
                )
            ).all()
            bal_map = {uid: bal for uid, bal in balances}
            winner_id = successes[0].order.user_id
            loser_id = u2.id if winner_id == u1.id else u1.id
            assert bal_map[winner_id] == Decimal("35.00000000")
            assert bal_map[loser_id] == Decimal("50.00000000")
    finally:
        await _cleanup_entities(
            db_session_factory, user_ids=user_ids, category_ids=cat_ids, product_ids=prod_ids
        )


async def test_real_postgres_concurrency_race_same_user_double_tap(
    db_session_factory: async_sessionmaker[AsyncSession],
    shop_service: ShopService,
) -> None:
    """One user with funds for only 1 purchase double-taps concurrently -> 1 succeeds, 1 InsufficientBalanceError."""
    user_ids: list[int] = []
    cat_ids: list[int] = []
    prod_ids: list[int] = []
    try:
        u1 = await _create_test_user(db_session_factory, balance_usd=Decimal("10.00"))
        user_ids.append(u1.id)

        cat = await shop_service.create_category(
            CategoryCreateDTO(slug=_unique_slug("dblcat"), names_json={"en": "DoubleTap"})
        )
        cat_ids.append(cat.id)
        prod = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("dblprod"),
                price_usd=Decimal("10.00"),
                titles_json={"en": "Item"},
            )
        )
        prod_ids.append(prod.id)
        await shop_service.add_auto_stock(prod.id, ["K1", "K2", "K3", "K4", "K5"])

        results = await asyncio.gather(
            shop_service.purchase_product(
                PurchaseRequestDTO(
                    user_id=u1.id, product_id=prod.id, quantity=1, currency=Currency.USD
                )
            ),
            shop_service.purchase_product(
                PurchaseRequestDTO(
                    user_id=u1.id, product_id=prod.id, quantity=1, currency=Currency.USD
                )
            ),
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], InsufficientBalanceError)

        detail = await shop_service.get_product_detail(prod.id)
        assert detail.available_stock == 4
        assert detail.sold_count == 1

        async with db_session_factory() as session:
            db_user = (await session.execute(select(User).where(User.id == u1.id))).scalar_one()
            assert db_user.balance_usd == Decimal("0.00000000")
    finally:
        await _cleanup_entities(
            db_session_factory, user_ids=user_ids, category_ids=cat_ids, product_ids=prod_ids
        )


async def test_real_postgres_concurrency_race_finite_manual_stock(
    db_session_factory: async_sessionmaker[AsyncSession],
    shop_service: ShopService,
) -> None:
    """Two users race concurrently for the last 1 finite manual stock unit -> 1 wins, 1 InsufficientStockError."""
    user_ids: list[int] = []
    cat_ids: list[int] = []
    prod_ids: list[int] = []
    try:
        u1 = await _create_test_user(db_session_factory, balance_usd=Decimal("50.00"))
        u2 = await _create_test_user(db_session_factory, balance_usd=Decimal("50.00"))
        user_ids.extend([u1.id, u2.id])

        cat = await shop_service.create_category(
            CategoryCreateDTO(slug=_unique_slug("mracecat"), names_json={"en": "MRace"})
        )
        cat_ids.append(cat.id)
        prod = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("mraceprod"),
                delivery_type=DeliveryType.MANUAL,
                price_usd=Decimal("20.00"),
                manual_stock_count=1,
                titles_json={"en": "Last Manual Slot"},
            )
        )
        prod_ids.append(prod.id)

        results = await asyncio.gather(
            shop_service.purchase_product(
                PurchaseRequestDTO(
                    user_id=u1.id, product_id=prod.id, quantity=1, currency=Currency.USD
                )
            ),
            shop_service.purchase_product(
                PurchaseRequestDTO(
                    user_id=u2.id, product_id=prod.id, quantity=1, currency=Currency.USD
                )
            ),
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], InsufficientStockError)

        detail = await shop_service.get_product_detail(prod.id)
        assert detail.manual_stock_count == 0
        assert detail.sold_count == 1
    finally:
        await _cleanup_entities(
            db_session_factory, user_ids=user_ids, category_ids=cat_ids, product_ids=prod_ids
        )


# ---------------------------------------------------------------------------
# 6. CMSService & Redis Invalidation Tests
# ---------------------------------------------------------------------------
async def test_cms_service_translations_buttons_settings_and_redis_invalidation(
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
) -> None:
    i18n = TranslationService(db_session_factory, redis_client=redis_client, cache_ttl=120)
    cms = CMSService(db_session_factory, redis_client=redis_client, translation_service=i18n)

    tmp_key = f"shop.test_cms_{secrets.token_hex(4)}"
    tmp_setting = f"test_setting_{secrets.token_hex(4)}"
    original_btn = None
    try:
        # Warm translation cache
        await cms.upsert_translation(tmp_key, "en", "First Text")
        assert await cms.get_translation(tmp_key, "en") == "First Text"
        assert await redis_client.get(f"{TranslationService.TEXTS_CACHE_PREFIX}en") is not None

        # Update via CMS -> must invalidate Redis and immediately return updated text
        await cms.upsert_translation(tmp_key, "en", "Second Text")
        assert await cms.get_translation(tmp_key, "en") == "Second Text"

        listed = await cms.list_translations(lang_code="en", search=tmp_key)
        assert len(listed) == 1 and listed[0].text_value == "Second Text"

        # Buttons + Redis cache invalidation
        buttons = await cms.list_buttons(menu_scope="main")
        assert len(buttons) == 5
        original_btn = next(b for b in buttons if b.button_key == "shop")

        await i18n.get_main_menu_buttons("en")
        assert await redis_client.get(f"{TranslationService.BUTTONS_CACHE_PREFIX}main") is not None

        with pytest.raises(ValidationError):
            await cms.update_button("shop", color_hex="not-hex")

        updated_btn = await cms.update_button(
            "shop",
            color_emoji="🟢",
            color_hex="#22C55E",
            labels_json={"en": "🛒 Buy Now"},
        )
        assert updated_btn.color_emoji == "🟢"
        assert updated_btn.color_hex == "#22c55e"
        assert updated_btn.labels_json["en"] == "🛒 Buy Now"
        assert await redis_client.get(f"{TranslationService.BUTTONS_CACHE_PREFIX}main") is None

        refreshed_buttons = await i18n.get_main_menu_buttons("en")
        refreshed_shop = next(b for b in refreshed_buttons if b.button_key == "shop")
        assert refreshed_shop.labels_json["en"] == "🛒 Buy Now"
        assert refreshed_shop.color_hex == "#22c55e"

        # Settings
        assert await cms.get_setting(tmp_setting, default="fallback") == "fallback"
        s1 = await cms.set_setting(tmp_setting, "42", description="Test setting")
        assert s1.value == "42"
        assert await cms.get_setting(tmp_setting) == "42"
        s2 = await cms.set_setting(tmp_setting, "99")
        assert s2.value == "99"
    finally:
        if original_btn is not None:
            await cms.update_button(
                "shop",
                color_emoji=original_btn.color_emoji,
                color_hex=original_btn.color_hex,
                labels_json=original_btn.labels_json,
                is_active=original_btn.is_active,
            )
        await _cleanup_entities(
            db_session_factory,
            translation_keys=[tmp_key],
            setting_keys=[tmp_setting],
        )
        await i18n.invalidate_cache()


# ---------------------------------------------------------------------------
# 7. AnalyticsService Real SQL Aggregations
# ---------------------------------------------------------------------------
async def test_analytics_service_real_aggregations(
    db_session_factory: async_sessionmaker[AsyncSession],
    shop_service: ShopService,
) -> None:
    analytics = AnalyticsService(db_session_factory)
    user_ids: list[int] = []
    cat_ids: list[int] = []
    prod_ids: list[int] = []
    try:
        baseline = await analytics.get_dashboard_stats()

        u_recent = await _create_test_user(
            db_session_factory, balance_usd=Decimal("1000.00"), balance_irt=Decimal("5000000.00")
        )
        u_old = await _create_test_user(db_session_factory, balance_usd=Decimal("0"))
        user_ids.extend([u_recent.id, u_old.id])

        async with db_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("UPDATE users SET updated_at = NOW() - INTERVAL '40 days' WHERE id = :id"),
                    {"id": u_old.id},
                )

        cat = await shop_service.create_category(
            CategoryCreateDTO(slug=_unique_slug("anacat"), names_json={"en": "Analytics"})
        )
        cat_ids.append(cat.id)

        p_auto_low = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("analow"),
                delivery_type=DeliveryType.AUTO,
                price_usd=Decimal("25.50"),
                price_irt=Decimal("500000.00"),
                low_stock_threshold=5,
                titles_json={"en": "Auto Low"},
            )
        )
        p_man_unlim = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("anaunlim"),
                delivery_type=DeliveryType.MANUAL,
                price_usd=Decimal("10.00"),
                manual_stock_count=-1,
                low_stock_threshold=5,
                titles_json={"en": "Manual Unlimited"},
            )
        )
        p_man_low = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("anamanlow"),
                delivery_type=DeliveryType.MANUAL,
                price_usd=Decimal("99.00"),
                manual_stock_count=3,
                low_stock_threshold=3,
                titles_json={"en": "Manual Low"},
            )
        )
        prod_ids.extend([p_auto_low.id, p_man_unlim.id, p_man_low.id])
        await shop_service.add_auto_stock(p_auto_low.id, ["A1", "A2", "A3", "A4"])

        # 1 completed USD order ($25.50) + 1 completed IRT order (500,000 IRT)
        await shop_service.purchase_product(
            PurchaseRequestDTO(
                user_id=u_recent.id, product_id=p_auto_low.id, quantity=1, currency=Currency.USD
            )
        )
        await shop_service.purchase_product(
            PurchaseRequestDTO(
                user_id=u_recent.id, product_id=p_auto_low.id, quantity=1, currency=Currency.IRT
            )
        )
        # 1 pending manual order ($10.00)
        await shop_service.purchase_product(
            PurchaseRequestDTO(
                user_id=u_recent.id, product_id=p_man_unlim.id, quantity=1, currency=Currency.USD
            )
        )
        # 1 refunded order ($99.00) -> excluded from revenue!
        to_refund = await shop_service.purchase_product(
            PurchaseRequestDTO(
                user_id=u_recent.id, product_id=p_man_low.id, quantity=1, currency=Currency.USD
            )
        )
        await shop_service.cancel_and_refund_order(to_refund.order.id, admin_id=1, reason="test")
        # Recreate a pending manual order on p_man_low so manual_stock_count=2 <= threshold 3
        await shop_service.purchase_product(
            PurchaseRequestDTO(
                user_id=u_recent.id, product_id=p_man_low.id, quantity=1, currency=Currency.USD
            )
        )
        # Refund that one? Wait: if we refund it, revenue stays 25.50 + 10.00; instead just check!
        # Let's keep it pending_fulfillment? Actually then revenue is 25.50 + 10.00 + 99.00 = 134.50 and pending_manual_orders = +2.
        # Even simpler: check exact delta!

        async with db_session_factory() as session:
            async with session.begin():
                session.add(
                    Payment(
                        user_id=u_recent.id,
                        payment_category=PaymentCategory.CRYPTO.value,
                        currency=Currency.USD.value,
                        amount_usd=Decimal("10.00"),
                        status=PaymentStatus.PENDING.value,
                        tx_hash=f"0xANA_{secrets.token_hex(6)}",
                    )
                )
                session.add(
                    SupportTicket(
                        ticket_number=f"TKT-{secrets.token_hex(4).upper()}",
                        user_id=u_recent.id,
                        category=TicketCategory.OTHER_ISSUE.value,
                        status=TicketStatus.OPEN.value,
                    )
                )
                session.add(
                    SupportTicket(
                        ticket_number=f"TKT-{secrets.token_hex(4).upper()}",
                        user_id=u_recent.id,
                        category=TicketCategory.OTHER_ISSUE.value,
                        status=TicketStatus.CLOSED.value,
                    )
                )

        stats = await analytics.get_dashboard_stats()
        assert stats.total_users == baseline.total_users + 2
        assert stats.active_users_24h == baseline.active_users_24h + 1
        assert stats.active_users_7d == baseline.active_users_7d + 1
        assert stats.active_users_30d == baseline.active_users_30d + 1
        assert stats.total_revenue_usd == baseline.total_revenue_usd + Decimal("134.50000000")
        assert stats.total_revenue_irt == baseline.total_revenue_irt + Decimal("500000.00")
        assert stats.pending_deposits_count == baseline.pending_deposits_count + 1
        assert stats.open_tickets_count == baseline.open_tickets_count + 1
        assert stats.pending_manual_orders_count == baseline.pending_manual_orders_count + 2

        alert_ids = {a.product_id for a in stats.low_stock_alerts}
        assert p_auto_low.id in alert_ids
        assert p_man_low.id in alert_ids
        assert p_man_unlim.id not in alert_ids
    finally:
        await _cleanup_entities(
            db_session_factory, user_ids=user_ids, category_ids=cat_ids, product_ids=prod_ids
        )


# ---------------------------------------------------------------------------
# 8. Shop Keyboards & Bot Handlers End-to-End SPA Flow
# ---------------------------------------------------------------------------
def test_shop_keyboards_and_callback_payload_sizes(
    shop_service: ShopService,
) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    from app.services.dtos import CategoryDTO, ProductDetailDTO

    cat = CategoryDTO(
        id=1,
        slug="ai",
        sort_order=1,
        is_active=True,
        color_emoji="🔵",
        color_hex="#3b82f6",
        names_json={"en": "AI Tools", "fa": "ابزارهای هوش مصنوعی"},
        descriptions_json={},
        created_at=now,
        updated_at=now,
    )
    prod = ProductDetailDTO(
        id=42,
        category_id=1,
        slug="gemini",
        delivery_type=DeliveryType.AUTO,
        price_usd=Decimal("9.99"),
        price_irt=Decimal("650000"),
        color_emoji="🟢",
        color_hex="#22c55e",
        is_pinned=True,
        is_active=True,
        sort_order=1,
        manual_stock_count=0,
        low_stock_threshold=5,
        sold_count=12,
        titles_json={"en": "Gemini Pro", "fa": "جمینای پرو"},
        descriptions_json={"en": "Desc"},
        rules_json={"en": "Rule"},
        manual_prompts_json={},
        available_stock=7,
        created_at=now,
        updated_at=now,
        bulk_tiers=[],
        category=cat,
    )

    cat_kb = build_categories_kb([cat], lang_code="en")
    prod_kb = build_products_kb([prod], category_id=1, lang_code="en")
    detail_en = build_product_detail_kb(prod, lang_code="en", selected_qty=5)
    detail_fa = build_product_detail_kb(prod, lang_code="fa", selected_qty=5)

    assert "🟢 Gemini Pro - Stock: 7" in prod_kb.inline_keyboard[0][0].text

    en_callbacks = [b.callback_data for row in detail_en.inline_keyboard for b in row if b.callback_data]
    fa_callbacks = [b.callback_data for row in detail_fa.inline_keyboard for b in row if b.callback_data]
    assert any("buy_usd" in cb for cb in en_callbacks)
    assert not any("buy_irt" in cb for cb in en_callbacks)
    assert any("buy_irt" in cb for cb in fa_callbacks)

    for kb in (cat_kb, prod_kb, detail_en, detail_fa):
        for row in kb.inline_keyboard:
            for btn in row:
                if btn.callback_data:
                    assert len(btn.callback_data.encode("utf-8")) <= 64

    assert format_usd(Decimal("18.50000000")) == "$18.50"
    assert format_usd(Decimal("0.00250000")) == "$0.0025"
    assert format_irt(Decimal("1200000.00")) == "1,200,000"


async def test_shop_bot_handlers_spa_auto_manual_and_idor(
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
    app_settings: Settings,
) -> None:
    key = generate_fernet_key()
    shop_service = ShopService(db_session_factory, encryption_key=key)
    i18n = TranslationService(db_session_factory, redis_client=redis_client, cache_ttl=120)
    storage = MemoryStorage()

    user_ids: list[int] = []
    cat_ids: list[int] = []
    prod_ids: list[int] = []
    try:
        db_user = await _create_test_user(
            db_session_factory,
            balance_usd=Decimal("100.00"),
            balance_irt=Decimal("5000000.00"),
            lang="fa",
        )
        db_other = await _create_test_user(db_session_factory, balance_usd=Decimal("10.00"))
        user_ids.extend([db_user.id, db_other.id])

        user_dto = UserDTO.model_validate(db_user)
        other_dto = UserDTO.model_validate(db_other)

        cat = await shop_service.create_category(
            CategoryCreateDTO(
                slug=_unique_slug("botcat"),
                color_emoji="🔵",
                names_json={"en": "Bot Cat", "fa": "دسته ربات"},
            )
        )
        cat_ids.append(cat.id)

        auto_prod = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("botauto"),
                delivery_type=DeliveryType.AUTO,
                price_usd=Decimal("5.00"),
                price_irt=Decimal("325000.00"),
                color_emoji="🟢",
                titles_json={"en": "Auto Item", "fa": "محصول خودکار"},
                descriptions_json={"en": "Fast key", "fa": "تحویل فوری"},
                rules_json={"en": "Rule <ok>", "fa": "قانون <مهم>"},
                bulk_tiers=[
                    ProductBulkTierDTO(
                        min_qty=5,
                        unit_price_usd=Decimal("4.00"),
                        unit_price_irt=Decimal("260000.00"),
                    )
                ],
            )
        )
        manual_prod = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat.id,
                slug=_unique_slug("botman"),
                delivery_type=DeliveryType.MANUAL,
                manual_prompt_key="email",
                manual_prompts_json={"en": "Enter email", "fa": "ایمیل خود را وارد کنید"},
                price_usd=Decimal("10.00"),
                price_irt=Decimal("650000.00"),
                manual_stock_count=-1,
                titles_json={"en": "Manual Item", "fa": "محصول دستی"},
            )
        )
        prod_ids.extend([auto_prod.id, manual_prod.id])
        await shop_service.add_auto_stock(
            auto_prod.id,
            ["SECRET<1>", "SECRET-2", "SECRET-3", "SECRET-4", "SECRET-5"],
        )

        bot_mock = MagicMock()
        bot_mock.send_message = AsyncMock()
        bot_mock.edit_message_text = AsyncMock()

        def _make_callback(tg_id: int) -> MagicMock:
            cb = MagicMock()
            cb.bot = bot_mock
            cb.answer = AsyncMock()
            msg = MagicMock()
            msg.chat = MagicMock(id=tg_id)
            msg.message_id = 777
            msg.photo = None
            msg.document = None
            msg.video = None
            msg.edit_text = AsyncMock()
            msg.edit_caption = AsyncMock()
            msg.answer = AsyncMock()
            cb.message = msg
            return cb

        def _make_message(tg_id: int, text_val: str) -> MagicMock:
            msg = MagicMock()
            msg.bot = bot_mock
            msg.chat = MagicMock(id=tg_id)
            msg.text = text_val
            msg.caption = None
            msg.answer = AsyncMock()
            return msg

        state = FSMContext(
            storage=storage,
            key=StorageKey(bot_id=1, chat_id=db_user.telegram_id, user_id=db_user.telegram_id),
        )

        # 1. Categories screen
        cb1 = _make_callback(db_user.telegram_id)
        await cb_shop_categories(cb1, state, "fa", i18n, shop_service)
        cb1.message.edit_text.assert_awaited_once()
        assert "دسته" in cb1.message.edit_text.call_args.kwargs["text"]

        # 2. Category products list
        cb2 = _make_callback(db_user.telegram_id)
        await cb_category_products(
            cb2, CatCB(id=cat.id, page=0), state, "fa", i18n, shop_service
        )
        cb2.message.edit_text.assert_awaited_once()

        # 3. Product detail + rules HTML escaping + fa IRT button
        cb3 = _make_callback(db_user.telegram_id)
        await cb_product_detail(
            cb3, ProdCB(id=auto_prod.id, action="view"), state, "fa", i18n, shop_service
        )
        card_text = cb3.message.edit_text.call_args.kwargs["text"]
        assert "&lt;مهم&gt;" in card_text
        assert "تومان" in card_text

        # 4. Select Qty=5 -> bulk tier unit price $4.00 applied in card
        cb4 = _make_callback(db_user.telegram_id)
        await cb_select_qty(
            cb4, QtyCB(prod_id=auto_prod.id, qty=5), state, "fa", i18n, shop_service
        )
        qty5_text = cb4.message.edit_text.call_args.kwargs["text"]
        assert "$4.00" in qty5_text
        assert "$20.00" in qty5_text

        # 5. Custom Qty invalid & valid
        cb_custom = _make_callback(db_user.telegram_id)
        await cb_custom_qty_prompt(
            cb_custom, ProdCB(id=auto_prod.id, action="custom_qty"), state, "en", i18n
        )
        assert await state.get_state() == ShopStates.waiting_custom_qty.state

        bad_msg = _make_message(db_user.telegram_id, "not_a_number")
        await msg_custom_qty_input(bad_msg, state, "en", i18n, shop_service)
        bad_msg.answer.assert_awaited_once()
        assert await state.get_state() == ShopStates.waiting_custom_qty.state

        good_msg = _make_message(db_user.telegram_id, "1")
        await msg_custom_qty_input(good_msg, state, "en", i18n, shop_service)
        assert await state.get_state() is None

        # 6. Auto-delivery purchase -> monospace <pre> with HTML-escaped credential
        cb_buy = _make_callback(db_user.telegram_id)
        await cb_purchase_product(
            cb_buy,
            ProdCB(id=auto_prod.id, action="buy_usd"),
            state,
            user_dto,
            "en",
            i18n,
            shop_service,
            app_settings,
        )
        delivered_text = cb_buy.message.edit_text.call_args.kwargs["text"]
        assert "<pre>" in delivered_text
        assert "SECRET&lt;1&gt;" in delivered_text

        # 7. Manual purchase with prompt -> FSM input -> admin notification
        cb_man_view = _make_callback(db_user.telegram_id)
        await cb_product_detail(
            cb_man_view, ProdCB(id=manual_prod.id, action="view"), state, "en", i18n, shop_service
        )
        cb_man_buy = _make_callback(db_user.telegram_id)
        await cb_purchase_product(
            cb_man_buy,
            ProdCB(id=manual_prod.id, action="buy_usd"),
            state,
            user_dto,
            "en",
            i18n,
            shop_service,
            app_settings,
        )
        assert await state.get_state() == ShopStates.waiting_manual_input.state

        test_settings = app_settings.model_copy(update={"ADMIN_TELEGRAM_IDS": [999888777]})
        man_msg = _make_message(db_user.telegram_id, "user<xss>@example.com")
        await msg_manual_fulfillment_input(
            man_msg, state, user_dto, "en", i18n, shop_service, test_settings
        )
        assert await state.get_state() is None
        man_msg.answer.assert_awaited_once()
        assert bot_mock.send_message.await_count == 1
        admin_msg_text = bot_mock.send_message.call_args.kwargs["text"]
        assert "user&lt;xss&gt;@example.com" in admin_msg_text
        admin_kb = bot_mock.send_message.call_args.kwargs["reply_markup"]
        unpacked_admin_cbs = [
            AdmCB.unpack(btn.callback_data)
            for row in admin_kb.inline_keyboard
            for btn in row
            if btn.callback_data and btn.callback_data.startswith("adm:")
        ]
        assert any(cb.sec == "ord" and cb.act == "view" and cb.id > 0 for cb in unpacked_admin_cbs)

        # 8. Order history & detail + IDOR guard
        cb_hist = _make_callback(db_user.telegram_id)
        await cb_order_history(cb_hist, state, user_dto, "en", i18n, shop_service)
        hist_kb = cb_hist.message.edit_text.call_args.kwargs["reply_markup"]
        order_cb_str = hist_kb.inline_keyboard[0][0].callback_data
        parsed_order_cb = OrderCB.unpack(order_cb_str)

        cb_det_ok = _make_callback(db_user.telegram_id)
        await cb_order_detail(cb_det_ok, parsed_order_cb, user_dto, "en", i18n, shop_service)
        cb_det_ok.message.edit_text.assert_awaited_once()

        cb_det_idor = _make_callback(db_other.telegram_id)
        await cb_order_detail(cb_det_idor, parsed_order_cb, other_dto, "en", i18n, shop_service)
        cb_det_idor.answer.assert_awaited_once()
        assert cb_det_idor.answer.call_args.kwargs.get("show_alert") is True
        cb_det_idor.message.edit_text.assert_not_awaited()
    finally:
        await storage.close()
        await _cleanup_entities(
            db_session_factory, user_ids=user_ids, category_ids=cat_ids, product_ids=prod_ids
        )


def test_manual_prompt_guide_link_and_xss_protection() -> None:
    from app.bot.handlers.shop import _render_manual_prompt_html

    # 1. {guide} with configured CMS guide_url -> clickable <a> + inline URL button
    html1, kb1 = _render_manual_prompt_html(
        "Send: email@gmail.com|password|2FA_32_character\n\n{guide}",
        guide_url="https://telegra.ph/2fa-setup",
        guide_label="📖 Guide",
    )
    assert '<a href="https://telegra.ph/2fa-setup">📖 Guide</a>' in html1
    assert "email@gmail.com|password|2FA_32_character" in html1
    assert kb1 is not None and kb1.inline_keyboard[0][0].url == "https://telegra.ph/2fa-setup"

    # 2. {guide} when guide_url is not configured yet -> clean text without broken href
    html2, kb2 = _render_manual_prompt_html(
        "Send: email@gmail.com|password|2FA_32_character\n\n{guide}",
        guide_url="",
        guide_label="📖 Guide",
    )
    assert "<a " not in html2
    assert "{guide}" not in html2
    assert kb2 is None

    # 3. Markdown [label](https://...) works, while javascript: and raw <script> are neutralized
    html3, kb3 = _render_manual_prompt_html(
        "Send <script>x</script> [bad](javascript:alert(1)) or [راهنما](https://example.com/g?a=1&b=2)",
        guide_url="",
        guide_label="📖 راهنما",
    )
    assert "&lt;script&gt;x&lt;/script&gt;" in html3
    assert "javascript:alert(1)" in html3 and '<a href="javascript:' not in html3
    assert '<a href="https://example.com/g?a=1&amp;b=2">راهنما</a>' in html3
    assert kb3 is not None and kb3.inline_keyboard[0][0].url == "https://example.com/g?a=1&b=2"
