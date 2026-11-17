"""Application configuration loaded strictly from environment / .env via pydantic-settings."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import quote_plus

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.exceptions import ConfigurationError


class Settings(BaseSettings):
    """Runtime settings for Bot, Web Admin, PostgreSQL, and Redis."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Application
    APP_ENV: Literal["development", "production", "test"] = "development"
    APP_NAME: str = "Telegram Digital Shop Bot"
    DEBUG: bool = False
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    DOMAIN: str = "localhost"
    LOG_LEVEL: str = "INFO"
    DEFAULT_LANGUAGE: str = "en"
    TZ: str = "UTC"

    # Telegram Bot
    BOT_TOKEN: str = Field(default="", repr=False)
    BOT_MODE: Literal["polling", "webhook"] = "polling"
    BOT_WEBHOOK_URL: str = ""
    BOT_WEBHOOK_PATH: str = "/webhook/telegram"
    BOT_WEBHOOK_SECRET: str = Field(default="", repr=False)
    TELEGRAM_BOT_USERNAME: str = ""
    MINIAPP_BASE_URL: str = Field(
        default="",
        validation_alias=AliasChoices("MINIAPP_BASE_URL", "WEBAPP_URL"),
    )

    # Admin & Web Security
    ADMIN_TELEGRAM_IDS: Annotated[list[int], NoDecode] = Field(default_factory=list)
    WEB_ADMIN_USERNAME: str = "admin"
    WEB_ADMIN_PASSWORD_HASH: str = Field(default="", repr=False)
    JWT_SECRET_KEY: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices("JWT_SECRET_KEY", "ADMIN_SECRET_KEY"),
    )
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 720
    CSRF_SECRET_KEY: str = Field(default="", repr=False)
    ENCRYPTION_KEY: str = Field(default="", repr=False)
    TWO_FACTOR_ENABLED: bool = False
    TOTP_SECRET: str = Field(default="", repr=False)
    TELEGRAM_LOGIN_WIDGET_ENABLED: bool = True

    # Database & Redis
    DATABASE_URL: str = Field(default="", repr=False)
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "telegram_shop"
    POSTGRES_USER: str = "shopbot"
    POSTGRES_PASSWORD: str = Field(default="", repr=False)

    REDIS_URL: str = Field(default="", repr=False)
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = Field(default="", repr=False)

    # Financial, Rate-Limit, Broadcast & Upload Constraints
    USD_TO_TOMAN_RATE: Decimal = Decimal("65000")
    RATE_MODE: Literal["manual", "live"] = "manual"
    RATE_LIMIT_MAX_REQUESTS: int = 5
    RATE_LIMIT_WINDOW_SECONDS: int = 3
    DEPOSIT_COOLDOWN_SECONDS: int = 10
    SUPPORT_COOLDOWN_SECONDS: int = 5
    BROADCAST_RATE_PER_SEC: int = 25
    UPLOAD_DIR: str = "uploads"
    MAX_UPLOAD_SIZE_MB: int = 5
    MAX_UPLOAD_SIZE_BYTES: int = 5 * 1024 * 1024

    # Optional Payment Gateway & Blockchain Explorer Keys
    ZARINPAL_MERCHANT_ID: str = Field(default="", repr=False)
    ZARINPAL_SANDBOX: bool = True
    ZIBAL_MERCHANT: str = Field(default="", repr=False)
    NEXTPAY_API_KEY: str = Field(default="", repr=False)
    IDPAY_API_KEY: str = Field(default="", repr=False)
    TRONGRID_API_KEY: str = Field(default="", repr=False)
    BSCSCAN_API_KEY: str = Field(default="", repr=False)
    NOWPAYMENTS_API_KEY: str = Field(default="", repr=False)
    NOWPAYMENTS_IPN_SECRET: str = Field(default="", repr=False)
    OXAPAY_MERCHANT_API_KEY: str = Field(default="", repr=False)

    @field_validator("ADMIN_TELEGRAM_IDS", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: Any) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [int(v) for v in value if str(v).strip()]
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                parsed = json.loads(raw)
                return [int(v) for v in parsed]
            return [int(part.strip()) for part in re.split(r"[,\s]+", raw) if part.strip()]
        raise ValueError("ADMIN_TELEGRAM_IDS must be a list of integers or comma-separated string")

    @field_validator("USD_TO_TOMAN_RATE")
    @classmethod
    def _validate_rate(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= Decimal("0"):
            raise ValueError("USD_TO_TOMAN_RATE must be a positive finite Decimal")
        return value

    @model_validator(mode="after")
    def _assemble_urls(self) -> "Settings":
        if not self.DATABASE_URL and self.POSTGRES_PASSWORD:
            user = quote_plus(self.POSTGRES_USER)
            pwd = quote_plus(self.POSTGRES_PASSWORD)
            self.DATABASE_URL = (
                f"postgresql+asyncpg://{user}:{pwd}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
            )
        if not self.REDIS_URL:
            if self.REDIS_PASSWORD:
                pwd = quote_plus(self.REDIS_PASSWORD)
                self.REDIS_URL = f"redis://:{pwd}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
            else:
                self.REDIS_URL = f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        if self.MAX_UPLOAD_SIZE_MB > 0:
            self.MAX_UPLOAD_SIZE_BYTES = self.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        return self

    @property
    def admin_ids(self) -> list[int]:
        """Return list of configured admin Telegram IDs."""
        return list(self.ADMIN_TELEGRAM_IDS)

    @property
    def webapp_url(self) -> str:
        """Return configured Telegram Mini App URL."""
        return self.MINIAPP_BASE_URL

    @property
    def effective_csrf_secret(self) -> str:
        """Return CSRF secret key, falling back to JWT_SECRET_KEY when dedicated CSRF secret is unset."""
        return self.CSRF_SECRET_KEY or self.JWT_SECRET_KEY

    def validate_runtime_secrets(self, *, require_bot_token: bool = True) -> None:
        """Validate mandatory security secrets before starting services."""
        missing: list[str] = []
        if not self.DATABASE_URL:
            missing.append("DATABASE_URL")
        if not self.REDIS_URL:
            missing.append("REDIS_URL")
        if require_bot_token and not self.BOT_TOKEN:
            missing.append("BOT_TOKEN")
        if not self.JWT_SECRET_KEY or len(self.JWT_SECRET_KEY) < 32:
            missing.append("JWT_SECRET_KEY (min 32 chars)")
        if not self.ENCRYPTION_KEY:
            missing.append("ENCRYPTION_KEY")
        if missing:
            raise ConfigurationError(f"Missing or weak runtime configuration: {', '.join(missing)}")

        try:
            Fernet(self.ENCRYPTION_KEY.encode("utf-8"))
        except Exception as exc:
            raise ConfigurationError("ENCRYPTION_KEY must be a valid base64 Fernet key") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance loaded from .env and process environment."""
    return Settings()


def clear_settings_cache() -> None:
    """Clear cached Settings instance (primarily for test isolation)."""
    get_settings.cache_clear()
