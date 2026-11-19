"""PostgreSQL 16 + SQLAlchemy 2.0 Async ORM models for the Telegram Digital Shop Bot."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """Registered Telegram user with multi-currency balances and RBAC/ban flags."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("balance_usd >= 0", name="balance_usd_non_negative"),
        CheckConstraint("balance_irt >= 0", name="balance_irt_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language_code: Mapped[str] = mapped_column(String(10), default="en", server_default="en", nullable=False)
    language_selected: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.false(), nullable=False
    )
    balance_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0.00000000"), server_default="0", nullable=False
    )
    balance_irt: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0.00"), server_default="0", nullable=False
    )
    is_blocked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.false(), index=True, nullable=False
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.false(), index=True, nullable=False
    )

    ledger_entries: Mapped[list[WalletLedger]] = relationship(back_populates="user")
    orders: Mapped[list[Order]] = relationship(back_populates="user")
    payments: Mapped[list[Payment]] = relationship(back_populates="user")
    support_tickets: Mapped[list[SupportTicket]] = relationship(back_populates="user")


class WalletLedger(Base):
    """Immutable append-only audit trail for every wallet balance mutation."""

    __tablename__ = "wallet_ledger"
    __table_args__ = (
        CheckConstraint("before_balance >= 0", name="before_balance_non_negative"),
        CheckConstraint("after_balance >= 0", name="after_balance_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    before_balance: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    change_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    after_balance: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    reference_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    reference_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="ledger_entries")


class Category(Base, TimestampMixin):
    """Digital goods category with dual-layer colors and 12-language localized names."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.true(), index=True, nullable=False
    )
    color_emoji: Mapped[str] = mapped_column(String(16), default="🔵", server_default="🔵", nullable=False)
    color_hex: Mapped[str] = mapped_column(String(7), default="#3b82f6", server_default="#3b82f6", nullable=False)
    names_json: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    descriptions_json: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    products: Mapped[list[Product]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class Product(Base, TimestampMixin):
    """Digital product or manual fulfillment service with bulk pricing and dual colors."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price_usd >= 0", name="price_usd_non_negative"),
        CheckConstraint("price_irt IS NULL OR price_irt >= 0", name="price_irt_non_negative"),
        CheckConstraint("sold_count >= 0", name="sold_count_non_negative"),
        CheckConstraint("manual_stock_count >= -1", name="manual_stock_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="CASCADE"), index=True, nullable=False
    )
    slug: Mapped[str] = mapped_column(String(96), unique=True, index=True, nullable=False)
    delivery_type: Mapped[str] = mapped_column(String(16), default="auto", server_default="auto", nullable=False)
    manual_prompt_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    price_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    price_irt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    color_emoji: Mapped[str] = mapped_column(String(16), default="🟢", server_default="🟢", nullable=False)
    color_hex: Mapped[str] = mapped_column(String(7), default="#22c55e", server_default="#22c55e", nullable=False)
    is_pinned: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.false(), index=True, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.true(), index=True, nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True, nullable=False)
    manual_stock_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5, server_default="5", nullable=False)
    sold_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    titles_json: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    descriptions_json: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    rules_json: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    manual_prompts_json: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    category: Mapped[Category] = relationship(back_populates="products")
    bulk_tiers: Mapped[list[ProductBulkTier]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductBulkTier.min_qty.asc()",
    )
    stock_items: Mapped[list[ProductStock]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    orders: Mapped[list[Order]] = relationship(back_populates="product")


class ProductBulkTier(Base, TimestampMixin):
    """Quantity-based discounted unit pricing tier for a product."""

    __tablename__ = "product_bulk_tiers"
    __table_args__ = (
        UniqueConstraint("product_id", "min_qty", name="uq_product_bulk_tiers_product_min_qty"),
        CheckConstraint("min_qty >= 2", name="min_qty_at_least_two"),
        CheckConstraint("unit_price_usd >= 0", name="unit_price_usd_non_negative"),
        CheckConstraint("unit_price_irt IS NULL OR unit_price_irt >= 0", name="unit_price_irt_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    min_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    unit_price_irt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)

    product: Mapped[Product] = relationship(back_populates="bulk_tiers")


class Order(Base, TimestampMixin):
    """Customer purchase order supporting both auto-delivery and manual fulfillment."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("quantity >= 1", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    fulfillment_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    fulfillment_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    fulfilled_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="orders")
    product: Mapped[Product] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    allocated_stock: Mapped[list[ProductStock]] = relationship(
        back_populates="order", foreign_keys="ProductStock.order_id"
    )


class ProductStock(Base):
    """Fernet-encrypted auto-delivery credential inventory locked with SKIP LOCKED."""

    __tablename__ = "product_stock"
    __table_args__ = (
        Index("ix_product_stock_alloc", "product_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="available", server_default="available", index=True, nullable=False
    )
    order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="SET NULL"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped[Product] = relationship(back_populates="stock_items")
    order: Mapped[Order | None] = relationship(
        back_populates="allocated_stock", foreign_keys=[order_id]
    )


class OrderItem(Base):
    """Delivered line item preserving Fernet-encrypted credentials for user Order History."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    stock_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("product_stock.id", ondelete="SET NULL"), nullable=True
    )
    encrypted_delivered_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="items")


class CryptoWallet(Base, TimestampMixin):
    """Dynamic admin-configured cryptocurrency deposit wallet and network instructions."""

    __tablename__ = "crypto_wallets"
    __table_args__ = (
        CheckConstraint("min_deposit_usd >= 0", name="min_deposit_usd_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin_name: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    network: Mapped[str] = mapped_column(String(32), nullable=False)
    wallet_address: Mapped[str] = mapped_column(String(256), nullable=False)
    qr_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    min_deposit_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("1.00000000"), server_default="1", nullable=False
    )
    fee_note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions_json: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    verification_mode: Mapped[str] = mapped_column(
        String(32), default="manual_txid", server_default="manual_txid", nullable=False
    )
    explorer_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.true(), index=True, nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class FiatPaymentMethod(Base, TimestampMixin):
    """Iranian Rial/Toman payment method (Card-to-Card or Gateway), active strictly for fa."""

    __tablename__ = "fiat_payment_methods"
    __table_args__ = (
        CheckConstraint("min_deposit_irt >= 0", name="min_deposit_irt_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    method_type: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    card_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sheba: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cardholder_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gateway_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gateway_merchant_id_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_deposit_irt: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("50000.00"), server_default="50000", nullable=False
    )
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.true(), index=True, nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class Payment(Base):
    """Wallet deposit request with strict replay protection on tx_hash & authority_track_id."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_usd >= 0", name="amount_usd_non_negative"),
        CheckConstraint("amount_irt >= 0", name="amount_irt_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    payment_category: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0.00000000"), server_default="0", nullable=False
    )
    amount_irt: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0.00"), server_default="0", nullable=False
    )
    exchange_rate_used: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    crypto_wallet_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("crypto_wallets.id", ondelete="SET NULL"), nullable=True
    )
    fiat_method_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("fiat_payment_methods.id", ondelete="SET NULL"), nullable=True
    )
    tx_hash: Mapped[str | None] = mapped_column(String(256), unique=True, index=True, nullable=True)
    authority_track_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
    receipt_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    receipt_local_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    receipt_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", index=True, nullable=False
    )
    admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="payments")
    crypto_wallet: Mapped[CryptoWallet | None] = relationship()
    fiat_method: Mapped[FiatPaymentMethod | None] = relationship()


class SupportTicket(Base, TimestampMixin):
    """Two-way support conversation ticket opened by a user."""

    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_number: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="open", server_default="open", index=True, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="support_tickets")
    messages: Mapped[list[SupportMessage]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="SupportMessage.created_at.asc()",
    )


class SupportMessage(Base):
    """Individual text or media message inside a SupportTicket thread."""

    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sender_role: Mapped[str] = mapped_column(String(16), nullable=False)
    sender_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    file_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticket: Mapped[SupportTicket] = relationship(back_populates="messages")


class UITranslation(Base):
    """Dynamic CMS text dictionary keyed by (key, lang_code) across 12 languages."""

    __tablename__ = "ui_translations"
    __table_args__ = (
        UniqueConstraint("key", "lang_code", name="uq_ui_translations_key_lang"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    lang_code: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    text_value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UIButton(Base, TimestampMixin):
    """Dynamic CMS button layout and dual-layer color configuration."""

    __tablename__ = "ui_buttons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    button_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    menu_scope: Mapped[str] = mapped_column(
        String(32), default="main", server_default="main", index=True, nullable=False
    )
    row_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    col_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    color_emoji: Mapped[str] = mapped_column(String(16), default="🔵", server_default="🔵", nullable=False)
    color_hex: Mapped[str] = mapped_column(String(7), default="#3b82f6", server_default="#3b82f6", nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.true(), nullable=False
    )
    labels_json: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)


class AppSetting(Base):
    """Dynamic key-value application settings editable from Admin Panel."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BroadcastJob(Base):
    """Audit and progress tracking record for admin mass-broadcast jobs."""

    __tablename__ = "broadcast_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_filter: Mapped[str] = mapped_column(String(64), default="all", server_default="all", nullable=False)
    message_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    total_targets: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default="pending", server_default="pending", index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
