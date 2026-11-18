"""Inline keyboards for region-aware Deposit flow and Admin payment review notifications."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards.common_kb import DepCB, MenuCB, PayCB, build_back_button
from app.core.constants import FIAT_ENABLED_LANGUAGES, FiatMethodType
from app.services.dtos import AvailablePaymentMethodsDTO, CryptoWalletDTO, FiatMethodDTO


def build_deposit_methods_kb(
    methods: AvailablePaymentMethodsDTO,
    *,
    crypto_section_label: str = "🪙 Cryptocurrency Methods",
    fiat_section_label: str = "🏦 Iranian Rial / Toman",
    back_text: str = "🔙 Back",
) -> InlineKeyboardMarkup:
    """Build region-aware deposit method selector keyboard.

    Strictly hides all Iranian fiat methods unless lang_code == 'fa' and fiat_enabled is True.
    """
    rows: list[list[InlineKeyboardButton]] = []
    show_fiat = (
        methods.fiat_enabled
        and methods.lang_code in FIAT_ENABLED_LANGUAGES
        and len(methods.fiat_methods) > 0
    )
    show_crypto = len(methods.crypto_wallets) > 0

    if show_fiat:
        if show_crypto:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=fiat_section_label,
                        callback_data=MenuCB(action="noop").pack(),
                    )
                ]
            )
        for fiat in methods.fiat_methods:
            if not fiat.is_active:
                continue
            icon = "💳" if fiat.method_type == FiatMethodType.CARD_TO_CARD else "🌐"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {fiat.title}",
                        callback_data=DepCB(kind="fiat", id=fiat.id, action="select").pack(),
                    )
                ]
            )

    if show_crypto:
        if show_fiat:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=crypto_section_label,
                        callback_data=MenuCB(action="noop").pack(),
                    )
                ]
            )
        pair: list[InlineKeyboardButton] = []
        for wallet in methods.crypto_wallets:
            if not wallet.is_active:
                continue
            label = f"🪙 {wallet.symbol} ({wallet.network})"
            pair.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=DepCB(kind="crypto", id=wallet.id, action="select").pack(),
                )
            )
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)

    rows.append([build_back_button(text=back_text, callback_data=MenuCB(action="main"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_deposit_cancel_kb(
    *,
    cancel_text: str = "❌ Cancel",
    back_to_deposit: bool = True,
) -> InlineKeyboardMarkup:
    """Build cancel/back keyboard for active deposit FSM prompts."""
    cb = (
        DepCB(kind="crypto", id=0, action="cancel").pack()
        if back_to_deposit
        else MenuCB(action="main").pack()
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=cancel_text, callback_data=cb)]]
    )


def build_gateway_redirect_kb(
    *,
    payment_url: str,
    pay_button_text: str = "🌐 Pay Online / پرداخت آنلاین",
    back_text: str = "🔙 Back",
) -> InlineKeyboardMarkup:
    """Build external gateway redirect URL button plus Back button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=pay_button_text, url=payment_url)],
            [build_back_button(text=back_text, callback_data=MenuCB(action="deposit"))],
        ]
    )


def build_admin_payment_review_kb(
    payment_id: int,
    *,
    approve_text: str = "✅ Approve & Credit",
    reject_text: str = "❌ Reject",
) -> InlineKeyboardMarkup:
    """Build 1-click Admin Approve / Reject inline keyboard for deposit notifications."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=approve_text,
                    callback_data=PayCB(id=payment_id, action="approve").pack(),
                ),
                InlineKeyboardButton(
                    text=reject_text,
                    callback_data=PayCB(id=payment_id, action="reject").pack(),
                ),
            ]
        ]
    )


__all__ = [
    "CryptoWalletDTO",
    "FiatMethodDTO",
    "build_admin_payment_review_kb",
    "build_deposit_cancel_kb",
    "build_deposit_methods_kb",
    "build_gateway_redirect_kb",
]
