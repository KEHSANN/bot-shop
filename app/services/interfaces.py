"""Async service protocol contracts implemented by Wave 2 domain services."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.core.constants import Currency, OrderStatus, PaymentStatus, SenderRole, TicketCategory, TicketStatus
from app.services.dtos import (
    AppSettingDTO,
    AvailablePaymentMethodsDTO,
    BalanceAdjustmentDTO,
    BroadcastCreateDTO,
    BroadcastJobDTO,
    CategoryCreateDTO,
    CategoryDTO,
    CategoryUpdateDTO,
    CryptoDepositCreateDTO,
    CryptoWalletCreateDTO,
    CryptoWalletDTO,
    CryptoWalletUpdateDTO,
    DashboardStatsDTO,
    ExchangeRateDTO,
    FiatDepositCreateDTO,
    FiatMethodCreateDTO,
    FiatMethodDTO,
    FiatMethodUpdateDTO,
    OrderDTO,
    PaymentDTO,
    PaymentReviewDTO,
    ProductBulkTierDTO,
    ProductCreateDTO,
    ProductDetailDTO,
    ProductDTO,
    ProductUpdateDTO,
    PurchaseRequestDTO,
    PurchaseResultDTO,
    StockImportResultDTO,
    SupportMessageDTO,
    SupportTicketDetailDTO,
    SupportTicketDTO,
    UIButtonDTO,
    UITranslationDTO,
    UserCreateOrUpdateDTO,
    UserDTO,
    WalletLedgerDTO,
)


@runtime_checkable
class UserServiceProtocol(Protocol):
    """Contract for Telegram user lifecycle, search, RBAC, and atomic wallet adjustments."""

    async def upsert_telegram_user(self, data: UserCreateOrUpdateDTO) -> UserDTO: ...

    async def get_by_id(self, user_id: int) -> UserDTO: ...

    async def get_by_telegram_id(self, telegram_id: int) -> UserDTO | None: ...

    async def search_users(self, query: str, *, limit: int = 20, offset: int = 0) -> list[UserDTO]: ...

    async def list_users(self, *, limit: int = 50, offset: int = 0, blocked_only: bool = False) -> list[UserDTO]: ...

    async def set_language(self, telegram_id: int, lang_code: str) -> UserDTO: ...

    async def set_blocked(self, user_id: int, *, blocked: bool, admin_id: int) -> UserDTO: ...

    async def adjust_balance(self, adjustment: BalanceAdjustmentDTO) -> tuple[UserDTO, WalletLedgerDTO]: ...

    async def get_wallet_history(self, user_id: int, *, limit: int = 20, offset: int = 0) -> list[WalletLedgerDTO]: ...


@runtime_checkable
class ShopServiceProtocol(Protocol):
    """Contract for catalog management, Fernet stock encryption, and atomic purchases."""

    async def list_categories(self, *, active_only: bool = True) -> list[CategoryDTO]: ...

    async def create_category(self, data: CategoryCreateDTO) -> CategoryDTO: ...

    async def update_category(self, category_id: int, data: CategoryUpdateDTO) -> CategoryDTO: ...

    async def delete_category(self, category_id: int) -> None: ...

    async def list_products(
        self,
        *,
        category_id: int | None = None,
        active_only: bool = True,
        pinned_only: bool = False,
    ) -> list[ProductDTO]: ...

    async def get_product_detail(self, product_id: int) -> ProductDetailDTO: ...

    async def create_product(self, data: ProductCreateDTO) -> ProductDetailDTO: ...

    async def update_product(self, product_id: int, data: ProductUpdateDTO) -> ProductDetailDTO: ...

    async def delete_product(self, product_id: int) -> None: ...

    async def set_bulk_tiers(self, product_id: int, tiers: list[ProductBulkTierDTO]) -> list[ProductBulkTierDTO]: ...

    async def add_auto_stock(self, product_id: int, payloads: list[str], *, deduplicate: bool = False) -> StockImportResultDTO: ...

    async def calculate_unit_price(self, product_id: int, quantity: int, currency: Currency) -> Decimal: ...

    async def purchase_product(self, request: PurchaseRequestDTO) -> PurchaseResultDTO: ...

    async def submit_manual_fulfillment_input(self, order_id: int, user_id: int, fulfillment_input: str) -> OrderDTO: ...

    async def fulfill_manual_order(self, order_id: int, admin_id: int, fulfillment_note: str | None = None) -> OrderDTO: ...

    async def cancel_and_refund_order(self, order_id: int, admin_id: int, reason: str | None = None) -> OrderDTO: ...

    async def get_order_detail(self, order_id: int, *, decrypt_items: bool = False) -> OrderDTO: ...

    async def list_user_orders(self, user_id: int, *, limit: int = 20, offset: int = 0) -> list[OrderDTO]: ...

    async def list_orders(self, *, status: OrderStatus | None = None, limit: int = 50, offset: int = 0) -> list[OrderDTO]: ...


@runtime_checkable
class PaymentServiceProtocol(Protocol):
    """Contract for region-aware deposits, dynamic wallets, gateway hooks, and atomic approvals."""

    async def get_available_methods_for_language(self, lang_code: str) -> AvailablePaymentMethodsDTO: ...

    async def list_crypto_wallets(self, *, active_only: bool = False) -> list[CryptoWalletDTO]: ...

    async def create_crypto_wallet(self, data: CryptoWalletCreateDTO) -> CryptoWalletDTO: ...

    async def update_crypto_wallet(self, wallet_id: int, data: CryptoWalletUpdateDTO) -> CryptoWalletDTO: ...

    async def delete_crypto_wallet(self, wallet_id: int) -> None: ...

    async def list_fiat_methods(self, *, active_only: bool = False) -> list[FiatMethodDTO]: ...

    async def create_fiat_method(self, data: FiatMethodCreateDTO) -> FiatMethodDTO: ...

    async def update_fiat_method(self, method_id: int, data: FiatMethodUpdateDTO) -> FiatMethodDTO: ...

    async def delete_fiat_method(self, method_id: int) -> None: ...

    async def create_crypto_deposit(self, data: CryptoDepositCreateDTO) -> PaymentDTO: ...

    async def create_fiat_deposit(self, data: FiatDepositCreateDTO) -> PaymentDTO: ...

    async def review_payment(self, review: PaymentReviewDTO) -> PaymentDTO: ...

    async def verify_gateway_callback(self, authority_track_id: str, gateway_params: dict[str, str]) -> PaymentDTO: ...

    async def get_payment(self, payment_id: int) -> PaymentDTO: ...

    async def list_payments(
        self,
        *,
        status: PaymentStatus | None = None,
        user_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PaymentDTO]: ...

    async def get_exchange_rate(self) -> ExchangeRateDTO: ...

    async def update_exchange_rate(self, rate: Decimal, *, rate_mode: str = "manual") -> ExchangeRateDTO: ...


@runtime_checkable
class SupportServiceProtocol(Protocol):
    """Contract for two-way support tickets and media message threads."""

    async def create_ticket(
        self,
        user_id: int,
        category: TicketCategory,
        *,
        subject: str | None = None,
        message_text: str | None = None,
        telegram_file_id: str | None = None,
        file_type: str | None = None,
    ) -> SupportTicketDetailDTO: ...

    async def add_message(
        self,
        ticket_id: int,
        sender_role: SenderRole,
        sender_telegram_id: int | None,
        *,
        message_text: str | None = None,
        telegram_file_id: str | None = None,
        file_type: str | None = None,
    ) -> SupportMessageDTO: ...

    async def get_ticket_detail(self, ticket_id: int) -> SupportTicketDetailDTO: ...

    async def list_user_tickets(self, user_id: int, *, limit: int = 20, offset: int = 0) -> list[SupportTicketDTO]: ...

    async def list_tickets(
        self, *, status: TicketStatus | None = None, limit: int = 50, offset: int = 0
    ) -> list[SupportTicketDTO]: ...

    async def close_ticket(self, ticket_id: int) -> SupportTicketDTO: ...


@runtime_checkable
class CMSServiceProtocol(Protocol):
    """Contract for runtime editing of 12-language texts, UI buttons, colors, and app settings."""

    async def get_translation(self, key: str, lang_code: str) -> str: ...

    async def list_translations(
        self, *, lang_code: str | None = None, search: str | None = None, limit: int = 200, offset: int = 0
    ) -> list[UITranslationDTO]: ...

    async def upsert_translation(self, key: str, lang_code: str, text_value: str) -> UITranslationDTO: ...

    async def list_buttons(self, *, menu_scope: str | None = None) -> list[UIButtonDTO]: ...

    async def update_button(
        self,
        button_key: str,
        *,
        color_emoji: str | None = None,
        color_hex: str | None = None,
        labels_json: dict[str, str] | None = None,
        is_active: bool | None = None,
    ) -> UIButtonDTO: ...

    async def get_setting(self, key: str, default: str | None = None) -> str | None: ...

    async def set_setting(self, key: str, value: str, *, description: str | None = None) -> AppSettingDTO: ...


@runtime_checkable
class BroadcastServiceProtocol(Protocol):
    """Contract for rate-limited mass broadcast jobs and target audience resolution."""

    async def create_job(self, data: BroadcastCreateDTO) -> BroadcastJobDTO: ...

    async def resolve_target_telegram_ids(self, target_filter: str) -> list[int]: ...

    async def update_progress(
        self,
        job_id: int,
        *,
        sent_count: int | None = None,
        failed_count: int | None = None,
        status: str | None = None,
    ) -> BroadcastJobDTO: ...

    async def get_job(self, job_id: int) -> BroadcastJobDTO: ...

    async def list_jobs(self, *, limit: int = 20, offset: int = 0) -> list[BroadcastJobDTO]: ...


@runtime_checkable
class AnalyticsServiceProtocol(Protocol):
    """Contract for Admin Dashboard KPI metrics and low-stock monitoring."""

    async def get_dashboard_stats(self) -> DashboardStatsDTO: ...
