"""Inline keyboard builders and formatting helpers for the Shop SPA flow."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.constants import Currency, DEFAULT_LANGUAGE, DeliveryType
from app.bot.keyboards.common_kb import (
    AdmCB,
    CatCB,
    MenuCB,
    OrderCB,
    ProdCB,
    QtyCB,
    build_back_button,
)
from app.services.dtos import CategoryDTO, OrderDTO, ProductDTO, ProductDetailDTO


def format_usd(amount: Decimal) -> str:
    """Format a USD Decimal cleanly with at least 2 decimal places and no float conversion."""
    quantized = Decimal(amount).quantize(Decimal("0.00000001"))
    text = format(quantized.normalize(), "f")
    if "." in text:
        whole, frac = text.split(".", 1)
        frac = frac.rstrip("0")
        if len(frac) < 2:
            frac = frac.ljust(2, "0")
        return f"${whole}.{frac}"
    return f"${text}.00"


def format_irt(amount: Decimal) -> str:
    """Format an IRT/Toman Decimal with thousand separators."""
    quantized = Decimal(amount).quantize(Decimal("0.01"))
    if quantized == quantized.to_integral_value():
        return f"{int(quantized):,}"
    return f"{quantized:,.2f}"


def format_currency_amount(amount: Decimal, currency: Currency, *, lang_code: str = "en") -> str:
    """Format monetary amount in USD or IRT for bot display."""
    if currency == Currency.USD:
        return format_usd(amount)
    unit = "تومان" if lang_code == "fa" else "IRT"
    return f"{format_irt(amount)} {unit}"


def _with_emoji_badge(text: str, emoji: str | None) -> str:
    badge = (emoji or "").strip()
    if not badge or text.startswith(badge):
        return text
    return f"{badge} {text}"


def build_categories_kb(
    categories: Sequence[CategoryDTO],
    *,
    lang_code: str = DEFAULT_LANGUAGE,
    order_history_text: str = "📜 Order History",
    back_text: str = "🔙 Back",
) -> InlineKeyboardMarkup:
    """Build the Shop categories keyboard + Order History + Back button."""
    active_cats = [c for c in categories if c.is_active]
    active_cats.sort(key=lambda c: (c.sort_order, c.id))

    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for cat in active_cats:
        name = (
            cat.names_json.get(lang_code)
            or cat.names_json.get(DEFAULT_LANGUAGE)
            or cat.slug
        )
        btn_text = _with_emoji_badge(name, cat.color_emoji)
        pair.append(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=CatCB(id=cat.id, page=0).pack(),
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
                text=order_history_text,
                callback_data=OrderCB(id=0, action="list").pack(),
            )
        ]
    )
    rows.append([build_back_button(text=back_text, callback_data=MenuCB(action="main"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_products_kb(
    products: Sequence[ProductDTO],
    *,
    category_id: int,
    lang_code: str = DEFAULT_LANGUAGE,
    page: int = 0,
    per_page: int = 8,
    stock_template: str = "Stock: {count}",
    unlimited_text: str = "Unlimited",
    out_of_stock_text: str = "Out of Stock",
    back_text: str = "🔙 Back",
) -> InlineKeyboardMarkup:
    """Build paginated category product list with `[color_emoji] [name] - Stock: [count]` buttons."""
    active_products = [p for p in products if p.is_active]
    active_products.sort(key=lambda p: (p.sort_order, p.id))

    total_pages = max(1, (len(active_products) + per_page - 1) // per_page)
    safe_page = max(0, min(page, total_pages - 1))
    start = safe_page * per_page
    page_items = active_products[start : start + per_page]

    rows: list[list[InlineKeyboardButton]] = []
    for prod in page_items:
        title = (
            prod.titles_json.get(lang_code)
            or prod.titles_json.get(DEFAULT_LANGUAGE)
            or prod.slug
        )
        display_title = _with_emoji_badge(title, prod.color_emoji)
        if prod.delivery_type == DeliveryType.MANUAL and prod.manual_stock_count == -1:
            stock_val = unlimited_text
        elif prod.available_stock <= 0:
            stock_val = out_of_stock_text
        else:
            stock_val = str(prod.available_stock)
        stock_part = stock_template.format(count=stock_val)
        btn_text = f"{display_title} - {stock_part}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=ProdCB(id=prod.id, action="view").pack(),
                )
            ]
        )

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if safe_page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=CatCB(id=category_id, page=safe_page - 1).pack(),
                )
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"{safe_page + 1}/{total_pages}",
                callback_data=MenuCB(action="noop").pack(),
            )
        )
        if safe_page + 1 < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=CatCB(id=category_id, page=safe_page + 1).pack(),
                )
            )
        rows.append(nav_row)

    rows.append([build_back_button(text=back_text, callback_data=MenuCB(action="shop"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_product_detail_kb(
    product: ProductDetailDTO,
    *,
    lang_code: str = DEFAULT_LANGUAGE,
    selected_qty: int = 1,
    buy_usd_text: str = "💵 Buy with USD Balance",
    buy_irt_text: str = "💳 Buy with Toman Balance",
    custom_qty_text: str = "✏️ Custom",
    back_text: str = "🔙 Back",
    main_menu_text: str | None = None,
) -> InlineKeyboardMarkup:
    """Build product detail keyboard with quantity presets (`1, 5, 10, Custom`) and buy buttons."""
    presets = (1, 5, 10)
    qty_row: list[InlineKeyboardButton] = []
    for q in presets:
        label = f"✅ {q}" if selected_qty == q else str(q)
        qty_row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=QtyCB(prod_id=product.id, qty=q).pack(),
            )
        )
    custom_label = (
        f"✅ {selected_qty}" if selected_qty not in presets else custom_qty_text
    )
    qty_row.append(
        InlineKeyboardButton(
            text=custom_label,
            callback_data=ProdCB(id=product.id, action="custom_qty").pack(),
        )
    )

    rows: list[list[InlineKeyboardButton]] = [
        qty_row,
        [
            InlineKeyboardButton(
                text=buy_usd_text,
                callback_data=ProdCB(id=product.id, action="buy_usd").pack(),
            )
        ],
    ]

    if lang_code == "fa" and product.price_irt is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text=buy_irt_text,
                    callback_data=ProdCB(id=product.id, action="buy_irt").pack(),
                )
            ]
        )

    back_row = [
        build_back_button(
            text=back_text,
            callback_data=CatCB(id=product.category_id, page=0),
        )
    ]
    if main_menu_text:
        back_row.append(
            InlineKeyboardButton(
                text=main_menu_text,
                callback_data=MenuCB(action="main").pack(),
            )
        )
    rows.append(back_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_order_history_kb(
    orders: Sequence[OrderDTO],
    *,
    lang_code: str = DEFAULT_LANGUAGE,
    back_text: str = "🔙 Back",
    main_menu_text: str | None = None,
) -> InlineKeyboardMarkup:
    """Build user Order History inline list keyboard."""
    rows: list[list[InlineKeyboardButton]] = []
    for order in orders:
        amount_str = format_currency_amount(
            order.total_amount, order.currency, lang_code=lang_code
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🧾 {order.order_number} · {order.status.value} · {amount_str}",
                    callback_data=OrderCB(id=order.id, action="view").pack(),
                )
            ]
        )

    back_row = [build_back_button(text=back_text, callback_data=MenuCB(action="shop"))]
    if main_menu_text:
        back_row.append(
            InlineKeyboardButton(
                text=main_menu_text,
                callback_data=MenuCB(action="main").pack(),
            )
        )
    rows.append(back_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_order_detail_kb(
    *,
    back_text: str = "🔙 Back",
    main_menu_text: str = "🏠 Main Menu",
) -> InlineKeyboardMarkup:
    """Build single order detail navigation keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                build_back_button(text=back_text, callback_data=OrderCB(id=0, action="list")),
                InlineKeyboardButton(
                    text=main_menu_text,
                    callback_data=MenuCB(action="main").pack(),
                ),
            ]
        ]
    )


def build_post_purchase_kb(
    *,
    category_id: int,
    order_history_text: str = "📜 Order History",
    back_text: str = "🔙 Back",
    main_menu_text: str = "🏠 Main Menu",
) -> InlineKeyboardMarkup:
    """Build post-purchase navigation keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=order_history_text,
                    callback_data=OrderCB(id=0, action="list").pack(),
                )
            ],
            [
                build_back_button(text=back_text, callback_data=CatCB(id=category_id, page=0)),
                InlineKeyboardButton(
                    text=main_menu_text,
                    callback_data=MenuCB(action="main").pack(),
                ),
            ],
        ]
    )


def build_admin_order_notify_kb(order_id: int) -> InlineKeyboardMarkup:
    """Build inline admin notification buttons linked to bot-admin's order inspection flow."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👁 View Order",
                    callback_data=AdmCB(sec="ord", act="view", id=order_id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Fulfill",
                    callback_data=AdmCB(sec="ord", act="fulfill", id=order_id).pack(),
                ),
                InlineKeyboardButton(
                    text="↩️ Refund",
                    callback_data=AdmCB(sec="ord", act="refund", id=order_id).pack(),
                ),
            ],
        ]
    )


__all__ = [
    "build_admin_order_notify_kb",
    "build_categories_kb",
    "build_order_detail_kb",
    "build_order_history_kb",
    "build_post_purchase_kb",
    "build_product_detail_kb",
    "build_products_kb",
    "format_currency_amount",
    "format_irt",
    "format_usd",
]
