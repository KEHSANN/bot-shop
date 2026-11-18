"""Aiogram 3.x inline SPA handlers for Shop categories, products, bulk pricing, purchase, and order history."""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.keyboards.common_kb import (
    CatCB,
    MenuCB,
    OrderCB,
    ProdCB,
    QtyCB,
    build_back_kb,
    build_cancel_kb,
)
from app.bot.keyboards.shop_kb import (
    build_admin_order_notify_kb,
    build_categories_kb,
    build_order_detail_kb,
    build_order_history_kb,
    build_post_purchase_kb,
    build_product_detail_kb,
    build_products_kb,
    format_currency_amount,
    format_irt,
    format_usd,
)
from app.bot.states import ShopStates
from app.config import Settings, get_settings
from app.core.constants import Currency, DEFAULT_LANGUAGE, DeliveryType, OrderStatus
from app.core.exceptions import (
    InsufficientBalanceError,
    InsufficientStockError,
    InvalidStateTransitionError,
    NotFoundError,
    UserBlockedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import escape_html, sanitize_username
from app.locales.i18n import TranslationService
from app.services.dtos import OrderDTO, ProductDetailDTO, PurchaseRequestDTO, UserDTO
from app.services.interfaces import ShopServiceProtocol

logger = get_logger(__name__)

router = Router(name="shop")


async def _safe_edit_or_send(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the existing message in place (SPA style), falling back cleanly if uneditable."""
    msg = callback.message
    if msg is None:
        return
    try:
        if getattr(msg, "photo", None) or getattr(msg, "document", None) or getattr(msg, "video", None):
            await msg.edit_caption(
                caption=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
        else:
            await msg.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        await msg.answer(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def _clear_shop_fsm_if_active(state: FSMContext) -> None:
    current = await state.get_state()
    if current and current.startswith("ShopStates:"):
        await state.set_state(None)


async def _notify_admins_new_manual_order(
    *,
    bot: object | None,
    order: OrderDTO,
    user: UserDTO,
    settings: Settings | None,
) -> None:
    """Send best-effort notification to configured admin IDs for pending manual fulfillment."""
    if bot is None:
        return
    cfg = settings or get_settings()
    if not cfg.admin_ids:
        return

    amount_str = format_currency_amount(order.total_amount, order.currency)
    input_preview = escape_html(order.fulfillment_input or "-")
    text = (
        f"📥 <b>New Manual Order Pending Fulfillment</b>\n\n"
        f"🧾 Order: <code>{escape_html(order.order_number)}</code>\n"
        f"👤 User: <code>{user.telegram_id}</code> ({sanitize_username(user.username)})\n"
        f"📦 Product ID: <code>{order.product_id}</code> × <b>{order.quantity}</b>\n"
        f"💰 Total: <b>{escape_html(amount_str)}</b>\n"
        f"📝 Customer Input:\n<pre>{input_preview}</pre>"
    )
    kb = build_admin_order_notify_kb(order.id)
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("Failed to notify admin %s for order %s: %s", admin_id, order.id, exc)


async def _render_product_card(
    *,
    product: ProductDetailDTO,
    selected_qty: int,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render localized product detail card text and quantity/purchase keyboard."""
    title = escape_html(
        product.titles_json.get(lang)
        or product.titles_json.get(DEFAULT_LANGUAGE)
        or product.slug
    )
    description = escape_html(
        product.descriptions_json.get(lang)
        or product.descriptions_json.get(DEFAULT_LANGUAGE)
        or ""
    )
    raw_rules = (
        product.rules_json.get(lang) or product.rules_json.get(DEFAULT_LANGUAGE) or ""
    ).strip()
    if raw_rules:
        rules_heading = await i18n.get_text("shop.rules_heading", lang)
        rules_block = f"\n{rules_heading}\n{escape_html(raw_rules)}\n"
    else:
        rules_block = ""

    usd_unit = await shop_service.calculate_unit_price(product.id, selected_qty, Currency.USD)
    price_parts = [f"<b>{format_usd(usd_unit)}</b>"]
    irt_unit: Decimal | None = None
    if lang == "fa" and product.price_irt is not None:
        irt_unit = await shop_service.calculate_unit_price(product.id, selected_qty, Currency.IRT)
        price_parts.append(f"<b>{format_irt(irt_unit)} تومان</b>")
    price_line = " / ".join(price_parts)

    if selected_qty > 1:
        usd_total = format_usd(usd_unit * Decimal(selected_qty))
        if irt_unit is not None:
            irt_total = format_irt(irt_unit * Decimal(selected_qty))
            price_line += f"  (×{selected_qty} = {usd_total} / {irt_total} تومان)"
        else:
            price_line += f"  (×{selected_qty} = {usd_total})"

    if product.bulk_tiers:
        bulk_heading = await i18n.get_text("shop.bulk_discounts_heading", lang)
        tier_lines: list[str] = []
        for tier in sorted(product.bulk_tiers, key=lambda t: t.min_qty):
            tier_price = format_usd(tier.unit_price_usd)
            if lang == "fa" and tier.unit_price_irt is not None:
                tier_price = f"{tier_price} ({format_irt(tier.unit_price_irt)} تومان)"
            tier_lines.append(
                await i18n.get_text(
                    "shop.bulk_tier_line",
                    lang,
                    min_qty=tier.min_qty,
                    price=tier_price,
                )
            )
        bulk_section = f"\n{bulk_heading}\n" + "\n".join(tier_lines)
    else:
        bulk_section = ""

    if product.delivery_type == DeliveryType.MANUAL and product.manual_stock_count == -1:
        stock_text = await i18n.get_text("shop.unlimited_stock", lang)
    elif product.available_stock <= 0:
        stock_text = await i18n.get_text("shop.out_of_stock", lang)
    else:
        stock_text = str(product.available_stock)

    body = await i18n.get_text(
        "shop.product_detail",
        lang,
        emoji=escape_html(product.color_emoji),
        title=title,
        description=description,
        rules=rules_block,
        price_line=price_line,
        bulk_section=bulk_section,
        stock_text=escape_html(stock_text),
        sold_count=product.sold_count,
    )
    qty_prompt = await i18n.get_text("shop.select_qty_prompt", lang)
    full_text = f"{body}\n\n{qty_prompt}"

    kb = build_product_detail_kb(
        product,
        lang_code=lang,
        selected_qty=selected_qty,
        buy_usd_text=await i18n.get_text("shop.buy_with_usd", lang),
        buy_irt_text=await i18n.get_text("shop.buy_with_irt", lang),
        back_text=await i18n.get_text("btn.back", lang),
        main_menu_text=await i18n.get_text("btn.main_menu", lang),
    )
    return full_text, kb


async def _prepare_categories_view(
    state: FSMContext,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
) -> tuple[str, InlineKeyboardMarkup]:
    await _clear_shop_fsm_if_active(state)
    categories = await shop_service.list_categories(active_only=True)
    text = (
        await i18n.get_text("shop.categories_title", lang)
        if categories
        else await i18n.get_text("shop.no_categories", lang)
    )
    kb = build_categories_kb(
        categories,
        lang_code=lang,
        order_history_text=await i18n.get_text("btn.order_history", lang),
        back_text=await i18n.get_text("btn.back", lang),
    )
    return text, kb


@router.message(Command("shop"))
async def cmd_shop(
    message: Message,
    state: FSMContext,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
) -> None:
    """Handle `/shop` command by opening the active categories menu."""
    text, kb = await _prepare_categories_view(state, lang, i18n, shop_service)
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.callback_query(MenuCB.filter(F.action == "shop"))
async def cb_shop_categories(
    callback: CallbackQuery,
    state: FSMContext,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
) -> None:
    """Open the Shop categories screen via in-place message edit."""
    text, kb = await _prepare_categories_view(state, lang, i18n, shop_service)
    await _safe_edit_or_send(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(CatCB.filter())
async def cb_category_products(
    callback: CallbackQuery,
    callback_data: CatCB,
    state: FSMContext,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
) -> None:
    """List active products inside the selected category with stock and color badges."""
    await _clear_shop_fsm_if_active(state)
    categories = await shop_service.list_categories(active_only=True)
    cat = next((c for c in categories if c.id == callback_data.id), None)
    if cat is None:
        await callback.answer("❌ Category not found", show_alert=True)
        return

    products = await shop_service.list_products(
        category_id=callback_data.id, active_only=True
    )
    cat_name = escape_html(
        cat.names_json.get(lang) or cat.names_json.get(DEFAULT_LANGUAGE) or cat.slug
    )
    text = (
        await i18n.get_text("shop.products_title", lang, category_name=cat_name)
        if products
        else await i18n.get_text("shop.no_products", lang)
    )
    kb = build_products_kb(
        products,
        category_id=callback_data.id,
        lang_code=lang,
        page=callback_data.page,
        stock_template=await i18n.get_text("shop.stock_label", lang),
        unlimited_text=await i18n.get_text("shop.unlimited_stock", lang),
        out_of_stock_text=await i18n.get_text("shop.out_of_stock", lang),
        back_text=await i18n.get_text("btn.back", lang),
    )
    await _safe_edit_or_send(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(ProdCB.filter(F.action == "view"))
async def cb_product_detail(
    callback: CallbackQuery,
    callback_data: ProdCB,
    state: FSMContext,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
) -> None:
    """Show the Product Detail card with bulk discounts, stock, and purchase options."""
    await _clear_shop_fsm_if_active(state)
    try:
        product = await shop_service.get_product_detail(callback_data.id)
    except NotFoundError:
        await callback.answer("❌ Product not found", show_alert=True)
        return

    await state.update_data(shop_prod_id=product.id, shop_qty=1)
    text, kb = await _render_product_card(
        product=product,
        selected_qty=1,
        lang=lang,
        i18n=i18n,
        shop_service=shop_service,
    )
    await _safe_edit_or_send(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(QtyCB.filter())
async def cb_select_qty(
    callback: CallbackQuery,
    callback_data: QtyCB,
    state: FSMContext,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
) -> None:
    """Select a preset quantity (`1`, `5`, `10`) and refresh bulk pricing in place."""
    if callback_data.qty < 1:
        await callback.answer(await i18n.get_text("shop.invalid_qty", lang), show_alert=True)
        return
    try:
        product = await shop_service.get_product_detail(callback_data.prod_id)
    except NotFoundError:
        await callback.answer("❌ Product not found", show_alert=True)
        return

    await state.update_data(shop_prod_id=product.id, shop_qty=callback_data.qty)
    text, kb = await _render_product_card(
        product=product,
        selected_qty=callback_data.qty,
        lang=lang,
        i18n=i18n,
        shop_service=shop_service,
    )
    await _safe_edit_or_send(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(ProdCB.filter(F.action == "custom_qty"))
async def cb_custom_qty_prompt(
    callback: CallbackQuery,
    callback_data: ProdCB,
    state: FSMContext,
    lang: str,
    i18n: TranslationService,
) -> None:
    """Prompt the customer to enter a custom integer quantity."""
    await state.set_state(ShopStates.waiting_custom_qty)
    msg = callback.message
    await state.update_data(
        shop_prod_id=callback_data.id,
        shop_prompt_chat_id=msg.chat.id if msg else None,
        shop_prompt_msg_id=msg.message_id if msg else None,
    )
    prompt_text = await i18n.get_text("shop.enter_custom_qty", lang)
    cancel_kb = build_cancel_kb(
        text=await i18n.get_text("btn.cancel", lang),
        callback_data=ProdCB(id=callback_data.id, action="view"),
    )
    await _safe_edit_or_send(callback, prompt_text, reply_markup=cancel_kb)
    await callback.answer()


@router.message(ShopStates.waiting_custom_qty)
async def msg_custom_qty_input(
    message: Message,
    state: FSMContext,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
) -> None:
    """Validate integer custom quantity input and re-render the Product Card."""
    raw = (message.text or "").strip()
    try:
        qty = int(raw)
    except ValueError:
        await message.answer(
            await i18n.get_text("shop.invalid_qty", lang),
            parse_mode=ParseMode.HTML,
        )
        return

    if qty < 1 or qty > 1000:
        await message.answer(
            await i18n.get_text("shop.invalid_qty", lang),
            parse_mode=ParseMode.HTML,
        )
        return

    fsm_data = await state.get_data()
    prod_id = fsm_data.get("shop_prod_id")
    if not prod_id:
        await state.set_state(None)
        text, kb = await _prepare_categories_view(state, lang, i18n, shop_service)
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    try:
        product = await shop_service.get_product_detail(int(prod_id))
    except NotFoundError:
        await state.set_state(None)
        await message.answer("❌ Product not found")
        return

    await state.set_state(None)
    await state.update_data(shop_prod_id=product.id, shop_qty=qty)
    text, kb = await _render_product_card(
        product=product,
        selected_qty=qty,
        lang=lang,
        i18n=i18n,
        shop_service=shop_service,
    )

    prompt_chat_id = fsm_data.get("shop_prompt_chat_id")
    prompt_msg_id = fsm_data.get("shop_prompt_msg_id")
    if message.bot is not None and prompt_chat_id and prompt_msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=prompt_chat_id,
                message_id=prompt_msg_id,
                text=text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
            return
        except TelegramBadRequest as exc:
            logger.debug("Could not edit custom qty prompt message %s: %s", prompt_msg_id, exc)
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.callback_query(ProdCB.filter(F.action.in_({"buy_usd", "buy_irt"})))
async def cb_purchase_product(
    callback: CallbackQuery,
    callback_data: ProdCB,
    state: FSMContext,
    user: UserDTO,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
    settings: Settings | None = None,
) -> None:
    """Execute atomic wallet purchase and deliver credentials or prompt for manual input."""
    fsm_data = await state.get_data()
    if fsm_data.get("shop_prod_id") == callback_data.id:
        selected_qty = int(fsm_data.get("shop_qty", 1))
    else:
        selected_qty = 1

    currency = Currency.IRT if callback_data.action == "buy_irt" else Currency.USD

    try:
        product = await shop_service.get_product_detail(callback_data.id)
        result = await shop_service.purchase_product(
            PurchaseRequestDTO(
                user_id=user.id,
                product_id=callback_data.id,
                quantity=selected_qty,
                currency=currency,
            )
        )
    except InsufficientStockError as exc:
        avail = exc.details.get("available", 0)
        await callback.answer(
            await i18n.get_text("shop.insufficient_stock", lang, available=avail),
            show_alert=True,
        )
        return
    except InsufficientBalanceError:
        await callback.answer(
            await i18n.get_text("shop.insufficient_balance", lang),
            show_alert=True,
        )
        return
    except (UserBlockedError, ValidationError, NotFoundError) as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return

    await state.update_data(shop_prod_id=product.id, shop_qty=1)

    order_history_text = await i18n.get_text("btn.order_history", lang)
    back_text = await i18n.get_text("btn.back", lang)
    main_menu_text = await i18n.get_text("btn.main_menu", lang)
    post_kb = build_post_purchase_kb(
        category_id=product.category_id,
        order_history_text=order_history_text,
        back_text=back_text,
        main_menu_text=main_menu_text,
    )

    if result.order.status == OrderStatus.COMPLETED:
        total_str = format_currency_amount(
            result.order.total_amount, result.order.currency, lang_code=lang
        )
        items_escaped = escape_html("\n".join(result.delivered_items))
        text = await i18n.get_text(
            "shop.order_completed_auto",
            lang,
            order_number=escape_html(result.order.order_number),
            total=escape_html(total_str),
            items=items_escaped,
        )
        await _safe_edit_or_send(callback, text, reply_markup=post_kb)
        await callback.answer()
        return

    if result.order.status == OrderStatus.PENDING_INPUT:
        await state.set_state(ShopStates.waiting_manual_input)
        await state.update_data(
            manual_order_id=result.order.id,
            manual_category_id=product.category_id,
        )
        custom_prompt = (
            product.manual_prompts_json.get(lang)
            or product.manual_prompts_json.get(DEFAULT_LANGUAGE)
            or ""
        ).strip()
        prompt_body = (
            escape_html(custom_prompt)
            if custom_prompt
            else await i18n.get_text("shop.manual_input_prompt", lang)
        )
        text = f"🧾 <code>{escape_html(result.order.order_number)}</code>\n\n{prompt_body}"
        await _safe_edit_or_send(callback, text, reply_markup=None)
        await callback.answer()
        return

    # OrderStatus.PENDING_FULFILLMENT (manual product without required prompt)
    text = await i18n.get_text(
        "shop.order_pending_fulfillment",
        lang,
        order_number=escape_html(result.order.order_number),
    )
    await _safe_edit_or_send(callback, text, reply_markup=post_kb)
    await callback.answer()
    await _notify_admins_new_manual_order(
        bot=callback.bot,
        order=result.order,
        user=user,
        settings=settings,
    )


@router.message(ShopStates.waiting_manual_input)
async def msg_manual_fulfillment_input(
    message: Message,
    state: FSMContext,
    user: UserDTO,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
    settings: Settings | None = None,
) -> None:
    """Receive user activation details for a manual fulfillment order and notify admins."""
    raw_input = (message.text or message.caption or "").strip()
    if not raw_input:
        await message.answer(
            await i18n.get_text("shop.manual_input_prompt", lang),
            parse_mode=ParseMode.HTML,
        )
        return

    fsm_data = await state.get_data()
    order_id = fsm_data.get("manual_order_id")
    category_id = int(fsm_data.get("manual_category_id") or 0)
    if not order_id:
        await state.set_state(None)
        text, kb = await _prepare_categories_view(state, lang, i18n, shop_service)
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    try:
        order = await shop_service.submit_manual_fulfillment_input(
            order_id=int(order_id),
            user_id=user.id,
            fulfillment_input=raw_input,
        )
    except (ValidationError, NotFoundError, InvalidStateTransitionError) as exc:
        await message.answer(escape_html(exc.message), parse_mode=ParseMode.HTML)
        if not isinstance(exc, ValidationError):
            await state.set_state(None)
        return

    await state.set_state(None)
    await state.update_data(manual_order_id=None)

    post_kb = build_post_purchase_kb(
        category_id=category_id,
        order_history_text=await i18n.get_text("btn.order_history", lang),
        back_text=await i18n.get_text("btn.back", lang),
        main_menu_text=await i18n.get_text("btn.main_menu", lang),
    )
    text = await i18n.get_text(
        "shop.order_pending_fulfillment",
        lang,
        order_number=escape_html(order.order_number),
    )
    await message.answer(text, reply_markup=post_kb, parse_mode=ParseMode.HTML)
    await _notify_admins_new_manual_order(
        bot=message.bot,
        order=order,
        user=user,
        settings=settings,
    )


@router.callback_query(MenuCB.filter(F.action == "orders"))
@router.callback_query(OrderCB.filter(F.action == "list"))
async def cb_order_history(
    callback: CallbackQuery,
    state: FSMContext,
    user: UserDTO,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
) -> None:
    """Display the authenticated user's order history in place."""
    await _clear_shop_fsm_if_active(state)
    orders = await shop_service.list_user_orders(user.id, limit=20, offset=0)
    back_text = await i18n.get_text("btn.back", lang)
    main_menu_text = await i18n.get_text("btn.main_menu", lang)

    if not orders:
        text = await i18n.get_text("shop.no_orders", lang)
        kb = build_back_kb(
            text=back_text,
            callback_data=MenuCB(action="shop"),
            main_menu_text=main_menu_text,
        )
    else:
        text = await i18n.get_text("shop.order_history_title", lang)
        kb = build_order_history_kb(
            orders,
            lang_code=lang,
            back_text=back_text,
            main_menu_text=main_menu_text,
        )
    await _safe_edit_or_send(callback, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(OrderCB.filter(F.action == "view"))
async def cb_order_detail(
    callback: CallbackQuery,
    callback_data: OrderCB,
    user: UserDTO,
    lang: str,
    i18n: TranslationService,
    shop_service: ShopServiceProtocol,
) -> None:
    """Display a single order owned by the user, decrypting delivered items cleanly."""
    try:
        order = await shop_service.get_order_detail(callback_data.id, decrypt_items=True)
    except NotFoundError:
        await callback.answer("❌ Order not found", show_alert=True)
        return

    if order.user_id != user.id:
        await callback.answer("⛔ Unauthorized", show_alert=True)
        return

    total_str = format_currency_amount(
        order.total_amount, order.currency, lang_code=lang
    )
    unit_str = format_currency_amount(
        order.unit_price, order.currency, lang_code=lang
    )
    lines = [
        f"🧾 <b>Order <code>{escape_html(order.order_number)}</code></b>",
        f"📌 Status: <b>{escape_html(order.status.value)}</b>",
        f"🔢 Qty: <b>{order.quantity}</b> × {escape_html(unit_str)}",
        f"💰 Total: <b>{escape_html(total_str)}</b>",
    ]
    if order.fulfillment_input:
        lines.append(f"📝 Input: <code>{escape_html(order.fulfillment_input)}</code>")
    if order.fulfillment_note:
        lines.append(f"💬 Note: {escape_html(order.fulfillment_note)}")

    delivered = [item.decrypted_payload for item in order.items if item.decrypted_payload]
    if delivered:
        lines.append(f"\n🔐 <b>Delivered:</b>\n<pre>{escape_html(chr(10).join(delivered))}</pre>")

    kb = build_order_detail_kb(
        back_text=await i18n.get_text("btn.back", lang),
        main_menu_text=await i18n.get_text("btn.main_menu", lang),
    )
    await _safe_edit_or_send(callback, "\n".join(lines), reply_markup=kb)
    await callback.answer()


__all__ = ["router"]
