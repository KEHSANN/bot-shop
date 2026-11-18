"""Base protocols, result models, and shared async HTTP utilities for payment gateways."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.core.exceptions import ExternalGatewayError
from app.core.logging import get_logger

logger = get_logger(__name__)


class GatewayPaymentRequestResult(BaseModel):
    """Result returned when initiating an external fiat gateway payment."""

    provider: str
    authority_track_id: str
    payment_url: str
    raw_response: dict[str, Any] = Field(default_factory=dict)


class GatewayVerifyResult(BaseModel):
    """Verification result returned by an Iranian payment gateway."""

    provider: str
    verified: bool
    authority_track_id: str
    ref_id: str | None = None
    card_pan: str | None = None
    amount_toman: Decimal | None = None
    message: str = ""
    raw_response: dict[str, Any] = Field(default_factory=dict)


class CryptoTxVerificationResult(BaseModel):
    """On-chain transaction verification result from a blockchain explorer."""

    provider: str
    verified: bool
    tx_hash: str
    confirmations: int = 0
    to_address: str | None = None
    from_address: str | None = None
    amount: Decimal | None = None
    symbol: str | None = None
    message: str = ""
    raw_response: dict[str, Any] = Field(default_factory=dict)


class CryptoInvoiceResult(BaseModel):
    """Hosted invoice / payment link result from a crypto payment gateway."""

    provider: str
    track_id: str
    payment_url: str
    pay_address: str | None = None
    pay_amount: Decimal | None = None
    pay_currency: str | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)


class CryptoWebhookEvent(BaseModel):
    """Normalized crypto gateway IPN/webhook payload after signature verification."""

    provider: str
    verified_signature: bool
    is_paid: bool
    is_failed: bool = False
    authority_track_id: str | None = None
    order_id: str | None = None
    tx_hash: str | None = None
    actually_paid_usd: Decimal | None = None
    status: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)


async def request_json(
    method: str,
    url: str,
    *,
    http_client: httpx.AsyncClient | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    form_data: dict[str, Any] | None = None,
    timeout: float = 15.0,
    provider_name: str = "gateway",
) -> dict[str, Any]:
    """Perform an async HTTP request and decode JSON response safely."""

    async def _send(client: httpx.AsyncClient) -> httpx.Response:
        return await client.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params,
            json=json_body,
            data=form_data,
            timeout=timeout,
        )

    try:
        if http_client is not None:
            response = await _send(http_client)
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await _send(client)
    except httpx.HTTPError as exc:
        logger.warning("%s HTTP transport error: %s", provider_name, exc)
        raise ExternalGatewayError(f"{provider_name} request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        if response.status_code >= 400:
            raise ExternalGatewayError(
                f"{provider_name} returned HTTP {response.status_code}"
            ) from exc
        raise ExternalGatewayError(f"{provider_name} returned non-JSON response") from exc

    if response.status_code >= 500:
        raise ExternalGatewayError(
            f"{provider_name} server error HTTP {response.status_code}",
            details={"status_code": response.status_code},
        )
    if response.status_code >= 400 and not isinstance(payload, dict):
        raise ExternalGatewayError(
            f"{provider_name} returned HTTP {response.status_code}",
            details={"status_code": response.status_code},
        )

    if isinstance(payload, dict):
        if response.status_code >= 400:
            payload = {**payload, "_http_status": response.status_code}
        return payload
    return {"data": payload, "_http_status": response.status_code}


class IranianGatewayAdapter(ABC):
    """Abstract base class for Iranian Rial/Toman payment gateway adapters."""

    provider_name: str

    @abstractmethod
    async def request_payment(
        self,
        *,
        amount_irt: Decimal,
        callback_url: str,
        description: str,
        order_id: str,
        mobile: str | None = None,
        email: str | None = None,
    ) -> GatewayPaymentRequestResult:
        """Initiate a payment session and return authority track ID plus redirect URL."""

    @abstractmethod
    async def verify_payment(
        self,
        *,
        authority_track_id: str,
        amount_irt: Decimal,
        params: dict[str, str] | None = None,
    ) -> GatewayVerifyResult:
        """Verify callback parameters with the gateway server."""


class CryptoExplorerAdapter(ABC):
    """Abstract base class for on-chain TXID verification via blockchain explorers."""

    provider_name: str

    @abstractmethod
    async def verify_transaction(
        self,
        *,
        tx_hash: str,
        expected_address: str,
        min_amount: Decimal,
        token_contract: str | None = None,
        decimals: int = 6,
    ) -> CryptoTxVerificationResult:
        """Verify an on-chain transaction hash against destination address and minimum amount."""


class CryptoGatewayAdapter(ABC):
    """Abstract base class for hosted crypto payment gateways (NOWPayments, Oxapay)."""

    provider_name: str

    @abstractmethod
    async def create_invoice(
        self,
        *,
        amount_usd: Decimal,
        order_id: str,
        callback_url: str,
        success_url: str | None = None,
        pay_currency: str | None = None,
        description: str = "",
    ) -> CryptoInvoiceResult:
        """Create a hosted crypto checkout invoice."""

    @abstractmethod
    def verify_webhook_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        """Verify HMAC webhook signature in constant time."""

    @abstractmethod
    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> CryptoWebhookEvent:
        """Validate signature and normalize webhook payload into CryptoWebhookEvent."""
