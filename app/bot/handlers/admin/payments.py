"""Admin Payments Queue, 1-click Approve/Reject, Crypto Wallets, Fiat Methods & Exchange Rate."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
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
    build_crypto_wallet_detail_kb,
    build_crypto_wallets_list_kb,
    build_exchange_rate_kb,
    build_fiat_method_detail_kb,
    build_fiat_methods_list_kb,
    build_payment_detail_kb,
    build_payments_menu_kb,
    build_payments_queue_kb,
)
from app.bot.keyboards.common_kb import AdmCB, PayCB
from app.bot.states import AdminStates
from app.core.constants import (
    CryptoVerificationMode,
    FiatMethodType,
    PaymentCategory,
    PaymentStatus,
)
from app.core.exceptions import ShopBotError
from app.core.logging import get_logger
from app.core.security import escape_html
from app.services.dtos import (
    CryptoWalletCreateDTO,
    CryptoWalletUpdateDTO,
    FiatMethodCreateDTO,
    FiatMethodUpdateDTO,
    PaymentDTO,
    PaymentReviewDTO,
)

logger = get_logger(__name__)
router = Router(name="admin_payments")


async def _notify_payment_reviewed(
    payment: PaymentDTO,
    approved: bool,
    reason: str | None,
    bot: Bot | None,
    data: dict[str, Any],
) -> None:
    """Notify deposit owner in their preferred language when payment is approved or rejected."""
    if bot is None:
        return
    user_service = get_optional_service(data, "user_service")
    if user_service is None:
        return
    try:
        user = await user_service.get_by_id(payment.user_id)
        if approved:
            amt_str = (
                f"${payment.amount_usd:,.2f} USD"
                if payment.amount_usd > 0
                else f"{payment.amount_irt:,.0f} IRT"
            )
            msg = await render_i18n_template(
                data,
                "deposit.approved_notify",
                user.language_code,
                amount=amt_str,
            )
        else:
            msg = await render_i18n_template(
                data,
                "deposit.rejected_notify",
                user.language_code,
                reason=escape_html(reason or "-"),
            )
        await notify_user_dm(bot, user.telegram_id, msg)
    except ShopBotError as exc:
        logger.warning("Failed to notify user on payment review #%s: %s", payment.id, exc)


async def _render_payment_card(target: CallbackQuery | Message, payment_id: int, data: dict[str, Any]) -> None:
    try:
        payment_service = get_service(data, "payment_service")
        p = await payment_service.get_payment(payment_id)
    except ShopBotError as exc:
        await safe_edit_or_send(target, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="pay", act="menu"))
        return

    rate_str = f"{p.exchange_rate_used:,.0f}" if p.exchange_rate_used else "-"
    text = (
        f"💳 <b>Deposit Payment #{p.id}</b>\n\n"
        f"👤 Customer ID: <code>{p.user_id}</code>\n"
        f"🏷 Category: <code>{p.payment_category.value}</code> | Currency: <code>{p.currency.value}</code>\n"
        f"💵 Amount USD: <b>${p.amount_usd:,.2f}</b>\n"
        f"💳 Amount IRT: <b>{p.amount_irt:,.0f} تومان</b> (Rate: {rate_str})\n"
        f"🔗 TXID / Hash: <code>{escape_html(p.tx_hash or '-')}</code>\n"
        f"🆔 Authority / Track: <code>{escape_html(p.authority_track_id or '-')}</code>\n"
        f"📝 Receipt Note: {escape_html(p.receipt_note or '-')}\n"
        f"🚦 Status: <b>{p.status.value}</b>\n"
        f"🛡 Admin Note: {escape_html(p.admin_note or '-')}\n"
        f"🕒 Created: <code>{fmt_dt(p.created_at)}</code>"
    )
    await safe_edit_or_send(target, text, reply_markup=build_payment_detail_kb(p))


async def _render_wallet_card(target: CallbackQuery | Message, wallet_id: int, data: dict[str, Any]) -> None:
    try:
        payment_service = get_service(data, "payment_service")
        wallets = await payment_service.list_crypto_wallets(active_only=False)
        w = next((item for item in wallets if item.id == wallet_id), None)
        if w is None:
            await safe_edit_or_send(target, "❌ Crypto wallet not found.", reply_markup=build_admin_back_kb(sec="wal", act="list"))
            return
    except ShopBotError as exc:
        await safe_edit_or_send(target, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="pay", act="menu"))
        return

    text = (
        f"🪙 <b>Crypto Wallet #{w.id}: {escape_html(w.coin_name)} ({escape_html(w.symbol)})</b>\n\n"
        f"🌐 Network: <code>{escape_html(w.network)}</code>\n"
        f"📥 Address:\n<code>{escape_html(w.wallet_address)}</code>\n"
        f"💵 Min Deposit: <b>${w.min_deposit_usd:,.2f}</b>\n"
        f"ℹ️ Fee Note: {escape_html(w.fee_note or '-')}\n"
        f"📝 Instructions: {escape_html(w.instructions or '-')}\n"
        f"🔍 Verification Mode: <code>{w.verification_mode.value}</code>\n"
        f"🚦 Active: <b>{'Yes' if w.is_active else 'No'}</b>"
    )
    await safe_edit_or_send(target, text, reply_markup=build_crypto_wallet_detail_kb(w))


async def _render_fiat_card(target: CallbackQuery | Message, method_id: int, data: dict[str, Any]) -> None:
    try:
        payment_service = get_service(data, "payment_service")
        methods = await payment_service.list_fiat_methods(active_only=False)
        m = next((item for item in methods if item.id == method_id), None)
        if m is None:
            await safe_edit_or_send(target, "❌ Fiat method not found.", reply_markup=build_admin_back_kb(sec="fiat", act="list"))
            return
    except ShopBotError as exc:
        await safe_edit_or_send(target, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="pay", act="menu"))
        return

    text = (
        f"🏦 <b>Fiat Method #{m.id}: {escape_html(m.title)}</b>\n\n"
        f"🏷 Type: <code>{m.method_type.value}</code>\n"
        f"💳 Card Number: <code>{escape_html(m.card_number or '-')}</code>\n"
        f"🏦 Sheba (IBAN): <code>{escape_html(m.sheba or '-')}</code>\n"
        f"👤 Holder: <b>{escape_html(m.cardholder_name or '-')}</b> ({escape_html(m.bank_name or '-')})\n"
        f"🌐 Gateway Provider: <code>{escape_html(m.gateway_provider or '-')}</code>\n"
        f"💰 Min Deposit: <b>{m.min_deposit_irt:,.0f} IRT</b>\n"
        f"📝 Instructions: {escape_html(m.instructions or '-')}\n"
        f"🚦 Active: <b>{'Yes' if m.is_active else 'No'}</b>"
    )
    await safe_edit_or_send(target, text, reply_markup=build_fiat_method_detail_kb(m))


@router.callback_query(AdmCB.filter((F.sec == "pay") & (F.act == "menu")))
async def cb_payments_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_or_send(
        callback,
        "💳 <b>Payments, Crypto Wallets &amp; Fiat Methods (قسمت پرداخت‌ها)</b>\n\nSelect an option:",
        reply_markup=build_payments_menu_kb(),
    )


@router.callback_query(AdmCB.filter((F.sec == "pay") & (F.act.in_({"q_pending", "q_approved", "q_rejected"}))))
async def cb_payments_queue(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    status_map = {
        "q_pending": PaymentStatus.PENDING,
        "q_approved": PaymentStatus.APPROVED,
        "q_rejected": PaymentStatus.REJECTED,
    }
    status = status_map[callback_data.act]
    try:
        payment_service = get_service(data, "payment_service")
        payments = await payment_service.list_payments(status=status, limit=25)
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="pay", act="menu"))
        return
    await safe_edit_or_send(
        callback,
        f"💳 <b>Payments Queue — {status.value.title()}</b> ({len(payments)}):",
        reply_markup=build_payments_queue_kb(payments),
    )


@router.callback_query(AdmCB.filter((F.sec == "pay") & (F.act == "view")))
async def cb_payment_view_adm(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await _render_payment_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "pay") & (F.act == "receipt")))
async def cb_payment_receipt(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        payment_service = get_service(data, "payment_service")
        p = await payment_service.get_payment(callback_data.id)
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    if not p.receipt_file_id or callback.message is None:
        await safe_answer_callback(callback, "No receipt file attached.", show_alert=True)
        return
    try:
        await callback.message.answer_photo(
            photo=p.receipt_file_id,
            caption=f"🧾 Receipt for Deposit #{p.id} (User #{p.user_id})",
        )
        await safe_answer_callback(callback)
    except TelegramAPIError as exc:
        await safe_answer_callback(callback, f"Could not send receipt: {exc}", show_alert=True)


@router.callback_query(PayCB.filter(F.action == "view"))
async def cb_paycb_view(callback: CallbackQuery, callback_data: PayCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await _render_payment_card(callback, callback_data.id, data)


@router.callback_query(PayCB.filter(F.action == "approve"))
async def cb_paycb_approve(
    callback: CallbackQuery,
    callback_data: PayCB,
    bot: Bot | None = None,
    **data: Any,
) -> None:
    """1-click atomic Approve & Credit for pending deposits."""
    try:
        payment_service = get_service(data, "payment_service")
        reviewed = await payment_service.review_payment(
            PaymentReviewDTO(
                payment_id=callback_data.id,
                admin_id=resolve_admin_actor_id(callback),
                approve=True,
            )
        )
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return

    await _notify_payment_reviewed(reviewed, True, None, bot or callback.bot, data)
    await safe_answer_callback(callback, f"Deposit #{reviewed.id} approved and credited!")
    await _render_payment_card(callback, reviewed.id, data)


@router.callback_query(PayCB.filter(F.action == "reject"))
async def cb_paycb_reject_prompt(callback: CallbackQuery, callback_data: PayCB, state: FSMContext) -> None:
    """Prompt for optional reject reason before rejecting deposit."""
    await state.set_state(AdminStates.waiting_reject_reason)
    await state.update_data(reject_payment_id=callback_data.id)
    await safe_edit_or_send(
        callback,
        f"❌ <b>Reject Deposit #{callback_data.id}</b>\n\n"
        f"Send a rejection reason for the user (or send <code>-</code> for default):",
        reply_markup=build_admin_cancel_kb(sec="pay", act="view", target_id=callback_data.id),
    )


@router.message(AdminStates.waiting_reject_reason)
async def msg_paycb_reject_commit(
    message: Message,
    state: FSMContext,
    bot: Bot | None = None,
    **data: Any,
) -> None:
    fsm_data = await state.get_data()
    payment_id = int(fsm_data.get("reject_payment_id", 0))
    raw_reason = (message.text or "").strip()
    reason = "Rejected by admin" if raw_reason in {"", "-"} else raw_reason

    try:
        payment_service = get_service(data, "payment_service")
        reviewed = await payment_service.review_payment(
            PaymentReviewDTO(
                payment_id=payment_id,
                admin_id=resolve_admin_actor_id(message),
                approve=False,
                admin_note=reason,
            )
        )
    except ShopBotError as exc:
        await state.clear()
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(sec="pay", act="q_pending"),
        )
        return

    await state.clear()
    await _notify_payment_reviewed(reviewed, False, reason, bot or message.bot, data)
    await _render_payment_card(message, reviewed.id, data)


@router.callback_query(AdmCB.filter((F.sec == "wal") & (F.act == "list")))
async def cb_wallets_list(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    try:
        payment_service = get_service(data, "payment_service")
        wallets = await payment_service.list_crypto_wallets(active_only=False)
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="pay", act="menu"))
        return
    await safe_edit_or_send(
        callback,
        f"🪙 <b>Crypto Wallets</b> ({len(wallets)}):",
        reply_markup=build_crypto_wallets_list_kb(wallets),
    )


@router.callback_query(AdmCB.filter((F.sec == "wal") & (F.act == "view")))
async def cb_wallet_view(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await _render_wallet_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "wal") & (F.act == "toggle")))
async def cb_wallet_toggle(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        payment_service = get_service(data, "payment_service")
        wallets = await payment_service.list_crypto_wallets(active_only=False)
        w = next((item for item in wallets if item.id == callback_data.id), None)
        if w is None:
            await safe_answer_callback(callback, "Wallet not found", show_alert=True)
            return
        await payment_service.update_crypto_wallet(w.id, CryptoWalletUpdateDTO(is_active=not w.is_active))
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_wallet_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "wal") & (F.act == "del")))
async def cb_wallet_delete(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        payment_service = get_service(data, "payment_service")
        await payment_service.delete_crypto_wallet(callback_data.id)
        wallets = await payment_service.list_crypto_wallets(active_only=False)
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await safe_answer_callback(callback, "Wallet deleted.")
    await safe_edit_or_send(callback, "🪙 <b>Crypto Wallets</b>", reply_markup=build_crypto_wallets_list_kb(wallets))


@router.callback_query(AdmCB.filter((F.sec == "wal") & (F.act.in_({"add", "edit"}))))
async def cb_wallet_input_prompt(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext) -> None:
    is_edit = callback_data.act == "edit"
    await state.set_state(AdminStates.waiting_wallet_input)
    await state.update_data(wal_mode="edit" if is_edit else "add", wal_id=callback_data.id)
    hint = (
        "Send: <code>wallet_address | min_deposit_usd | fee_note | instructions</code> (use <code>-</code> to keep unchanged)"
        if is_edit
        else "Send: <code>coin_name | symbol | network | wallet_address | min_deposit_usd | fee_note | instructions</code>\n"
        "Example: <code>Tether USD | USDT | TRC20 | TXyz1234567890... | 5 | 1 USDT fee | Send TRC20 only</code>"
    )
    await safe_edit_or_send(
        callback,
        f"🪙 <b>{'Edit' if is_edit else 'Add'} Crypto Wallet</b>\n\n{hint}",
        reply_markup=build_admin_cancel_kb(sec="wal", act="list"),
    )


@router.message(AdminStates.waiting_wallet_input)
async def msg_wallet_input(message: Message, state: FSMContext, **data: Any) -> None:
    fsm_data = await state.get_data()
    mode = str(fsm_data.get("wal_mode", "add"))
    wal_id = int(fsm_data.get("wal_id", 0))
    parts = [p.strip() for p in (message.text or "").split("|")]

    try:
        payment_service = get_service(data, "payment_service")
        if mode == "edit":
            upd = CryptoWalletUpdateDTO()
            if len(parts) > 0 and parts[0] not in {"", "-"}:
                upd.wallet_address = parts[0]
            if len(parts) > 1 and parts[1] not in {"", "-"}:
                min_usd = parse_positive_decimal(parts[1])
                if min_usd is None:
                    raise ValueError("Invalid min_deposit_usd")
                upd.min_deposit_usd = min_usd
            if len(parts) > 2 and parts[2] not in {"", "-"}:
                upd.fee_note = parts[2]
            if len(parts) > 3 and parts[3] not in {"", "-"}:
                upd.instructions = parts[3]
            await payment_service.update_crypto_wallet(wal_id, upd)
            await state.clear()
            await _render_wallet_card(message, wal_id, data)
            return

        if len(parts) < 4:
            raise ValueError("Expected: coin_name | symbol | network | wallet_address | min_deposit_usd | fee_note | instructions")
        min_usd = parse_positive_decimal(parts[4]) if len(parts) > 4 and parts[4] else Decimal("1")
        if min_usd is None:
            min_usd = Decimal("1")
        created = await payment_service.create_crypto_wallet(
            CryptoWalletCreateDTO(
                coin_name=parts[0],
                symbol=parts[1].upper(),
                network=parts[2].upper(),
                wallet_address=parts[3],
                min_deposit_usd=min_usd,
                fee_note=parts[5] if len(parts) > 5 and parts[5] else None,
                instructions=parts[6] if len(parts) > 6 and parts[6] else None,
                verification_mode=CryptoVerificationMode.MANUAL_TXID,
            )
        )
        await state.clear()
        await _render_wallet_card(message, created.id, data)
    except (ShopBotError, ValueError) as exc:
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(str(exc))}",
            reply_markup=build_admin_cancel_kb(sec="wal", act="list"),
        )


@router.callback_query(AdmCB.filter((F.sec == "fiat") & (F.act == "list")))
async def cb_fiat_list(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    try:
        payment_service = get_service(data, "payment_service")
        methods = await payment_service.list_fiat_methods(active_only=False)
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="pay", act="menu"))
        return
    await safe_edit_or_send(
        callback,
        f"🏦 <b>Iranian Fiat Methods (ریالی)</b> ({len(methods)}):",
        reply_markup=build_fiat_methods_list_kb(methods),
    )


@router.callback_query(AdmCB.filter((F.sec == "fiat") & (F.act == "view")))
async def cb_fiat_view(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext, **data: Any) -> None:
    await state.clear()
    await _render_fiat_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "fiat") & (F.act == "toggle")))
async def cb_fiat_toggle(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        payment_service = get_service(data, "payment_service")
        methods = await payment_service.list_fiat_methods(active_only=False)
        m = next((item for item in methods if item.id == callback_data.id), None)
        if m is None:
            await safe_answer_callback(callback, "Method not found", show_alert=True)
            return
        await payment_service.update_fiat_method(m.id, FiatMethodUpdateDTO(is_active=not m.is_active))
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await _render_fiat_card(callback, callback_data.id, data)


@router.callback_query(AdmCB.filter((F.sec == "fiat") & (F.act == "del")))
async def cb_fiat_delete(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    try:
        payment_service = get_service(data, "payment_service")
        await payment_service.delete_fiat_method(callback_data.id)
        methods = await payment_service.list_fiat_methods(active_only=False)
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await safe_answer_callback(callback, "Fiat method deleted.")
    await safe_edit_or_send(callback, "🏦 <b>Iranian Fiat Methods</b>", reply_markup=build_fiat_methods_list_kb(methods))


@router.callback_query(AdmCB.filter((F.sec == "fiat") & (F.act.in_({"add", "edit"}))))
async def cb_fiat_input_prompt(callback: CallbackQuery, callback_data: AdmCB, state: FSMContext) -> None:
    is_edit = callback_data.act == "edit"
    await state.set_state(AdminStates.waiting_fiat_input)
    await state.update_data(fiat_mode="edit" if is_edit else "add", fiat_id=callback_data.id)
    hint = (
        "Send: <code>title | card_number | sheba | cardholder_name | bank_name | min_deposit_irt | instructions</code> (use <code>-</code> to keep unchanged)"
        if is_edit
        else "Send: <code>method_type(card_to_card/gateway) | title | card_number | sheba | cardholder_name | bank_name | min_deposit_irt | instructions</code>"
    )
    await safe_edit_or_send(
        callback,
        f"🏦 <b>{'Edit' if is_edit else 'Add'} Iranian Fiat Method</b>\n\n{hint}",
        reply_markup=build_admin_cancel_kb(sec="fiat", act="list"),
    )


@router.message(AdminStates.waiting_fiat_input)
async def msg_fiat_input(message: Message, state: FSMContext, **data: Any) -> None:
    fsm_data = await state.get_data()
    mode = str(fsm_data.get("fiat_mode", "add"))
    fiat_id = int(fsm_data.get("fiat_id", 0))
    parts = [p.strip() for p in (message.text or "").split("|")]

    try:
        payment_service = get_service(data, "payment_service")
        if mode == "edit":
            upd = FiatMethodUpdateDTO()
            if len(parts) > 0 and parts[0] not in {"", "-"}:
                upd.title = parts[0]
            if len(parts) > 1 and parts[1] not in {"", "-"}:
                upd.card_number = parts[1]
            if len(parts) > 2 and parts[2] not in {"", "-"}:
                upd.sheba = parts[2]
            if len(parts) > 3 and parts[3] not in {"", "-"}:
                upd.cardholder_name = parts[3]
            if len(parts) > 4 and parts[4] not in {"", "-"}:
                upd.bank_name = parts[4]
            if len(parts) > 5 and parts[5] not in {"", "-"}:
                min_irt = parse_positive_decimal(parts[5])
                if min_irt is None:
                    raise ValueError("Invalid min_deposit_irt")
                upd.min_deposit_irt = min_irt
            if len(parts) > 6 and parts[6] not in {"", "-"}:
                upd.instructions = parts[6]
            await payment_service.update_fiat_method(fiat_id, upd)
            await state.clear()
            await _render_fiat_card(message, fiat_id, data)
            return

        if len(parts) < 2:
            raise ValueError("Expected: method_type | title | card_number | sheba | cardholder | bank | min_irt | instructions")
        m_type = FiatMethodType(parts[0].lower())
        min_irt = parse_positive_decimal(parts[6]) if len(parts) > 6 and parts[6] else Decimal("50000")
        created = await payment_service.create_fiat_method(
            FiatMethodCreateDTO(
                method_type=m_type,
                title=parts[1],
                card_number=parts[2] if len(parts) > 2 and parts[2] not in {"", "-"} else None,
                sheba=parts[3] if len(parts) > 3 and parts[3] not in {"", "-"} else None,
                cardholder_name=parts[4] if len(parts) > 4 and parts[4] not in {"", "-"} else None,
                bank_name=parts[5] if len(parts) > 5 and parts[5] not in {"", "-"} else None,
                min_deposit_irt=min_irt or Decimal("50000"),
                instructions=parts[7] if len(parts) > 7 and parts[7] not in {"", "-"} else None,
                gateway_provider=parts[2] if m_type == FiatMethodType.GATEWAY and len(parts) > 2 else None,
            )
        )
        await state.clear()
        await _render_fiat_card(message, created.id, data)
    except (ShopBotError, ValueError) as exc:
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(str(exc))}",
            reply_markup=build_admin_cancel_kb(sec="fiat", act="list"),
        )


@router.callback_query(AdmCB.filter((F.sec == "pay") & (F.act == "rate")))
async def cb_exchange_rate_view(callback: CallbackQuery, state: FSMContext, **data: Any) -> None:
    await state.clear()
    try:
        payment_service = get_service(data, "payment_service")
        rate_dto = await payment_service.get_exchange_rate()
    except ShopBotError as exc:
        await safe_edit_or_send(callback, f"⚠️ {escape_html(exc.message)}", reply_markup=build_admin_back_kb(sec="pay", act="menu"))
        return

    await safe_edit_or_send(
        callback,
        f"💱 <b>USD to Toman Exchange Rate</b>\n\n"
        f"💵 1 USD = <b>{rate_dto.usd_to_toman_rate:,.0f} تومان (IRT)</b>\n"
        f"⚙️ Mode: <code>{escape_html(rate_dto.rate_mode)}</code>",
        reply_markup=build_exchange_rate_kb(rate_dto.rate_mode),
    )


@router.callback_query(AdmCB.filter((F.sec == "pay") & (F.act.in_({"mode_manual", "mode_live"}))))
async def cb_exchange_rate_mode(callback: CallbackQuery, callback_data: AdmCB, **data: Any) -> None:
    new_mode = "manual" if callback_data.act == "mode_manual" else "live"
    try:
        payment_service = get_service(data, "payment_service")
        curr = await payment_service.get_exchange_rate()
        updated = await payment_service.update_exchange_rate(curr.usd_to_toman_rate, rate_mode=new_mode)
    except ShopBotError as exc:
        await safe_answer_callback(callback, exc.message, show_alert=True)
        return
    await safe_edit_or_send(
        callback,
        f"💱 <b>USD to Toman Exchange Rate</b>\n\n"
        f"💵 1 USD = <b>{updated.usd_to_toman_rate:,.0f} تومان (IRT)</b>\n"
        f"⚙️ Mode: <code>{escape_html(updated.rate_mode)}</code>",
        reply_markup=build_exchange_rate_kb(updated.rate_mode),
    )


@router.callback_query(AdmCB.filter((F.sec == "pay") & (F.act == "set_rate")))
async def cb_exchange_rate_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_exchange_rate)
    await safe_edit_or_send(
        callback,
        "💱 Enter the new 1 USD to Toman (IRT) rate (e.g. <code>68500</code>):",
        reply_markup=build_admin_cancel_kb(sec="pay", act="rate"),
    )


@router.message(AdminStates.waiting_exchange_rate)
async def msg_exchange_rate_commit(message: Message, state: FSMContext, **data: Any) -> None:
    rate = parse_positive_decimal(message.text or "")
    if rate is None:
        await safe_edit_or_send(
            message,
            "❌ Invalid rate. Enter a positive number (e.g. <code>68500</code>):",
            reply_markup=build_admin_cancel_kb(sec="pay", act="rate"),
        )
        return
    try:
        payment_service = get_service(data, "payment_service")
        curr = await payment_service.get_exchange_rate()
        updated = await payment_service.update_exchange_rate(rate, rate_mode=curr.rate_mode)
    except ShopBotError as exc:
        await state.clear()
        await safe_edit_or_send(
            message,
            f"⚠️ {escape_html(exc.message)}",
            reply_markup=build_admin_back_kb(sec="pay", act="rate"),
        )
        return
    await state.clear()
    await safe_edit_or_send(
        message,
        f"✅ Updated Exchange Rate:\n💵 1 USD = <b>{updated.usd_to_toman_rate:,.0f} تومان (IRT)</b>",
        reply_markup=build_exchange_rate_kb(updated.rate_mode),
    )
