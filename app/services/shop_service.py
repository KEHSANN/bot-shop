"""Shop, inventory, bulk discount pricing, and atomic purchase/fulfillment service."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.constants import (
    Currency,
    DeliveryType,
    OrderStatus,
    StockStatus,
    WalletLedgerRefType,
    is_valid_hex_color,
    normalize_hex_color,
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
from app.core.logging import get_logger
from app.core.security import decrypt_secret, encrypt_secret, sha256_hex
from app.database.models import (
    Category,
    Order,
    OrderItem,
    Product,
    ProductBulkTier,
    ProductStock,
    User,
    WalletLedger,
)
from app.services.dtos import (
    CategoryCreateDTO,
    CategoryDTO,
    CategoryUpdateDTO,
    OrderDTO,
    OrderItemDTO,
    ProductBulkTierDTO,
    ProductCreateDTO,
    ProductDTO,
    ProductDetailDTO,
    ProductUpdateDTO,
    PurchaseRequestDTO,
    PurchaseResultDTO,
    StockImportResultDTO,
)

logger = get_logger(__name__)

_USD_QUANT = Decimal("0.00000001")
_IRT_QUANT = Decimal("0.01")


def _quantize_for_currency(amount: Decimal, currency: Currency) -> Decimal:
    """Quantize a Decimal monetary amount using strict ROUND_HALF_UP rules."""
    if not amount.is_finite():
        raise ValidationError("Monetary amount must be finite")
    if amount < Decimal("0"):
        raise ValidationError("Monetary amount must be non-negative")
    quantum = _USD_QUANT if currency == Currency.USD else _IRT_QUANT
    return amount.quantize(quantum, rounding=ROUND_HALF_UP)


def _generate_order_number(now: datetime) -> str:
    """Generate a unique human-readable order identifier within 32 chars."""
    return f"ORD-{now:%y%m%d}-{secrets.token_hex(5).upper()}"


class ShopService:
    """Implements ShopServiceProtocol with Fernet-encrypted stock and ACID purchases."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption_key: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._encryption_key = (
            encryption_key if encryption_key is not None else get_settings().ENCRYPTION_KEY
        )

    @staticmethod
    async def _fetch_available_stock_map(
        session: AsyncSession, product_ids: Sequence[int]
    ) -> dict[int, int]:
        if not product_ids:
            return {}
        rows = (
            await session.execute(
                select(ProductStock.product_id, func.count(ProductStock.id))
                .where(
                    ProductStock.product_id.in_(list(product_ids)),
                    ProductStock.status == StockStatus.AVAILABLE.value,
                )
                .group_by(ProductStock.product_id)
            )
        ).all()
        return {int(pid): int(cnt) for pid, cnt in rows}

    @staticmethod
    def _compute_available_stock(product: Product, stock_map: dict[int, int]) -> int:
        if product.delivery_type == DeliveryType.AUTO.value:
            return stock_map.get(product.id, 0)
        return int(product.manual_stock_count)

    @classmethod
    def _to_product_dto(cls, product: Product, available_stock: int) -> ProductDTO:
        return ProductDTO(
            id=product.id,
            category_id=product.category_id,
            slug=product.slug,
            delivery_type=DeliveryType(product.delivery_type),
            manual_prompt_key=product.manual_prompt_key,
            price_usd=product.price_usd,
            price_irt=product.price_irt,
            color_emoji=product.color_emoji,
            color_hex=product.color_hex,
            is_pinned=product.is_pinned,
            is_active=product.is_active,
            sort_order=product.sort_order,
            manual_stock_count=product.manual_stock_count,
            low_stock_threshold=product.low_stock_threshold,
            sold_count=product.sold_count,
            titles_json=dict(product.titles_json or {}),
            descriptions_json=dict(product.descriptions_json or {}),
            rules_json=dict(product.rules_json or {}),
            manual_prompts_json=dict(product.manual_prompts_json or {}),
            available_stock=available_stock,
            created_at=product.created_at,
            updated_at=product.updated_at,
        )

    @classmethod
    def _to_product_detail_dto(cls, product: Product, available_stock: int) -> ProductDetailDTO:
        tiers = sorted(product.bulk_tiers or [], key=lambda t: t.min_qty)
        return ProductDetailDTO(
            **cls._to_product_dto(product, available_stock).model_dump(),
            bulk_tiers=[ProductBulkTierDTO.model_validate(t) for t in tiers],
            category=CategoryDTO.model_validate(product.category) if product.category else None,
        )

    def _to_order_dto(
        self,
        order: Order,
        *,
        decrypt_items: bool = False,
        decrypted_map: dict[int, str] | None = None,
    ) -> OrderDTO:
        item_dtos: list[OrderItemDTO] = []
        for item in sorted(order.items or [], key=lambda i: i.id):
            plain: str | None = None
            if decrypted_map is not None and item.id in decrypted_map:
                plain = decrypted_map[item.id]
            elif decrypt_items and item.encrypted_delivered_payload:
                plain = decrypt_secret(item.encrypted_delivered_payload, self._encryption_key)
            item_dtos.append(
                OrderItemDTO(
                    id=item.id,
                    order_id=item.order_id,
                    product_id=item.product_id,
                    stock_id=item.stock_id,
                    decrypted_payload=plain,
                    unit_price=item.unit_price,
                    created_at=item.created_at,
                )
            )
        return OrderDTO(
            id=order.id,
            order_number=order.order_number,
            user_id=order.user_id,
            product_id=order.product_id,
            quantity=order.quantity,
            currency=Currency(order.currency),
            unit_price=order.unit_price,
            total_amount=order.total_amount,
            status=OrderStatus(order.status),
            fulfillment_input=order.fulfillment_input,
            fulfillment_note=order.fulfillment_note,
            fulfilled_by_admin_id=order.fulfilled_by_admin_id,
            fulfilled_at=order.fulfilled_at,
            created_at=order.created_at,
            updated_at=order.updated_at,
            items=item_dtos,
        )

    @staticmethod
    def _resolve_unit_price(product: Product, quantity: int, currency: Currency) -> Decimal:
        if quantity < 1:
            raise ValidationError("Quantity must be at least 1")

        matching_tiers = [t for t in (product.bulk_tiers or []) if t.min_qty <= quantity]
        best_tier = max(matching_tiers, key=lambda t: t.min_qty) if matching_tiers else None

        if currency == Currency.USD:
            raw_price = best_tier.unit_price_usd if best_tier is not None else product.price_usd
            return _quantize_for_currency(Decimal(raw_price), Currency.USD)

        if currency == Currency.IRT:
            if product.price_irt is None:
                raise ValidationError("IRT pricing is not configured for this product")
            if best_tier is not None and best_tier.unit_price_irt is not None:
                raw_price = best_tier.unit_price_irt
            else:
                raw_price = product.price_irt
            return _quantize_for_currency(Decimal(raw_price), Currency.IRT)

        raise ValidationError(f"Unsupported currency: {currency}")

    @staticmethod
    def _validate_hex_and_emoji(color_emoji: str, color_hex: str) -> tuple[str, str]:
        cleaned_emoji = (color_emoji or "").strip()
        if not cleaned_emoji:
            raise ValidationError("color_emoji must not be empty")
        if not is_valid_hex_color(color_hex):
            raise ValidationError("color_hex must match #RRGGBB")
        return cleaned_emoji, normalize_hex_color(color_hex)

    # ------------------------------------------------------------------
    # Categories CRUD
    # ------------------------------------------------------------------
    async def list_categories(self, *, active_only: bool = True) -> list[CategoryDTO]:
        """List categories ordered by sort_order then id."""
        async with self._session_factory() as session:
            stmt = select(Category)
            if active_only:
                stmt = stmt.where(Category.is_active.is_(True))
            stmt = stmt.order_by(Category.sort_order.asc(), Category.id.asc())
            rows = (await session.execute(stmt)).scalars().all()
            return [CategoryDTO.model_validate(row) for row in rows]

    async def create_category(self, data: CategoryCreateDTO) -> CategoryDTO:
        """Create a new product category with dual-layer color validation."""
        slug = data.slug.strip()
        if not slug:
            raise ValidationError("Category slug must not be empty")
        emoji, hex_color = self._validate_hex_and_emoji(data.color_emoji, data.color_hex)

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    cat = Category(
                        slug=slug,
                        sort_order=data.sort_order,
                        is_active=data.is_active,
                        color_emoji=emoji,
                        color_hex=hex_color,
                        names_json=dict(data.names_json),
                        descriptions_json=dict(data.descriptions_json),
                    )
                    session.add(cat)
                    await session.flush()
                    await session.refresh(cat)
                    return CategoryDTO.model_validate(cat)
        except IntegrityError as exc:
            raise ValidationError(f"Category with slug '{slug}' already exists") from exc

    async def update_category(self, category_id: int, data: CategoryUpdateDTO) -> CategoryDTO:
        """Update an existing category's metadata, colors, or localized labels."""
        updates = data.model_dump(exclude_unset=True)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    cat = (
                        await session.execute(
                            select(Category).where(Category.id == category_id)
                        )
                    ).scalar_one_or_none()
                    if cat is None:
                        raise NotFoundError(f"Category {category_id} not found")

                    if "slug" in updates and updates["slug"] is not None:
                        slug = updates["slug"].strip()
                        if not slug:
                            raise ValidationError("Category slug must not be empty")
                        cat.slug = slug
                    if "sort_order" in updates and updates["sort_order"] is not None:
                        cat.sort_order = updates["sort_order"]
                    if "is_active" in updates and updates["is_active"] is not None:
                        cat.is_active = updates["is_active"]
                    if "color_emoji" in updates and updates["color_emoji"] is not None:
                        cleaned_emoji = updates["color_emoji"].strip()
                        if not cleaned_emoji:
                            raise ValidationError("color_emoji must not be empty")
                        cat.color_emoji = cleaned_emoji
                    if "color_hex" in updates and updates["color_hex"] is not None:
                        if not is_valid_hex_color(updates["color_hex"]):
                            raise ValidationError("color_hex must match #RRGGBB")
                        cat.color_hex = normalize_hex_color(updates["color_hex"])
                    if "names_json" in updates and updates["names_json"] is not None:
                        cat.names_json = dict(updates["names_json"])
                    if "descriptions_json" in updates and updates["descriptions_json"] is not None:
                        cat.descriptions_json = dict(updates["descriptions_json"])

                    await session.flush()
                    await session.refresh(cat)
                    return CategoryDTO.model_validate(cat)
        except IntegrityError as exc:
            raise ValidationError("Category update violated uniqueness constraints") from exc

    async def delete_category(self, category_id: int) -> None:
        """Delete a category if no existing orders block deletion."""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    cat = (
                        await session.execute(
                            select(Category).where(Category.id == category_id)
                        )
                    ).scalar_one_or_none()
                    if cat is None:
                        raise NotFoundError(f"Category {category_id} not found")
                    await session.delete(cat)
                    await session.flush()
        except IntegrityError as exc:
            raise ValidationError(
                "Cannot delete category with products linked to existing orders; deactivate it instead"
            ) from exc

    # ------------------------------------------------------------------
    # Products CRUD & Bulk Tiers
    # ------------------------------------------------------------------
    async def list_products(
        self,
        *,
        category_id: int | None = None,
        active_only: bool = True,
        pinned_only: bool = False,
    ) -> list[ProductDTO]:
        """List products filtered by category, active status, or pinned flag."""
        async with self._session_factory() as session:
            stmt = select(Product)
            if category_id is not None:
                stmt = stmt.where(Product.category_id == category_id)
            if active_only:
                stmt = stmt.where(Product.is_active.is_(True))
            if pinned_only:
                stmt = stmt.join(Category, Product.category_id == Category.id).where(
                    Product.is_pinned.is_(True),
                    Category.is_active.is_(True),
                )
            stmt = stmt.order_by(Product.sort_order.asc(), Product.id.asc())
            products = list((await session.execute(stmt)).scalars().all())
            auto_ids = [p.id for p in products if p.delivery_type == DeliveryType.AUTO.value]
            stock_map = await self._fetch_available_stock_map(session, auto_ids)
            return [
                self._to_product_dto(p, self._compute_available_stock(p, stock_map))
                for p in products
            ]

    async def get_product_detail(self, product_id: int) -> ProductDetailDTO:
        """Fetch full product details including bulk discount tiers, category, and stock count."""
        async with self._session_factory() as session:
            product = (
                await session.execute(
                    select(Product)
                    .options(
                        selectinload(Product.bulk_tiers),
                        selectinload(Product.category),
                    )
                    .where(Product.id == product_id)
                )
            ).scalar_one_or_none()
            if product is None:
                raise NotFoundError(f"Product {product_id} not found")

            stock_map: dict[int, int] = {}
            if product.delivery_type == DeliveryType.AUTO.value:
                stock_map = await self._fetch_available_stock_map(session, [product.id])
            return self._to_product_detail_dto(
                product, self._compute_available_stock(product, stock_map)
            )

    async def create_product(self, data: ProductCreateDTO) -> ProductDetailDTO:
        """Create a new product and optional initial bulk discount tiers."""
        slug = data.slug.strip()
        if not slug:
            raise ValidationError("Product slug must not be empty")
        emoji, hex_color = self._validate_hex_and_emoji(data.color_emoji, data.color_hex)
        price_usd = _quantize_for_currency(Decimal(data.price_usd), Currency.USD)
        price_irt = (
            _quantize_for_currency(Decimal(data.price_irt), Currency.IRT)
            if data.price_irt is not None
            else None
        )

        seen_min_qtys: set[int] = set()
        for tier in data.bulk_tiers:
            if tier.min_qty in seen_min_qtys:
                raise ValidationError(f"Duplicate bulk tier min_qty: {tier.min_qty}")
            seen_min_qtys.add(tier.min_qty)

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    cat = (
                        await session.execute(
                            select(Category.id).where(Category.id == data.category_id)
                        )
                    ).scalar_one_or_none()
                    if cat is None:
                        raise NotFoundError(f"Category {data.category_id} not found")

                    product = Product(
                        category_id=data.category_id,
                        slug=slug,
                        delivery_type=data.delivery_type.value,
                        manual_prompt_key=(
                            data.manual_prompt_key.strip() if data.manual_prompt_key else None
                        ),
                        price_usd=price_usd,
                        price_irt=price_irt,
                        color_emoji=emoji,
                        color_hex=hex_color,
                        is_pinned=data.is_pinned,
                        is_active=data.is_active,
                        sort_order=data.sort_order,
                        manual_stock_count=data.manual_stock_count,
                        low_stock_threshold=data.low_stock_threshold,
                        sold_count=0,
                        titles_json=dict(data.titles_json),
                        descriptions_json=dict(data.descriptions_json),
                        rules_json=dict(data.rules_json),
                        manual_prompts_json=dict(data.manual_prompts_json),
                    )
                    session.add(product)
                    await session.flush()

                    for tier in sorted(data.bulk_tiers, key=lambda t: t.min_qty):
                        tier_usd = _quantize_for_currency(Decimal(tier.unit_price_usd), Currency.USD)
                        tier_irt = (
                            _quantize_for_currency(Decimal(tier.unit_price_irt), Currency.IRT)
                            if tier.unit_price_irt is not None
                            else None
                        )
                        session.add(
                            ProductBulkTier(
                                product_id=product.id,
                                min_qty=tier.min_qty,
                                unit_price_usd=tier_usd,
                                unit_price_irt=tier_irt,
                            )
                        )
                    await session.flush()
                    new_product_id = product.id
        except IntegrityError as exc:
            raise ValidationError(f"Product with slug '{slug}' already exists") from exc

        return await self.get_product_detail(new_product_id)

    async def update_product(self, product_id: int, data: ProductUpdateDTO) -> ProductDetailDTO:
        """Update product fields, dual-layer colors, pricing, and localization JSON."""
        updates = data.model_dump(exclude_unset=True)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    product = (
                        await session.execute(
                            select(Product).where(Product.id == product_id)
                        )
                    ).scalar_one_or_none()
                    if product is None:
                        raise NotFoundError(f"Product {product_id} not found")

                    if "category_id" in updates and updates["category_id"] is not None:
                        cat_exists = (
                            await session.execute(
                                select(Category.id).where(Category.id == updates["category_id"])
                            )
                        ).scalar_one_or_none()
                        if cat_exists is None:
                            raise NotFoundError(f"Category {updates['category_id']} not found")
                        product.category_id = updates["category_id"]

                    if "slug" in updates and updates["slug"] is not None:
                        slug = updates["slug"].strip()
                        if not slug:
                            raise ValidationError("Product slug must not be empty")
                        product.slug = slug

                    if "delivery_type" in updates and updates["delivery_type"] is not None:
                        dt = updates["delivery_type"]
                        product.delivery_type = dt.value if isinstance(dt, DeliveryType) else str(dt)

                    if "manual_prompt_key" in updates:
                        mpk = updates["manual_prompt_key"]
                        product.manual_prompt_key = (
                            mpk.strip() if isinstance(mpk, str) and mpk.strip() else None
                        )

                    if "price_usd" in updates and updates["price_usd"] is not None:
                        product.price_usd = _quantize_for_currency(
                            Decimal(updates["price_usd"]), Currency.USD
                        )

                    if "price_irt" in updates:
                        product.price_irt = (
                            _quantize_for_currency(Decimal(updates["price_irt"]), Currency.IRT)
                            if updates["price_irt"] is not None
                            else None
                        )

                    if "color_emoji" in updates and updates["color_emoji"] is not None:
                        cleaned_emoji = updates["color_emoji"].strip()
                        if not cleaned_emoji:
                            raise ValidationError("color_emoji must not be empty")
                        product.color_emoji = cleaned_emoji

                    if "color_hex" in updates and updates["color_hex"] is not None:
                        if not is_valid_hex_color(updates["color_hex"]):
                            raise ValidationError("color_hex must match #RRGGBB")
                        product.color_hex = normalize_hex_color(updates["color_hex"])

                    for bool_or_int_field in (
                        "is_pinned",
                        "is_active",
                        "sort_order",
                        "manual_stock_count",
                        "low_stock_threshold",
                    ):
                        if bool_or_int_field in updates and updates[bool_or_int_field] is not None:
                            setattr(product, bool_or_int_field, updates[bool_or_int_field])

                    for json_field in (
                        "titles_json",
                        "descriptions_json",
                        "rules_json",
                        "manual_prompts_json",
                    ):
                        if json_field in updates and updates[json_field] is not None:
                            setattr(product, json_field, dict(updates[json_field]))

                    await session.flush()
        except IntegrityError as exc:
            raise ValidationError("Product update violated uniqueness constraints") from exc

        return await self.get_product_detail(product_id)

    async def delete_product(self, product_id: int) -> None:
        """Delete a product if no historical orders reference it."""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    product = (
                        await session.execute(
                            select(Product).where(Product.id == product_id)
                        )
                    ).scalar_one_or_none()
                    if product is None:
                        raise NotFoundError(f"Product {product_id} not found")
                    await session.delete(product)
                    await session.flush()
        except IntegrityError as exc:
            raise ValidationError(
                "Cannot delete product referenced by existing orders; deactivate it instead"
            ) from exc

    async def set_bulk_tiers(
        self, product_id: int, tiers: list[ProductBulkTierDTO]
    ) -> list[ProductBulkTierDTO]:
        """Replace all quantity-based bulk discount tiers for a product atomically."""
        seen_min_qtys: set[int] = set()
        for tier in tiers:
            if tier.min_qty < 2:
                raise ValidationError("Bulk tier min_qty must be at least 2")
            if tier.min_qty in seen_min_qtys:
                raise ValidationError(f"Duplicate bulk tier min_qty: {tier.min_qty}")
            seen_min_qtys.add(tier.min_qty)

        async with self._session_factory() as session:
            async with session.begin():
                product = (
                    await session.execute(
                        select(Product)
                        .options(selectinload(Product.bulk_tiers))
                        .where(Product.id == product_id)
                    )
                ).scalar_one_or_none()
                if product is None:
                    raise NotFoundError(f"Product {product_id} not found")

                product.bulk_tiers.clear()
                await session.flush()

                new_rows: list[ProductBulkTier] = []
                for tier in sorted(tiers, key=lambda t: t.min_qty):
                    tier_usd = _quantize_for_currency(Decimal(tier.unit_price_usd), Currency.USD)
                    tier_irt = (
                        _quantize_for_currency(Decimal(tier.unit_price_irt), Currency.IRT)
                        if tier.unit_price_irt is not None
                        else None
                    )
                    row = ProductBulkTier(
                        product_id=product_id,
                        min_qty=tier.min_qty,
                        unit_price_usd=tier_usd,
                        unit_price_irt=tier_irt,
                    )
                    session.add(row)
                    new_rows.append(row)

                await session.flush()
                for row in new_rows:
                    await session.refresh(row)
                return [ProductBulkTierDTO.model_validate(r) for r in new_rows]

    # ------------------------------------------------------------------
    # Auto-Delivery Inventory & Pricing
    # ------------------------------------------------------------------
    async def add_auto_stock(
        self, product_id: int, payloads: list[str], *, deduplicate: bool = False
    ) -> StockImportResultDTO:
        """Encrypt each credential payload with Fernet and persist to product_stock."""
        async with self._session_factory() as session:
            async with session.begin():
                product = (
                    await session.execute(
                        select(Product).where(Product.id == product_id)
                    )
                ).scalar_one_or_none()
                if product is None:
                    raise NotFoundError(f"Product {product_id} not found")
                if product.delivery_type != DeliveryType.AUTO.value:
                    raise ValidationError(
                        "Auto-delivery stock can only be added to auto-delivery products"
                    )

                cleaned_items = [str(item).strip() for item in payloads if str(item).strip()]
                existing_hashes: set[str] = set()
                if deduplicate and cleaned_items:
                    candidate_hashes = [sha256_hex(item) for item in cleaned_items]
                    existing_hashes = set(
                        (
                            await session.execute(
                                select(ProductStock.payload_hash).where(
                                    ProductStock.product_id == product_id,
                                    ProductStock.payload_hash.in_(candidate_hashes),
                                )
                            )
                        ).scalars().all()
                    )

                added_count = 0
                skipped_duplicates = 0
                for item in cleaned_items:
                    payload_hash = sha256_hex(item)
                    if deduplicate and payload_hash in existing_hashes:
                        skipped_duplicates += 1
                        continue
                    encrypted = encrypt_secret(item, self._encryption_key)
                    session.add(
                        ProductStock(
                            product_id=product_id,
                            encrypted_payload=encrypted,
                            payload_hash=payload_hash,
                            status=StockStatus.AVAILABLE.value,
                        )
                    )
                    if deduplicate:
                        existing_hashes.add(payload_hash)
                    added_count += 1

                await session.flush()
                total_available = int(
                    (
                        await session.execute(
                            select(func.count(ProductStock.id)).where(
                                ProductStock.product_id == product_id,
                                ProductStock.status == StockStatus.AVAILABLE.value,
                            )
                        )
                    ).scalar_one()
                )
                return StockImportResultDTO(
                    product_id=product_id,
                    added_count=added_count,
                    skipped_duplicates=skipped_duplicates,
                    total_available=total_available,
                )

    async def calculate_unit_price(
        self, product_id: int, quantity: int, currency: Currency
    ) -> Decimal:
        """Compute bulk-discounted Decimal unit price where highest matching min_qty <= quantity wins."""
        if quantity < 1:
            raise ValidationError("Quantity must be at least 1")
        async with self._session_factory() as session:
            product = (
                await session.execute(
                    select(Product)
                    .options(selectinload(Product.bulk_tiers))
                    .where(Product.id == product_id)
                )
            ).scalar_one_or_none()
            if product is None:
                raise NotFoundError(f"Product {product_id} not found")
            return self._resolve_unit_price(product, quantity, currency)

    # ------------------------------------------------------------------
    # Atomic Purchase & Fulfillment
    # ------------------------------------------------------------------
    async def purchase_product(self, request: PurchaseRequestDTO) -> PurchaseResultDTO:
        """Execute balance verification, row-locked stock allocation, ledger entry, and order creation atomically."""
        if request.quantity < 1 or request.quantity > 1000:
            raise ValidationError("Quantity must be between 1 and 1000")

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                user = (
                    await session.execute(
                        select(User).where(User.id == request.user_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if user is None:
                    raise NotFoundError(f"User {request.user_id} not found")
                if user.is_blocked:
                    raise UserBlockedError("User account is blocked")

                product = (
                    await session.execute(
                        select(Product)
                        .options(
                            selectinload(Product.bulk_tiers),
                            selectinload(Product.category),
                        )
                        .where(Product.id == request.product_id)
                    )
                ).scalar_one_or_none()
                if product is None:
                    raise NotFoundError(f"Product {request.product_id} not found")
                if not product.is_active or (
                    product.category is not None and not product.category.is_active
                ):
                    raise ValidationError("Product is currently unavailable")

                unit_price = self._resolve_unit_price(product, request.quantity, request.currency)
                total_amount = _quantize_for_currency(
                    unit_price * Decimal(request.quantity), request.currency
                )

                if request.currency == Currency.USD:
                    before_balance = _quantize_for_currency(
                        Decimal(user.balance_usd), Currency.USD
                    )
                    if before_balance < total_amount:
                        raise InsufficientBalanceError(
                            "Insufficient USD balance",
                            details={
                                "required": str(total_amount),
                                "available": str(before_balance),
                                "currency": Currency.USD.value,
                            },
                        )
                    after_balance = _quantize_for_currency(
                        before_balance - total_amount, Currency.USD
                    )
                    user.balance_usd = after_balance
                elif request.currency == Currency.IRT:
                    before_balance = _quantize_for_currency(
                        Decimal(user.balance_irt), Currency.IRT
                    )
                    if before_balance < total_amount:
                        raise InsufficientBalanceError(
                            "Insufficient IRT balance",
                            details={
                                "required": str(total_amount),
                                "available": str(before_balance),
                                "currency": Currency.IRT.value,
                            },
                        )
                    after_balance = _quantize_for_currency(
                        before_balance - total_amount, Currency.IRT
                    )
                    user.balance_irt = after_balance
                else:
                    raise ValidationError(f"Unsupported currency: {request.currency}")

                stock_rows: list[ProductStock] = []
                delivered_items: list[str] = []
                cleaned_input = (
                    request.fulfillment_input.strip()
                    if request.fulfillment_input and request.fulfillment_input.strip()
                    else None
                )

                if product.delivery_type == DeliveryType.AUTO.value:
                    stock_rows = list(
                        (
                            await session.execute(
                                select(ProductStock)
                                .where(
                                    ProductStock.product_id == product.id,
                                    ProductStock.status == StockStatus.AVAILABLE.value,
                                )
                                .order_by(ProductStock.id.asc())
                                .limit(request.quantity)
                                .with_for_update(skip_locked=True)
                            )
                        ).scalars().all()
                    )
                    if len(stock_rows) < request.quantity:
                        raise InsufficientStockError(
                            f"Insufficient stock: requested {request.quantity}, available {len(stock_rows)}",
                            details={
                                "available": len(stock_rows),
                                "requested": request.quantity,
                            },
                        )
                    for row in stock_rows:
                        delivered_items.append(
                            decrypt_secret(row.encrypted_payload, self._encryption_key)
                        )
                    order_status = OrderStatus.COMPLETED.value
                    fulfilled_at: datetime | None = now
                    await session.execute(
                        update(Product)
                        .where(Product.id == product.id)
                        .values(sold_count=Product.sold_count + request.quantity)
                    )
                else:
                    locked_product = (
                        await session.execute(
                            select(Product)
                            .where(Product.id == product.id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                    ).scalar_one()
                    if locked_product.manual_stock_count >= 0:
                        if locked_product.manual_stock_count < request.quantity:
                            raise InsufficientStockError(
                                f"Insufficient manual stock: requested {request.quantity}, available {locked_product.manual_stock_count}",
                                details={
                                    "available": locked_product.manual_stock_count,
                                    "requested": request.quantity,
                                },
                            )
                        locked_product.manual_stock_count -= request.quantity
                    locked_product.sold_count += request.quantity

                    has_prompt = bool(
                        (
                            locked_product.manual_prompt_key
                            and locked_product.manual_prompt_key.strip()
                        )
                        or (
                            locked_product.manual_prompts_json
                            and any(
                                str(v).strip()
                                for v in locked_product.manual_prompts_json.values()
                            )
                        )
                    )
                    if cleaned_input is not None:
                        order_status = OrderStatus.PENDING_FULFILLMENT.value
                    elif has_prompt:
                        order_status = OrderStatus.PENDING_INPUT.value
                    else:
                        order_status = OrderStatus.PENDING_FULFILLMENT.value
                    fulfilled_at = None

                order = Order(
                    order_number=_generate_order_number(now),
                    user_id=user.id,
                    product_id=product.id,
                    quantity=request.quantity,
                    currency=request.currency.value,
                    unit_price=unit_price,
                    total_amount=total_amount,
                    status=order_status,
                    fulfillment_input=cleaned_input,
                    fulfilled_at=fulfilled_at,
                )
                session.add(order)
                await session.flush()

                decrypted_by_item_id: dict[int, str] = {}
                created_items: list[OrderItem] = []
                if stock_rows:
                    for stock_row in stock_rows:
                        stock_row.status = StockStatus.DELIVERED.value
                        stock_row.order_id = order.id
                        stock_row.delivered_at = now
                        order_item = OrderItem(
                            order_id=order.id,
                            product_id=product.id,
                            stock_id=stock_row.id,
                            encrypted_delivered_payload=stock_row.encrypted_payload,
                            unit_price=unit_price,
                        )
                        session.add(order_item)
                        created_items.append(order_item)
                    await session.flush()
                    for order_item, plain in zip(created_items, delivered_items, strict=True):
                        decrypted_by_item_id[order_item.id] = plain

                session.add(
                    WalletLedger(
                        user_id=user.id,
                        currency=request.currency.value,
                        before_balance=before_balance,
                        change_amount=-total_amount,
                        after_balance=after_balance,
                        reference_type=WalletLedgerRefType.ORDER_PURCHASE.value,
                        reference_id=order.id,
                        admin_id=None,
                        note=f"Order {order.order_number} ({request.quantity}x {product.slug})",
                    )
                )

                await session.flush()
                refreshed_order = (
                    await session.execute(
                        select(Order)
                        .options(selectinload(Order.items))
                        .where(Order.id == order.id)
                    )
                ).scalar_one()
                order_dto = self._to_order_dto(
                    refreshed_order, decrypted_map=decrypted_by_item_id
                )
                return PurchaseResultDTO(
                    order=order_dto,
                    delivered_items=delivered_items,
                    new_balance=after_balance,
                )

    async def submit_manual_fulfillment_input(
        self, order_id: int, user_id: int, fulfillment_input: str
    ) -> OrderDTO:
        """Record user-submitted activation details on a pending_input order."""
        cleaned = (fulfillment_input or "").strip()
        if not cleaned:
            raise ValidationError("Fulfillment input must not be empty")

        async with self._session_factory() as session:
            async with session.begin():
                order = (
                    await session.execute(
                        select(Order)
                        .options(selectinload(Order.items))
                        .where(Order.id == order_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if order is None:
                    raise NotFoundError(f"Order {order_id} not found")
                if order.user_id != user_id:
                    raise AuthorizationError("You are not authorized to update this order")
                if order.status != OrderStatus.PENDING_INPUT.value:
                    raise InvalidStateTransitionError(
                        f"Order {order.order_number} is in status '{order.status}', expected 'pending_input'"
                    )

                order.fulfillment_input = cleaned
                order.status = OrderStatus.PENDING_FULFILLMENT.value
                await session.flush()
                await session.refresh(order)
                return self._to_order_dto(order)

    async def fulfill_manual_order(
        self, order_id: int, admin_id: int, fulfillment_note: str | None = None
    ) -> OrderDTO:
        """Mark a pending manual order as completed with an optional admin delivery note."""
        async with self._session_factory() as session:
            async with session.begin():
                order = (
                    await session.execute(
                        select(Order)
                        .options(selectinload(Order.items))
                        .where(Order.id == order_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if order is None:
                    raise NotFoundError(f"Order {order_id} not found")
                if order.status not in {
                    OrderStatus.PENDING_INPUT.value,
                    OrderStatus.PENDING_FULFILLMENT.value,
                }:
                    raise InvalidStateTransitionError(
                        f"Cannot fulfill order in status '{order.status}'"
                    )

                order.status = OrderStatus.COMPLETED.value
                order.fulfilled_by_admin_id = admin_id
                order.fulfilled_at = datetime.now(UTC)
                order.fulfillment_note = (
                    fulfillment_note.strip()
                    if fulfillment_note and fulfillment_note.strip()
                    else None
                )
                await session.flush()
                await session.refresh(order)
                return self._to_order_dto(order)

    async def cancel_and_refund_order(
        self, order_id: int, admin_id: int, reason: str | None = None
    ) -> OrderDTO:
        """Atomically refund an order to the user's wallet and append an immutable WalletLedger entry."""
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                order = (
                    await session.execute(
                        select(Order)
                        .options(selectinload(Order.items))
                        .where(Order.id == order_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if order is None:
                    raise NotFoundError(f"Order {order_id} not found")
                if order.status in {OrderStatus.REFUNDED.value, OrderStatus.CANCELLED.value}:
                    raise InvalidStateTransitionError(
                        f"Order {order.order_number} is already '{order.status}'"
                    )

                user = (
                    await session.execute(
                        select(User).where(User.id == order.user_id).with_for_update()
                    )
                ).scalar_one()

                currency = Currency(order.currency)
                refund_amount = _quantize_for_currency(Decimal(order.total_amount), currency)

                if currency == Currency.USD:
                    before_balance = _quantize_for_currency(
                        Decimal(user.balance_usd), Currency.USD
                    )
                    after_balance = _quantize_for_currency(
                        before_balance + refund_amount, Currency.USD
                    )
                    user.balance_usd = after_balance
                else:
                    before_balance = _quantize_for_currency(
                        Decimal(user.balance_irt), Currency.IRT
                    )
                    after_balance = _quantize_for_currency(
                        before_balance + refund_amount, Currency.IRT
                    )
                    user.balance_irt = after_balance

                product = (
                    await session.execute(
                        select(Product).where(Product.id == order.product_id).with_for_update()
                    )
                ).scalar_one()
                product.sold_count = max(0, product.sold_count - order.quantity)
                if (
                    product.delivery_type == DeliveryType.MANUAL.value
                    and product.manual_stock_count >= 0
                ):
                    product.manual_stock_count += order.quantity

                cleaned_reason = reason.strip() if reason and reason.strip() else None
                order.status = OrderStatus.REFUNDED.value
                if cleaned_reason is not None:
                    order.fulfillment_note = cleaned_reason
                order.fulfilled_by_admin_id = admin_id
                order.fulfilled_at = now

                session.add(
                    WalletLedger(
                        user_id=user.id,
                        currency=currency.value,
                        before_balance=before_balance,
                        change_amount=refund_amount,
                        after_balance=after_balance,
                        reference_type=WalletLedgerRefType.ORDER_REFUND.value,
                        reference_id=order.id,
                        admin_id=admin_id,
                        note=cleaned_reason or f"Refund for order {order.order_number}",
                    )
                )
                await session.flush()
                await session.refresh(order)
                return self._to_order_dto(order)

    # ------------------------------------------------------------------
    # Order Queries
    # ------------------------------------------------------------------
    async def get_order_detail(
        self, order_id: int, *, decrypt_items: bool = False
    ) -> OrderDTO:
        """Fetch a single order and optionally decrypt delivered item payloads."""
        async with self._session_factory() as session:
            order = (
                await session.execute(
                    select(Order)
                    .options(selectinload(Order.items))
                    .where(Order.id == order_id)
                )
            ).scalar_one_or_none()
            if order is None:
                raise NotFoundError(f"Order {order_id} not found")
            return self._to_order_dto(order, decrypt_items=decrypt_items)

    async def list_user_orders(
        self, user_id: int, *, limit: int = 20, offset: int = 0
    ) -> list[OrderDTO]:
        """List paginated orders placed by a specific user, newest first."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Order)
                    .options(selectinload(Order.items))
                    .where(Order.user_id == user_id)
                    .order_by(Order.created_at.desc(), Order.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars().all()
            return [self._to_order_dto(row, decrypt_items=False) for row in rows]

    async def list_orders(
        self,
        *,
        status: OrderStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[OrderDTO]:
        """List orders globally for Admin review, optionally filtered by status."""
        async with self._session_factory() as session:
            stmt = select(Order).options(selectinload(Order.items))
            if status is not None:
                stmt = stmt.where(Order.status == status.value)
            stmt = (
                stmt.order_by(Order.created_at.desc(), Order.id.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_order_dto(row, decrypt_items=False) for row in rows]
