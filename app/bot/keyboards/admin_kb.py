"""Inline keyboard builders for the in-bot /admin management console."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards.common_kb import AdmCB, MenuCB, PayCB
from app.core.constants import (
    COLOR_PALETTE,
    LANGUAGE_ORDER,
    SUPPORTED_LANGUAGES,
    OrderStatus,
    PaymentStatus,
    TicketStatus,
)
from app.services.dtos import (
    BroadcastJobDTO,
    CategoryDTO,
    CryptoWalletDTO,
    FiatMethodDTO,
    OrderDTO,
    PaymentDTO,
    ProductDTO,
    SupportTicketDTO,
    UIButtonDTO,
    UITranslationDTO,
    UserDTO,
)


def build_admin_home_kb() -> InlineKeyboardMarkup:
    """Build the root /admin console inline keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Dashboard & Analytics",
                    callback_data=AdmCB(sec="dash", act="view").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Users & Search",
                    callback_data=AdmCB(sec="usr", act="menu").pack(),
                ),
                InlineKeyboardButton(
                    text="📢 Broadcast & DM",
                    callback_data=AdmCB(sec="bc", act="menu").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📦 Catalog, Stock & Colors",
                    callback_data=AdmCB(sec="cat", act="menu").pack(),
                ),
                InlineKeyboardButton(
                    text="📋 Manual Orders",
                    callback_data=AdmCB(sec="ord", act="list", page=0).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💳 Payments & Wallets (پرداخت‌ها)",
                    callback_data=AdmCB(sec="pay", act="menu").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆘 Support Tickets",
                    callback_data=AdmCB(sec="sup", act="menu").pack(),
                ),
                InlineKeyboardButton(
                    text="🎨 CMS, Buttons & Settings",
                    callback_data=AdmCB(sec="cms", act="menu").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Customer Main Menu",
                    callback_data=MenuCB(action="main").pack(),
                )
            ],
        ]
    )


def build_admin_back_kb(
    *,
    sec: str = "home",
    act: str = "view",
    target_id: int = 0,
    page: int = 0,
    label: str = "🔙 Back",
) -> InlineKeyboardMarkup:
    """Build a single-button Back keyboard returning to an admin section."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=AdmCB(sec=sec, act=act, id=target_id, page=page).pack(),
                )
            ]
        ]
    )


def build_admin_cancel_kb(
    *,
    sec: str = "home",
    act: str = "view",
    target_id: int = 0,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Build a Cancel keyboard for active AdminStates FSM prompts."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data=AdmCB(sec=sec, act=act, id=target_id, page=page).pack(),
                )
            ]
        ]
    )


def build_dashboard_kb() -> InlineKeyboardMarkup:
    """Build the Dashboard & Analytics action keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Refresh KPIs",
                    callback_data=AdmCB(sec="dash", act="view").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏳ Pending Deposits",
                    callback_data=AdmCB(sec="pay", act="q_pending", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="📋 Pending Orders",
                    callback_data=AdmCB(sec="ord", act="pending", page=0).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🆘 Open Tickets",
                    callback_data=AdmCB(sec="sup", act="open", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="📦 Products & Stock",
                    callback_data=AdmCB(sec="prd", act="list", page=0).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Admin",
                    callback_data=AdmCB(sec="home", act="view").pack(),
                )
            ],
        ]
    )


def build_users_menu_kb() -> InlineKeyboardMarkup:
    """Build the Member Search & User Management menu keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Search User (ID / @username / Name)",
                    callback_data=AdmCB(sec="usr", act="search").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 All Users",
                    callback_data=AdmCB(sec="usr", act="list", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="🚫 Blocked Users",
                    callback_data=AdmCB(sec="usr", act="blocked", page=0).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Admin",
                    callback_data=AdmCB(sec="home", act="view").pack(),
                )
            ],
        ]
    )


def build_user_list_kb(
    users: Sequence[UserDTO],
    *,
    act: str = "list",
    page: int = 0,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    """Build a paginated list of users leading to their inspection cards."""
    rows: list[list[InlineKeyboardButton]] = []
    for u in users:
        status_icon = "🚫" if u.is_blocked else "🟢"
        uname = f"@{u.username}" if u.username else (u.first_name or str(u.telegram_id))
        label = f"{status_icon} {uname} ({u.telegram_id}) | ${u.balance_usd:.2f}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=AdmCB(sec="usr", act="view", id=u.id).pack(),
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Prev",
                callback_data=AdmCB(sec="usr", act=act, page=page - 1).pack(),
            )
        )
    if has_next:
        nav_row.append(
            InlineKeyboardButton(
                text="Next ➡️",
                callback_data=AdmCB(sec="usr", act=act, page=page + 1).pack(),
            )
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Users Menu",
                callback_data=AdmCB(sec="usr", act="menu").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_user_inspection_kb(user: UserDTO) -> InlineKeyboardMarkup:
    """Build the User Profile Inspection & Actions Card inline keyboard."""
    ban_label = "✅ Unblock User" if user.is_blocked else "🚫 Block User"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Charge USD ($)",
                    callback_data=AdmCB(sec="usr", act="chg_usd", id=user.id).pack(),
                ),
                InlineKeyboardButton(
                    text="➖ Deduct USD ($)",
                    callback_data=AdmCB(sec="usr", act="ded_usd", id=user.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ Charge IRT (تومان)",
                    callback_data=AdmCB(sec="usr", act="chg_irt", id=user.id).pack(),
                ),
                InlineKeyboardButton(
                    text="➖ Deduct IRT (تومان)",
                    callback_data=AdmCB(sec="usr", act="ded_irt", id=user.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=ban_label,
                    callback_data=AdmCB(sec="usr", act="toggle_ban", id=user.id).pack(),
                ),
                InlineKeyboardButton(
                    text="✉️ Direct Message",
                    callback_data=AdmCB(sec="usr", act="dm", id=user.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💳 Deposit History",
                    callback_data=AdmCB(sec="usr", act="deposits", id=user.id, page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="📜 Order History",
                    callback_data=AdmCB(sec="usr", act="orders", id=user.id, page=0).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📒 Wallet Ledger",
                    callback_data=AdmCB(sec="usr", act="ledger", id=user.id, page=0).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Users",
                    callback_data=AdmCB(sec="usr", act="menu").pack(),
                )
            ],
        ]
    )


def build_catalog_menu_kb() -> InlineKeyboardMarkup:
    """Build the Catalog, Stock & Dual-Layer Color Manager menu keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📂 Categories List",
                    callback_data=AdmCB(sec="cat", act="list", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="➕ New Category",
                    callback_data=AdmCB(sec="cat", act="add").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📦 Products List",
                    callback_data=AdmCB(sec="prd", act="list", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="➕ New Product",
                    callback_data=AdmCB(sec="prd", act="add").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 Manual Fulfillment Queue",
                    callback_data=AdmCB(sec="ord", act="pending", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="📜 All Orders",
                    callback_data=AdmCB(sec="ord", act="list", page=0).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Admin",
                    callback_data=AdmCB(sec="home", act="view").pack(),
                )
            ],
        ]
    )


def build_categories_list_kb(categories: Sequence[CategoryDTO]) -> InlineKeyboardMarkup:
    """Build inline list of categories with color badges."""
    rows: list[list[InlineKeyboardButton]] = []
    for cat in categories:
        name = cat.names_json.get("en") or cat.names_json.get("fa") or cat.slug
        active_mark = "✅" if cat.is_active else "❌"
        label = f"{cat.color_emoji} {name} ({cat.color_hex}) {active_mark}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=AdmCB(sec="cat", act="view", id=cat.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Add Category",
                callback_data=AdmCB(sec="cat", act="add").pack(),
            ),
            InlineKeyboardButton(
                text="🔙 Back",
                callback_data=AdmCB(sec="cat", act="menu").pack(),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_category_detail_kb(cat: CategoryDTO) -> InlineKeyboardMarkup:
    """Build management keyboard for a single Category."""
    toggle_label = "❌ Disable Category" if cat.is_active else "✅ Enable Category"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_label,
                    callback_data=AdmCB(sec="cat", act="toggle", id=cat.id).pack(),
                ),
                InlineKeyboardButton(
                    text=f"🎨 Color ({cat.color_emoji} {cat.color_hex})",
                    callback_data=AdmCB(sec="cat", act="color", id=cat.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Edit Category",
                    callback_data=AdmCB(sec="cat", act="edit", id=cat.id).pack(),
                ),
                InlineKeyboardButton(
                    text="📦 Category Products",
                    callback_data=AdmCB(sec="prd", act="by_cat", id=cat.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Delete Category",
                    callback_data=AdmCB(sec="cat", act="del", id=cat.id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Categories",
                    callback_data=AdmCB(sec="cat", act="list").pack(),
                )
            ],
        ]
    )


def build_products_list_kb(products: Sequence[ProductDTO], *, category_id: int = 0) -> InlineKeyboardMarkup:
    """Build inline list of products with color badge, pin state, and stock."""
    rows: list[list[InlineKeyboardButton]] = []
    for prod in products:
        title = prod.titles_json.get("en") or prod.titles_json.get("fa") or prod.slug
        pin_mark = "📌" if prod.is_pinned else ""
        active_mark = "✅" if prod.is_active else "❌"
        stock_str = "∞" if prod.manual_stock_count == -1 else str(prod.available_stock)
        label = f"{prod.color_emoji}{pin_mark} {title} | ${prod.price_usd:.2f} | Stk:{stock_str} {active_mark}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=AdmCB(sec="prd", act="view", id=prod.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Add Product",
                callback_data=AdmCB(sec="prd", act="add", id=category_id).pack(),
            ),
            InlineKeyboardButton(
                text="🔙 Back",
                callback_data=AdmCB(sec="cat", act="menu").pack(),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_product_detail_kb(prod: ProductDTO) -> InlineKeyboardMarkup:
    """Build full management keyboard for a single Product (stock, tiers, colors, pin)."""
    pin_label = "📍 Unpin Featured" if prod.is_pinned else "📌 Pin to Main Menu"
    active_label = "❌ Disable" if prod.is_active else "✅ Enable"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=pin_label,
                    callback_data=AdmCB(sec="prd", act="pin", id=prod.id).pack(),
                ),
                InlineKeyboardButton(
                    text=active_label,
                    callback_data=AdmCB(sec="prd", act="toggle", id=prod.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📥 Upload / Paste Stock",
                    callback_data=AdmCB(sec="prd", act="stock", id=prod.id).pack(),
                ),
                InlineKeyboardButton(
                    text="🏷 Bulk Discount Tiers",
                    callback_data=AdmCB(sec="prd", act="tiers", id=prod.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"🎨 Color ({prod.color_emoji} {prod.color_hex})",
                    callback_data=AdmCB(sec="prd", act="color", id=prod.id).pack(),
                ),
                InlineKeyboardButton(
                    text="✏️ Edit Product",
                    callback_data=AdmCB(sec="prd", act="edit", id=prod.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Delete Product",
                    callback_data=AdmCB(sec="prd", act="del", id=prod.id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Products",
                    callback_data=AdmCB(sec="prd", act="list").pack(),
                )
            ],
        ]
    )


def build_dual_color_picker_kb(sec: str, target_id: int) -> InlineKeyboardMarkup:
    """Build the Dual-Layer Color Picker (8 Telegram Emoji Badges + Custom #RRGGBB Hex)."""
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for key, preset in COLOR_PALETTE.items():
        pair.append(
            InlineKeyboardButton(
                text=f"{preset.emoji} {preset.label} ({preset.hex_code})",
                callback_data=AdmCB(sec=sec, act=f"col_{key}", id=target_id).pack(),
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    rows.append(
        [
            InlineKeyboardButton(
                text="🎨 Enter Custom Hex (#RRGGBB) / Emoji",
                callback_data=AdmCB(sec=sec, act="hex", id=target_id).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back",
                callback_data=AdmCB(sec=sec, act="view", id=target_id).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_orders_list_kb(orders: Sequence[OrderDTO], *, filter_act: str = "list") -> InlineKeyboardMarkup:
    """Build inline list of orders with status filter tabs."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="⏳ Pending Manual",
                callback_data=AdmCB(sec="ord", act="pending", page=0).pack(),
            ),
            InlineKeyboardButton(
                text="📋 All Orders",
                callback_data=AdmCB(sec="ord", act="list", page=0).pack(),
            ),
        ]
    ]
    for o in orders:
        icon = (
            "⏳"
            if o.status in {OrderStatus.PENDING_FULFILLMENT, OrderStatus.PENDING_INPUT}
            else ("✅" if o.status == OrderStatus.COMPLETED else "💸")
        )
        label = f"{icon} {o.order_number} | x{o.quantity} | {o.total_amount:.2f} {o.currency.value}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=AdmCB(sec="ord", act="view", id=o.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Catalog Menu",
                callback_data=AdmCB(sec="cat", act="menu").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_order_detail_kb(order: OrderDTO) -> InlineKeyboardMarkup:
    """Build fulfillment and refund action buttons for an Order."""
    rows: list[list[InlineKeyboardButton]] = []
    if order.status in {OrderStatus.PENDING_FULFILLMENT, OrderStatus.PENDING_INPUT}:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Fulfill Order",
                    callback_data=AdmCB(sec="ord", act="fulfill", id=order.id).pack(),
                ),
                InlineKeyboardButton(
                    text="💸 Cancel & Refund",
                    callback_data=AdmCB(sec="ord", act="refund", id=order.id).pack(),
                ),
            ]
        )
    elif order.status == OrderStatus.COMPLETED:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💸 Refund Order",
                    callback_data=AdmCB(sec="ord", act="refund", id=order.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="👤 Inspect Customer",
                callback_data=AdmCB(sec="usr", act="view", id=order.user_id).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Orders",
                callback_data=AdmCB(sec="ord", act="list").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_payments_menu_kb() -> InlineKeyboardMarkup:
    """Build the Payments & Wallets (قسمت پرداخت‌ها) menu keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏳ Pending Queue",
                    callback_data=AdmCB(sec="pay", act="q_pending", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="✅ Approved",
                    callback_data=AdmCB(sec="pay", act="q_approved", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Rejected",
                    callback_data=AdmCB(sec="pay", act="q_rejected", page=0).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🪙 Crypto Wallets",
                    callback_data=AdmCB(sec="wal", act="list", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="➕ Add Crypto Wallet",
                    callback_data=AdmCB(sec="wal", act="add").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏦 Fiat / Card Methods (ریالی)",
                    callback_data=AdmCB(sec="fiat", act="list", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="➕ Add Fiat Method",
                    callback_data=AdmCB(sec="fiat", act="add").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💱 USD-to-Toman Exchange Rate",
                    callback_data=AdmCB(sec="pay", act="rate").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Admin",
                    callback_data=AdmCB(sec="home", act="view").pack(),
                )
            ],
        ]
    )


def build_payments_queue_kb(payments: Sequence[PaymentDTO]) -> InlineKeyboardMarkup:
    """Build filterable Payments Queue keyboard."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="⏳ Pending",
                callback_data=AdmCB(sec="pay", act="q_pending", page=0).pack(),
            ),
            InlineKeyboardButton(
                text="✅ Approved",
                callback_data=AdmCB(sec="pay", act="q_approved", page=0).pack(),
            ),
            InlineKeyboardButton(
                text="❌ Rejected",
                callback_data=AdmCB(sec="pay", act="q_rejected", page=0).pack(),
            ),
        ]
    ]
    for p in payments:
        icon = (
            "⏳"
            if p.status == PaymentStatus.PENDING
            else ("✅" if p.status == PaymentStatus.APPROVED else "❌")
        )
        amt = f"${p.amount_usd:.2f}" if p.amount_usd > 0 else f"{p.amount_irt:.0f} IRT"
        label = f"{icon} #{p.id} | User:{p.user_id} | {amt} ({p.payment_category.value})"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=AdmCB(sec="pay", act="view", id=p.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Payments Menu",
                callback_data=AdmCB(sec="pay", act="menu").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_payment_detail_kb(payment: PaymentDTO) -> InlineKeyboardMarkup:
    """Build 1-click Approve & Credit / Reject buttons for a Payment."""
    rows: list[list[InlineKeyboardButton]] = []
    if payment.status == PaymentStatus.PENDING:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Approve & Credit",
                    callback_data=PayCB(id=payment.id, action="approve").pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Reject",
                    callback_data=PayCB(id=payment.id, action="reject").pack(),
                ),
            ]
        )
    if payment.receipt_file_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🖼 View Receipt Image",
                    callback_data=AdmCB(sec="pay", act="receipt", id=payment.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="👤 Inspect User",
                callback_data=AdmCB(sec="usr", act="view", id=payment.user_id).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Queue",
                callback_data=AdmCB(sec="pay", act="q_pending", page=0).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_crypto_wallets_list_kb(wallets: Sequence[CryptoWalletDTO]) -> InlineKeyboardMarkup:
    """Build list of configured CryptoWallets."""
    rows: list[list[InlineKeyboardButton]] = []
    for w in wallets:
        active_mark = "✅" if w.is_active else "❌"
        label = f"🪙 {w.symbol} ({w.network}) | Min ${w.min_deposit_usd:.2f} {active_mark}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=AdmCB(sec="wal", act="view", id=w.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Add Crypto Wallet",
                callback_data=AdmCB(sec="wal", act="add").pack(),
            ),
            InlineKeyboardButton(
                text="🔙 Back",
                callback_data=AdmCB(sec="pay", act="menu").pack(),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_crypto_wallet_detail_kb(wallet: CryptoWalletDTO) -> InlineKeyboardMarkup:
    """Build CRUD actions for a single CryptoWallet."""
    toggle_label = "❌ Disable Wallet" if wallet.is_active else "✅ Enable Wallet"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_label,
                    callback_data=AdmCB(sec="wal", act="toggle", id=wallet.id).pack(),
                ),
                InlineKeyboardButton(
                    text="✏️ Edit Wallet",
                    callback_data=AdmCB(sec="wal", act="edit", id=wallet.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Delete Wallet",
                    callback_data=AdmCB(sec="wal", act="del", id=wallet.id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Crypto Wallets",
                    callback_data=AdmCB(sec="wal", act="list").pack(),
                )
            ],
        ]
    )


def build_fiat_methods_list_kb(methods: Sequence[FiatMethodDTO]) -> InlineKeyboardMarkup:
    """Build list of Iranian Fiat payment methods."""
    rows: list[list[InlineKeyboardButton]] = []
    for m in methods:
        active_mark = "✅" if m.is_active else "❌"
        label = f"🏦 {m.title} ({m.method_type.value}) {active_mark}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=AdmCB(sec="fiat", act="view", id=m.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Add Fiat Method",
                callback_data=AdmCB(sec="fiat", act="add").pack(),
            ),
            InlineKeyboardButton(
                text="🔙 Back",
                callback_data=AdmCB(sec="pay", act="menu").pack(),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_fiat_method_detail_kb(method: FiatMethodDTO) -> InlineKeyboardMarkup:
    """Build CRUD actions for a single FiatMethodDTO."""
    toggle_label = "❌ Disable Method" if method.is_active else "✅ Enable Method"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_label,
                    callback_data=AdmCB(sec="fiat", act="toggle", id=method.id).pack(),
                ),
                InlineKeyboardButton(
                    text="✏️ Edit Method",
                    callback_data=AdmCB(sec="fiat", act="edit", id=method.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Delete Method",
                    callback_data=AdmCB(sec="fiat", act="del", id=method.id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Fiat Methods",
                    callback_data=AdmCB(sec="fiat", act="list").pack(),
                )
            ],
        ]
    )


def build_exchange_rate_kb(rate_mode: str = "manual") -> InlineKeyboardMarkup:
    """Build Exchange Rate configuration keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Set USD-to-Toman Rate",
                    callback_data=AdmCB(sec="pay", act="set_rate").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{'✅ ' if rate_mode == 'manual' else ''}Manual Mode",
                    callback_data=AdmCB(sec="pay", act="mode_manual").pack(),
                ),
                InlineKeyboardButton(
                    text=f"{'✅ ' if rate_mode == 'live' else ''}Live API Mode",
                    callback_data=AdmCB(sec="pay", act="mode_live").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Payments",
                    callback_data=AdmCB(sec="pay", act="menu").pack(),
                )
            ],
        ]
    )


def build_support_menu_kb() -> InlineKeyboardMarkup:
    """Build the Support Tickets filter menu keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟢 Open Tickets",
                    callback_data=AdmCB(sec="sup", act="open", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="💬 Answered",
                    callback_data=AdmCB(sec="sup", act="answered", page=0).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Closed Tickets",
                    callback_data=AdmCB(sec="sup", act="closed", page=0).pack(),
                ),
                InlineKeyboardButton(
                    text="📋 All Tickets",
                    callback_data=AdmCB(sec="sup", act="all", page=0).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Admin",
                    callback_data=AdmCB(sec="home", act="view").pack(),
                )
            ],
        ]
    )


def build_tickets_list_kb(tickets: Sequence[SupportTicketDTO]) -> InlineKeyboardMarkup:
    """Build inline list of support tickets."""
    rows: list[list[InlineKeyboardButton]] = []
    for t in tickets:
        icon = (
            "🟢"
            if t.status == TicketStatus.OPEN
            else ("💬" if t.status == TicketStatus.ANSWERED else "🔒")
        )
        label = f"{icon} {t.ticket_number} | User:{t.user_id} ({t.category.value})"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=AdmCB(sec="sup", act="view", id=t.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Support Menu",
                callback_data=AdmCB(sec="sup", act="menu").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_ticket_detail_kb(ticket: SupportTicketDTO) -> InlineKeyboardMarkup:
    """Build reply, close, and user inspection actions for a SupportTicket."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="✍️ Reply (Text / Photo / File)",
                callback_data=AdmCB(sec="sup", act="reply", id=ticket.id).pack(),
            )
        ]
    ]
    if ticket.status != TicketStatus.CLOSED:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔒 Close Ticket",
                    callback_data=AdmCB(sec="sup", act="close", id=ticket.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="👤 Inspect User",
                callback_data=AdmCB(sec="usr", act="view", id=ticket.user_id).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Tickets",
                callback_data=AdmCB(sec="sup", act="menu").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_cms_menu_kb() -> InlineKeyboardMarkup:
    """Build the CMS, UI Buttons & App Settings menu keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🌐 Edit 12-Language Texts",
                    callback_data=AdmCB(sec="cms", act="langs").pack(),
                ),
                InlineKeyboardButton(
                    text="✏️ Upsert Key Directly",
                    callback_data=AdmCB(sec="cms", act="upsert").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎛 Main Menu Buttons & Colors",
                    callback_data=AdmCB(sec="btn", act="list").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ App Settings (Maintenance / Pinned)",
                    callback_data=AdmCB(sec="set", act="list").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Admin",
                    callback_data=AdmCB(sec="home", act="view").pack(),
                )
            ],
        ]
    )


def build_cms_languages_kb() -> InlineKeyboardMarkup:
    """Build 2-column 12-language picker for CMS translation editing."""
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for code in LANGUAGE_ORDER:
        info = SUPPORTED_LANGUAGES[code]
        pair.append(
            InlineKeyboardButton(
                text=f"{info.flag} {info.native_name} ({code})",
                callback_data=AdmCB(sec="cms", act=f"lang_{code}", page=0).pack(),
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to CMS Menu",
                callback_data=AdmCB(sec="cms", act="menu").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_cms_translations_list_kb(
    translations: Sequence[UITranslationDTO],
    *,
    lang_code: str,
    page: int = 0,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    """Build paginated list of UITranslation entries for a language."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, tr in enumerate(translations):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔑 {tr.key}"[:60],
                    callback_data=AdmCB(sec="cms", act=f"ed_{lang_code}", id=idx, page=page).pack(),
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Prev",
                callback_data=AdmCB(sec="cms", act=f"lang_{lang_code}", page=page - 1).pack(),
            )
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(
                text="Next ➡️",
                callback_data=AdmCB(sec="cms", act=f"lang_{lang_code}", page=page + 1).pack(),
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Edit Any Key (key | lang | text)",
                callback_data=AdmCB(sec="cms", act="upsert").pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Languages",
                callback_data=AdmCB(sec="cms", act="langs").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_ui_buttons_list_kb(buttons: Sequence[UIButtonDTO]) -> InlineKeyboardMarkup:
    """Build list of UIButton rows for the Main Menu button customizer."""
    rows: list[list[InlineKeyboardButton]] = []
    for btn in buttons:
        active_mark = "✅" if btn.is_active else "❌"
        en_label = btn.labels_json.get("en") or btn.button_key
        label = f"{btn.color_emoji} {en_label} ({btn.button_key}) [{btn.color_hex}] {active_mark}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=AdmCB(sec="btn", act="view", id=btn.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to CMS Menu",
                callback_data=AdmCB(sec="cms", act="menu").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_ui_button_detail_kb(btn: UIButtonDTO) -> InlineKeyboardMarkup:
    """Build color, active status, and 12-language label editor keyboard for a UIButton."""
    toggle_label = "❌ Disable Button" if btn.is_active else "✅ Enable Button"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_label,
                    callback_data=AdmCB(sec="btn", act="toggle", id=btn.id).pack(),
                ),
                InlineKeyboardButton(
                    text=f"🎨 Color ({btn.color_emoji} {btn.color_hex})",
                    callback_data=AdmCB(sec="btn", act="color", id=btn.id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 Edit Button Labels (12 Langs)",
                    callback_data=AdmCB(sec="btn", act="label", id=btn.id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Buttons",
                    callback_data=AdmCB(sec="btn", act="list").pack(),
                )
            ],
        ]
    )


def build_settings_kb(*, maintenance_on: bool = False, force_join_on: bool = False) -> InlineKeyboardMarkup:
    """Build AppSettings toggle keyboard (Maintenance Mode, Force Join, Pinned Limit & Guide URL)."""
    maint_text = "🟢 Disable Maintenance Mode" if maintenance_on else "🛠 Enable Maintenance Mode"
    fjoin_text = "🔴 Disable Force Channel Join" if force_join_on else "📢 Enable Force Channel Join"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=maint_text,
                    callback_data=AdmCB(sec="set", act="tog_maint").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=fjoin_text,
                    callback_data=AdmCB(sec="set", act="tog_fjoin").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📣 Set Force-Join Channel(s)",
                    callback_data=AdmCB(sec="set", act="fjoin_ch").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📌 Max Pinned: 2",
                    callback_data=AdmCB(sec="set", act="pin_lim", id=2).pack(),
                ),
                InlineKeyboardButton(
                    text="📌 Max Pinned: 4",
                    callback_data=AdmCB(sec="set", act="pin_lim", id=4).pack(),
                ),
                InlineKeyboardButton(
                    text="📌 Max Pinned: 6",
                    callback_data=AdmCB(sec="set", act="pin_lim", id=6).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚠️ Low Stock: 3",
                    callback_data=AdmCB(sec="set", act="low_stk", id=3).pack(),
                ),
                InlineKeyboardButton(
                    text="⚠️ Low Stock: 5",
                    callback_data=AdmCB(sec="set", act="low_stk", id=5).pack(),
                ),
                InlineKeyboardButton(
                    text="⚠️ Low Stock: 10",
                    callback_data=AdmCB(sec="set", act="low_stk", id=10).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📖 Set Guide URL (Manual Prompts)",
                    callback_data=AdmCB(sec="set", act="guide_url").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to CMS Menu",
                    callback_data=AdmCB(sec="cms", act="menu").pack(),
                )
            ],
        ]
    )


def build_broadcast_menu_kb() -> InlineKeyboardMarkup:
    """Build the Messaging & Mass Broadcast menu keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 New Mass Broadcast Wizard",
                    callback_data=AdmCB(sec="bc", act="new").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="✉️ Single-User Direct Message",
                    callback_data=AdmCB(sec="bc", act="dm").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📜 Broadcast Jobs History",
                    callback_data=AdmCB(sec="bc", act="list", page=0).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back to Admin",
                    callback_data=AdmCB(sec="home", act="view").pack(),
                )
            ],
        ]
    )


def build_broadcast_audience_kb() -> InlineKeyboardMarkup:
    """Build audience segment selector for Mass Broadcast."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🌍 All Active Users",
                callback_data=AdmCB(sec="bc", act="aud_all").pack(),
            ),
            InlineKeyboardButton(
                text="💰 Users with Balance > 0",
                callback_data=AdmCB(sec="bc", act="aud_bal").pack(),
            ),
        ]
    ]
    pair: list[InlineKeyboardButton] = []
    for code in LANGUAGE_ORDER:
        info = SUPPORTED_LANGUAGES[code]
        pair.append(
            InlineKeyboardButton(
                text=f"{info.flag} {code.upper()} ({info.native_name})",
                callback_data=AdmCB(sec="bc", act=f"aud_{code}").pack(),
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Broadcast Menu",
                callback_data=AdmCB(sec="bc", act="menu").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_broadcast_confirm_kb() -> InlineKeyboardMarkup:
    """Build confirmation keyboard before launching a mass broadcast."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Launch Broadcast Now",
                    callback_data=AdmCB(sec="bc", act="confirm").pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data=AdmCB(sec="bc", act="menu").pack(),
                ),
            ]
        ]
    )


def build_broadcast_jobs_kb(jobs: Sequence[BroadcastJobDTO]) -> InlineKeyboardMarkup:
    """Build list of recent BroadcastJobs."""
    rows: list[list[InlineKeyboardButton]] = []
    for j in jobs:
        label = f"#{j.id} [{j.status.value}] {j.target_filter} ({j.sent_count}/{j.total_targets})"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=AdmCB(sec="bc", act="job", id=j.id).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Back to Broadcast Menu",
                callback_data=AdmCB(sec="bc", act="menu").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


__all__ = [
    "build_admin_back_kb",
    "build_admin_cancel_kb",
    "build_admin_home_kb",
    "build_broadcast_audience_kb",
    "build_broadcast_confirm_kb",
    "build_broadcast_jobs_kb",
    "build_broadcast_menu_kb",
    "build_catalog_menu_kb",
    "build_categories_list_kb",
    "build_category_detail_kb",
    "build_cms_languages_kb",
    "build_cms_menu_kb",
    "build_cms_translations_list_kb",
    "build_crypto_wallet_detail_kb",
    "build_crypto_wallets_list_kb",
    "build_dashboard_kb",
    "build_dual_color_picker_kb",
    "build_exchange_rate_kb",
    "build_fiat_method_detail_kb",
    "build_fiat_methods_list_kb",
    "build_order_detail_kb",
    "build_orders_list_kb",
    "build_payment_detail_kb",
    "build_payments_menu_kb",
    "build_payments_queue_kb",
    "build_product_detail_kb",
    "build_products_list_kb",
    "build_settings_kb",
    "build_support_menu_kb",
    "build_ticket_detail_kb",
    "build_tickets_list_kb",
    "build_ui_button_detail_kb",
    "build_ui_buttons_list_kb",
    "build_user_inspection_kb",
    "build_user_list_kb",
    "build_users_menu_kb",
]
