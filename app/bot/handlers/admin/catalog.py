"""Admin Catalog, Stock, Bulk Tiers, Dual-Layer Color Manager & Order Fulfillment handlers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.common import (
    fmt_dt,
    get_optional_service,
    get_service,
    notify_user_dm,
    parse_positive_decimal,
    render_i18n_template,
    resolve_admin_actor_id,
    safe_answer_callback,
    safe_edit_or_send,
)
from app.bot.keyboards.admin_kb import (
    build_admin_back_kb,
    build_admin_cancel_kb,
    build_catalog_menu_kb,
    build_categories_list_kb,
    build_category_detail_kb,
    build_dual_color_picker_kb,
    build_order_detail_kb,
    build_orders_list_kb,
    build_product_detail_kb,
    build_products_list_kb,
)
from app.bot.keyboards.common_kb import AdmCB
from app.bot.states import AdminStates
from app.core.constants import (
    COLOR_PALETTE,
    DeliveryType,
    OrderStatus,
    is_valid_hex_color,
)
from app.core.exceptions import ShopBotError
from app.core.logging import get_logger
from app.core.security import escape_html, validate_stock_upload_file
from app.services.dtos import (
    CategoryCreateDTO,
    CategoryUpdateDTO,
    ProductBulkTierDTO,
    ProductCreateDTO,
    ProductUpdateDTO,
)

logger = get_logger(__name__)
router = Router(name="admin_catalog")


async def _render_category_card(target: CallbackQuery | Message, cat_id: int, data: dict[str, Any]) -> None:
    try:
        shop_service = get_service(data, "shop_service")
        cats = await shop_service.list_categories(active_only=False)
        cat = next((c for c in cats if c.id == cat_id), None)
        if cat is None:
            await safe_edit_or_send(target, "❌ Category not found.", reply_markup=build_admin_back_kb(sec="cat", act="list"))
            return
    except ShopBotError as exc:
        await safe_edit_or_send(target, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="cat", act="menu"))
        return

    en_name = escape_html(cat.names_json.get("en") or cat.slug)
    fa_name = escape_html(cat.names_json.get("fa") or "-")
    text = (
        f"📂 <b>Category #{cat.id}: {cat.color_emoji} {en_name}</b>\n\n"
        f"🔗 Slug: <code>{escape_html(cat.slug)}</code>\n"
        f"🇮🇷 Persian Name: <b>{fa_name}</b>\n"
        f"🎨 Color: {cat.color_emoji} <code>{cat.color_hex}</code>\n"
        f"🔢 Sort Order: <b>{cat.sort_order}</b> | Active: <b>{'Yes' if cat.is_active else 'No'}</b>"
    )
    await safe_edit_or_send(target, text, reply_markup=build_category_detail_kb(cat))


async def _render_product_card(target: CallbackQuery | Message, prod_id: int, data: dict[str, Any]) -> None:
    try:
        shop_service = get_service(data, "shop_service")
        prod = await shop_service.get_product_detail(prod_id)
    except ShopBotError as exc:
        await safe_edit_or_send(target, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="prd", act="list"))
        return

    en_title = escape_html(prod.titles_json.get("en") or prod.slug)
    fa_title = escape_html(prod.titles_json.get("fa") or "-")
    price_irt_txt = f"{prod.price_irt:,.0f} IRT" if prod.price_irt is not None else "Not set"
    stock_txt = "Unlimited (-1)" if prod.manual_stock_count == -1 else str(prod.available_stock)
    tiers_txt = (
        ", ".join(f"{t.min_qty}+ → ${t.unit_price_usd:.2f}" for t in prod.bulk_tiers)
        if prod.bulk_tiers
        else "None"
    )
    text = (
        f"📦 <b>Product #{prod.id}: {prod.color_emoji} {en_title}</b>\n\n"
        f"🔗 Slug: <code>{escape_html(prod.slug)}</code> | Cat ID: <code>{prod.category_id}</code>\n"
        f"🇮🇷 Persian Title: <b>{fa_title}</b>\n"
        f"🚚 Delivery: <code>{prod.delivery_type.value}</code>\n"
        f"💵 Price USD: <b>${prod.price_usd:,.2f}</b> | IRT: <b>{price_irt_txt}</b>\n"
        f"🏷 Bulk Tiers: {escape_html(tiers_txt)}\n"
        f"🎨 Dual Color: {prod.color_emoji} <code>{prod.color_hex}</code>\n"
        f"📦 Stock: <b>{stock_txt}</b> (Low Thr: {prod.low_stock_threshold}) | 🔥 Sold: <b>{prod.sold_count}</b>\n"
        f"📌 Pinned: <b>{'Yes' if prod.is_pinned else 'No'}</b> | Active: <b>{'Yes' if prod.is_active else 'No'}</b>"
    )
    await safe_edit_or_send(target, text, reply_markup=build_product_detail_kb(prod))


async def _render_order_card(target: CallbackQuery | Message, order_id: int, data: dict[str, Any]) -> None:
    try:
        shop_service = get_service(data, "shop_service")
        order = await shop_service.get_order_detail(order_id, decrypt_items=False)
    except ShopBotError as exc:
        await safe_edit_or_send(target, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="ord", act="list"))
        return

    text = (
        f"📋 <b>Order {escape_html(order.order_number)}</b> (#{order.id})\n\n"
        f"👤 Customer ID: <code>{order.user_id}</code> | Product ID: <code>{order.product_id}</code>\n"
        f"📦 Qty: <b>{order.quantity}</b> | Total: <b>{order.total_amount:,.2f} {order.currency.value}</b>\n"
        f"🚦 Status: <b>{order.status.value}</b>\n"
        f"📝 User Fulfillment Input:\n<code>{escape_html(order.fulfillment_input or '-')}</code>\n"
        f"🛡 Admin Note: {escape_html(order.fulfillment_note or '-')}\n"
        f"🕒 Created: <code>{fmt_dt(order.created_at)}</code>"
    )
    await safe_edit_or_send(target, text, reply_markup=build_order_detail_kb(order))


@router.callback_query(AdmCB.filter((F.sec == "cat") & (F.act == "menu")))
async def cb_catalog_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_or_send(
        callback,
        "📦 <b>Catalog, Stock &amp; Dual-Layer Color Manager</b>\n\nSelect a management section:",
        reply_markup=build_catalog_menu_kb(),
    )


@router.callback_query(AdmCB.filter((F.sec == "cat") & (F.act == "list")))
async def cb_categories_list(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    try:
        shop_service = get_service(data, "shop_service")
        cats = await shop_service.list_categories(active_only=False)
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="cat", act="menu"))
        return
    await safe_edit_or_send(
        callback,
        "📂 <b>Product Categories</b> (click to manage):",
        reply_markup=build_categories_list_kb(cats),
    )


@router.callback_query(AdmCB.filter((F.sec == "cat") & (F.act == "view")))
async def cb_category_view(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await _render_category_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "cat") & (F.act == "toggle")))
async def cb_category_toggle(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        shop_service = get_service(data, "shop_service")
        cats = await shop_service.list_categories(active_only=False)
        cat = next((c for c in cats if c.id == callback_data.id), None)
        if cat is None:
            await safe_answer_callback(callback, "Category not found", show_alert=True)
            return
        await shop_service.update_category(cat.id, CategoryUpdateDTO(is_active=not cat.is_active))
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_category_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "cat") & (F.act == "del")))
async def cb_category_delete(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        shop_service = get_service(data, "shop_service")
        await shop_service.delete_category(callback_data.id)
        cats = await shop_service.list_categories(active_only=False)
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await safe_answer_callback(callback, "Category deleted.")
    await safe_edit_or_send(callback, "📂 <b>Product Categories</b>", reply_markup=build_categories_list_kb(cats))


@router.callback_query(AdmCB.filter((F.sec == "cat") & (F.act.in_({"add", "edit"}))))
async def cb_category_input_prompt(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext) -> None:
    is_edit = callback_data.act == "edit"
    await state.set_state(AdminStates.waiting_category_input)
    await state.update_data(cat_mode="edit" if is_edit else "add", cat_id=callback_data.id)
    hint = (
        "Send: <code>English Name | Persian Name | sort_order</code>"
        if is_edit
        else "Send: <code>slug | English Name | Persian Name | sort_order</code>\nExample: <code>vpn-subs | 🔒 VPN Accounts | اشتراک VPN | 2</code>"
    )
    await safe_edit_or_send(
        callback,
        f"📂 <b>{'Edit' if is_edit else 'Create'} Category</b>\n\n{hint}",
        reply_markup=build_admin_cancel_kb(sec="cat", act="list"),
    )


@router.callback_query(AdmCB.filter((F.sec == "cat") & (F.act == "color")))
async def cb_category_color_picker(callback: CallbackQuery, callback_data: AdmCB) -> None:
    await safe_edit_or_send(
        callback,
        f"🎨 <b>Category #{callback_data.id} Dual-Layer Color Picker</b>\n\n"
        f"Pick a preset Emoji Badge + Hex color, or enter a custom <code>#RRGGBB</code> Hex:",
        reply_markup=build_dual_color_picker_kb("cat", callback_data.id),
    )


@router.callback_query(AdmCB.filter((F.sec == "cat") & (F.act.startswith("col_"))))
async def cb_category_color_preset(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    key = callback_data.act.removeprefix("col_")
    preset = COLOR_PALETTE.get(key)
    if preset is None:
        await safe_answer_callback(callback, "Unknown color preset", show_alert=True)
        return
    try:
        shop_service = get_service(data, "shop_service")
        await shop_service.update_category(
            callback_data.id,
            CategoryUpdateDTO(color_emoji=preset.emoji, color_hex=preset.hex_code),
        )
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_category_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "cat") & (F.act == "hex")))
async def cb_category_hex_prompt(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_category_input)
    await state.update_data(cat_mode="hex", cat_id=callback_data.id)
    await safe_edit_or_send(
        callback,
        "🎨 Send custom Hex color (e.g. <code>#22c55e</code>) or <code>#RRGGBB | 🟢</code>:",
        reply_markup=build_admin_cancel_kb(sec="cat", act="view", target_id=callback_data.id),
    )


@router.message(AdminStates.waiting_category_input)
async def msg_category_input(message: Message, state: FSMContext, **data: Any) -> None:
    fsm_data = await state.get_data()
    mode = str(fsm_data.get("cat_mode", "add"))
    cat_id = int(fsm_data.get("cat_id", 0))
    raw = (message.text or "").strip()
    parts = [p.strip() for p in raw.split("|")]

    try:
        shop_service = get_service(data, "shop_service")
        if mode == "hex":
            hex_val = parts[0]
            if not is_valid_hex_color(hex_val):
                await safe_edit_or_send(
                    message,
                    "❌ Invalid Hex color. Must match <code>#RRGGBB</code> (e.g. <code>#3b82f6</code>).",
                    reply_markup=build_admin_cancel_kb(sec="cat", act="view", target_id=cat_id),
                )
                return
            emoji_val = parts[1] if len(parts) > 1 and parts[1] else None
            await shop_service.update_category(
                cat_id,
                CategoryUpdateDTO(color_hex=hex_val.lower(), color_emoji=emoji_val),
            )
            await state.clear()
            await _render_category_card(message, cat_id, data)
            return

        if mode == "edit":
            if not parts or not parts[0]:
                raise ValueError("English name is required")
            names = {"en": parts[0]}
            if len(parts) > 1 and parts[1]:
                names["fa"] = parts[1]
            sort_order = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else None
            await shop_service.update_category(
                cat_id,
                CategoryUpdateDTO(names_json=names, sort_order=sort_order),
            )
            await state.clear()
            await _render_category_card(message, cat_id, data)
            return

        if len(parts) < 2 or not parts[0] or not parts[1]:
            await safe_edit_or_send(
                message,
                "❌ Format: <code>slug | English Name | Persian Name | sort_order</code>",
                reply_markup=build_admin_cancel_kb(sec="cat", act="list"),
            )
            return
        names_json = {"en": parts[1], "fa": parts[2] if len(parts) > 2 and parts[2] else parts[1]}
        sort_order = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else 0
        created = await shop_service.create_category(
            CategoryCreateDTO(slug=parts[0].lower(), names_json=names_json, sort_order=sort_order)
        )
        await state.clear()
        await _render_category_card(message, created.id, data)
    except (ShopBotError, ValueError) as exc:
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(str(exc))}",
            reply_markup=build_admin_cancel_kb(sec="cat", act="list"),
        )


@router.callback_query(AdmCB.filter((F.sec == "prd") & (F.act.in_({"list", "by_cat"}))))
async def cb_products_list(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    cat_id = callback_data.id if callback_data.act == "by_cat" else None
    try:
        shop_service = get_service(data, "shop_service")
        prods = await shop_service.list_products(category_id=cat_id, active_only=False)
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="cat", act="menu"))
        return
    await safe_edit_or_send(
        callback,
        f"📦 <b>Products</b> ({len(prods)}):",
        reply_markup=build_products_list_kb(prods, category_id=cat_id or 0),
    )


@router.callback_query(AdmCB.filter((F.sec == "prd") & (F.act == "view")))
async def cb_product_view(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await _render_product_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "prd") & (F.act.in_({"pin", "toggle"}))))
async def cb_product_toggle_flag(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        shop_service = get_service(data, "shop_service")
        prod = await shop_service.get_product_detail(callback_data.id)
        if callback_data.act == "pin":
            await shop_service.update_product(prod.id, ProductUpdateDTO(is_pinned=not prod.is_pinned))
        else:
            await shop_service.update_product(prod.id, ProductUpdateDTO(is_active=not prod.is_active))
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_product_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "prd") & (F.act == "del")))
async def cb_product_delete(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        shop_service = get_service(data, "shop_service")
        await shop_service.delete_product(callback_data.id)
        prods = await shop_service.list_products(active_only=False)
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await safe_answer_callback(callback, "Product deleted.")
    await safe_edit_or_send(callback, "📦 <b>Products</b>", reply_markup=build_products_list_kb(prods))


@router.callback_query(AdmCB.filter((F.sec == "prd") & (F.act == "color")))
async def cb_product_color_picker(callback: CallbackQuery, callback_data: AdmCB) -> None:
    await safe_edit_or_send(
        callback,
        f"🎨 <b>Product #{callback_data.id} Dual-Layer Color Picker</b>\n\n"
        f"Select an Emoji Indicator + CSS Hex color preset, or enter a custom <code>#RRGGBB</code>:",
        reply_markup=build_dual_color_picker_kb("prd", callback_data.id),
    )


@router.callback_query(AdmCB.filter((F.sec == "prd") & (F.act.startswith("col_"))))
async def cb_product_color_preset(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    key = callback_data.act.removeprefix("col_")
    preset = COLOR_PALETTE.get(key)
    if preset is None:
        await safe_answer_callback(callback, "Unknown color preset", show_alert=True)
        return
    try:
        shop_service = get_service(data, "shop_service")
        await shop_service.update_product(
            callback_data.id,
            ProductUpdateDTO(color_emoji=preset.emoji, color_hex=preset.hex_code),
        )
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_product_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "prd") & (F.act == "hex")))
async def cb_product_hex_prompt(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_product_input)
    await state.update_data(prd_mode="hex", prd_id=callback_data.id)
    await safe_edit_or_send(
        callback,
        "🎨 Send custom Hex color (e.g. <code>#22c55e</code>) or <code>#RRGGBB | 🟢</code>:",
        reply_markup=build_admin_cancel_kb(sec="prd", act="view", target_id=callback_data.id),
    )


@router.callback_query(AdmCB.filter((F.sec == "prd") & (F.act.in_({"add", "edit", "tiers"}))))
async def cb_product_input_prompt(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext) -> None:
    mode = callback_data.act
    await state.set_state(AdminStates.waiting_product_input)
    await state.update_data(prd_mode=mode, prd_id=callback_data.id)
    if mode == "tiers":
        prompt = (
            "🏷 <b>Set Bulk Discount Tiers</b>\n\n"
            "Send one tier per line as <code>min_qty:unit_price_usd[:unit_price_irt]</code>\n"
            "Example:\n<code>10:8.50:550000\n30:7.50:490000</code>\n"
            "Send <code>clear</code> to remove all bulk tiers."
        )
        cancel_act = "view"
    elif mode == "edit":
        prompt = (
            "✏️ <b>Edit Product</b>\n\n"
            "Send <code>price_usd | price_irt | English Title | Persian Title | Description</code>\n"
            "(Use <code>-</code> for fields you want to keep unchanged)"
        )
        cancel_act = "view"
    else:
        prompt = (
            "➕ <b>Create Product</b>\n\n"
            "Send: <code>category_id | slug | English Title | price_usd | delivery_type(auto/manual) | price_irt(optional)</code>\n"
            "Example: <code>1 | chatgpt-plus | ChatGPT Plus 1M | 19.99 | auto | 1300000</code>"
        )
        cancel_act = "list"
    await safe_edit_or_send(
        callback,
        prompt,
        reply_markup=build_admin_cancel_kb(sec="prd", act=cancel_act, target_id=callback_data.id),
    )


@router.message(AdminStates.waiting_product_input)
async def msg_product_input(message: Message, state: FSMContext, **data: Any) -> None:
    fsm_data = await state.get_data()
    mode = str(fsm_data.get("prd_mode", "add"))
    prd_id = int(fsm_data.get("prd_id", 0))
    raw = (message.text or "").strip()

    try:
        shop_service = get_service(data, "shop_service")
        if mode == "hex":
            parts = [p.strip() for p in raw.split("|")]
            if not is_valid_hex_color(parts[0]):
                await safe_edit_or_send(
                    message,
                    "❌ Invalid Hex format. Expected <code>#RRGGBB</code>.",
                    reply_markup=build_admin_cancel_kb(sec="prd", act="view", target_id=prd_id),
                )
                return
            emoji_val = parts[1] if len(parts) > 1 and parts[1] else None
            await shop_service.update_product(prd_id, ProductUpdateDTO(color_hex=parts[0].lower(), color_emoji=emoji_val))
            await state.clear()
            await _render_product_card(message, prd_id, data)
            return

        if mode == "tiers":
            tiers: list[ProductBulkTierDTO] = []
            if raw.lower() != "clear":
                for line in raw.splitlines():
                    cleaned = line.strip()
                    if not cleaned:
                        continue
                    cols = [c.strip() for c in cleaned.split(":")]
                    if len(cols) < 2:
                        raise ValueError(f"Invalid tier line '{cleaned}'")
                    min_qty = int(cols[0])
                    usd = Decimal(cols[1])
                    irt = Decimal(cols[2]) if len(cols) > 2 and cols[2] else None
                    tiers.append(ProductBulkTierDTO(min_qty=min_qty, unit_price_usd=usd, unit_price_irt=irt))
            await shop_service.set_bulk_tiers(prd_id, tiers)
            await state.clear()
            await _render_product_card(message, prd_id, data)
            return

        parts = [p.strip() for p in raw.split("|")]
        if mode == "edit":
            existing = await shop_service.get_product_detail(prd_id)
            upd = ProductUpdateDTO()
            if len(parts) > 0 and parts[0] not in {"", "-"}:
                p_usd = parse_positive_decimal(parts[0])
                if p_usd is None:
                    raise ValueError("Invalid price_usd")
                upd.price_usd = p_usd
            if len(parts) > 1 and parts[1] not in {"", "-"}:
                p_irt = parse_positive_decimal(parts[1])
                if p_irt is None:
                    raise ValueError("Invalid price_irt")
                upd.price_irt = p_irt
            titles = dict(existing.titles_json)
            if len(parts) > 2 and parts[2] not in {"", "-"}:
                titles["en"] = parts[2]
                upd.titles_json = titles
            if len(parts) > 3 and parts[3] not in {"", "-"}:
                titles["fa"] = parts[3]
                upd.titles_json = titles
            if len(parts) > 4 and parts[4] not in {"", "-"}:
                descs = dict(existing.descriptions_json)
                descs["en"] = parts[4]
                upd.descriptions_json = descs
            await shop_service.update_product(prd_id, upd)
            await state.clear()
            await _render_product_card(message, prd_id, data)
            return

        if len(parts) < 4:
            raise ValueError("Expected: category_id | slug | English Title | price_usd | delivery_type | price_irt")
        cat_id = int(parts[0]) if parts[0].isdigit() else (prd_id if prd_id > 0 else 1)
        slug = parts[1].lower()
        title_en = parts[2]
        price_usd = parse_positive_decimal(parts[3])
        if price_usd is None:
            raise ValueError("price_usd must be a positive number")
        dtype_raw = parts[4].lower() if len(parts) > 4 and parts[4] else "auto"
        dtype = DeliveryType(dtype_raw)
        price_irt = parse_positive_decimal(parts[5]) if len(parts) > 5 and parts[5] else None
        created = await shop_service.create_product(
            ProductCreateDTO(
                category_id=cat_id,
                slug=slug,
                delivery_type=dtype,
                price_usd=price_usd,
                price_irt=price_irt,
                titles_json={"en": title_en, "fa": title_en},
                manual_stock_count=-1 if dtype == DeliveryType.MANUAL else 0,
            )
        )
        await state.clear()
        await _render_product_card(message, created.id, data)
    except (ShopBotError, ValueError, InvalidOperation) as exc:
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(str(exc))}",
            reply_markup=build_admin_cancel_kb(sec="prd", act="list"),
        )


@router.callback_query(AdmCB.filter((F.sec == "prd") & (F.act == "stock")))
async def cb_product_stock_prompt(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_stock_upload)
    await state.update_data(stock_prd_id=callback_data.id)
    await safe_edit_or_send(
        callback,
        f"📥 <b>Upload Stock for Product #{callback_data.id}</b>\n\n"
        f"Paste stock items (one key/account per line) OR upload a UTF-8 <code>.txt</code> / <code>.csv</code> file.\n"
        f"For manual products, send <code>MANUAL:count</code> (e.g. <code>MANUAL:-1</code> for unlimited).",
        reply_markup=build_admin_cancel_kb(sec="prd", act="view", target_id=callback_data.id),
    )


@router.message(AdminStates.waiting_stock_upload)
async def msg_product_stock_upload(
    message: Message,
    state: FSMContext,
    bot: Bot | None = None,
    **data: Any,
) -> None:
    fsm_data = await state.get_data()
    prd_id = int(fsm_data.get("stock_prd_id", 0))

    try:
        shop_service = get_service(data, "shop_service")
        if message.document is not None:
            active_bot = bot or message.bot
            if active_bot is None:
                raise ValueError("Bot instance required to download stock file")
            buf = BytesIO()
            await active_bot.download(message.document, destination=buf)
            lines = validate_stock_upload_file(buf.getvalue(), filename=message.document.file_name)
        else:
            raw = (message.text or "").strip()
            if raw.upper().startswith("MANUAL:"):
                count = int(raw.split(":", 1)[1].strip())
                await shop_service.update_product(prd_id, ProductUpdateDTO(manual_stock_count=count))
                await state.clear()
                await _render_product_card(message, prd_id, data)
                return
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if not lines:
                raise ValueError("Please paste at least one stock item or upload a .txt/.csv file.")

        res = await shop_service.add_auto_stock(prd_id, lines, deduplicate=True)
        await state.clear()
        await safe_edit_or_send(
            message,
            f"✅ <b>Stock Import Complete (Product #{prd_id})</b>\n\n"
            f"➕ Added: <b>{res.added_count}</b>\n"
            f"⏭ Duplicates skipped: <b>{res.skipped_duplicates}</b>\n"
            f"📦 Total available: <b>{res.total_available}</b>",
            reply_markup=build_admin_back_kb(sec="prd", act="view", target_id=prd_id, label="🔙 Back to Product"),
        )
    except (ShopBotError, ValueError) as exc:
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(str(exc))}",
            reply_markup=build_admin_cancel_kb(sec="prd", act="view", target_id=prd_id),
        )


@router.callback_query(AdmCB.filter((F.sec == "ord") & (F.act.in_({"list", "pending"}))))
async def cb_orders_list(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    status_filter = OrderStatus.PENDING_FULFILLMENT if callback_data.act == "pending" else None
    try:
        shop_service = get_service(data, "shop_service")
        orders = await shop_service.list_orders(status=status_filter, limit=25)
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="cat", act="menu"))
        return
    title = "⏳ <b>Pending Manual Orders</b>" if status_filter else "📋 <b>Recent Orders</b>"
    await safe_edit_or_send(callback, title, reply_markup=build_orders_list_kb(orders, filter_act=callback_data.act))


@router.callback_query(AdmCB.filter((F.sec == "ord") & (F.act == "view")))
async def cb_order_view(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await _render_order_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "ord") & (F.act == "fulfill")))
async def cb_order_fulfill_prompt(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_fulfill_note)
    await state.update_data(fulfill_order_id=callback_data.id)
    await safe_edit_or_send(
        callback,
        f"✅ <b>Fulfill Order #{callback_data.id}</b>\n\n"
        f"Enter fulfillment note / credentials to send to the customer (or <code>-</code>):",
        reply_markup=build_admin_cancel_kb(sec="ord", act="view", target_id=callback_data.id),
    )


@router.message(AdminStates.waiting_fulfill_note)
async def msg_order_fulfill_commit(
    message: Message,
    state: FSMContext,
    bot: Bot | None = None,
    **data: Any,
) -> None:
    fsm_data = await state.get_data()
    order_id = int(fsm_data.get("fulfill_order_id", 0))
    raw_note = (message.text or "").strip()
    note = None if raw_note in {"", "-"} else raw_note

    try:
        shop_service = get_service(data, "shop_service")
        order = await shop_service.fulfill_manual_order(
            order_id,
            resolve_admin_actor_id(message),
            fulfillment_note=note,
        )
    except ShopBotError as exc:
        await state.clear()
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(sec="ord", act="view", target_id=order_id),
        )
        return

    await state.clear()
    active_bot = bot or message.bot
    user_service = get_optional_service(data, "user_service")
    if active_bot is not None and user_service is not None:
        try:
            user = await user_service.get_by_id(order.user_id)
            notify_text = await render_i18n_template(
                data,
                "shop.order_fulfilled_notify",
                user.language_code,
                order_number=escape_html(order.order_number),
                note=escape_html(note or "-"),
            )
            await notify_user_dm(active_bot, user.telegram_id, notify_text)
        except ShopBotError as exc:
            logger.warning("Failed to notify user on order fulfill: %s", exc)

    await _render_order_card(message, order.id, data)


@router.callback_query(AdmCB.filter((F.sec == "ord") & (F.act == "refund")))
async def cb_order_refund(
    callback: CallbackQuery,
    callback_data: AdmCB,
    bot: Bot | None = None,
    **data: Any,
) -> None:
    try:
        shop_service = get_service(data, "shop_service")
        order = await shop_service.cancel_and_refund_order(
            callback_data.id,
            resolve_admin_actor_id(callback),
            reason="Admin cancelled and refunded",
        )
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return

    active_bot = bot or callback.bot
    user_service = get_optional_service(data, "user_service")
    if active_bot is not None and user_service is not None:
        try:
            user = await user_service.get_by_id(order.user_id)
            await notify_user_dm(
                active_bot,
                user.telegram_id,
                f"💸 Order <code>{escape_html(order.order_number)}</code> has been cancelled and "
                f"<b>{order.total_amount:,.2f} {order.currency.value}</b> refunded to your balance.",
            )
        except ShopBotError as exc:
            logger.warning("Failed to notify user on order refund: %s", exc)

    await safe_answer_callback(callback, "Order refunded.")
    await _render_order_card(callback, order.id, data)
