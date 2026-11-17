"""Initial PostgreSQL 16 schema for Telegram Digital Shop Bot.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-17 16:20:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column("language_code", sa.String(length=10), server_default="en", nullable=False),
        sa.Column("balance_usd", sa.Numeric(precision=18, scale=8), server_default="0", nullable=False),
        sa.Column("balance_irt", sa.Numeric(precision=18, scale=2), server_default="0", nullable=False),
        sa.Column("is_blocked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("balance_usd >= 0", name=op.f("ck_users_balance_usd_non_negative")),
        sa.CheckConstraint("balance_irt >= 0", name=op.f("ck_users_balance_irt_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_telegram_id"), "users", ["telegram_id"], unique=True)
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=False)
    op.create_index(op.f("ix_users_is_blocked"), "users", ["is_blocked"], unique=False)
    op.create_index(op.f("ix_users_is_admin"), "users", ["is_admin"], unique=False)

    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("color_emoji", sa.String(length=16), server_default="🔵", nullable=False),
        sa.Column("color_hex", sa.String(length=7), server_default="#3b82f6", nullable=False),
        sa.Column("names_json", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("descriptions_json", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
    )
    op.create_index(op.f("ix_categories_slug"), "categories", ["slug"], unique=True)
    op.create_index(op.f("ix_categories_sort_order"), "categories", ["sort_order"], unique=False)
    op.create_index(op.f("ix_categories_is_active"), "categories", ["is_active"], unique=False)

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=96), nullable=False),
        sa.Column("delivery_type", sa.String(length=16), server_default="auto", nullable=False),
        sa.Column("manual_prompt_key", sa.String(length=128), nullable=True),
        sa.Column("price_usd", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("price_irt", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("color_emoji", sa.String(length=16), server_default="🟢", nullable=False),
        sa.Column("color_hex", sa.String(length=7), server_default="#22c55e", nullable=False),
        sa.Column("is_pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("manual_stock_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("low_stock_threshold", sa.Integer(), server_default="5", nullable=False),
        sa.Column("sold_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("titles_json", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("descriptions_json", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("rules_json", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("manual_prompts_json", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("price_usd >= 0", name=op.f("ck_products_price_usd_non_negative")),
        sa.CheckConstraint("price_irt IS NULL OR price_irt >= 0", name=op.f("ck_products_price_irt_non_negative")),
        sa.CheckConstraint("sold_count >= 0", name=op.f("ck_products_sold_count_non_negative")),
        sa.CheckConstraint("manual_stock_count >= -1", name=op.f("ck_products_manual_stock_valid")),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], name=op.f("fk_products_category_id_categories"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
    )
    op.create_index(op.f("ix_products_category_id"), "products", ["category_id"], unique=False)
    op.create_index(op.f("ix_products_slug"), "products", ["slug"], unique=True)
    op.create_index(op.f("ix_products_is_pinned"), "products", ["is_pinned"], unique=False)
    op.create_index(op.f("ix_products_is_active"), "products", ["is_active"], unique=False)
    op.create_index(op.f("ix_products_sort_order"), "products", ["sort_order"], unique=False)

    op.create_table(
        "product_bulk_tiers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("min_qty", sa.Integer(), nullable=False),
        sa.Column("unit_price_usd", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("unit_price_irt", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("min_qty >= 2", name=op.f("ck_product_bulk_tiers_min_qty_at_least_two")),
        sa.CheckConstraint("unit_price_usd >= 0", name=op.f("ck_product_bulk_tiers_unit_price_usd_non_negative")),
        sa.CheckConstraint(
            "unit_price_irt IS NULL OR unit_price_irt >= 0",
            name=op.f("ck_product_bulk_tiers_unit_price_irt_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name=op.f("fk_product_bulk_tiers_product_id_products"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_bulk_tiers")),
        sa.UniqueConstraint("product_id", "min_qty", name="uq_product_bulk_tiers_product_min_qty"),
    )
    op.create_index(op.f("ix_product_bulk_tiers_product_id"), "product_bulk_tiers", ["product_id"], unique=False)

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_number", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("total_amount", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("fulfillment_input", sa.Text(), nullable=True),
        sa.Column("fulfillment_note", sa.Text(), nullable=True),
        sa.Column("fulfilled_by_admin_id", sa.BigInteger(), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("quantity >= 1", name=op.f("ck_orders_quantity_positive")),
        sa.CheckConstraint("unit_price >= 0", name=op.f("ck_orders_unit_price_non_negative")),
        sa.CheckConstraint("total_amount >= 0", name=op.f("ck_orders_total_amount_non_negative")),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_orders_product_id_products"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_orders_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )
    op.create_index(op.f("ix_orders_order_number"), "orders", ["order_number"], unique=True)
    op.create_index(op.f("ix_orders_user_id"), "orders", ["user_id"], unique=False)
    op.create_index(op.f("ix_orders_product_id"), "orders", ["product_id"], unique=False)
    op.create_index(op.f("ix_orders_status"), "orders", ["status"], unique=False)

    op.create_table(
        "product_stock",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("encrypted_payload", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="available", nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_product_stock_order_id_orders"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_product_stock_product_id_products"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_stock")),
    )
    op.create_index(op.f("ix_product_stock_product_id"), "product_stock", ["product_id"], unique=False)
    op.create_index(op.f("ix_product_stock_payload_hash"), "product_stock", ["payload_hash"], unique=False)
    op.create_index(op.f("ix_product_stock_status"), "product_stock", ["status"], unique=False)
    op.create_index(op.f("ix_product_stock_order_id"), "product_stock", ["order_id"], unique=False)
    op.create_index("ix_product_stock_alloc", "product_stock", ["product_id", "status", "id"], unique=False)

    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=True),
        sa.Column("encrypted_delivered_payload", sa.Text(), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_order_items_order_id_orders"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_order_items_product_id_products"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stock_id"], ["product_stock.id"], name=op.f("fk_order_items_stock_id_product_stock"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )
    op.create_index(op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False)
    op.create_index(op.f("ix_order_items_product_id"), "order_items", ["product_id"], unique=False)

    op.create_table(
        "wallet_ledger",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("before_balance", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("change_amount", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("after_balance", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("reference_type", sa.String(length=32), nullable=False),
        sa.Column("reference_id", sa.BigInteger(), nullable=True),
        sa.Column("admin_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("before_balance >= 0", name=op.f("ck_wallet_ledger_before_balance_non_negative")),
        sa.CheckConstraint("after_balance >= 0", name=op.f("ck_wallet_ledger_after_balance_non_negative")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_wallet_ledger_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallet_ledger")),
    )
    op.create_index(op.f("ix_wallet_ledger_user_id"), "wallet_ledger", ["user_id"], unique=False)
    op.create_index(op.f("ix_wallet_ledger_reference_type"), "wallet_ledger", ["reference_type"], unique=False)
    op.create_index(op.f("ix_wallet_ledger_created_at"), "wallet_ledger", ["created_at"], unique=False)

    op.create_table(
        "crypto_wallets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("coin_name", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("wallet_address", sa.String(length=256), nullable=False),
        sa.Column("qr_image_path", sa.String(length=512), nullable=True),
        sa.Column("min_deposit_usd", sa.Numeric(precision=18, scale=8), server_default="1", nullable=False),
        sa.Column("fee_note", sa.String(length=256), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("instructions_json", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("verification_mode", sa.String(length=32), server_default="manual_txid", nullable=False),
        sa.Column("explorer_config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("min_deposit_usd >= 0", name=op.f("ck_crypto_wallets_min_deposit_usd_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crypto_wallets")),
    )
    op.create_index(op.f("ix_crypto_wallets_symbol"), "crypto_wallets", ["symbol"], unique=False)
    op.create_index(op.f("ix_crypto_wallets_is_active"), "crypto_wallets", ["is_active"], unique=False)

    op.create_table(
        "fiat_payment_methods",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("method_type", sa.String(length=24), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("card_number", sa.String(length=32), nullable=True),
        sa.Column("sheba", sa.String(length=36), nullable=True),
        sa.Column("cardholder_name", sa.String(length=128), nullable=True),
        sa.Column("bank_name", sa.String(length=64), nullable=True),
        sa.Column("gateway_provider", sa.String(length=32), nullable=True),
        sa.Column("gateway_merchant_id_encrypted", sa.Text(), nullable=True),
        sa.Column("min_deposit_irt", sa.Numeric(precision=18, scale=2), server_default="50000", nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("min_deposit_irt >= 0", name=op.f("ck_fiat_payment_methods_min_deposit_irt_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fiat_payment_methods")),
    )
    op.create_index(op.f("ix_fiat_payment_methods_is_active"), "fiat_payment_methods", ["is_active"], unique=False)

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("payment_category", sa.String(length=24), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("amount_usd", sa.Numeric(precision=18, scale=8), server_default="0", nullable=False),
        sa.Column("amount_irt", sa.Numeric(precision=18, scale=2), server_default="0", nullable=False),
        sa.Column("exchange_rate_used", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("crypto_wallet_id", sa.Integer(), nullable=True),
        sa.Column("fiat_method_id", sa.Integer(), nullable=True),
        sa.Column("tx_hash", sa.String(length=256), nullable=True),
        sa.Column("authority_track_id", sa.String(length=128), nullable=True),
        sa.Column("receipt_file_id", sa.String(length=256), nullable=True),
        sa.Column("receipt_local_path", sa.String(length=512), nullable=True),
        sa.Column("receipt_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("admin_id", sa.BigInteger(), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_usd >= 0", name=op.f("ck_payments_amount_usd_non_negative")),
        sa.CheckConstraint("amount_irt >= 0", name=op.f("ck_payments_amount_irt_non_negative")),
        sa.ForeignKeyConstraint(
            ["crypto_wallet_id"], ["crypto_wallets.id"], name=op.f("fk_payments_crypto_wallet_id_crypto_wallets"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["fiat_method_id"], ["fiat_payment_methods.id"], name=op.f("fk_payments_fiat_method_id_fiat_payment_methods"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_payments_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
    )
    op.create_index(op.f("ix_payments_user_id"), "payments", ["user_id"], unique=False)
    op.create_index(op.f("ix_payments_payment_category"), "payments", ["payment_category"], unique=False)
    op.create_index(op.f("ix_payments_tx_hash"), "payments", ["tx_hash"], unique=True)
    op.create_index(op.f("ix_payments_authority_track_id"), "payments", ["authority_track_id"], unique=True)
    op.create_index(op.f("ix_payments_status"), "payments", ["status"], unique=False)
    op.create_index(op.f("ix_payments_created_at"), "payments", ["created_at"], unique=False)

    op.create_table(
        "support_tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_number", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_support_tickets_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_tickets")),
    )
    op.create_index(op.f("ix_support_tickets_ticket_number"), "support_tickets", ["ticket_number"], unique=True)
    op.create_index(op.f("ix_support_tickets_user_id"), "support_tickets", ["user_id"], unique=False)
    op.create_index(op.f("ix_support_tickets_status"), "support_tickets", ["status"], unique=False)

    op.create_table(
        "support_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("sender_role", sa.String(length=16), nullable=False),
        sa.Column("sender_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("message_text", sa.Text(), nullable=True),
        sa.Column("telegram_file_id", sa.String(length=256), nullable=True),
        sa.Column("file_type", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["support_tickets.id"], name=op.f("fk_support_messages_ticket_id_support_tickets"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_messages")),
    )
    op.create_index(op.f("ix_support_messages_ticket_id"), "support_messages", ["ticket_id"], unique=False)

    op.create_table(
        "ui_translations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("lang_code", sa.String(length=10), nullable=False),
        sa.Column("text_value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ui_translations")),
        sa.UniqueConstraint("key", "lang_code", name="uq_ui_translations_key_lang"),
    )
    op.create_index(op.f("ix_ui_translations_key"), "ui_translations", ["key"], unique=False)
    op.create_index(op.f("ix_ui_translations_lang_code"), "ui_translations", ["lang_code"], unique=False)

    op.create_table(
        "ui_buttons",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("button_key", sa.String(length=64), nullable=False),
        sa.Column("menu_scope", sa.String(length=32), server_default="main", nullable=False),
        sa.Column("row_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("col_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("color_emoji", sa.String(length=16), server_default="🔵", nullable=False),
        sa.Column("color_hex", sa.String(length=7), server_default="#3b82f6", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("labels_json", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ui_buttons")),
    )
    op.create_index(op.f("ix_ui_buttons_button_key"), "ui_buttons", ["button_key"], unique=True)
    op.create_index(op.f("ix_ui_buttons_menu_scope"), "ui_buttons", ["menu_scope"], unique=False)

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_settings")),
    )

    op.create_table(
        "broadcast_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("admin_id", sa.BigInteger(), nullable=False),
        sa.Column("target_filter", sa.String(length=64), server_default="all", nullable=False),
        sa.Column("message_payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("total_targets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sent_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broadcast_jobs")),
    )
    op.create_index(op.f("ix_broadcast_jobs_status"), "broadcast_jobs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_broadcast_jobs_status"), table_name="broadcast_jobs")
    op.drop_table("broadcast_jobs")
    op.drop_table("app_settings")
    op.drop_index(op.f("ix_ui_buttons_menu_scope"), table_name="ui_buttons")
    op.drop_index(op.f("ix_ui_buttons_button_key"), table_name="ui_buttons")
    op.drop_table("ui_buttons")
    op.drop_index(op.f("ix_ui_translations_lang_code"), table_name="ui_translations")
    op.drop_index(op.f("ix_ui_translations_key"), table_name="ui_translations")
    op.drop_table("ui_translations")
    op.drop_index(op.f("ix_support_messages_ticket_id"), table_name="support_messages")
    op.drop_table("support_messages")
    op.drop_index(op.f("ix_support_tickets_status"), table_name="support_tickets")
    op.drop_index(op.f("ix_support_tickets_user_id"), table_name="support_tickets")
    op.drop_index(op.f("ix_support_tickets_ticket_number"), table_name="support_tickets")
    op.drop_table("support_tickets")
    op.drop_index(op.f("ix_payments_created_at"), table_name="payments")
    op.drop_index(op.f("ix_payments_status"), table_name="payments")
    op.drop_index(op.f("ix_payments_authority_track_id"), table_name="payments")
    op.drop_index(op.f("ix_payments_tx_hash"), table_name="payments")
    op.drop_index(op.f("ix_payments_payment_category"), table_name="payments")
    op.drop_index(op.f("ix_payments_user_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_index(op.f("ix_fiat_payment_methods_is_active"), table_name="fiat_payment_methods")
    op.drop_table("fiat_payment_methods")
    op.drop_index(op.f("ix_crypto_wallets_is_active"), table_name="crypto_wallets")
    op.drop_index(op.f("ix_crypto_wallets_symbol"), table_name="crypto_wallets")
    op.drop_table("crypto_wallets")
    op.drop_index(op.f("ix_wallet_ledger_created_at"), table_name="wallet_ledger")
    op.drop_index(op.f("ix_wallet_ledger_reference_type"), table_name="wallet_ledger")
    op.drop_index(op.f("ix_wallet_ledger_user_id"), table_name="wallet_ledger")
    op.drop_table("wallet_ledger")
    op.drop_index(op.f("ix_order_items_product_id"), table_name="order_items")
    op.drop_index(op.f("ix_order_items_order_id"), table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_product_stock_alloc", table_name="product_stock")
    op.drop_index(op.f("ix_product_stock_order_id"), table_name="product_stock")
    op.drop_index(op.f("ix_product_stock_status"), table_name="product_stock")
    op.drop_index(op.f("ix_product_stock_payload_hash"), table_name="product_stock")
    op.drop_index(op.f("ix_product_stock_product_id"), table_name="product_stock")
    op.drop_table("product_stock")
    op.drop_index(op.f("ix_orders_status"), table_name="orders")
    op.drop_index(op.f("ix_orders_product_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_user_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_order_number"), table_name="orders")
    op.drop_table("orders")
    op.drop_index(op.f("ix_product_bulk_tiers_product_id"), table_name="product_bulk_tiers")
    op.drop_table("product_bulk_tiers")
    op.drop_index(op.f("ix_products_sort_order"), table_name="products")
    op.drop_index(op.f("ix_products_is_active"), table_name="products")
    op.drop_index(op.f("ix_products_is_pinned"), table_name="products")
    op.drop_index(op.f("ix_products_slug"), table_name="products")
    op.drop_index(op.f("ix_products_category_id"), table_name="products")
    op.drop_table("products")
    op.drop_index(op.f("ix_categories_is_active"), table_name="categories")
    op.drop_index(op.f("ix_categories_sort_order"), table_name="categories")
    op.drop_index(op.f("ix_categories_slug"), table_name="categories")
    op.drop_table("categories")
    op.drop_index(op.f("ix_users_is_admin"), table_name="users")
    op.drop_index(op.f("ix_users_is_blocked"), table_name="users")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_index(op.f("ix_users_telegram_id"), table_name="users")
    op.drop_table("users")
