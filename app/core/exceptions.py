"""Domain exception hierarchy shared across services, bot handlers, and web routes."""

from __future__ import annotations

from typing import Any


class ShopBotError(Exception):
    """Base domain exception with machine-readable code and optional metadata."""

    code: str = "shop_bot_error"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}


class ConfigurationError(ShopBotError):
    code = "configuration_error"


class AuthenticationError(ShopBotError):
    code = "authentication_error"


class AuthorizationError(ShopBotError):
    code = "authorization_error"


class UserBlockedError(ShopBotError):
    code = "user_blocked"


class ValidationError(ShopBotError):
    code = "validation_error"


class NotFoundError(ShopBotError):
    code = "not_found"


class InsufficientBalanceError(ShopBotError):
    code = "insufficient_balance"


class InsufficientStockError(ShopBotError):
    code = "insufficient_stock"


class DuplicateTransactionError(ShopBotError):
    code = "duplicate_transaction"


class InvalidStateTransitionError(ShopBotError):
    code = "invalid_state_transition"


class RateLimitExceededError(ShopBotError):
    code = "rate_limit_exceeded"


class EncryptionError(ShopBotError):
    code = "encryption_error"


class FileValidationError(ShopBotError):
    code = "file_validation_error"


class ExternalGatewayError(ShopBotError):
    code = "external_gateway_error"
