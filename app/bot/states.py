"""Shared aiogram FSM StatesGroup contracts backed by RedisStorage."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ShopStates(StatesGroup):
    """FSM states for custom quantity entry and manual fulfillment input."""

    waiting_custom_qty = State()
    waiting_manual_input = State()


class DepositStates(StatesGroup):
    """FSM states for crypto and region-aware fiat deposit submission."""

    waiting_crypto_amount = State()
    waiting_crypto_txid_or_receipt = State()
    waiting_fiat_amount = State()
    waiting_fiat_receipt = State()


class SupportStates(StatesGroup):
    """FSM states for customer support ticket creation and live replies."""

    waiting_ticket_message = State()
    waiting_reply_message = State()


class AdminStates(StatesGroup):
    """FSM states for the in-bot /admin management console."""

    waiting_user_search = State()
    waiting_balance_amount = State()
    waiting_balance_note = State()
    waiting_direct_message = State()
    waiting_reject_reason = State()
    waiting_fulfill_note = State()
    waiting_category_input = State()
    waiting_product_input = State()
    waiting_stock_upload = State()
    waiting_wallet_input = State()
    waiting_fiat_input = State()
    waiting_cms_text = State()
    waiting_broadcast_content = State()
    waiting_support_reply = State()
    waiting_exchange_rate = State()


__all__ = [
    "AdminStates",
    "DepositStates",
    "ShopStates",
    "SupportStates",
]
