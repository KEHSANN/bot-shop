"""Customer wallet balance, recent ledger entries, and deposit/order shortcuts."""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers import format_irt, format_usd, safe_edit_or_send
from app.bot.keyboards.common_kb import MenuCB, OrderCB, build_back_button
from app.core.constants import Currency, DEFAULT_LANGUAGE
from app.core.security import escape_html
from app.locales.i18n import TranslationService
from app.services.dtos import UserDTO
from app.services.interfaces import UserServiceProtocol

router = Router(name="customer_balance")


async def _render_balance_screen(
    target: Message | CallbackQuery,
    *,
    user: UserDTO,
    user_service: UserServiceProtocol,
    i18n: TranslationService,
    lang: str,
) -> None:
    fresh_user = await user_service.get_by_id(user.id)
    active_lang = i18n.normalize_lang(lang or fresh_user.language_code)

    balance_irt_line = ""
    if active_lang == "fa":
        balance_irt_line = await i18n.get_text(
            "welcome.balance_irt_line",
            active_lang,
            balance_irt=format_irt(fresh_user.balance_irt),
        )

    title_block = await i18n.get_text(
        "balance.title",
        active_lang,
        balance_usd=format_usd(fresh_user.balance_usd),
        balance_irt_line=balance_irt_line,
    )

    history = await user_service.get_wallet_history(fresh_user.id, limit=10)
    if history:
        tx_heading = await i18n.get_text("balance.recent_transactions", active_lang)
        lines: list[str] = [tx_heading]
        for entry in history:
            sign = "+" if entry.change_amount >= Decimal("0") else "-"
            abs_val = abs(entry.change_amount)
            if entry.currency == Currency.USD:
                amt_text = f"${format_usd(abs_val)}"
            else:
                amt_text = f"{format_irt(abs_val)} IRT"
            stamp = entry.created_at.strftime("%Y-%m-%d %H:%M")
            ref_label = escape_html(entry.reference_type.value)
            note_suffix = f" — <i>{escape_html(entry.note)}</i>" if entry.note else ""
            lines.append(f"• <code>{stamp}</code> | <b>{sign}{amt_text}</b> ({ref_label}){note_suffix}")
        body_text = f"{title_block}\n\n" + "\n".join(lines)
    else:
        no_tx = await i18n.get_text("balance.no_transactions", active_lang)
        body_text = f"{title_block}\n\n{no_tx}"

    deposit_label = await i18n.get_text("btn.deposit", active_lang)
    orders_label = await i18n.get_text("btn.order_history", active_lang)
    back_label = await i18n.get_text("btn.back", active_lang)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=deposit_label,
                    callback_data=MenuCB(action="deposit").pack(),
                ),
                InlineKeyboardButton(
                    text=orders_label,
                    callback_data=OrderCB(id=0, action="list").pack(),
                ),
            ],
            [build_back_button(text=back_label, callback_data=MenuCB(action="main"))],
        ]
    )
    await safe_edit_or_send(target, body_text, reply_markup=kb)


@router.message(Command("balance", "wallet"))
async def cmd_balance(
    message: Message,
    user: UserDTO,
    user_service: UserServiceProtocol,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    state: FSMContext | None = None,
) -> None:
    """Display wallet balances and recent ledger history via command."""
    if state is not None:
        await state.clear()
    await _render_balance_screen(
        message,
        user=user,
        user_service=user_service,
        i18n=i18n,
        lang=lang,
    )


@router.callback_query(MenuCB.filter(F.action == "balance"))
async def cb_balance(
    callback: CallbackQuery,
    user: UserDTO,
    user_service: UserServiceProtocol,
    i18n: TranslationService,
    lang: str = DEFAULT_LANGUAGE,
    state: FSMContext | None = None,
) -> None:
    """Display wallet balances and recent ledger history in-place."""
    if state is not None:
        await state.clear()
    await _render_balance_screen(
        callback,
        user=user,
        user_service=user_service,
        i18n=i18n,
        lang=lang,
    )
