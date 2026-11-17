"""Shared aiogram CallbackData contracts and core customer inline keyboards."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import field_validator

from app.core.constants import (
    DEFAULT_LANGUAGE,
    FORBIDDEN_MAIN_MENU_FEATURES,
    LANGUAGE_ORDER,
    MAIN_MENU_BUTTON_KEYS,
    SUPPORTED_LANGUAGES,
)
from app.locales.seed_translations import TRANSLATIONS
from app.services.dtos import ProductDTO, UIButtonDTO


class MenuCB(CallbackData, prefix="menu"):
    """Top-level navigation callback (`main`, `shop`, `deposit`, `balance`, `support`, `lang`, `orders`, `noop`)."""

    action: str


class LangCB(CallbackData, prefix="lang"):
    """Language selection callback for the 12 supported locales."""

    code: str


class CatCB(CallbackData, prefix="cat"):
    """Category navigation callback with pagination support."""

    id: int
    page: int = 0


class ProdCB(CallbackData, prefix="prod"):
    """Product detail, quantity selection, and direct wallet purchase callback."""

    id: int
    action: str = "view"


class QtyCB(CallbackData, prefix="qty"):
    """Quick quantity preset callback (`1`, `5`, `10`, etc.)."""

    prod_id: int
    qty: int


class OrderCB(CallbackData, prefix="ord"):
    """Order list and single-order detail callback."""

    id: int = 0
    action: str = "view"


class DepCB(CallbackData, prefix="dep"):
    """Deposit method selection and action callback (`crypto` | `fiat`)."""

    kind: str
    id: int = 0
    action: str = "select"


class PayCB(CallbackData, prefix="pay"):
    """Payment review callback (`approve`, `reject`, `view`)."""

    id: int
    action: str = "view"


class SupCB(CallbackData, prefix="sup"):
    """Support ticket creation, viewing, replying, and closing callback."""

    action: str
    id: int = 0
    cat: str = ""

    @field_validator("cat", mode="before")
    @classmethod
    def _coerce_none_cat(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value)


class AdmCB(CallbackData, prefix="adm"):
    """In-bot /admin section, action, target ID, and pagination callback."""

    sec: str
    act: str = "list"
    id: int = 0
    page: int = 0


_MAIN_BUTTON_ACTION_MAP: dict[str, str] = {
    "shop": "shop",
    "deposit": "deposit",
    "balance": "balance",
    "support": "support",
    "language": "lang",
}


def _pack_callback(callback_data: CallbackData | str) -> str:
    if isinstance(callback_data, CallbackData):
        return callback_data.pack()
    return str(callback_data)


def _normalize_lang(lang_code: str | None) -> str:
    if not lang_code:
        return DEFAULT_LANGUAGE
    cleaned = lang_code.strip().lower().split("-")[0].split("_")[0]
    return cleaned if cleaned in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def _fallback_button_label(button_key: str, lang_code: str, labels: Mapping[str, str] | None = None) -> str:
    if labels:
        if f"btn.{button_key}" in labels and labels[f"btn.{button_key}"]:
            return labels[f"btn.{button_key}"]
        if button_key in labels and labels[button_key]:
            return labels[button_key]
    seed_entry = TRANSLATIONS.get(f"btn.{button_key}")
    if seed_entry:
        return seed_entry.get(lang_code) or seed_entry.get(DEFAULT_LANGUAGE) or button_key.title()
    return button_key.title()


def build_back_button(
    text: str = "🔙 Back",
    callback_data: CallbackData | str | None = None,
) -> InlineKeyboardButton:
    """Create a single inline Back button targeting parent callback data or main menu."""
    target = _pack_callback(callback_data) if callback_data is not None else MenuCB(action="main").pack()
    return InlineKeyboardButton(text=text, callback_data=target)


def build_back_kb(
    text: str = "🔙 Back",
    callback_data: CallbackData | str | None = None,
    *,
    main_menu_text: str | None = None,
) -> InlineKeyboardMarkup:
    """Create a single-row Back keyboard, optionally paired with a Main Menu shortcut."""
    row = [build_back_button(text=text, callback_data=callback_data)]
    if main_menu_text:
        row.append(InlineKeyboardButton(text=main_menu_text, callback_data=MenuCB(action="main").pack()))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def build_cancel_kb(
    text: str = "❌ Cancel",
    callback_data: CallbackData | str | None = None,
) -> InlineKeyboardMarkup:
    """Create a single-row Cancel keyboard for active FSM input prompts."""
    target = _pack_callback(callback_data) if callback_data is not None else MenuCB(action="main").pack()
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=target)]]
    )


def build_language_kb(
    *,
    current_lang: str | None = None,
    include_back: bool = True,
    back_text: str = "🔙 Back",
    back_callback: CallbackData | str | None = None,
) -> InlineKeyboardMarkup:
    """Build the 12-language 2-column inline keyboard picker."""
    normalized_current = _normalize_lang(current_lang) if current_lang else None
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []

    for code in LANGUAGE_ORDER:
        info = SUPPORTED_LANGUAGES[code]
        prefix = "✅ " if normalized_current == code else ""
        label = f"{prefix}{info.flag} {info.native_name}"
        pair.append(InlineKeyboardButton(text=label, callback_data=LangCB(code=code).pack()))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    if include_back:
        rows.append([build_back_button(text=back_text, callback_data=back_callback or MenuCB(action="main"))])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_main_menu_kb(
    *,
    lang_code: str = DEFAULT_LANGUAGE,
    buttons: Sequence[UIButtonDTO] | None = None,
    pinned_products: Sequence[ProductDTO] | None = None,
    labels: Mapping[str, str] | None = None,
    pinned_limit: int | None = None,
) -> InlineKeyboardMarkup:
    """Build the strictly scoped 5-button Main Menu keyboard with optional pinned products above Row 1."""
    lang = _normalize_lang(lang_code)
    keyboard_rows: list[list[InlineKeyboardButton]] = []

    if pinned_products:
        active_pinned = [p for p in pinned_products if p.is_pinned and p.is_active]
        active_pinned.sort(key=lambda p: (p.sort_order, p.id))
        if pinned_limit is not None and pinned_limit >= 0:
            active_pinned = active_pinned[:pinned_limit]
        for prod in active_pinned:
            title = (
                prod.titles_json.get(lang)
                or prod.titles_json.get(DEFAULT_LANGUAGE)
                or prod.slug
            )
            badge = (prod.color_emoji or "").strip()
            btn_text = title if (not badge or title.startswith(badge)) else f"{badge} {title}"
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text=btn_text,
                        callback_data=ProdCB(id=prod.id, action="view").pack(),
                    )
                ]
            )

    if buttons:
        filtered = [
            b
            for b in buttons
            if b.is_active
            and b.button_key in MAIN_MENU_BUTTON_KEYS
            and b.button_key not in FORBIDDEN_MAIN_MENU_FEATURES
        ]
        filtered.sort(key=lambda b: (b.row_index, b.col_index, b.id))
        grouped: dict[int, list[InlineKeyboardButton]] = defaultdict(list)
        for btn in filtered:
            raw_label = btn.labels_json.get(lang) or btn.labels_json.get(DEFAULT_LANGUAGE)
            if not raw_label:
                raw_label = _fallback_button_label(btn.button_key, lang, labels)
            emoji = (btn.color_emoji or "").strip()
            text = raw_label if (not emoji or raw_label.startswith(emoji)) else f"{emoji} {raw_label}"
            action = _MAIN_BUTTON_ACTION_MAP[btn.button_key]
            grouped[btn.row_index].append(
                InlineKeyboardButton(text=text, callback_data=MenuCB(action=action).pack())
            )
        for row_idx in sorted(grouped.keys()):
            if grouped[row_idx]:
                keyboard_rows.append(grouped[row_idx])

    if not buttons or len(keyboard_rows) == (
        len([p for p in (pinned_products or []) if p.is_pinned and p.is_active][:pinned_limit])
        if pinned_products
        else 0
    ):
        def _btn(key: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(
                text=_fallback_button_label(key, lang, labels),
                callback_data=MenuCB(action=_MAIN_BUTTON_ACTION_MAP[key]).pack(),
            )

        keyboard_rows.extend(
            [
                [_btn("shop")],
                [_btn("deposit"), _btn("balance")],
                [_btn("support"), _btn("language")],
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


__all__ = [
    "AdmCB",
    "CatCB",
    "DepCB",
    "LangCB",
    "MenuCB",
    "OrderCB",
    "PayCB",
    "ProdCB",
    "QtyCB",
    "SupCB",
    "build_back_button",
    "build_back_kb",
    "build_cancel_kb",
    "build_language_kb",
    "build_main_menu_kb",
]
