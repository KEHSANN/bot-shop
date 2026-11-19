"""Comprehensive tests for BroadcastService and in-bot /admin panel handlers & RBAC."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers.admin_bot import admin_router
from app.bot.keyboards.admin_kb import (
    build_admin_home_kb,
    build_dual_color_picker_kb,
)
from app.bot.keyboards.common_kb import AdmCB, PayCB
from app.config import Settings, clear_settings_cache
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
)
from app.core.exceptions import ValidationError
from app.database.models import BroadcastJob, User
from app.services.broadcast_service import (
    BroadcastService,
    build_broadcast_inline_kb,
    format_broadcast_progress_bar,
)
from app.services.dtos import (
    AppSettingDTO,
    BalanceAdjustmentDTO,
    BroadcastCreateDTO,
    CategoryCreateDTO,
    CategoryDTO,
    CategoryUpdateDTO,
    CryptoWalletCreateDTO,
    CryptoWalletDTO,
    CryptoWalletUpdateDTO,
    DashboardStatsDTO,
    ExchangeRateDTO,
    FiatMethodCreateDTO,
    FiatMethodDTO,
    FiatMethodUpdateDTO,
    LowStockAlertDTO,
    OrderDTO,
    PaymentDTO,
    PaymentReviewDTO,
    ProductBulkTierDTO,
    ProductCreateDTO,
    ProductDetailDTO,
    ProductDTO,
    ProductUpdateDTO,
    StockImportResultDTO,
    SupportMessageDTO,
    SupportTicketDetailDTO,
    SupportTicketDTO,
    UIButtonDTO,
    UITranslationDTO,
    UserDTO,
    WalletLedgerDTO,
)
from app.services.interfaces import BroadcastServiceProtocol


# ---------------------------------------------------------------------------
# Protocol Doubles for In-Bot /admin Handler Tests
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakeAnalyticsService:
    async def get_dashboard_stats(self) -> DashboardStatsDTO:
        return DashboardStatsDTO(
            total_users=42,
            active_users_24h=10,
            active_users_7d=25,
            active_users_30d=39,
            total_revenue_usd=Decimal("1234.50"),
            total_revenue_irt=Decimal("54000000"),
            pending_deposits_count=3,
            open_tickets_count=2,
            pending_manual_orders_count=1,
            low_stock_alerts=[
                LowStockAlertDTO(
                    product_id=7,
                    slug="gemini-pro",
                    title="Gemini <script>alert(1)</script>",
                    delivery_type=DeliveryType.AUTO,
                    available_stock=2,
                    threshold=5,
                )
            ],
        )


class FakeUserService:
    def __init__(self, users: list[UserDTO]) -> None:
        self.users: dict[int, UserDTO] = {u.id: u for u in users}
        self.adjustments: list[BalanceAdjustmentDTO] = []

    async def get_by_id(self, user_id: int) -> UserDTO:
        return self.users[user_id]

    async def get_by_telegram_id(self, telegram_id: int) -> UserDTO | None:
        return next((u for u in self.users.values() if u.telegram_id == telegram_id), None)

    async def search_users(self, query: str, *, limit: int = 20, offset: int = 0) -> list[UserDTO]:
        q = query.lstrip("@").lower()
        return [
            u
            for u in self.users.values()
            if q in (u.username or "").lower()
            or q in (u.first_name or "").lower()
            or q == str(u.telegram_id)
        ][:limit]

    async def list_users(self, *, limit: int = 50, offset: int = 0, blocked_only: bool = False) -> list[UserDTO]:
        items = [u for u in self.users.values() if (not blocked_only or u.is_blocked)]
        return items[offset : offset + limit]

    async def set_blocked(self, user_id: int, *, blocked: bool, admin_id: int) -> UserDTO:
        u = self.users[user_id].model_copy(update={"is_blocked": blocked})
        self.users[user_id] = u
        return u

    async def adjust_balance(self, adjustment: BalanceAdjustmentDTO) -> tuple[UserDTO, WalletLedgerDTO]:
        self.adjustments.append(adjustment)
        u = self.users[adjustment.user_id]
        if adjustment.currency == Currency.USD:
            before = u.balance_usd
            after = before + adjustment.amount
            u = u.model_copy(update={"balance_usd": after})
        else:
            before = u.balance_irt
            after = before + adjustment.amount
            u = u.model_copy(update={"balance_irt": after})
        self.users[u.id] = u
        ledger = WalletLedgerDTO(
            id=1,
            user_id=u.id,
            currency=adjustment.currency,
            before_balance=before,
            change_amount=adjustment.amount,
            after_balance=after,
            reference_type=(
                WalletLedgerRefType.ADMIN_CHARGE
                if adjustment.amount > 0
                else WalletLedgerRefType.ADMIN_DEDUCT
            ),
            admin_id=adjustment.admin_id,
            note=adjustment.note,
            created_at=_now(),
        )
        return u, ledger

    async def get_wallet_history(self, user_id: int, *, limit: int = 20, offset: int = 0) -> list[WalletLedgerDTO]:
        return []


class FakeShopService:
    def __init__(self) -> None:
        now = _now()
        self.categories: dict[int, CategoryDTO] = {
            1: CategoryDTO(
                id=1,
                slug="ai",
                sort_order=1,
                is_active=True,
                color_emoji="🔵",
                color_hex="#3b82f6",
                names_json={"en": "AI Tools", "fa": "هوش مصنوعی"},
                descriptions_json={},
                created_at=now,
                updated_at=now,
            )
        }
        self.products: dict[int, ProductDetailDTO] = {
            1: ProductDetailDTO(
                id=1,
                category_id=1,
                slug="chatgpt",
                delivery_type=DeliveryType.AUTO,
                price_usd=Decimal("10.00"),
                price_irt=Decimal("650000"),
                color_emoji="🟢",
                color_hex="#22c55e",
                is_pinned=False,
                is_active=True,
                sort_order=0,
                manual_stock_count=0,
                low_stock_threshold=5,
                sold_count=3,
                titles_json={"en": "ChatGPT", "fa": "چت‌جی‌پی‌تی"},
                descriptions_json={},
                rules_json={},
                manual_prompts_json={},
                available_stock=5,
                created_at=now,
                updated_at=now,
                bulk_tiers=[],
            )
        }
        self.orders: dict[int, OrderDTO] = {
            10: OrderDTO(
                id=10,
                order_number="ORD-10",
                user_id=2,
                product_id=1,
                quantity=1,
                currency=Currency.USD,
                unit_price=Decimal("10.00"),
                total_amount=Decimal("10.00"),
                status=OrderStatus.PENDING_FULFILLMENT,
                fulfillment_input="user@example.com <script>",
                created_at=now,
                updated_at=now,
            )
        }
        self.stock_batches: list[tuple[int, list[str]]] = []

    async def list_categories(self, *, active_only: bool = True) -> list[CategoryDTO]:
        return [c for c in self.categories.values() if not active_only or c.is_active]

    async def create_category(self, data: CategoryCreateDTO) -> CategoryDTO:
        now = _now()
        cid = max(self.categories.keys(), default=0) + 1
        cat = CategoryDTO(
            id=cid,
            slug=data.slug,
            sort_order=data.sort_order,
            is_active=data.is_active,
            color_emoji=data.color_emoji,
            color_hex=data.color_hex,
            names_json=data.names_json,
            descriptions_json=data.descriptions_json,
            created_at=now,
            updated_at=now,
        )
        self.categories[cid] = cat
        return cat

    async def update_category(self, category_id: int, data: CategoryUpdateDTO) -> CategoryDTO:
        cat = self.categories[category_id]
        updates = data.model_dump(exclude_none=True)
        cat = cat.model_copy(update=updates)
        self.categories[category_id] = cat
        return cat

    async def delete_category(self, category_id: int) -> None:
        self.categories.pop(category_id, None)

    async def list_products(self, *, category_id: int | None = None, active_only: bool = True, pinned_only: bool = False) -> list[ProductDTO]:
        return [
            p
            for p in self.products.values()
            if (category_id is None or p.category_id == category_id)
            and (not active_only or p.is_active)
            and (not pinned_only or p.is_pinned)
        ]

    async def get_product_detail(self, product_id: int) -> ProductDetailDTO:
        return self.products[product_id]

    async def create_product(self, data: ProductCreateDTO) -> ProductDetailDTO:
        now = _now()
        pid = max(self.products.keys(), default=0) + 1
        p = ProductDetailDTO(
            id=pid,
            category_id=data.category_id,
            slug=data.slug,
            delivery_type=data.delivery_type,
            price_usd=data.price_usd,
            price_irt=data.price_irt,
            color_emoji=data.color_emoji,
            color_hex=data.color_hex,
            is_pinned=data.is_pinned,
            is_active=data.is_active,
            sort_order=data.sort_order,
            manual_stock_count=data.manual_stock_count,
            low_stock_threshold=data.low_stock_threshold,
            sold_count=0,
            titles_json=data.titles_json,
            descriptions_json=data.descriptions_json,
            rules_json=data.rules_json,
            manual_prompts_json=data.manual_prompts_json,
            available_stock=0,
            created_at=now,
            updated_at=now,
            bulk_tiers=data.bulk_tiers,
        )
        self.products[pid] = p
        return p

    async def update_product(self, product_id: int, data: ProductUpdateDTO) -> ProductDetailDTO:
        p = self.products[product_id]
        p = p.model_copy(update=data.model_dump(exclude_none=True))
        self.products[product_id] = p
        return p

    async def delete_product(self, product_id: int) -> None:
        self.products.pop(product_id, None)

    async def set_bulk_tiers(self, product_id: int, tiers: list[ProductBulkTierDTO]) -> list[ProductBulkTierDTO]:
        p = self.products[product_id].model_copy(update={"bulk_tiers": tiers})
        self.products[product_id] = p
        return tiers

    async def add_auto_stock(self, product_id: int, payloads: list[str], *, deduplicate: bool = False) -> StockImportResultDTO:
        self.stock_batches.append((product_id, payloads))
        p = self.products[product_id]
        new_avail = p.available_stock + len(payloads)
        self.products[product_id] = p.model_copy(update={"available_stock": new_avail})
        return StockImportResultDTO(
            product_id=product_id,
            added_count=len(payloads),
            skipped_duplicates=0,
            total_available=new_avail,
        )

    async def list_user_orders(self, user_id: int, *, limit: int = 20, offset: int = 0) -> list[OrderDTO]:
        return [o for o in self.orders.values() if o.user_id == user_id]

    async def list_orders(self, *, status: OrderStatus | None = None, limit: int = 50, offset: int = 0) -> list[OrderDTO]:
        return [o for o in self.orders.values() if status is None or o.status == status]

    async def get_order_detail(self, order_id: int, *, decrypt_items: bool = False) -> OrderDTO:
        return self.orders[order_id]

    async def fulfill_manual_order(self, order_id: int, admin_id: int, fulfillment_note: str | None = None) -> OrderDTO:
        o = self.orders[order_id].model_copy(
            update={
                "status": OrderStatus.COMPLETED,
                "fulfillment_note": fulfillment_note,
                "fulfilled_by_admin_id": admin_id,
                "fulfilled_at": _now(),
            }
        )
        self.orders[order_id] = o
        return o

    async def cancel_and_refund_order(self, order_id: int, admin_id: int, reason: str | None = None) -> OrderDTO:
        o = self.orders[order_id].model_copy(
            update={"status": OrderStatus.REFUNDED, "fulfillment_note": reason}
        )
        self.orders[order_id] = o
        return o


class FakePaymentService:
    def __init__(self) -> None:
        now = _now()
        self.rate = ExchangeRateDTO(usd_to_toman_rate=Decimal("65000"), rate_mode="manual", updated_at=now)
        self.wallets: dict[int, CryptoWalletDTO] = {
            1: CryptoWalletDTO(
                id=1,
                coin_name="Tether",
                symbol="USDT",
                network="TRC20",
                wallet_address="TTestWallet123",
                min_deposit_usd=Decimal("5"),
                verification_mode=CryptoVerificationMode.MANUAL_TXID,
                is_active=True,
                sort_order=0,
                created_at=now,
                updated_at=now,
            )
        }
        self.fiat_methods: dict[int, FiatMethodDTO] = {
            1: FiatMethodDTO(
                id=1,
                method_type=FiatMethodType.CARD_TO_CARD,
                title="Melli Card",
                card_number="6037991122334455",
                sheba="IR123456789012345678901234",
                cardholder_name="Ali Rezaei",
                bank_name="Melli",
                min_deposit_irt=Decimal("50000"),
                is_active=True,
                sort_order=0,
                created_at=now,
                updated_at=now,
            )
        }
        self.payments: dict[int, PaymentDTO] = {
            5: PaymentDTO(
                id=5,
                user_id=2,
                payment_category=PaymentCategory.CRYPTO,
                currency=Currency.USD,
                amount_usd=Decimal("25.00"),
                amount_irt=Decimal("0"),
                crypto_wallet_id=1,
                tx_hash="0xTXHASH<script>",
                status=PaymentStatus.PENDING,
                created_at=now,
            ),
            6: PaymentDTO(
                id=6,
                user_id=2,
                payment_category=PaymentCategory.FIAT_CARD,
                currency=Currency.IRT,
                amount_usd=Decimal("0"),
                amount_irt=Decimal("500000"),
                fiat_method_id=1,
                status=PaymentStatus.PENDING,
                created_at=now,
            ),
        }

    async def list_payments(self, *, status: PaymentStatus | None = None, user_id: int | None = None, limit: int = 50, offset: int = 0) -> list[PaymentDTO]:
        return [
            p
            for p in self.payments.values()
            if (status is None or p.status == status) and (user_id is None or p.user_id == user_id)
        ]

    async def get_payment(self, payment_id: int) -> PaymentDTO:
        return self.payments[payment_id]

    async def review_payment(self, review: PaymentReviewDTO) -> PaymentDTO:
        p = self.payments[review.payment_id].model_copy(
            update={
                "status": PaymentStatus.APPROVED if review.approve else PaymentStatus.REJECTED,
                "admin_id": review.admin_id,
                "admin_note": review.admin_note,
                "reviewed_at": _now(),
            }
        )
        self.payments[p.id] = p
        return p

    async def list_crypto_wallets(self, *, active_only: bool = False) -> list[CryptoWalletDTO]:
        return [w for w in self.wallets.values() if not active_only or w.is_active]

    async def create_crypto_wallet(self, data: CryptoWalletCreateDTO) -> CryptoWalletDTO:
        now = _now()
        wid = max(self.wallets.keys(), default=0) + 1
        w = CryptoWalletDTO(
            id=wid,
            coin_name=data.coin_name,
            symbol=data.symbol,
            network=data.network,
            wallet_address=data.wallet_address,
            min_deposit_usd=data.min_deposit_usd,
            fee_note=data.fee_note,
            instructions=data.instructions,
            verification_mode=data.verification_mode,
            is_active=data.is_active,
            sort_order=data.sort_order,
            created_at=now,
            updated_at=now,
        )
        self.wallets[wid] = w
        return w

    async def update_crypto_wallet(self, wallet_id: int, data: CryptoWalletUpdateDTO) -> CryptoWalletDTO:
        w = self.wallets[wallet_id].model_copy(update=data.model_dump(exclude_none=True))
        self.wallets[wallet_id] = w
        return w

    async def delete_crypto_wallet(self, wallet_id: int) -> None:
        self.wallets.pop(wallet_id, None)

    async def list_fiat_methods(self, *, active_only: bool = False) -> list[FiatMethodDTO]:
        return [m for m in self.fiat_methods.values() if not active_only or m.is_active]

    async def create_fiat_method(self, data: FiatMethodCreateDTO) -> FiatMethodDTO:
        now = _now()
        fid = max(self.fiat_methods.keys(), default=0) + 1
        m = FiatMethodDTO(
            id=fid,
            method_type=data.method_type,
            title=data.title,
            card_number=data.card_number,
            sheba=data.sheba,
            cardholder_name=data.cardholder_name,
            bank_name=data.bank_name,
            gateway_provider=data.gateway_provider,
            min_deposit_irt=data.min_deposit_irt,
            instructions=data.instructions,
            is_active=data.is_active,
            sort_order=data.sort_order,
            created_at=now,
            updated_at=now,
        )
        self.fiat_methods[fid] = m
        return m

    async def update_fiat_method(self, method_id: int, data: FiatMethodUpdateDTO) -> FiatMethodDTO:
        m = self.fiat_methods[method_id].model_copy(update=data.model_dump(exclude_none=True))
        self.fiat_methods[method_id] = m
        return m

    async def delete_fiat_method(self, method_id: int) -> None:
        self.fiat_methods.pop(method_id, None)

    async def get_exchange_rate(self) -> ExchangeRateDTO:
        return self.rate

    async def update_exchange_rate(self, rate: Decimal, *, rate_mode: str = "manual") -> ExchangeRateDTO:
        self.rate = ExchangeRateDTO(usd_to_toman_rate=rate, rate_mode=rate_mode, updated_at=_now())
        return self.rate


class FakeSupportService:
    def __init__(self) -> None:
        now = _now()
        self.tickets: dict[int, SupportTicketDetailDTO] = {
            3: SupportTicketDetailDTO(
                id=3,
                ticket_number="TCK-3",
                user_id=2,
                category=TicketCategory.OTHER_ISSUE,
                subject="Need help",
                status=TicketStatus.OPEN,
                created_at=now,
                updated_at=now,
                messages=[
                    SupportMessageDTO(
                        id=1,
                        ticket_id=3,
                        sender_role=SenderRole.USER,
                        sender_telegram_id=900002,
                        message_text="Hello <b>admin</b> <script>xss</script>",
                        created_at=now,
                    )
                ],
            )
        }

    async def list_tickets(self, *, status: TicketStatus | None = None, limit: int = 50, offset: int = 0) -> list[SupportTicketDTO]:
        return [t for t in self.tickets.values() if status is None or t.status == status]

    async def get_ticket_detail(self, ticket_id: int) -> SupportTicketDetailDTO:
        return self.tickets[ticket_id]

    async def add_message(self, ticket_id: int, sender_role: SenderRole, sender_telegram_id: int | None, *, message_text: str | None = None, telegram_file_id: str | None = None, file_type: str | None = None) -> SupportMessageDTO:
        t = self.tickets[ticket_id]
        msg = SupportMessageDTO(
            id=len(t.messages) + 1,
            ticket_id=ticket_id,
            sender_role=sender_role,
            sender_telegram_id=sender_telegram_id,
            message_text=message_text,
            telegram_file_id=telegram_file_id,
            file_type=file_type,
            created_at=_now(),
        )
        t.messages.append(msg)
        if sender_role == SenderRole.ADMIN:
            self.tickets[ticket_id] = t.model_copy(update={"status": TicketStatus.ANSWERED})
        return msg

    async def close_ticket(self, ticket_id: int) -> SupportTicketDTO:
        t = self.tickets[ticket_id].model_copy(update={"status": TicketStatus.CLOSED})
        self.tickets[ticket_id] = t
        return t


class FakeCMSService:
    def __init__(self) -> None:
        now = _now()
        self.translations: dict[tuple[str, str], UITranslationDTO] = {
            ("welcome.banner", "en"): UITranslationDTO(id=1, key="welcome.banner", lang_code="en", text_value="Welcome!", updated_at=now),
            ("welcome.banner", "fa"): UITranslationDTO(id=2, key="welcome.banner", lang_code="fa", text_value="خوش آمدید!", updated_at=now),
        }
        self.buttons: list[UIButtonDTO] = [
            UIButtonDTO(
                id=1,
                button_key="shop",
                menu_scope=MenuScope.MAIN,
                row_index=0,
                col_index=0,
                color_emoji="🔵",
                color_hex="#3b82f6",
                is_active=True,
                labels_json={"en": "🛒 Shop", "fa": "🛒 فروشگاه"},
                created_at=now,
                updated_at=now,
            )
        ]
        self.settings: dict[str, str] = {
            "maintenance_mode": "false",
            "pinned_limit": "4",
            "low_stock_default_threshold": "5",
            "force_join_enabled": "false",
            "force_join_channels": "",
        }

    async def get_translation(self, key: str, lang_code: str) -> str:
        tr = self.translations.get((key, lang_code))
        return tr.text_value if tr else key

    async def list_translations(self, *, lang_code: str | None = None, search: str | None = None, limit: int = 200, offset: int = 0) -> list[UITranslationDTO]:
        return [v for v in self.translations.values() if lang_code is None or v.lang_code == lang_code]

    async def upsert_translation(self, key: str, lang_code: str, text_value: str) -> UITranslationDTO:
        dto = UITranslationDTO(id=len(self.translations) + 1, key=key, lang_code=lang_code, text_value=text_value, updated_at=_now())
        self.translations[(key, lang_code)] = dto
        return dto

    async def list_buttons(self, *, menu_scope: str | None = None) -> list[UIButtonDTO]:
        return list(self.buttons)

    async def update_button(self, button_key: str, *, color_emoji: str | None = None, color_hex: str | None = None, labels_json: dict[str, str] | None = None, is_active: bool | None = None) -> UIButtonDTO:
        for i, b in enumerate(self.buttons):
            if b.button_key == button_key:
                upd: dict[str, Any] = {}
                if color_emoji is not None:
                    upd["color_emoji"] = color_emoji
                if color_hex is not None:
                    upd["color_hex"] = color_hex
                if labels_json is not None:
                    upd["labels_json"] = labels_json
                if is_active is not None:
                    upd["is_active"] = is_active
                self.buttons[i] = b.model_copy(update=upd)
                return self.buttons[i]
        raise KeyError(button_key)

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        return self.settings.get(key, default)

    async def set_setting(self, key: str, value: str, *, description: str | None = None) -> AppSettingDTO:
        self.settings[key] = value
        return AppSettingDTO(key=key, value=value, description=description, updated_at=_now())


# ---------------------------------------------------------------------------
# Helpers for Feeding Mocked Updates into Aiogram Dispatcher
# ---------------------------------------------------------------------------
def _make_bot_recorder() -> tuple[Bot, list[Any]]:
    """Create a real aiogram Bot instance with a mocked session recording all API calls."""
    recorded: list[Any] = []
    bot = Bot(token=f"123456:{secrets.token_urlsafe(24)}")

    async def _fake_call(_bot: Any, method: Any, **kwargs: Any) -> Any:
        recorded.append(method)
        if isinstance(method, (SendMessage, EditMessageText, SendPhoto)):
            return Message(
                message_id=101,
                date=_now(),
                chat=Chat(id=int(method.chat_id) if method.chat_id else 900001, type="private"),
                text=getattr(method, "text", None) or getattr(method, "caption", None) or "ok",
            ).as_(bot)
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    bot.session.make_request = AsyncMock(side_effect=_fake_call)  # type: ignore[method-assign]
    return bot, recorded


def _make_message_update(user_id: int, text: str, *, username: str = "tester", first_name: str = "Test") -> Update:
    msg = Message(
        message_id=10,
        date=_now(),
        chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name=first_name, username=username),
        text=text,
    )
    return Update(update_id=1, message=msg)


def _make_callback_update(user_id: int, data_str: str, *, username: str = "tester", first_name: str = "Test") -> Update:
    tg_user = TgUser(id=user_id, is_bot=False, first_name=first_name, username=username)
    msg = Message(
        message_id=10,
        date=_now(),
        chat=Chat(id=user_id, type="private"),
        from_user=tg_user,
        text="panel",
    )
    cb = CallbackQuery(
        id="cb_1",
        from_user=tg_user,
        chat_instance="ci_1",
        message=msg,
        data=data_str,
    )
    return Update(update_id=2, callback_query=cb)


def _build_test_dispatcher() -> tuple[Dispatcher, dict[str, Any]]:
    now = _now()
    admin_user = UserDTO(
        id=1,
        telegram_id=900001,
        username="boss_admin",
        first_name="Boss",
        language_code="en",
        balance_usd=Decimal("0"),
        balance_irt=Decimal("0"),
        is_blocked=False,
        is_admin=True,
        created_at=now,
        updated_at=now,
    )
    customer = UserDTO(
        id=2,
        telegram_id=900002,
        username="alice_xss",
        first_name="Alice <img src=x onerror=alert(1)>",
        language_code="fa",
        balance_usd=Decimal("15.00"),
        balance_irt=Decimal("100000"),
        is_blocked=False,
        is_admin=False,
        created_at=now,
        updated_at=now,
    )
    services: dict[str, Any] = {
        "analytics_service": FakeAnalyticsService(),
        "user_service": FakeUserService([admin_user, customer]),
        "shop_service": FakeShopService(),
        "payment_service": FakePaymentService(),
        "support_service": FakeSupportService(),
        "cms_service": FakeCMSService(),
    }
    admin_router._parent_router = None
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.workflow_data.update(services)
    return dp, services


# ---------------------------------------------------------------------------
# 1. BroadcastService Real PostgreSQL + Rate-Limiting + FloodWait Tests
# ---------------------------------------------------------------------------
async def test_broadcast_service_target_filtering_and_rate_limited_floodwait(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = BroadcastService(session_factory=db_session_factory)
    assert isinstance(svc, BroadcastServiceProtocol)

    test_tids = [9910001, 9910002, 9910003, 9910004]
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(delete(User).where(User.telegram_id.in_(test_tids)))
            session.add_all(
                [
                    User(telegram_id=9910001, language_code="fa", balance_usd=Decimal("10.00"), is_blocked=False),
                    User(telegram_id=9910002, language_code="en", balance_usd=Decimal("0"), balance_irt=Decimal("50000"), is_blocked=False),
                    User(telegram_id=9910003, language_code="en", balance_usd=Decimal("0"), is_blocked=False),
                    User(telegram_id=9910004, language_code="fa", balance_usd=Decimal("99.00"), is_blocked=True),
                ]
            )

    try:
        all_ids = await svc.resolve_target_telegram_ids("all")
        assert 9910001 in all_ids
        assert 9910002 in all_ids
        assert 9910003 in all_ids
        assert 9910004 not in all_ids  # blocked user excluded

        fa_ids = await svc.resolve_target_telegram_ids("lang:fa")
        assert 9910001 in fa_ids
        assert 9910002 not in fa_ids
        assert 9910004 not in fa_ids

        bal_ids = await svc.resolve_target_telegram_ids("has_balance")
        assert 9910001 in bal_ids
        assert 9910002 in bal_ids
        assert 9910003 not in bal_ids
        assert 9910004 not in bal_ids

        with pytest.raises(ValidationError):
            await svc.resolve_target_telegram_ids("lang:xx")

        # Create BroadcastJob with photo + inline URL button
        job = await svc.create_job(
            BroadcastCreateDTO(
                admin_id=900001,
                target_filter="has_balance",
                message_payload={
                    "content_type": "photo",
                    "photo_file_id": "AgACAgIAAxkBA",
                    "caption": "Special Promo!",
                    "button_text": "Open Shop",
                    "button_url": "https://example.com/shop",
                },
            )
        )
        assert job.status == BroadcastStatus.PENDING
        assert job.total_targets >= 2

        # Mock Bot & sleep_func to verify 25 msg/s pacing + TelegramRetryAfter retry + Forbidden handling
        sleeps: list[float] = []

        async def _fake_sleep(secs: float) -> None:
            sleeps.append(secs)

        mock_bot = MagicMock(spec=Bot)
        attempts: dict[int, int] = {}

        async def _send_photo_side_effect(*, chat_id: int, **kwargs: Any) -> MagicMock:
            attempts[chat_id] = attempts.get(chat_id, 0) + 1
            if chat_id == 9910001 and attempts[chat_id] == 1:
                raise TelegramRetryAfter(method=SendPhoto(chat_id=chat_id, photo="x"), message="Flood", retry_after=2)
            if chat_id == 9910002:
                raise TelegramForbiddenError(method=SendPhoto(chat_id=chat_id, photo="x"), message="Forbidden: bot blocked")
            return MagicMock()

        mock_bot.send_photo = AsyncMock(side_effect=_send_photo_side_effect)
        mock_bot.send_message = AsyncMock()

        # Restrict test run targets cleanly via user: or verify against our known IDs
        job_svc = BroadcastService(
            session_factory=db_session_factory,
            bot=mock_bot,
            rate_per_sec=25,
            sleep_func=_fake_sleep,
        )
        progress_snapshots: list[str] = []

        async def _cb(pjob: Any) -> None:
            progress_snapshots.append(format_broadcast_progress_bar(pjob))

        completed = await job_svc.execute_job(job.id, progress_callback=_cb, progress_batch_size=1)
        assert completed.status == BroadcastStatus.COMPLETED
        assert completed.completed_at is not None
        assert attempts[9910001] == 2  # retried after FloodWait
        assert completed.sent_count >= 1
        assert completed.failed_count >= 1
        assert any(abs(s - 2.0) < 0.01 for s in sleeps)  # FloodWait sleep(2)
        assert any(abs(s - (1.0 / 25.0)) < 0.001 for s in sleeps)  # 25 msg/s rate limit sleep
        assert progress_snapshots
        assert "100%" in progress_snapshots[-1]

        # Test direct message BadRequest graceful handling
        mock_bot.send_message = AsyncMock(
            side_effect=TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="chat not found")
        )
        dm_ok = await job_svc.send_direct_message(123456, {"content_type": "text", "text": "Hi"})
        assert dm_ok is False

        kb = build_broadcast_inline_kb({"buttons": [{"text": "Go", "url": "https://t.me/test"}]})
        assert kb is not None
        assert kb.inline_keyboard[0][0].url == "https://t.me/test"
    finally:
        async with db_session_factory() as session:
            async with session.begin():
                await session.execute(delete(BroadcastJob).where(BroadcastJob.admin_id == 900001))
                await session.execute(delete(User).where(User.telegram_id.in_(test_tids)))


# ---------------------------------------------------------------------------
# 2. RBAC Admin Rejection & Authorization Tests
# ---------------------------------------------------------------------------
async def test_admin_rbac_rejects_non_admin_and_allows_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "[900001]")
    clear_settings_cache()
    try:
        dp, _services = _build_test_dispatcher()
        bot, recorded = _make_bot_recorder()

        # Non-admin (telegram_id=900002, is_admin=False) tries /admin
        await dp.feed_update(bot, _make_message_update(900002, "/admin", username="normie"))
        assert len(recorded) == 1
        assert isinstance(recorded[0], SendMessage)
        assert "Access denied" in recorded[0].text

        # Non-admin tries AdmCB callback
        recorded.clear()
        await dp.feed_update(bot, _make_callback_update(900002, AdmCB(sec="dash", act="view").pack()))
        assert len(recorded) == 1
        assert isinstance(recorded[0], AnswerCallbackQuery)
        assert recorded[0].show_alert is True
        assert "Access denied" in (recorded[0].text or "")

        # Authorized admin (900001 in ADMIN_TELEGRAM_IDS) sends /admin
        recorded.clear()
        await dp.feed_update(bot, _make_message_update(900001, "/admin", username="boss_admin"))
        assert len(recorded) == 1
        assert isinstance(recorded[0], SendMessage)
        assert "In-Bot Admin Console" in recorded[0].text

        # DB is_admin=True user (even if not in env ADMIN_TELEGRAM_IDS) is allowed
        _services["user_service"].users[3] = UserDTO(
            id=3,
            telegram_id=900099,
            username="db_admin",
            first_name="DBAdmin",
            language_code="en",
            is_admin=True,
            created_at=_now(),
            updated_at=_now(),
        )
        recorded.clear()
        await dp.feed_update(bot, _make_message_update(900099, "/admin", username="db_admin"))
        assert isinstance(recorded[0], SendMessage)
        assert "In-Bot Admin Console" in recorded[0].text
    finally:
        clear_settings_cache()


# ---------------------------------------------------------------------------
# 3. Dashboard, Users, Catalog/Colors, Payments, Support, CMS, Broadcast Flow
# ---------------------------------------------------------------------------
async def test_admin_dashboard_and_xss_escaping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "[900001]")
    clear_settings_cache()
    try:
        dp, _services = _build_test_dispatcher()
        bot, recorded = _make_bot_recorder()

        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="dash", act="view").pack()))
        edits = [m for m in recorded if isinstance(m, EditMessageText)]
        assert edits
        body = edits[-1].text
        assert "Total Users: <b>42</b>" in body
        assert "$1,234.50" in body
        assert "Pending Deposits: <b>3</b>" in body
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    finally:
        clear_settings_cache()


async def test_admin_user_search_balance_adjust_and_ban(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "[900001]")
    clear_settings_cache()
    try:
        dp, services = _build_test_dispatcher()
        bot, recorded = _make_bot_recorder()

        # Open user search -> search by @username
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="usr", act="search").pack()))
        await dp.feed_update(bot, _make_message_update(900001, "@alice_xss"))
        sent = [m for m in recorded if isinstance(m, SendMessage)]
        assert sent
        assert "&lt;img src=x onerror=alert(1)&gt;" in sent[-1].text
        assert "<img src=x" not in sent[-1].text

        # Charge USD balance + note -> notifies customer
        recorded.clear()
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="usr", act="chg_usd", id=2).pack()))
        await dp.feed_update(bot, _make_message_update(900001, "12.50"))
        await dp.feed_update(bot, _make_message_update(900001, "Bonus <ok>"))

        assert services["user_service"].users[2].balance_usd == Decimal("27.50")
        assert services["user_service"].adjustments[-1].amount == Decimal("12.50")
        dm_calls = [m for m in recorded if isinstance(m, SendMessage) and int(m.chat_id) == 900002]
        assert dm_calls
        assert "Bonus &lt;ok&gt;" in dm_calls[0].text

        # Toggle block/unblock
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="usr", act="toggle_ban", id=2).pack()))
        assert services["user_service"].users[2].is_blocked is True
    finally:
        clear_settings_cache()


async def test_admin_catalog_dual_colors_stock_tiers_and_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "[900001]")
    clear_settings_cache()
    try:
        dp, services = _build_test_dispatcher()
        bot, recorded = _make_bot_recorder()

        # Toggle pin on product #1
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="prd", act="pin", id=1).pack()))
        assert services["shop_service"].products[1].is_pinned is True

        # Pick Red preset color on product #1
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="prd", act="col_red", id=1).pack()))
        assert services["shop_service"].products[1].color_emoji == "🔴"
        assert services["shop_service"].products[1].color_hex == "#ef4444"

        # Custom Hex color via FSM
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="prd", act="hex", id=1).pack()))
        await dp.feed_update(bot, _make_message_update(900001, "#10B981 | 🟢"))
        assert services["shop_service"].products[1].color_hex == "#10b981"

        # Bulk tiers via FSM
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="prd", act="tiers", id=1).pack()))
        await dp.feed_update(bot, _make_message_update(900001, "10:8.50:550000\n30:7.50"))
        assert len(services["shop_service"].products[1].bulk_tiers) == 2

        # Paste bulk stock lines
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="prd", act="stock", id=1).pack()))
        await dp.feed_update(bot, _make_message_update(900001, "KEY-111\nKEY-222"))
        assert services["shop_service"].stock_batches[-1] == (1, ["KEY-111", "KEY-222"])

        # Fulfill manual order #10 -> notifies customer
        recorded.clear()
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="ord", act="fulfill", id=10).pack()))
        await dp.feed_update(bot, _make_message_update(900001, "Activated!"))
        assert services["shop_service"].orders[10].status == OrderStatus.COMPLETED
        dm_calls = [m for m in recorded if isinstance(m, SendMessage) and int(m.chat_id) == 900002]
        assert dm_calls
        assert "ORD-10" in dm_calls[0].text
    finally:
        clear_settings_cache()


async def test_admin_payments_approve_reject_wallets_and_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "[900001]")
    clear_settings_cache()
    try:
        dp, services = _build_test_dispatcher()
        bot, recorded = _make_bot_recorder()

        # 1-click Approve Payment #5 via PayCB
        await dp.feed_update(bot, _make_callback_update(900001, PayCB(id=5, action="approve").pack()))
        assert services["payment_service"].payments[5].status == PaymentStatus.APPROVED
        dm_approve = [m for m in recorded if isinstance(m, SendMessage) and int(m.chat_id) == 900002]
        assert dm_approve

        # Reject Payment #6 with reason
        recorded.clear()
        await dp.feed_update(bot, _make_callback_update(900001, PayCB(id=6, action="reject").pack()))
        await dp.feed_update(bot, _make_message_update(900001, "Receipt <blurry>"))
        assert services["payment_service"].payments[6].status == PaymentStatus.REJECTED
        assert services["payment_service"].payments[6].admin_note == "Receipt <blurry>"
        dm_reject = [m for m in recorded if isinstance(m, SendMessage) and int(m.chat_id) == 900002]
        assert dm_reject
        assert "Receipt &lt;blurry&gt;" in dm_reject[0].text

        # Create Crypto Wallet + Update USD-to-Toman rate
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="wal", act="add").pack()))
        await dp.feed_update(bot, _make_message_update(900001, "Litecoin | LTC | LTC | ltc1qtestaddr | 2 | low fee | Send LTC"))
        assert len(services["payment_service"].wallets) == 2

        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="pay", act="set_rate").pack()))
        await dp.feed_update(bot, _make_message_update(900001, "69500"))
        assert services["payment_service"].rate.usd_to_toman_rate == Decimal("69500")
    finally:
        clear_settings_cache()


async def test_admin_support_cms_settings_and_broadcast_wizard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "[900001]")
    clear_settings_cache()
    try:
        dp, services = _build_test_dispatcher()
        bot, recorded = _make_bot_recorder()

        # Support ticket view escapes XSS, then reply & close
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="sup", act="view", id=3).pack()))
        edits = [m for m in recorded if isinstance(m, EditMessageText)]
        assert "&lt;script&gt;xss&lt;/script&gt;" in edits[-1].text

        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="sup", act="reply", id=3).pack()))
        await dp.feed_update(bot, _make_message_update(900001, "Here is your fix"))
        assert services["support_service"].tickets[3].status == TicketStatus.ANSWERED

        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="sup", act="close", id=3).pack()))
        assert services["support_service"].tickets[3].status == TicketStatus.CLOSED

        # CMS translation upsert + UI button color + maintenance mode toggle
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="cms", act="upsert").pack()))
        await dp.feed_update(bot, _make_message_update(900001, "welcome.banner | fa | سلام جدید!"))
        assert services["cms_service"].translations[("welcome.banner", "fa")].text_value == "سلام جدید!"

        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="btn", act="col_green", id=1).pack()))
        assert services["cms_service"].buttons[0].color_hex == "#22c55e"

        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="set", act="tog_maint").pack()))
        assert services["cms_service"].settings["maintenance_mode"] == "true"

        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="set", act="tog_fjoin").pack()))
        assert services["cms_service"].settings["force_join_enabled"] == "true"

        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="set", act="fjoin_ch").pack()))
        await dp.feed_update(bot, _make_message_update(900001, "@myshop_ch"))
        assert services["cms_service"].settings["force_join_channels"] == "@myshop_ch"

        # Keyboard invariants
        home_kb = build_admin_home_kb()
        assert len(home_kb.inline_keyboard) >= 5
        picker_kb = build_dual_color_picker_kb("prd", 1)
        assert len(picker_kb.inline_keyboard) >= 5
    finally:
        clear_settings_cache()


async def test_admin_broadcast_wizard_fiat_crud_and_stock_file_upload(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "[900001]")
    clear_settings_cache()
    try:
        dp, services = _build_test_dispatcher()
        real_bs = BroadcastService(
            session_factory=db_session_factory,
            rate_per_sec=100,
        )
        # Inject a fake-session BroadcastService for the wizard test using FakeUserService targets
        class WizardBroadcastDouble:
            def __init__(self) -> None:
                self.jobs: dict[int, Any] = {}
                self.dms: list[tuple[int, dict[str, Any]]] = []

            async def resolve_target_telegram_ids(self, target_filter: str) -> list[int]:
                assert target_filter == "lang:fa"
                return [900002]

            async def create_job(self, data: BroadcastCreateDTO) -> Any:
                from app.services.dtos import BroadcastJobDTO
                job = BroadcastJobDTO(
                    id=77,
                    admin_id=data.admin_id,
                    target_filter=data.target_filter,
                    message_payload=data.message_payload,
                    total_targets=1,
                    sent_count=0,
                    failed_count=0,
                    status=BroadcastStatus.PENDING,
                    created_at=_now(),
                )
                self.jobs[77] = job
                return job

            async def execute_job(self, job_id: int, *, bot: Any = None, progress_callback: Any = None, progress_batch_size: int = 10) -> Any:
                job = self.jobs[job_id].model_copy(
                    update={"sent_count": 1, "failed_count": 0, "status": BroadcastStatus.COMPLETED, "completed_at": _now()}
                )
                self.jobs[job_id] = job
                if progress_callback is not None:
                    await progress_callback(job)
                return job

            async def send_direct_message(self, telegram_id: int, payload: dict[str, Any], *, bot: Any = None) -> bool:
                self.dms.append((telegram_id, payload))
                return True

            async def list_jobs(self, *, limit: int = 20, offset: int = 0) -> list[Any]:
                return list(self.jobs.values())

            async def get_job(self, job_id: int) -> Any:
                return self.jobs[job_id]

        wiz_bc = WizardBroadcastDouble()
        dp.workflow_data["broadcast_service"] = wiz_bc
        bot, recorded = _make_bot_recorder()

        # Run Mass Broadcast Wizard: new -> aud_fa -> text + BTN -> confirm
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="bc", act="new").pack()))
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="bc", act="aud_fa").pack()))
        await dp.feed_update(
            bot,
            _make_message_update(900001, "Flash Sale!\nBTN: Shop Now | https://example.com"),
        )
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="bc", act="confirm").pack()))
        assert 77 in wiz_bc.jobs
        assert wiz_bc.jobs[77].status == BroadcastStatus.COMPLETED
        assert wiz_bc.jobs[77].message_payload["buttons"] == [{"text": "Shop Now", "url": "https://example.com"}]

        # Single-user DM via bc:dm -> @alice_xss -> text
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="bc", act="dm").pack()))
        await dp.feed_update(bot, _make_message_update(900001, "@alice_xss"))
        await dp.feed_update(bot, _make_message_update(900001, "Hello Alice"))
        assert wiz_bc.dms[-1] == (900002, {"content_type": "text", "text": "Hello Alice"})

        # Add Iranian Fiat Method & toggle exchange rate mode
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="fiat", act="add").pack()))
        await dp.feed_update(
            bot,
            _make_message_update(900001, "card_to_card | Mellat | 6104337788990011 | IR000000000000000000000001 | Reza | Mellat | 100000 | Send receipt"),
        )
        assert len(services["payment_service"].fiat_methods) == 2

        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="pay", act="mode_live").pack()))
        assert services["payment_service"].rate.rate_mode == "live"

        # Invalid hex rejected without changing product #1 color
        prev_hex = services["shop_service"].products[1].color_hex
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="prd", act="hex", id=1).pack()))
        await dp.feed_update(bot, _make_message_update(900001, "invalid-hex"))
        assert services["shop_service"].products[1].color_hex == prev_hex

        # .txt document stock upload via mocked bot.download
        from aiogram.types import Document
        async def _fake_download(file: Any, destination: Any, **kw: Any) -> None:
            destination.write(b"ACC-1\nACC-2\n")
        bot.download = AsyncMock(side_effect=_fake_download)  # type: ignore[method-assign]
        await dp.feed_update(bot, _make_callback_update(900001, AdmCB(sec="prd", act="stock", id=1).pack()))
        doc_msg = Message(
            message_id=99,
            date=_now(),
            chat=Chat(id=900001, type="private"),
            from_user=TgUser(id=900001, is_bot=False, first_name="Boss", username="boss_admin"),
            document=Document(file_id="doc_1", file_unique_id="udoc_1", file_name="stock.txt"),
        )
        await dp.feed_update(bot, Update(update_id=99, message=doc_msg))
        assert services["shop_service"].stock_batches[-1] == (1, ["ACC-1", "ACC-2"])
        assert isinstance(real_bs, BroadcastServiceProtocol)
    finally:
        clear_settings_cache()
