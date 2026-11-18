"""Async blockchain explorer adapters (TronGrid, BscScan) and crypto gateways (NOWPayments, Oxapay)."""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.constants import CryptoVerificationMode
from app.core.exceptions import ConfigurationError, ExternalGatewayError, ValidationError
from app.services.gateways.base import (
    CryptoExplorerAdapter,
    CryptoGatewayAdapter,
    CryptoInvoiceResult,
    CryptoTxVerificationResult,
    CryptoWebhookEvent,
    request_json,
)

DEFAULT_TRC20_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
DEFAULT_BEP20_USDT_CONTRACT = "0x55d398326f99059ff775485246999027b3197955"
_ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(value: str) -> bytes:
    num = 0
    for ch in value:
        idx = _BASE58_ALPHABET.find(ch)
        if idx == -1:
            raise ValueError(f"Invalid Base58 character: {ch}")
        num = num * 58 + idx
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num > 0 else b""
    leading_zeros = len(value) - len(value.lstrip("1"))
    return (b"\x00" * leading_zeros) + raw


def tron_address_to_hex(address: str) -> str:
    """Normalize a Tron Base58Check (T...) or 41.../0x... address to lowercase 41-prefixed hex."""
    cleaned = address.strip()
    if cleaned.startswith("0x") or cleaned.startswith("0X"):
        hex_part = cleaned[2:].lower()
        return hex_part if hex_part.startswith("41") and len(hex_part) == 42 else f"41{hex_part[-40:]}"
    if cleaned.startswith("41") and len(cleaned) == 42:
        return cleaned.lower()
    if cleaned.startswith("T"):
        decoded = _base58_decode(cleaned)
        if len(decoded) == 25:
            payload, checksum = decoded[:-4], decoded[-4:]
            expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
            if checksum == expected:
                return payload.hex().lower()
    return cleaned.lower()


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return dec if dec.is_finite() else None


def _get_header_ci(headers: dict[str, str], target_name: str) -> str:
    lower_target = target_name.lower()
    for key, val in headers.items():
        if key.lower() == lower_target:
            return val.strip()
    return ""


class TronGridExplorer(CryptoExplorerAdapter):
    """TronGrid async adapter for verifying TRC20 and native TRX transactions."""

    provider_name = "trongrid"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.trongrid.io",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["TRON-PRO-API-KEY"] = self._api_key
        return headers

    async def verify_transaction(
        self,
        *,
        tx_hash: str,
        expected_address: str,
        min_amount: Decimal,
        token_contract: str | None = DEFAULT_TRC20_USDT_CONTRACT,
        decimals: int = 6,
    ) -> CryptoTxVerificationResult:
        txid = tx_hash.strip()
        if not txid:
            raise ValidationError("Transaction hash is required")

        expected_hex = tron_address_to_hex(expected_address)
        is_native_trx = (token_contract or "").strip().upper() == "TRX"

        if not is_native_trx:
            url = f"{self._base_url}/v1/transactions/{txid}/events"
            payload = await request_json(
                "GET",
                url,
                http_client=self._http_client,
                headers=self._headers(),
                provider_name=self.provider_name,
            )
            events = payload.get("data") or []
            if isinstance(events, list) and events:
                contract_hex = tron_address_to_hex(token_contract) if token_contract else None
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    if ev.get("event_name") != "Transfer":
                        continue
                    ev_contract = ev.get("contract_address")
                    if contract_hex and ev_contract and tron_address_to_hex(str(ev_contract)) != contract_hex:
                        continue
                    res = ev.get("result") or {}
                    to_addr = str(res.get("to") or res.get("_to") or "")
                    from_addr = str(res.get("from") or res.get("_from") or "")
                    raw_val = res.get("value") or res.get("_value")
                    if not to_addr or raw_val is None:
                        continue
                    if tron_address_to_hex(to_addr) != expected_hex:
                        continue
                    val_dec = _parse_decimal(raw_val)
                    if val_dec is None:
                        continue
                    amount = val_dec / (Decimal(10) ** decimals)
                    if amount >= min_amount:
                        return CryptoTxVerificationResult(
                            provider=self.provider_name,
                            verified=True,
                            tx_hash=txid,
                            confirmations=1,
                            to_address=to_addr,
                            from_address=from_addr or None,
                            amount=amount,
                            symbol="TRC20",
                            message="TRC20 transfer verified on TronGrid",
                            raw_response=payload,
                        )
                    return CryptoTxVerificationResult(
                        provider=self.provider_name,
                        verified=False,
                        tx_hash=txid,
                        to_address=to_addr,
                        from_address=from_addr or None,
                        amount=amount,
                        message=f"Transferred amount {amount} is below minimum {min_amount}",
                        raw_response=payload,
                    )

        # Native TRX fallback / explicit TRX verification
        if is_native_trx or token_contract is None:
            tx_url = f"{self._base_url}/wallet/gettransactionbyid"
            tx_data = await request_json(
                "POST",
                tx_url,
                http_client=self._http_client,
                headers=self._headers(),
                json_body={"value": txid},
                provider_name=self.provider_name,
            )
            ret = tx_data.get("ret") or []
            if not ret or ret[0].get("contractRet") != "SUCCESS":
                return CryptoTxVerificationResult(
                    provider=self.provider_name,
                    verified=False,
                    tx_hash=txid,
                    message="Transaction not found or failed on TRON network",
                    raw_response=tx_data,
                )
            contracts = (tx_data.get("raw_data") or {}).get("contract") or []
            for item in contracts:
                if item.get("type") != "TransferContract":
                    continue
                val = (item.get("parameter") or {}).get("value") or {}
                to_hex = tron_address_to_hex(str(val.get("to_address", "")))
                if to_hex == expected_hex:
                    sun = _parse_decimal(val.get("amount")) or Decimal("0")
                    trx_amount = sun / Decimal("1000000")
                    verified = trx_amount >= min_amount
                    return CryptoTxVerificationResult(
                        provider=self.provider_name,
                        verified=verified,
                        tx_hash=txid,
                        confirmations=1 if verified else 0,
                        to_address=str(val.get("to_address", "")),
                        from_address=str(val.get("owner_address", "")) or None,
                        amount=trx_amount,
                        symbol="TRX",
                        message="TRX transfer verified" if verified else "TRX amount below minimum",
                        raw_response=tx_data,
                    )

        return CryptoTxVerificationResult(
            provider=self.provider_name,
            verified=False,
            tx_hash=txid,
            message="No matching transfer to target wallet address found",
        )


class BscScanExplorer(CryptoExplorerAdapter):
    """BscScan async adapter for verifying BEP20 token and native BNB transactions."""

    provider_name = "bscscan"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.bscscan.com/api",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

    async def verify_transaction(
        self,
        *,
        tx_hash: str,
        expected_address: str,
        min_amount: Decimal,
        token_contract: str | None = DEFAULT_BEP20_USDT_CONTRACT,
        decimals: int = 18,
    ) -> CryptoTxVerificationResult:
        txid = tx_hash.strip()
        if not txid:
            raise ValidationError("Transaction hash is required")

        expected_norm = expected_address.strip().lower()
        is_native_bnb = (token_contract or "").strip().upper() == "BNB"

        receipt_params: dict[str, Any] = {
            "module": "proxy",
            "action": "eth_getTransactionReceipt",
            "txhash": txid,
        }
        if self._api_key:
            receipt_params["apikey"] = self._api_key

        receipt_resp = await request_json(
            "GET",
            self._base_url,
            http_client=self._http_client,
            params=receipt_params,
            provider_name=self.provider_name,
        )
        receipt = receipt_resp.get("result")
        if not isinstance(receipt, dict):
            return CryptoTxVerificationResult(
                provider=self.provider_name,
                verified=False,
                tx_hash=txid,
                message="Transaction receipt not found on BscScan",
                raw_response=receipt_resp,
            )

        if str(receipt.get("status", "")).lower() != "0x1":
            return CryptoTxVerificationResult(
                provider=self.provider_name,
                verified=False,
                tx_hash=txid,
                message="Transaction reverted or unconfirmed on BSC",
                raw_response=receipt_resp,
            )

        if not is_native_bnb:
            contract_norm = token_contract.strip().lower() if token_contract else None
            for log in receipt.get("logs") or []:
                if not isinstance(log, dict):
                    continue
                topics = log.get("topics") or []
                if len(topics) < 3 or str(topics[0]).lower() != _ERC20_TRANSFER_TOPIC:
                    continue
                log_addr = str(log.get("address", "")).lower()
                if contract_norm and log_addr != contract_norm:
                    continue
                to_addr = "0x" + str(topics[2])[-40:].lower()
                from_addr = "0x" + str(topics[1])[-40:].lower()
                if to_addr != expected_norm:
                    continue
                data_hex = str(log.get("data", "0x0"))
                raw_int = int(data_hex, 16)
                amount = Decimal(raw_int) / (Decimal(10) ** decimals)
                if amount >= min_amount:
                    return CryptoTxVerificationResult(
                        provider=self.provider_name,
                        verified=True,
                        tx_hash=txid,
                        confirmations=1,
                        to_address=to_addr,
                        from_address=from_addr,
                        amount=amount,
                        symbol="BEP20",
                        message="BEP20 transfer verified on BscScan",
                        raw_response=receipt_resp,
                    )
                return CryptoTxVerificationResult(
                    provider=self.provider_name,
                    verified=False,
                    tx_hash=txid,
                    to_address=to_addr,
                    from_address=from_addr,
                    amount=amount,
                    message=f"Transferred amount {amount} is below minimum {min_amount}",
                    raw_response=receipt_resp,
                )

        if is_native_bnb or token_contract is None:
            tx_params: dict[str, Any] = {
                "module": "proxy",
                "action": "eth_getTransactionByHash",
                "txhash": txid,
            }
            if self._api_key:
                tx_params["apikey"] = self._api_key
            tx_resp = await request_json(
                "GET",
                self._base_url,
                http_client=self._http_client,
                params=tx_params,
                provider_name=self.provider_name,
            )
            tx_obj = tx_resp.get("result")
            if isinstance(tx_obj, dict) and str(tx_obj.get("to", "")).lower() == expected_norm:
                wei = int(str(tx_obj.get("value", "0x0")), 16)
                bnb_amount = Decimal(wei) / Decimal("1000000000000000000")
                verified = bnb_amount >= min_amount
                return CryptoTxVerificationResult(
                    provider=self.provider_name,
                    verified=verified,
                    tx_hash=txid,
                    confirmations=1 if verified else 0,
                    to_address=str(tx_obj.get("to", "")),
                    from_address=str(tx_obj.get("from", "")) or None,
                    amount=bnb_amount,
                    symbol="BNB",
                    message="BNB transfer verified" if verified else "BNB amount below minimum",
                    raw_response=tx_resp,
                )

        return CryptoTxVerificationResult(
            provider=self.provider_name,
            verified=False,
            tx_hash=txid,
            message="No matching BEP20/BNB transfer to target wallet found",
            raw_response=receipt_resp,
        )


class NOWPaymentsGateway(CryptoGatewayAdapter):
    """NOWPayments async invoice creator and HMAC-SHA512 IPN webhook verifier."""

    provider_name = "nowpayments"

    def __init__(
        self,
        *,
        api_key: str,
        ipn_secret: str = "",
        base_url: str = "https://api.nowpayments.io/v1",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._ipn_secret = ipn_secret.strip()
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

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
        if not self._api_key:
            raise ConfigurationError("NOWPAYMENTS_API_KEY is not configured")
        body: dict[str, Any] = {
            "price_amount": str(amount_usd),
            "price_currency": "usd",
            "order_id": order_id,
            "ipn_callback_url": callback_url,
            "order_description": description or f"Wallet deposit {order_id}",
        }
        if success_url:
            body["success_url"] = success_url
        if pay_currency:
            body["pay_currency"] = pay_currency.lower()

        data = await request_json(
            "POST",
            f"{self._base_url}/invoice",
            http_client=self._http_client,
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            json_body=body,
            provider_name=self.provider_name,
        )
        invoice_id = str(data.get("id") or "")
        invoice_url = str(data.get("invoice_url") or "")
        if not invoice_id or not invoice_url:
            raise ExternalGatewayError(f"NOWPayments failed to create invoice: {data.get('message', data)}")
        return CryptoInvoiceResult(
            provider=self.provider_name,
            track_id=invoice_id,
            payment_url=invoice_url,
            pay_currency=pay_currency,
            raw_response=data,
        )

    def verify_webhook_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        if not self._ipn_secret:
            return False
        provided = _get_header_ci(headers, "x-nowpayments-sig").lower()
        if not provided:
            return False
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(parsed, dict):
            return False
        canonical = json.dumps(parsed, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
        expected = hmac.new(self._ipn_secret.encode("utf-8"), canonical, hashlib.sha512).hexdigest().lower()
        return hmac.compare_digest(provided, expected)

    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> CryptoWebhookEvent:
        verified = self.verify_webhook_signature(raw_body, headers)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Invalid NOWPayments JSON payload") from exc
        if not isinstance(payload, dict):
            raise ValidationError("NOWPayments payload must be a JSON object")

        status = str(payload.get("payment_status") or "").strip().lower()
        track_id = payload.get("invoice_id") or payload.get("payment_id")
        return CryptoWebhookEvent(
            provider=self.provider_name,
            verified_signature=verified,
            is_paid=status in {"finished", "confirmed"},
            is_failed=status in {"failed", "refunded", "expired"},
            authority_track_id=str(track_id) if track_id is not None else None,
            order_id=str(payload["order_id"]) if payload.get("order_id") is not None else None,
            tx_hash=str(payload["payin_hash"]) if payload.get("payin_hash") else None,
            actually_paid_usd=_parse_decimal(payload.get("price_amount") or payload.get("actually_paid")),
            status=status,
            raw_payload=payload,
        )


class OxapayGateway(CryptoGatewayAdapter):
    """Oxapay async merchant invoice creator and HMAC-SHA512 webhook verifier."""

    provider_name = "oxapay"

    def __init__(
        self,
        *,
        merchant_api_key: str,
        base_url: str = "https://api.oxapay.com",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._merchant_api_key = merchant_api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

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
        if not self._merchant_api_key:
            raise ConfigurationError("OXAPAY_MERCHANT_API_KEY is not configured")
        body: dict[str, Any] = {
            "merchant": self._merchant_api_key,
            "amount": str(amount_usd),
            "currency": "USD",
            "callbackUrl": callback_url,
            "orderId": order_id,
            "description": description or f"Wallet deposit {order_id}",
        }
        if success_url:
            body["returnUrl"] = success_url
        if pay_currency:
            body["payCurrency"] = pay_currency.upper()

        data = await request_json(
            "POST",
            f"{self._base_url}/merchants/request",
            http_client=self._http_client,
            headers={"Content-Type": "application/json"},
            json_body=body,
            provider_name=self.provider_name,
        )
        if data.get("result") != 100 or not data.get("payLink") or not data.get("trackId"):
            raise ExternalGatewayError(f"Oxapay failed to create invoice: {data.get('message', data)}")
        return CryptoInvoiceResult(
            provider=self.provider_name,
            track_id=str(data["trackId"]),
            payment_url=str(data["payLink"]),
            pay_currency=pay_currency,
            raw_response=data,
        )

    def verify_webhook_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        if not self._merchant_api_key:
            return False
        provided = _get_header_ci(headers, "hmac").lower()
        if not provided:
            return False
        expected = hmac.new(self._merchant_api_key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest().lower()
        return hmac.compare_digest(provided, expected)

    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> CryptoWebhookEvent:
        verified = self.verify_webhook_signature(raw_body, headers)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Invalid Oxapay JSON payload") from exc
        if not isinstance(payload, dict):
            raise ValidationError("Oxapay payload must be a JSON object")

        status = str(payload.get("status") or "").strip().lower()
        track_id = payload.get("trackId")
        return CryptoWebhookEvent(
            provider=self.provider_name,
            verified_signature=verified,
            is_paid=status in {"paid", "confirmed"},
            is_failed=status in {"expired", "failed", "canceled", "cancelled"},
            authority_track_id=str(track_id) if track_id is not None else None,
            order_id=str(payload["orderId"]) if payload.get("orderId") is not None else None,
            tx_hash=str(payload["txID"]) if payload.get("txID") else None,
            actually_paid_usd=_parse_decimal(payload.get("amount")),
            status=status,
            raw_payload=payload,
        )


def get_crypto_explorer(
    mode: CryptoVerificationMode | str,
    *,
    settings: Settings | None = None,
    explorer_config: dict[str, Any] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> CryptoExplorerAdapter:
    """Instantiate a blockchain explorer adapter for the requested verification mode."""
    cfg = settings or get_settings()
    extra = explorer_config or {}
    mode_val = mode.value if isinstance(mode, CryptoVerificationMode) else str(mode).strip().lower()

    if mode_val == CryptoVerificationMode.TRONGRID.value:
        return TronGridExplorer(
            api_key=str(extra.get("api_key") or cfg.TRONGRID_API_KEY),
            base_url=str(extra.get("base_url") or "https://api.trongrid.io"),
            http_client=http_client,
        )
    if mode_val == CryptoVerificationMode.BSCSCAN.value:
        return BscScanExplorer(
            api_key=str(extra.get("api_key") or cfg.BSCSCAN_API_KEY),
            base_url=str(extra.get("base_url") or "https://api.bscscan.com/api"),
            http_client=http_client,
        )
    raise ValidationError(f"Unsupported blockchain explorer verification mode: {mode_val}")


def get_crypto_gateway(
    provider: str,
    *,
    settings: Settings | None = None,
    gateway_config: dict[str, Any] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> CryptoGatewayAdapter:
    """Instantiate a hosted crypto payment gateway adapter (NOWPayments or Oxapay)."""
    cfg = settings or get_settings()
    extra = gateway_config or {}
    name = provider.strip().lower()

    if name == CryptoVerificationMode.NOWPAYMENTS.value:
        return NOWPaymentsGateway(
            api_key=str(extra.get("api_key") or cfg.NOWPAYMENTS_API_KEY),
            ipn_secret=str(extra.get("ipn_secret") or cfg.NOWPAYMENTS_IPN_SECRET),
            base_url=str(extra.get("base_url") or "https://api.nowpayments.io/v1"),
            http_client=http_client,
        )
    if name == CryptoVerificationMode.OXAPAY.value:
        return OxapayGateway(
            merchant_api_key=str(extra.get("merchant_api_key") or cfg.OXAPAY_MERCHANT_API_KEY),
            base_url=str(extra.get("base_url") or "https://api.oxapay.com"),
            http_client=http_client,
        )
    raise ValidationError(f"Unsupported crypto gateway provider: {provider}")
