"""Region-aware Telegram Bot Deposit handlers (Crypto for all 12 languages; Fiat + Crypto for fa)."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from redis.asyncio import Redis
from sqlalchemy import select

from app.bot.keyboards.common_kb import DepCB, MenuCB, PayCB, build_back_kb
from app.bot.keyboards.deposit_kb import (
    build_admin_payment_review_kb,
    build_deposit_cancel_kb,
    build_deposit_methods_kb,
    build_gateway_redirect_kb,
)
from app.bot.states import DepositStates
from app.config import Settings, get_settings
from app.core.constants import (
    CryptoVerificationMode,
    Currency,
    DEFAULT_LANGUAGE,
    FIAT_ENABLED_LANGUAGES,
    FiatMethodType,
    PaymentCategory,
    PaymentStatus,
)
from app.core.exceptions import (
    DuplicateTransactionError,
    FileValidationError,
    InvalidStateTransitionError,
    NotFoundError,
    ShopBotError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import escape_html, sanitize_username, validate_receipt_image
from app.database.models import User
from app.database.session import get_session_factory
from app.locales.i18n import TranslationService
from app.services.dtos import (
    CryptoDepositCreateDTO,
    FiatDepositCreateDTO,
    PaymentDTO,
    PaymentReviewDTO,
    UserDTO,
)
from app.services.payment_service import PaymentService

logger = get_logger(__name__)

router = Router(name="deposit")

_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def _normalize_numeric_input(raw: str) -> str:
    cleaned = raw.translate(_DIGIT_TRANSLATION).strip()
    cleaned = cleaned.replace(",", "").replace("_", "").replace(" ", "")
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    return cleaned


def _parse_positive_decimal(raw: str) -> Decimal | None:
    cleaned = _normalize_numeric_input(raw)
    if not cleaned:
        return None
    try:
        val = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if not val.is_finite() or val <= Decimal("0"):
        return None
    return val


async def _resolve_user_and_services(
    telegram_user: Any,
    data: dict[str, Any],
) -> tuple[UserDTO, str, TranslationService, PaymentService, Settings, Redis | None]:
    settings: Settings = data.get("settings") or get_settings()
    session_factory = data.get("session_factory") or get_session_factory()
    redis: Redis | None = data.get("redis") or data.get("redis_client")
    i18n: TranslationService = (
        data.get("i18n")
        or data.get("translation_service")
        or TranslationService(session_factory, redis)
    )
    payment_service: PaymentService = data.get("payment_service") or PaymentService(
        session_factory=session_factory,
        settings=settings,
    )

    user: UserDTO | None = data.get("user") or data.get("db_user")
    if user is None:
        async with session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(User).where(User.telegram_id == telegram_user.id)
                    )
                ).scalar_one_or_none()
                if row is None:
                    lang_init = TranslationService.normalize_lang(
                        getattr(telegram_user, "language_code", None)
                    )
                    row = User(
                        telegram_id=telegram_user.id,
                        username=getattr(telegram_user, "username", None),
                        first_name=getattr(telegram_user, "first_name", None),
                        last_name=getattr(telegram_user, "last_name", None),
                        language_code=lang_init,
                    )
                    session.add(row)
                    await session.flush()
                    await session.refresh(row)
                user = UserDTO.model_validate(row)

    lang_code = TranslationService.normalize_lang(
        data.get("lang_code") or data.get("lang") or user.language_code
    )
    return user, lang_code, i18n, payment_service, settings, redis


def _is_callback_event(event: CallbackQuery | Message) -> bool:
    return isinstance(event, CallbackQuery) or (
        hasattr(event, "message") and not hasattr(event, "chat")
    )


async def _safe_edit_or_send(
    event: CallbackQuery | Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the existing message in place (SPA style) or send a replacement if media was shown."""
    if _is_callback_event(event):
        msg = event.message
        if msg is not None:
            if getattr(msg, "photo", None):
                try:
                    await msg.delete()
                except Exception:
                    pass
                await msg.answer(text, reply_markup=reply_markup, parse_mode="HTML")
                return
            try:
                await msg.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
                try:
                    await msg.answer(text, reply_markup=reply_markup, parse_mode="HTML")
                    return
                except Exception as send_exc:
                    logger.warning("Fallback message send failed: %s", send_exc)
                    return
    else:
        await event.answer(text, reply_markup=reply_markup, parse_mode="HTML")


async def _check_deposit_cooldown(redis: Redis | None, telegram_id: int) -> bool:
    """Return True if user is currently on deposit submission cooldown."""
    if redis is None:
        return False
    key = f"cooldown:deposit:{telegram_id}"
    try:
        exists = await redis.exists(key)
        return bool(exists)
    except Exception as exc:
        logger.warning("Redis deposit cooldown check failed: %s", exc)
        return False


async def _set_deposit_cooldown(redis: Redis | None, telegram_id: int, seconds: int) -> None:
    if redis is None or seconds <= 0:
        return
    key = f"cooldown:deposit:{telegram_id}"
    try:
        await redis.set(key, "1", ex=seconds)
    except Exception as exc:
        logger.warning("Redis deposit cooldown set failed: %s", exc)


async def _download_and_validate_receipt(
    message: Message,
    settings: Settings,
) -> tuple[str, str]:
    """Download Telegram photo/document image, validate magic bytes & <=5MB, save to disk."""
    file_id: str | None = None
    filename: str | None = None
    content_type: str | None = None
    file_size: int | None = None

    if message.photo:
        largest = message.photo[-1]
        file_id = largest.file_id
        file_size = getattr(largest, "file_size", None)
        filename = None
    elif message.document:
        file_id = message.document.file_id
        filename = getattr(message.document, "file_name", None)
        content_type = getattr(message.document, "mime_type", None)
        file_size = getattr(message.document, "file_size", None)

    if not file_id:
        raise FileValidationError("No image attachment found")
    if file_size is not None and file_size > settings.MAX_UPLOAD_SIZE_BYTES:
        raise FileValidationError("Receipt file exceeds 5 MB limit")

    bot: Bot | None = message.bot
    raw_bytes: bytes = b""
    if bot is not None:
        buf = BytesIO()
        try:
            file_info = await bot.get_file(file_id)
            await bot.download_file(file_info.file_path, destination=buf)
            raw_bytes = buf.getvalue()
        except AttributeError:
            await bot.download(file_id, destination=buf)
            raw_bytes = buf.getvalue()

    detected_mime = validate_receipt_image(
        raw_bytes,
        filename=filename,
        content_type=content_type,
        max_bytes=settings.MAX_UPLOAD_SIZE_BYTES,
    )
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[detected_mime]
    receipts_dir = Path(settings.UPLOAD_DIR) / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    rel_name = f"receipt_{message.from_user.id}_{uuid.uuid4().hex[:12]}.{ext}"
    abs_path = receipts_dir / rel_name
    await asyncio.to_thread(abs_path.write_bytes, raw_bytes)
    return file_id, str(Path("receipts") / rel_name)


async def _notify_admins_new_deposit(
    bot: Bot | None,
    settings: Settings,
    user: UserDTO,
    payment: PaymentDTO,
    method_label: str,
) -> None:
    """Send instant deposit review card with 1-click Approve/Reject inline buttons to all admins."""
    if bot is None or not settings.admin_ids:
        return

    amount_display = (
        f"${payment.amount_usd.normalize()} USD"
        if payment.currency == Currency.USD
        else f"{int(payment.amount_irt):,} IRT (~${payment.amount_usd.normalize()})"
    )
    tx_line = (
        f"\n🔗 <b>TXID / Track:</b> <code>{escape_html(payment.tx_hash or payment.authority_track_id)}</code>"
        if (payment.tx_hash or payment.authority_track_id)
        else ""
    )
    note_line = (
        f"\n📝 <b>Note:</b> {escape_html(payment.receipt_note)}"
        if payment.receipt_note
        else ""
    )
    admin_text = (
        f"📥 <b>New Deposit Request #{payment.id}</b>\n\n"
        f"👤 <b>User:</b> <code>{user.telegram_id}</code> ({sanitize_username(user.username)})\n"
        f"🌐 <b>Lang:</b> <code>{escape_html(user.language_code)}</code>\n"
        f"💳 <b>Method:</b> {escape_html(method_label)}\n"
        f"💰 <b>Amount:</b> <b>{escape_html(amount_display)}</b>"
        f"{tx_line}{note_line}"
    )
    kb = build_admin_payment_review_kb(payment.id)

    for admin_id in settings.admin_ids:
        try:
            if payment.receipt_file_id:
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=payment.receipt_file_id,
                    caption=admin_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=admin_id,
                    text=admin_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
        except Exception as exc:
            logger.warning("Failed to notify admin %s for payment %s: %s", admin_id, payment.id, exc)


async def show_deposit_menu(
    event: CallbackQuery | Message,
    state: FSMContext,
    **data: Any,
) -> None:
    """Render region-aware Deposit menu in place."""
    await state.clear()
    tg_user = event.from_user
    _, lang_code, i18n, payment_service, _, _ = await _resolve_user_and_services(tg_user, data)

    methods = await payment_service.get_available_methods_for_language(lang_code)
    title = await i18n.get_text("deposit.choose_method", lang_code)
    crypto_label = await i18n.get_text("deposit.crypto_section", lang_code)
    fiat_label = await i18n.get_text("deposit.fiat_section", lang_code)
    back_text = await i18n.get_text("btn.back", lang_code)

    if not methods.crypto_wallets and not (methods.fiat_enabled and methods.fiat_methods):
        no_methods = await i18n.get_text("deposit.no_methods", lang_code)
        title = f"{title}\n\n<i>{escape_html(no_methods)}</i>"

    kb = build_deposit_methods_kb(
        methods,
        crypto_section_label=crypto_label,
        fiat_section_label=fiat_label,
        back_text=back_text,
    )
    await _safe_edit_or_send(event, title, reply_markup=kb)
    if _is_callback_event(event):
        await event.answer()


@router.callback_query(MenuCB.filter(F.action == "deposit"))
async def cb_open_deposit_menu(
    callback: CallbackQuery,
    state: FSMContext,
    **data: Any,
) -> None:
    """Open Deposit menu from Main Menu or Back button."""
    await show_deposit_menu(callback, state, **data)


@router.callback_query(DepCB.filter(F.action == "cancel"))
async def cb_cancel_deposit_flow(
    callback: CallbackQuery,
    state: FSMContext,
    **data: Any,
) -> None:
    """Cancel active deposit prompt and return to Deposit method list."""
    await show_deposit_menu(callback, state, **data)


@router.callback_query(DepCB.filter((F.kind == "crypto") & (F.action == "select")))
async def cb_select_crypto_wallet(
    callback: CallbackQuery,
    callback_data: DepCB,
    state: FSMContext,
    **data: Any,
) -> None:
    """Prompt user for USD deposit amount for the chosen cryptocurrency wallet."""
    _, lang_code, i18n, payment_service, _, _ = await _resolve_user_and_services(
        callback.from_user, data
    )
    try:
        wallet = await payment_service.get_crypto_wallet(callback_data.id)
    except NotFoundError:
        err_text = await i18n.get_text("deposit.no_methods", lang_code)
        await callback.answer(err_text, show_alert=True)
        return

    if not wallet.is_active:
        err_text = await i18n.get_text("deposit.no_methods", lang_code)
        await callback.answer(err_text, show_alert=True)
        return

    await state.update_data(crypto_wallet_id=wallet.id)
    await state.set_state(DepositStates.waiting_crypto_amount)

    min_str = str(wallet.min_deposit_usd.normalize())
    prompt = await i18n.get_text("deposit.enter_amount_usd", lang_code, min_amount=escape_html(min_str))
    cancel_text = await i18n.get_text("btn.cancel", lang_code)
    header = f"🪙 <b>{escape_html(wallet.coin_name)} ({escape_html(wallet.symbol)}) — {escape_html(wallet.network)}</b>\n\n{prompt}"

    await _safe_edit_or_send(
        callback,
        header,
        reply_markup=build_deposit_cancel_kb(cancel_text=cancel_text),
    )
    await callback.answer()


@router.message(DepositStates.waiting_crypto_amount)
async def msg_enter_crypto_amount(
    message: Message,
    state: FSMContext,
    **data: Any,
) -> None:
    """Validate USD amount, display wallet address + QR + fee note, or redirect to crypto gateway."""
    user, lang_code, i18n, payment_service, settings, _ = await _resolve_user_and_services(
        message.from_user, data
    )
    fsm_data = await state.get_data()
    wallet_id = fsm_data.get("crypto_wallet_id")
    if not wallet_id:
        await show_deposit_menu(message, state, **data)
        return

    wallet = await payment_service.get_crypto_wallet(int(wallet_id))
    cancel_text = await i18n.get_text("btn.cancel", lang_code)
    cancel_kb = build_deposit_cancel_kb(cancel_text=cancel_text)

    amount_usd = _parse_positive_decimal(message.text or "")
    if amount_usd is None or amount_usd < wallet.min_deposit_usd:
        min_str = f"${wallet.min_deposit_usd.normalize()}"
        invalid_msg = await i18n.get_text("deposit.invalid_amount", lang_code, min_amount=escape_html(min_str))
        await message.answer(invalid_msg, reply_markup=cancel_kb, parse_mode="HTML")
        return

    if wallet.verification_mode in {
        CryptoVerificationMode.NOWPAYMENTS,
        CryptoVerificationMode.OXAPAY,
    }:
        base_url = settings.MINIAPP_BASE_URL or f"https://{settings.DOMAIN}"
        try:
            _, pay_url = await payment_service.initiate_crypto_gateway_deposit(
                user_id=user.id,
                crypto_wallet_id=wallet.id,
                amount_usd=amount_usd,
                callback_base_url=base_url,
            )
        except ShopBotError as exc:
            await message.answer(escape_html(exc.message), reply_markup=cancel_kb, parse_mode="HTML")
            return

        await state.clear()
        back_text = await i18n.get_text("btn.back", lang_code)
        redirect_text = (
            f"🪙 <b>{escape_html(wallet.coin_name)} ({escape_html(wallet.symbol)})</b>\n"
            f"💵 <b>${escape_html(str(amount_usd.normalize()))} USD</b>"
        )
        await message.answer(
            redirect_text,
            reply_markup=build_gateway_redirect_kb(payment_url=pay_url, back_text=back_text),
            parse_mode="HTML",
        )
        return

    await state.update_data(amount_usd=str(amount_usd))
    await state.set_state(DepositStates.waiting_crypto_txid_or_receipt)

    localized_instr = (
        wallet.instructions_json.get(lang_code)
        or wallet.instructions_json.get(DEFAULT_LANGUAGE)
        or wallet.instructions
        or "-"
    )
    details_text = await i18n.get_text(
        "deposit.crypto_instructions",
        lang_code,
        coin_name=escape_html(wallet.coin_name),
        symbol=escape_html(wallet.symbol),
        network=escape_html(wallet.network),
        amount_usd=escape_html(str(amount_usd.normalize())),
        wallet_address=escape_html(wallet.wallet_address),
        fee_note=escape_html(wallet.fee_note or "-"),
        instructions=escape_html(localized_instr),
    )
    tx_prompt = await i18n.get_text("deposit.send_txid_or_receipt", lang_code)
    full_caption = f"{details_text}\n\n{tx_prompt}"

    try:
        qr_bytes = await payment_service.get_wallet_qr_bytes(wallet.id)
        qr_file = BufferedInputFile(qr_bytes, filename=f"{wallet.symbol}_{wallet.network}_qr.png")
        await message.answer_photo(
            photo=qr_file,
            caption=full_caption,
            reply_markup=cancel_kb,
            parse_mode="HTML",
        )
    except Exception:
        await message.answer(full_caption, reply_markup=cancel_kb, parse_mode="HTML")


@router.message(DepositStates.waiting_crypto_txid_or_receipt)
async def msg_submit_crypto_proof(
    message: Message,
    state: FSMContext,
    **data: Any,
) -> None:
    """Validate uploaded receipt image or TXID string, enforce uniqueness & cooldown, notify admins."""
    user, lang_code, i18n, payment_service, settings, redis = await _resolve_user_and_services(
        message.from_user, data
    )
    if await _check_deposit_cooldown(redis, user.telegram_id):
        rate_msg = await i18n.get_text("err.rate_limited", lang_code)
        await message.answer(rate_msg, parse_mode="HTML")
        return

    fsm_data = await state.get_data()
    wallet_id = fsm_data.get("crypto_wallet_id")
    raw_amount = fsm_data.get("amount_usd")
    if not wallet_id or not raw_amount:
        await show_deposit_menu(message, state, **data)
        return

    wallet = await payment_service.get_crypto_wallet(int(wallet_id))
    amount_usd = Decimal(str(raw_amount))
    cancel_text = await i18n.get_text("btn.cancel", lang_code)
    cancel_kb = build_deposit_cancel_kb(cancel_text=cancel_text)

    tx_hash: str | None = None
    receipt_file_id: str | None = None
    receipt_local_path: str | None = None
    receipt_note: str | None = None

    if message.photo or message.document:
        try:
            receipt_file_id, receipt_local_path = await _download_and_validate_receipt(message, settings)
        except FileValidationError:
            err_file = await i18n.get_text("err.invalid_file", lang_code)
            await message.answer(err_file, reply_markup=cancel_kb, parse_mode="HTML")
            return
        caption = (getattr(message, "caption", None) or "").strip()
        if caption:
            if len(caption) >= 16 and " " not in caption:
                tx_hash = caption[:256]
            else:
                receipt_note = caption[:500]
    elif message.text:
        cleaned_tx = message.text.strip()
        if len(cleaned_tx) < 6 or len(cleaned_tx) > 256 or any(ch.isspace() for ch in cleaned_tx):
            tx_prompt = await i18n.get_text("deposit.send_txid_or_receipt", lang_code)
            await message.answer(tx_prompt, reply_markup=cancel_kb, parse_mode="HTML")
            return
        tx_hash = cleaned_tx
    else:
        err_file = await i18n.get_text("err.invalid_file", lang_code)
        await message.answer(err_file, reply_markup=cancel_kb, parse_mode="HTML")
        return

    try:
        payment = await payment_service.create_crypto_deposit(
            CryptoDepositCreateDTO(
                user_id=user.id,
                crypto_wallet_id=wallet.id,
                amount_usd=amount_usd,
                tx_hash=tx_hash,
                receipt_file_id=receipt_file_id,
                receipt_local_path=receipt_local_path,
                receipt_note=receipt_note,
            )
        )
    except DuplicateTransactionError:
        dup_msg = await i18n.get_text("deposit.duplicate_txid", lang_code)
        await message.answer(dup_msg, reply_markup=cancel_kb, parse_mode="HTML")
        return
    except ShopBotError as exc:
        await message.answer(escape_html(exc.message), reply_markup=cancel_kb, parse_mode="HTML")
        return

    await _set_deposit_cooldown(redis, user.telegram_id, settings.DEPOSIT_COOLDOWN_SECONDS)
    await state.clear()

    if (
        payment.tx_hash
        and wallet.verification_mode in {CryptoVerificationMode.TRONGRID, CryptoVerificationMode.BSCSCAN}
    ):
        try:
            payment = await payment_service.verify_crypto_deposit_onchain(payment.id)
        except Exception as exc:
            logger.warning("Auto on-chain verification skipped for payment %s: %s", payment.id, exc)

    back_text = await i18n.get_text("btn.main_menu", lang_code)
    back_kb = build_back_kb(text=back_text, callback_data=MenuCB(action="main"))

    if payment.status == PaymentStatus.APPROVED:
        approved_msg = await i18n.get_text(
            "deposit.approved_notify",
            lang_code,
            amount=escape_html(f"${payment.amount_usd.normalize()}"),
        )
        await message.answer(approved_msg, reply_markup=back_kb, parse_mode="HTML")
        return

    pending_msg = await i18n.get_text("deposit.submitted_pending", lang_code, payment_id=payment.id)
    await message.answer(pending_msg, reply_markup=back_kb, parse_mode="HTML")
    await _notify_admins_new_deposit(
        message.bot,
        settings,
        user,
        payment,
        f"{wallet.coin_name} ({wallet.symbol} - {wallet.network})",
    )


@router.callback_query(DepCB.filter((F.kind == "fiat") & (F.action == "select")))
async def cb_select_fiat_method(
    callback: CallbackQuery,
    callback_data: DepCB,
    state: FSMContext,
    **data: Any,
) -> None:
    """Strictly allow Iranian fiat deposit selection only for Persian ('fa') users."""
    _, lang_code, i18n, payment_service, _, _ = await _resolve_user_and_services(
        callback.from_user, data
    )
    if lang_code not in FIAT_ENABLED_LANGUAGES:
        no_methods = await i18n.get_text("deposit.no_methods", lang_code)
        await callback.answer(no_methods, show_alert=True)
        await show_deposit_menu(callback, state, **data)
        return

    try:
        method = await payment_service.get_fiat_method(callback_data.id)
    except NotFoundError:
        no_methods = await i18n.get_text("deposit.no_methods", lang_code)
        await callback.answer(no_methods, show_alert=True)
        return

    if not method.is_active:
        no_methods = await i18n.get_text("deposit.no_methods", lang_code)
        await callback.answer(no_methods, show_alert=True)
        return

    await state.update_data(fiat_method_id=method.id)
    await state.set_state(DepositStates.waiting_fiat_amount)

    min_irt_str = f"{int(method.min_deposit_irt):,}"
    prompt = await i18n.get_text("deposit.enter_amount_irt", lang_code, min_amount=escape_html(min_irt_str))
    cancel_text = await i18n.get_text("btn.cancel", lang_code)
    header = f"🏦 <b>{escape_html(method.title)}</b>\n\n{prompt}"

    await _safe_edit_or_send(
        callback,
        header,
        reply_markup=build_deposit_cancel_kb(cancel_text=cancel_text),
    )
    await callback.answer()


@router.message(DepositStates.waiting_fiat_amount)
async def msg_enter_fiat_amount(
    message: Message,
    state: FSMContext,
    **data: Any,
) -> None:
    """Handle Toman amount entry for Card-to-Card or Iranian Payment Gateway (fa only)."""
    user, lang_code, i18n, payment_service, settings, _ = await _resolve_user_and_services(
        message.from_user, data
    )
    if lang_code not in FIAT_ENABLED_LANGUAGES:
        await show_deposit_menu(message, state, **data)
        return

    fsm_data = await state.get_data()
    method_id = fsm_data.get("fiat_method_id")
    if not method_id:
        await show_deposit_menu(message, state, **data)
        return

    method = await payment_service.get_fiat_method(int(method_id))
    cancel_text = await i18n.get_text("btn.cancel", lang_code)
    cancel_kb = build_deposit_cancel_kb(cancel_text=cancel_text)

    amount_irt = _parse_positive_decimal(message.text or "")
    if amount_irt is None or amount_irt < method.min_deposit_irt:
        min_str = f"{int(method.min_deposit_irt):,} IRT"
        invalid_msg = await i18n.get_text("deposit.invalid_amount", lang_code, min_amount=escape_html(min_str))
        await message.answer(invalid_msg, reply_markup=cancel_kb, parse_mode="HTML")
        return

    if method.method_type == FiatMethodType.GATEWAY:
        base_url = settings.MINIAPP_BASE_URL or f"https://{settings.DOMAIN}"
        try:
            _, payment_url = await payment_service.initiate_fiat_gateway_deposit(
                user_id=user.id,
                fiat_method_id=method.id,
                amount_irt=amount_irt,
                callback_base_url=base_url,
            )
        except ShopBotError as exc:
            await message.answer(escape_html(exc.message), reply_markup=cancel_kb, parse_mode="HTML")
            return

        await state.clear()
        redirect_msg = await i18n.get_text(
            "deposit.gateway_redirect",
            lang_code,
            amount_irt=escape_html(f"{int(amount_irt):,}"),
        )
        back_text = await i18n.get_text("btn.back", lang_code)
        await message.answer(
            redirect_msg,
            reply_markup=build_gateway_redirect_kb(
                payment_url=payment_url,
                pay_button_text="🌐 پرداخت آنلاین",
                back_text=back_text,
            ),
            parse_mode="HTML",
        )
        return

    await state.update_data(amount_irt=str(amount_irt))
    await state.set_state(DepositStates.waiting_fiat_receipt)

    card_text = await i18n.get_text(
        "deposit.fiat_card_instructions",
        lang_code,
        title=escape_html(method.title),
        amount_irt=escape_html(f"{int(amount_irt):,}"),
        card_number=escape_html(method.card_number or "-"),
        sheba=escape_html(method.sheba or "-"),
        cardholder=escape_html(method.cardholder_name or "-"),
        bank_name=escape_html(method.bank_name or "-"),
        instructions=escape_html(method.instructions or "-"),
    )
    receipt_prompt = await i18n.get_text("deposit.send_fiat_receipt", lang_code)
    await message.answer(
        f"{card_text}\n\n{receipt_prompt}",
        reply_markup=cancel_kb,
        parse_mode="HTML",
    )


@router.message(DepositStates.waiting_fiat_receipt)
async def msg_submit_fiat_receipt(
    message: Message,
    state: FSMContext,
    **data: Any,
) -> None:
    """Validate Card-to-Card receipt image or bank tracking code and queue for admin approval."""
    user, lang_code, i18n, payment_service, settings, redis = await _resolve_user_and_services(
        message.from_user, data
    )
    if lang_code not in FIAT_ENABLED_LANGUAGES:
        await show_deposit_menu(message, state, **data)
        return

    if await _check_deposit_cooldown(redis, user.telegram_id):
        rate_msg = await i18n.get_text("err.rate_limited", lang_code)
        await message.answer(rate_msg, parse_mode="HTML")
        return

    fsm_data = await state.get_data()
    method_id = fsm_data.get("fiat_method_id")
    raw_amount = fsm_data.get("amount_irt")
    if not method_id or not raw_amount:
        await show_deposit_menu(message, state, **data)
        return

    method = await payment_service.get_fiat_method(int(method_id))
    amount_irt = Decimal(str(raw_amount))
    cancel_text = await i18n.get_text("btn.cancel", lang_code)
    cancel_kb = build_deposit_cancel_kb(cancel_text=cancel_text)

    authority_track_id: str | None = None
    receipt_file_id: str | None = None
    receipt_local_path: str | None = None
    receipt_note: str | None = None

    if message.photo or message.document:
        try:
            receipt_file_id, receipt_local_path = await _download_and_validate_receipt(message, settings)
        except FileValidationError:
            err_file = await i18n.get_text("err.invalid_file", lang_code)
            await message.answer(err_file, reply_markup=cancel_kb, parse_mode="HTML")
            return
        caption = (getattr(message, "caption", None) or "").strip()
        if caption:
            if len(caption) >= 4 and " " not in caption:
                authority_track_id = caption[:128]
            else:
                receipt_note = caption[:500]
    elif message.text:
        cleaned_track = message.text.strip()
        if len(cleaned_track) < 4 or len(cleaned_track) > 128:
            prompt = await i18n.get_text("deposit.send_fiat_receipt", lang_code)
            await message.answer(prompt, reply_markup=cancel_kb, parse_mode="HTML")
            return
        authority_track_id = cleaned_track
    else:
        err_file = await i18n.get_text("err.invalid_file", lang_code)
        await message.answer(err_file, reply_markup=cancel_kb, parse_mode="HTML")
        return

    try:
        payment = await payment_service.create_fiat_deposit(
            FiatDepositCreateDTO(
                user_id=user.id,
                fiat_method_id=method.id,
                amount_irt=amount_irt,
                payment_category=PaymentCategory.FIAT_CARD,
                authority_track_id=authority_track_id,
                receipt_file_id=receipt_file_id,
                receipt_local_path=receipt_local_path,
                receipt_note=receipt_note,
            )
        )
    except DuplicateTransactionError:
        dup_msg = await i18n.get_text("deposit.duplicate_txid", lang_code)
        await message.answer(dup_msg, reply_markup=cancel_kb, parse_mode="HTML")
        return
    except ShopBotError as exc:
        await message.answer(escape_html(exc.message), reply_markup=cancel_kb, parse_mode="HTML")
        return

    await _set_deposit_cooldown(redis, user.telegram_id, settings.DEPOSIT_COOLDOWN_SECONDS)
    await state.clear()

    back_text = await i18n.get_text("btn.main_menu", lang_code)
    back_kb = build_back_kb(text=back_text, callback_data=MenuCB(action="main"))
    pending_msg = await i18n.get_text("deposit.submitted_pending", lang_code, payment_id=payment.id)
    await message.answer(pending_msg, reply_markup=back_kb, parse_mode="HTML")
    await _notify_admins_new_deposit(
        message.bot,
        settings,
        user,
        payment,
        f"Card-to-Card: {method.title}",
    )


@router.callback_query(PayCB.filter(F.action.in_({"approve", "reject"})))
async def cb_admin_review_deposit_notification(
    callback: CallbackQuery,
    callback_data: PayCB,
    **data: Any,
) -> None:
    """Handle 1-click Admin Approve or Reject on instant deposit notifications."""
    caller, _, i18n, payment_service, settings, _ = await _resolve_user_and_services(
        callback.from_user, data
    )
    is_admin = bool(data.get("is_admin")) or (callback.from_user.id in settings.admin_ids) or caller.is_admin
    if not is_admin:
        await callback.answer("⛔ Access denied", show_alert=True)
        return

    approve = callback_data.action == "approve"
    try:
        reviewed = await payment_service.review_payment(
            PaymentReviewDTO(
                payment_id=callback_data.id,
                admin_id=callback.from_user.id,
                approve=approve,
                admin_note="Approved via Bot" if approve else "Rejected via Bot",
            )
        )
    except InvalidStateTransitionError as exc:
        await callback.answer(exc.message, show_alert=True)
        return
    except NotFoundError as exc:
        await callback.answer(exc.message, show_alert=True)
        return

    badge = "✅ APPROVED" if approve else "❌ REJECTED"
    if callback.message is not None:
        status_line = f"\n\n<b>{badge}</b> (#{reviewed.id})"
        try:
            if getattr(callback.message, "photo", None) and callback.message.caption:
                await callback.message.edit_caption(
                    caption=f"{callback.message.caption}{status_line}",
                    reply_markup=None,
                    parse_mode="HTML",
                )
            elif getattr(callback.message, "text", None):
                await callback.message.edit_text(
                    text=f"{callback.message.text}{status_line}",
                    reply_markup=None,
                    parse_mode="HTML",
                )
            else:
                await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    session_factory = data.get("session_factory") or get_session_factory()
    async with session_factory() as session:
        target_user = await session.get(User, reviewed.user_id)

    if target_user is not None and callback.bot is not None:
        u_lang = TranslationService.normalize_lang(target_user.language_code)
        try:
            if approve:
                amt = (
                    f"${reviewed.amount_usd.normalize()}"
                    if reviewed.currency == Currency.USD
                    else f"{int(reviewed.amount_irt):,} IRT"
                )
                dm_text = await i18n.get_text("deposit.approved_notify", u_lang, amount=escape_html(amt))
            else:
                dm_text = await i18n.get_text(
                    "deposit.rejected_notify",
                    u_lang,
                    reason=escape_html(reviewed.admin_note or "-"),
                )
            await callback.bot.send_message(chat_id=target_user.telegram_id, text=dm_text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("Failed to DM customer %s after deposit review: %s", target_user.telegram_id, exc)

    await callback.answer(f"{badge} (#{reviewed.id})", show_alert=False)
