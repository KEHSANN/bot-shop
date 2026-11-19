"""Strict Decimal-only Pydantic v2 Data Transfer Objects shared across layers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.constants import (
    BroadcastStatus,
    CryptoVerificationMode,
    Currency,
    DeliveryType,
    FiatMethodType,
    MenuScope,
    OrderStatus,
    PaymentCategory,
    PaymentStatus,
    SenderRole,
    TicketCategory,
    TicketStatus,
    WalletLedgerRefType,
    is_valid_hex_color,
)


class DTOBase(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---------------------------------------------------------------------------
# User & Wallet DTOs
# ---------------------------------------------------------------------------
class UserDTO(DTOBase):
    id: int
    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str = "en"
    language_selected: bool = False
    balance_usd: Decimal = Decimal("0")
    balance_irt: Decimal = Decimal("0")
    is_blocked: bool = False
    is_admin: bool = False
    created_at: datetime
    updated_at: datetime


class UserCreateOrUpdateDTO(DTOBase):
    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None


class WalletLedgerDTO(DTOBase):
    id: int
    user_id: int
    currency: Currency
    before_balance: Decimal
    change_amount: Decimal
    after_balance: Decimal
    reference_type: WalletLedgerRefType
    reference_id: int | None = None
    admin_id: int | None = None
    note: str | None = None
    created_at: datetime


class BalanceAdjustmentDTO(DTOBase):
    user_id: int
    currency: Currency
    amount: Decimal
    admin_id: int
    note: str | None = None

    @field_validator("amount")
    @classmethod
    def _validate_non_zero(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value == Decimal("0"):
            raise ValueError("Adjustment amount must be a finite non-zero Decimal")
        return value


# ---------------------------------------------------------------------------
# Shop, Catalog & Order DTOs
# ---------------------------------------------------------------------------
class CategoryDTO(DTOBase):
    id: int
    slug: str
    sort_order: int
    is_active: bool
    color_emoji: str
    color_hex: str
    names_json: dict[str, str]
    descriptions_json: dict[str, str]
    created_at: datetime
    updated_at: datetime


class CategoryCreateDTO(DTOBase):
    slug: str = Field(min_length=1, max_length=64)
    sort_order: int = 0
    is_active: bool = True
    color_emoji: str = "🔵"
    color_hex: str = "#3b82f6"
    names_json: dict[str, str] = Field(default_factory=dict)
    descriptions_json: dict[str, str] = Field(default_factory=dict)

    @field_validator("color_hex")
    @classmethod
    def _check_hex(cls, value: str) -> str:
        if not is_valid_hex_color(value):
            raise ValueError("color_hex must match #RRGGBB")
        return value.lower()


class CategoryUpdateDTO(DTOBase):
    slug: str | None = Field(default=None, min_length=1, max_length=64)
    sort_order: int | None = None
    is_active: bool | None = None
    color_emoji: str | None = None
    color_hex: str | None = None
    names_json: dict[str, str] | None = None
    descriptions_json: dict[str, str] | None = None

    @field_validator("color_hex")
    @classmethod
    def _check_hex(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_hex_color(value):
            raise ValueError("color_hex must match #RRGGBB")
        return value.lower() if value else None


class ProductBulkTierDTO(DTOBase):
    id: int | None = None
    min_qty: int = Field(ge=2)
    unit_price_usd: Decimal = Field(ge=Decimal("0"))
    unit_price_irt: Decimal | None = Field(default=None, ge=Decimal("0"))


class ProductDTO(DTOBase):
    id: int
    category_id: int
    slug: str
    delivery_type: DeliveryType
    manual_prompt_key: str | None = None
    price_usd: Decimal
    price_irt: Decimal | None = None
    color_emoji: str
    color_hex: str
    is_pinned: bool
    is_active: bool
    sort_order: int
    manual_stock_count: int
    low_stock_threshold: int
    sold_count: int
    titles_json: dict[str, str]
    descriptions_json: dict[str, str]
    rules_json: dict[str, str]
    manual_prompts_json: dict[str, str]
    available_stock: int = 0
    created_at: datetime
    updated_at: datetime


class ProductDetailDTO(ProductDTO):
    bulk_tiers: list[ProductBulkTierDTO] = Field(default_factory=list)
    category: CategoryDTO | None = None


class ProductCreateDTO(DTOBase):
    category_id: int
    slug: str = Field(min_length=1, max_length=96)
    delivery_type: DeliveryType = DeliveryType.AUTO
    manual_prompt_key: str | None = None
    price_usd: Decimal = Field(ge=Decimal("0"))
    price_irt: Decimal | None = Field(default=None, ge=Decimal("0"))
    color_emoji: str = "🟢"
    color_hex: str = "#22c55e"
    is_pinned: bool = False
    is_active: bool = True
    sort_order: int = 0
    manual_stock_count: int = Field(default=0, ge=-1)
    low_stock_threshold: int = Field(default=5, ge=0)
    titles_json: dict[str, str] = Field(default_factory=dict)
    descriptions_json: dict[str, str] = Field(default_factory=dict)
    rules_json: dict[str, str] = Field(default_factory=dict)
    manual_prompts_json: dict[str, str] = Field(default_factory=dict)
    bulk_tiers: list[ProductBulkTierDTO] = Field(default_factory=list)

    @field_validator("color_hex")
    @classmethod
    def _check_hex(cls, value: str) -> str:
        if not is_valid_hex_color(value):
            raise ValueError("color_hex must match #RRGGBB")
        return value.lower()


class ProductUpdateDTO(DTOBase):
    category_id: int | None = None
    slug: str | None = Field(default=None, min_length=1, max_length=96)
    delivery_type: DeliveryType | None = None
    manual_prompt_key: str | None = None
    price_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    price_irt: Decimal | None = Field(default=None, ge=Decimal("0"))
    color_emoji: str | None = None
    color_hex: str | None = None
    is_pinned: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = None
    manual_stock_count: int | None = Field(default=None, ge=-1)
    low_stock_threshold: int | None = Field(default=None, ge=0)
    titles_json: dict[str, str] | None = None
    descriptions_json: dict[str, str] | None = None
    rules_json: dict[str, str] | None = None
    manual_prompts_json: dict[str, str] | None = None

    @field_validator("color_hex")
    @classmethod
    def _check_hex(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_hex_color(value):
            raise ValueError("color_hex must match #RRGGBB")
        return value.lower() if value else None


class StockImportResultDTO(DTOBase):
    product_id: int
    added_count: int
    skipped_duplicates: int
    total_available: int


class OrderItemDTO(DTOBase):
    id: int
    order_id: int
    product_id: int
    stock_id: int | None = None
    decrypted_payload: str | None = None
    unit_price: Decimal
    created_at: datetime


class OrderDTO(DTOBase):
    id: int
    order_number: str
    user_id: int
    product_id: int
    quantity: int
    currency: Currency
    unit_price: Decimal
    total_amount: Decimal
    status: OrderStatus
    fulfillment_input: str | None = None
    fulfillment_note: str | None = None
    fulfilled_by_admin_id: int | None = None
    fulfilled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemDTO] = Field(default_factory=list)


class PurchaseRequestDTO(DTOBase):
    user_id: int
    product_id: int
    quantity: int = Field(ge=1, le=1000)
    currency: Currency
    fulfillment_input: str | None = None


class PurchaseResultDTO(DTOBase):
    order: OrderDTO
    delivered_items: list[str] = Field(default_factory=list)
    new_balance: Decimal


# ---------------------------------------------------------------------------
# Payments & Region-Aware Deposit DTOs
# ---------------------------------------------------------------------------
class CryptoWalletDTO(DTOBase):
    id: int
    coin_name: str
    symbol: str
    network: str
    wallet_address: str
    qr_image_path: str | None = None
    min_deposit_usd: Decimal
    fee_note: str | None = None
    instructions: str | None = None
    instructions_json: dict[str, str] = Field(default_factory=dict)
    verification_mode: CryptoVerificationMode
    explorer_config: dict[str, Any] = Field(default_factory=dict)
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class CryptoWalletCreateDTO(DTOBase):
    coin_name: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=16)
    network: str = Field(min_length=1, max_length=32)
    wallet_address: str = Field(min_length=4, max_length=256)
    qr_image_path: str | None = None
    min_deposit_usd: Decimal = Field(default=Decimal("1"), ge=Decimal("0"))
    fee_note: str | None = None
    instructions: str | None = None
    instructions_json: dict[str, str] = Field(default_factory=dict)
    verification_mode: CryptoVerificationMode = CryptoVerificationMode.MANUAL_TXID
    explorer_config: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    sort_order: int = 0


class CryptoWalletUpdateDTO(DTOBase):
    coin_name: str | None = None
    symbol: str | None = None
    network: str | None = None
    wallet_address: str | None = None
    qr_image_path: str | None = None
    min_deposit_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    fee_note: str | None = None
    instructions: str | None = None
    instructions_json: dict[str, str] | None = None
    verification_mode: CryptoVerificationMode | None = None
    explorer_config: dict[str, Any] | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class FiatMethodDTO(DTOBase):
    id: int
    method_type: FiatMethodType
    title: str
    card_number: str | None = None
    sheba: str | None = None
    cardholder_name: str | None = None
    bank_name: str | None = None
    gateway_provider: str | None = None
    min_deposit_irt: Decimal
    instructions: str | None = None
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class FiatMethodCreateDTO(DTOBase):
    method_type: FiatMethodType
    title: str = Field(min_length=1, max_length=128)
    card_number: str | None = None
    sheba: str | None = None
    cardholder_name: str | None = None
    bank_name: str | None = None
    gateway_provider: str | None = None
    gateway_merchant_id: str | None = None
    min_deposit_irt: Decimal = Field(default=Decimal("50000"), ge=Decimal("0"))
    instructions: str | None = None
    is_active: bool = True
    sort_order: int = 0


class FiatMethodUpdateDTO(DTOBase):
    method_type: FiatMethodType | None = None
    title: str | None = None
    card_number: str | None = None
    sheba: str | None = None
    cardholder_name: str | None = None
    bank_name: str | None = None
    gateway_provider: str | None = None
    gateway_merchant_id: str | None = None
    min_deposit_irt: Decimal | None = Field(default=None, ge=Decimal("0"))
    instructions: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class AvailablePaymentMethodsDTO(DTOBase):
    lang_code: str
    fiat_enabled: bool
    crypto_wallets: list[CryptoWalletDTO] = Field(default_factory=list)
    fiat_methods: list[FiatMethodDTO] = Field(default_factory=list)


class PaymentDTO(DTOBase):
    id: int
    user_id: int
    payment_category: PaymentCategory
    currency: Currency
    amount_usd: Decimal
    amount_irt: Decimal
    exchange_rate_used: Decimal | None = None
    crypto_wallet_id: int | None = None
    fiat_method_id: int | None = None
    tx_hash: str | None = None
    authority_track_id: str | None = None
    receipt_file_id: str | None = None
    receipt_local_path: str | None = None
    receipt_note: str | None = None
    status: PaymentStatus
    admin_id: int | None = None
    admin_note: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None


class CryptoDepositCreateDTO(DTOBase):
    user_id: int
    crypto_wallet_id: int
    amount_usd: Decimal = Field(gt=Decimal("0"))
    tx_hash: str | None = None
    receipt_file_id: str | None = None
    receipt_local_path: str | None = None
    receipt_note: str | None = None


class FiatDepositCreateDTO(DTOBase):
    user_id: int
    fiat_method_id: int
    amount_irt: Decimal = Field(gt=Decimal("0"))
    payment_category: PaymentCategory = PaymentCategory.FIAT_CARD
    authority_track_id: str | None = None
    receipt_file_id: str | None = None
    receipt_local_path: str | None = None
    receipt_note: str | None = None


class PaymentReviewDTO(DTOBase):
    payment_id: int
    admin_id: int
    approve: bool
    admin_note: str | None = None


class ExchangeRateDTO(DTOBase):
    usd_to_toman_rate: Decimal
    rate_mode: str = "manual"
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Support DTOs
# ---------------------------------------------------------------------------
class SupportMessageDTO(DTOBase):
    id: int
    ticket_id: int
    sender_role: SenderRole
    sender_telegram_id: int | None = None
    message_text: str | None = None
    telegram_file_id: str | None = None
    file_type: str | None = None
    created_at: datetime


class SupportTicketDTO(DTOBase):
    id: int
    ticket_number: str
    user_id: int
    category: TicketCategory
    subject: str | None = None
    status: TicketStatus
    created_at: datetime
    updated_at: datetime


class SupportTicketDetailDTO(SupportTicketDTO):
    messages: list[SupportMessageDTO] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# CMS, UI, Broadcast & Analytics DTOs
# ---------------------------------------------------------------------------
class UITranslationDTO(DTOBase):
    id: int
    key: str
    lang_code: str
    text_value: str
    updated_at: datetime


class UIButtonDTO(DTOBase):
    id: int
    button_key: str
    menu_scope: MenuScope
    row_index: int
    col_index: int
    color_emoji: str
    color_hex: str
    is_active: bool
    labels_json: dict[str, str]
    created_at: datetime
    updated_at: datetime


class AppSettingDTO(DTOBase):
    key: str
    value: str
    description: str | None = None
    updated_at: datetime


class BroadcastCreateDTO(DTOBase):
    admin_id: int
    target_filter: str = "all"
    message_payload: dict[str, Any] = Field(default_factory=dict)


class BroadcastJobDTO(DTOBase):
    id: int
    admin_id: int
    target_filter: str
    message_payload: dict[str, Any]
    total_targets: int
    sent_count: int
    failed_count: int
    status: BroadcastStatus
    created_at: datetime
    completed_at: datetime | None = None


class LowStockAlertDTO(DTOBase):
    product_id: int
    slug: str
    title: str
    delivery_type: DeliveryType
    available_stock: int
    threshold: int


class DashboardStatsDTO(DTOBase):
    total_users: int
    active_users_24h: int
    active_users_7d: int
    active_users_30d: int
    total_revenue_usd: Decimal
    total_revenue_irt: Decimal
    pending_deposits_count: int
    open_tickets_count: int
    pending_manual_orders_count: int
    low_stock_alerts: list[LowStockAlertDTO] = Field(default_factory=list)
