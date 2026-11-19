"""Global constants, 12-language metadata, dual-layer color palette, and domain enums."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, slots=True)
class LanguageInfo:
    """Metadata for one supported UI/region language."""

    code: str
    native_name: str
    english_name: str
    flag: str
    is_rtl: bool
    supports_fiat_irr: bool


SUPPORTED_LANGUAGES: dict[str, LanguageInfo] = {
    "fa": LanguageInfo("fa", "فارسی", "Persian", "🇮🇷", is_rtl=True, supports_fiat_irr=True),
    "en": LanguageInfo("en", "English", "English", "🇺🇸", is_rtl=False, supports_fiat_irr=False),
    "ar": LanguageInfo("ar", "العربية", "Arabic", "🇸🇦", is_rtl=True, supports_fiat_irr=False),
    "hi": LanguageInfo("hi", "हिन्दी", "Hindi", "🇮🇳", is_rtl=False, supports_fiat_irr=False),
    "ru": LanguageInfo("ru", "Русский", "Russian", "🇷🇺", is_rtl=False, supports_fiat_irr=False),
    "de": LanguageInfo("de", "Deutsch", "German", "🇩🇪", is_rtl=False, supports_fiat_irr=False),
    "zh": LanguageInfo("zh", "简体中文", "Chinese (Simplified)", "🇨🇳", is_rtl=False, supports_fiat_irr=False),
    "es": LanguageInfo("es", "Español", "Spanish", "🇪🇸", is_rtl=False, supports_fiat_irr=False),
    "pt": LanguageInfo("pt", "Português", "Portuguese (Brazil)", "🇧🇷", is_rtl=False, supports_fiat_irr=False),
    "id": LanguageInfo("id", "Bahasa Indonesia", "Indonesian", "🇮🇩", is_rtl=False, supports_fiat_irr=False),
    "fr": LanguageInfo("fr", "Français", "French", "🇫🇷", is_rtl=False, supports_fiat_irr=False),
    "tr": LanguageInfo("tr", "Türkçe", "Turkish", "🇹🇷", is_rtl=False, supports_fiat_irr=False),
}

LANGUAGE_ORDER: tuple[str, ...] = (
    "fa",
    "en",
    "ar",
    "hi",
    "ru",
    "de",
    "zh",
    "es",
    "pt",
    "id",
    "fr",
    "tr",
)

DEFAULT_LANGUAGE: str = "en"
FIAT_ENABLED_LANGUAGES: frozenset[str] = frozenset({"fa"})


@dataclass(frozen=True, slots=True)
class ColorPreset:
    """Dual-layer color preset pairing a Telegram emoji indicator with a CSS Hex code."""

    key: str
    emoji: str
    hex_code: str
    label: str


COLOR_PALETTE: dict[str, ColorPreset] = {
    "green": ColorPreset("green", "🟢", "#22c55e", "Green"),
    "red": ColorPreset("red", "🔴", "#ef4444", "Red"),
    "yellow": ColorPreset("yellow", "🟡", "#eab308", "Yellow"),
    "blue": ColorPreset("blue", "🔵", "#3b82f6", "Blue"),
    "purple": ColorPreset("purple", "🟣", "#a855f7", "Purple"),
    "orange": ColorPreset("orange", "🟠", "#f97316", "Orange"),
    "black": ColorPreset("black", "⚫", "#1f2937", "Black"),
    "white": ColorPreset("white", "⚪", "#f8fafc", "White"),
}

COLOR_EMOJI_CHOICES: tuple[str, ...] = tuple(p.emoji for p in COLOR_PALETTE.values())

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def is_valid_hex_color(value: str) -> bool:
    """Return True if value matches strict #RRGGBB hex notation."""
    return bool(isinstance(value, str) and _HEX_COLOR_RE.match(value.strip()))


def normalize_hex_color(value: str | None, default: str = "#3b82f6") -> str:
    """Normalize a hex color string to lowercase #rrggbb or raise ValueError if invalid."""
    if value is None or not value.strip():
        return default
    cleaned = value.strip()
    if not _HEX_COLOR_RE.match(cleaned):
        raise ValueError(f"Invalid hex color '{value}'; expected #RRGGBB")
    return cleaned.lower()


# Strict 5-button Main Menu specification (plus admin-pinned featured products above Row 1)
MAIN_MENU_BUTTON_KEYS: tuple[str, ...] = ("shop", "deposit", "balance", "support", "language")
FORBIDDEN_MAIN_MENU_FEATURES: tuple[str, ...] = (
    "referral",
    "status",
    "daily_top_invites",
    "device_verification",
    "api_docs",
)

MAX_RECEIPT_FILE_BYTES: int = 5 * 1024 * 1024  # 5 MB
ALLOWED_RECEIPT_MIME_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})


class Currency(str, Enum):
    USD = "USD"
    IRT = "IRT"


class DeliveryType(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


class StockStatus(str, Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    DELIVERED = "delivered"


class OrderStatus(str, Enum):
    PENDING_INPUT = "pending_input"
    PENDING_FULFILLMENT = "pending_fulfillment"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentCategory(str, Enum):
    CRYPTO = "crypto"
    FIAT_CARD = "fiat_card"
    FIAT_GATEWAY = "fiat_gateway"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class CryptoVerificationMode(str, Enum):
    MANUAL_TXID = "manual_txid"
    TRONGRID = "trongrid"
    BSCSCAN = "bscscan"
    NOWPAYMENTS = "nowpayments"
    OXAPAY = "oxapay"


class FiatMethodType(str, Enum):
    CARD_TO_CARD = "card_to_card"
    GATEWAY = "gateway"


class WalletLedgerRefType(str, Enum):
    DEPOSIT_CRYPTO = "deposit_crypto"
    DEPOSIT_FIAT = "deposit_fiat"
    ORDER_PURCHASE = "order_purchase"
    ORDER_REFUND = "order_refund"
    ADMIN_CHARGE = "admin_charge"
    ADMIN_DEDUCT = "admin_deduct"


class TicketCategory(str, Enum):
    DEPOSIT_ISSUE = "deposit_issue"
    OTHER_ISSUE = "other_issue"


class TicketStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"


class SenderRole(str, Enum):
    USER = "user"
    ADMIN = "admin"


class MenuScope(str, Enum):
    MAIN = "main"
    SHOP = "shop"
    DEPOSIT = "deposit"
    BALANCE = "balance"
    SUPPORT = "support"
    LANGUAGE = "language"


class BroadcastStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SettingKey(str, Enum):
    USD_TO_TOMAN_RATE = "usd_to_toman_rate"
    RATE_MODE = "rate_mode"
    MAINTENANCE_MODE = "maintenance_mode"
    PINNED_LIMIT = "pinned_limit"
    LOW_STOCK_DEFAULT_THRESHOLD = "low_stock_default_threshold"
    GUIDE_URL = "guide_url"
    FORCE_JOIN_ENABLED = "force_join_enabled"
    FORCE_JOIN_CHANNELS = "force_join_channels"
