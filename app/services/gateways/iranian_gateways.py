"""Async Iranian Rial/Toman payment gateway adapters (ZarinPal, Zibal, NextPay, IDPay)."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.exceptions import ConfigurationError, ExternalGatewayError, ValidationError
from app.services.gateways.base import (
    GatewayPaymentRequestResult,
    GatewayVerifyResult,
    IranianGatewayAdapter,
    request_json,
)

SUPPORTED_IRANIAN_GATEWAYS: frozenset[str] = frozenset({"zarinpal", "zibal", "nextpay", "idpay"})


def _irt_to_irr_int(amount_irt: Decimal) -> int:
    if not amount_irt.is_finite() or amount_irt <= Decimal("0"):
        raise ValidationError("amount_irt must be a positive finite Decimal")
    return int((amount_irt * Decimal("10")).to_integral_value(rounding=ROUND_HALF_UP))


def _irt_to_toman_int(amount_irt: Decimal) -> int:
    if not amount_irt.is_finite() or amount_irt <= Decimal("0"):
        raise ValidationError("amount_irt must be a positive finite Decimal")
    return int(amount_irt.to_integral_value(rounding=ROUND_HALF_UP))


class ZarinPalGateway(IranianGatewayAdapter):
    """ZarinPal v4 async payment request & verification adapter (sandbox + production)."""

    provider_name = "zarinpal"

    def __init__(
        self,
        *,
        merchant_id: str,
        sandbox: bool = True,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._merchant_id = merchant_id.strip()
        self._sandbox = sandbox
        self._http_client = http_client
        host = "sandbox.zarinpal.com" if sandbox else "api.zarinpal.com"
        pay_host = "sandbox.zarinpal.com" if sandbox else "payment.zarinpal.com"
        self._api_base = f"https://{host}/pg/v4/payment"
        self._start_pay_base = f"https://{pay_host}/pg/StartPay"

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
        if not self._merchant_id:
            raise ConfigurationError("ZarinPal merchant_id is not configured")
        amount_irr = _irt_to_irr_int(amount_irt)
        metadata: dict[str, Any] = {"order_id": order_id}
        if mobile:
            metadata["mobile"] = mobile
        if email:
            metadata["email"] = email

        resp = await request_json(
            "POST",
            f"{self._api_base}/request.json",
            http_client=self._http_client,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json_body={
                "merchant_id": self._merchant_id,
                "amount": amount_irr,
                "callback_url": callback_url,
                "description": description or f"Wallet charge {order_id}",
                "metadata": metadata,
            },
            provider_name=self.provider_name,
        )
        data = resp.get("data") or {}
        if not isinstance(data, dict) or data.get("code") != 100 or not data.get("authority"):
            errors = resp.get("errors") or data
            raise ExternalGatewayError(f"ZarinPal payment request failed: {errors}")

        authority = str(data["authority"])
        return GatewayPaymentRequestResult(
            provider=self.provider_name,
            authority_track_id=authority,
            payment_url=f"{self._start_pay_base}/{authority}",
            raw_response=resp,
        )

    async def verify_payment(
        self,
        *,
        authority_track_id: str,
        amount_irt: Decimal,
        params: dict[str, str] | None = None,
    ) -> GatewayVerifyResult:
        if not self._merchant_id:
            raise ConfigurationError("ZarinPal merchant_id is not configured")
        cb_params = params or {}
        status = cb_params.get("Status") or cb_params.get("status")
        if status is not None and status.upper() != "OK":
            return GatewayVerifyResult(
                provider=self.provider_name,
                verified=False,
                authority_track_id=authority_track_id,
                amount_toman=amount_irt,
                message=f"Gateway returned status '{status}'",
                raw_response=dict(cb_params),
            )

        amount_irr = _irt_to_irr_int(amount_irt)
        resp = await request_json(
            "POST",
            f"{self._api_base}/verify.json",
            http_client=self._http_client,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json_body={
                "merchant_id": self._merchant_id,
                "amount": amount_irr,
                "authority": authority_track_id,
            },
            provider_name=self.provider_name,
        )
        data = resp.get("data") or {}
        code = data.get("code") if isinstance(data, dict) else None
        if code in (100, 101):
            return GatewayVerifyResult(
                provider=self.provider_name,
                verified=True,
                authority_track_id=authority_track_id,
                ref_id=str(data.get("ref_id")) if data.get("ref_id") is not None else None,
                card_pan=str(data.get("card_pan")) if data.get("card_pan") else None,
                amount_toman=amount_irt,
                message="Verified" if code == 100 else "Already verified",
                raw_response=resp,
            )
        return GatewayVerifyResult(
            provider=self.provider_name,
            verified=False,
            authority_track_id=authority_track_id,
            amount_toman=amount_irt,
            message=f"ZarinPal verification rejected (code={code})",
            raw_response=resp,
        )


class ZibalGateway(IranianGatewayAdapter):
    """Zibal async payment request & verification adapter."""

    provider_name = "zibal"

    def __init__(
        self,
        *,
        merchant: str,
        base_url: str = "https://gateway.zibal.ir",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._merchant = merchant.strip()
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

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
        if not self._merchant:
            raise ConfigurationError("Zibal merchant is not configured")
        body: dict[str, Any] = {
            "merchant": self._merchant,
            "amount": _irt_to_irr_int(amount_irt),
            "callbackUrl": callback_url,
            "description": description or f"Wallet charge {order_id}",
            "orderId": order_id,
        }
        if mobile:
            body["mobile"] = mobile

        resp = await request_json(
            "POST",
            f"{self._base_url}/v1/request",
            http_client=self._http_client,
            json_body=body,
            provider_name=self.provider_name,
        )
        if resp.get("result") != 100 or not resp.get("trackId"):
            raise ExternalGatewayError(f"Zibal payment request failed: {resp.get('message', resp)}")

        track_id = str(resp["trackId"])
        return GatewayPaymentRequestResult(
            provider=self.provider_name,
            authority_track_id=track_id,
            payment_url=f"{self._base_url}/start/{track_id}",
            raw_response=resp,
        )

    async def verify_payment(
        self,
        *,
        authority_track_id: str,
        amount_irt: Decimal,
        params: dict[str, str] | None = None,
    ) -> GatewayVerifyResult:
        if not self._merchant:
            raise ConfigurationError("Zibal merchant is not configured")
        cb_params = params or {}
        if "success" in cb_params and str(cb_params["success"]) != "1":
            return GatewayVerifyResult(
                provider=self.provider_name,
                verified=False,
                authority_track_id=authority_track_id,
                amount_toman=amount_irt,
                message=f"Zibal callback failed (success={cb_params['success']})",
                raw_response=dict(cb_params),
            )

        resp = await request_json(
            "POST",
            f"{self._base_url}/v1/verify",
            http_client=self._http_client,
            json_body={"merchant": self._merchant, "trackId": authority_track_id},
            provider_name=self.provider_name,
        )
        result_code = resp.get("result")
        if result_code in (100, 201):
            return GatewayVerifyResult(
                provider=self.provider_name,
                verified=True,
                authority_track_id=authority_track_id,
                ref_id=str(resp.get("refNumber")) if resp.get("refNumber") is not None else None,
                card_pan=str(resp.get("cardNumber")) if resp.get("cardNumber") else None,
                amount_toman=amount_irt,
                message="Verified" if result_code == 100 else "Already verified",
                raw_response=resp,
            )
        return GatewayVerifyResult(
            provider=self.provider_name,
            verified=False,
            authority_track_id=authority_track_id,
            amount_toman=amount_irt,
            message=f"Zibal verification failed (result={result_code})",
            raw_response=resp,
        )


class NextPayGateway(IranianGatewayAdapter):
    """NextPay async payment token & verification adapter."""

    provider_name = "nextpay"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://nextpay.org/nx/gateway",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

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
        if not self._api_key:
            raise ConfigurationError("NextPay API key is not configured")
        form: dict[str, Any] = {
            "api_key": self._api_key,
            "order_id": order_id,
            "amount": _irt_to_toman_int(amount_irt),
            "callback_uri": callback_url,
        }
        if mobile:
            form["customer_phone"] = mobile

        resp = await request_json(
            "POST",
            f"{self._base_url}/token",
            http_client=self._http_client,
            form_data=form,
            provider_name=self.provider_name,
        )
        if resp.get("code") != -1 or not resp.get("trans_id"):
            raise ExternalGatewayError(f"NextPay token request failed: {resp}")

        trans_id = str(resp["trans_id"])
        return GatewayPaymentRequestResult(
            provider=self.provider_name,
            authority_track_id=trans_id,
            payment_url=f"{self._base_url}/payment/{trans_id}",
            raw_response=resp,
        )

    async def verify_payment(
        self,
        *,
        authority_track_id: str,
        amount_irt: Decimal,
        params: dict[str, str] | None = None,
    ) -> GatewayVerifyResult:
        if not self._api_key:
            raise ConfigurationError("NextPay API key is not configured")
        cb_params = params or {}
        if cb_params.get("np_status") and cb_params["np_status"].lower() != "ok":
            return GatewayVerifyResult(
                provider=self.provider_name,
                verified=False,
                authority_track_id=authority_track_id,
                amount_toman=amount_irt,
                message=f"NextPay callback status '{cb_params['np_status']}'",
                raw_response=dict(cb_params),
            )

        resp = await request_json(
            "POST",
            f"{self._base_url}/verify",
            http_client=self._http_client,
            form_data={
                "api_key": self._api_key,
                "amount": _irt_to_toman_int(amount_irt),
                "trans_id": authority_track_id,
            },
            provider_name=self.provider_name,
        )
        if resp.get("code") == 0:
            return GatewayVerifyResult(
                provider=self.provider_name,
                verified=True,
                authority_track_id=authority_track_id,
                ref_id=str(resp.get("Shaparak_Ref_Id")) if resp.get("Shaparak_Ref_Id") else None,
                card_pan=str(resp.get("card_holder")) if resp.get("card_holder") else None,
                amount_toman=amount_irt,
                message="Verified",
                raw_response=resp,
            )
        return GatewayVerifyResult(
            provider=self.provider_name,
            verified=False,
            authority_track_id=authority_track_id,
            amount_toman=amount_irt,
            message=f"NextPay verification failed (code={resp.get('code')})",
            raw_response=resp,
        )


class IDPayGateway(IranianGatewayAdapter):
    """IDPay v1.1 async payment creation & verification adapter."""

    provider_name = "idpay"

    def __init__(
        self,
        *,
        api_key: str,
        sandbox: bool = True,
        base_url: str = "https://api.idpay.ir/v1.1",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._sandbox = sandbox
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self._api_key,
        }
        if self._sandbox:
            headers["X-SANDBOX"] = "1"
        return headers

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
        if not self._api_key:
            raise ConfigurationError("IDPay API key is not configured")
        body: dict[str, Any] = {
            "order_id": order_id,
            "amount": _irt_to_irr_int(amount_irt),
            "callback": callback_url,
            "desc": description or f"Wallet charge {order_id}",
        }
        if mobile:
            body["phone"] = mobile
        if email:
            body["mail"] = email

        resp = await request_json(
            "POST",
            f"{self._base_url}/payment",
            http_client=self._http_client,
            headers=self._headers(),
            json_body=body,
            provider_name=self.provider_name,
        )
        if not resp.get("id") or not resp.get("link"):
            raise ExternalGatewayError(f"IDPay payment creation failed: {resp}")

        return GatewayPaymentRequestResult(
            provider=self.provider_name,
            authority_track_id=str(resp["id"]),
            payment_url=str(resp["link"]),
            raw_response=resp,
        )

    async def verify_payment(
        self,
        *,
        authority_track_id: str,
        amount_irt: Decimal,
        params: dict[str, str] | None = None,
    ) -> GatewayVerifyResult:
        if not self._api_key:
            raise ConfigurationError("IDPay API key is not configured")
        cb_params = params or {}
        if "status" in cb_params and str(cb_params["status"]) not in {"10", "100", "101"}:
            return GatewayVerifyResult(
                provider=self.provider_name,
                verified=False,
                authority_track_id=authority_track_id,
                amount_toman=amount_irt,
                message=f"IDPay callback status '{cb_params['status']}'",
                raw_response=dict(cb_params),
            )

        order_id = cb_params.get("order_id") or ""
        resp = await request_json(
            "POST",
            f"{self._base_url}/payment/verify",
            http_client=self._http_client,
            headers=self._headers(),
            json_body={"id": authority_track_id, "order_id": order_id},
            provider_name=self.provider_name,
        )
        status_code = resp.get("status")
        if status_code in (100, 101):
            return GatewayVerifyResult(
                provider=self.provider_name,
                verified=True,
                authority_track_id=authority_track_id,
                ref_id=str(resp.get("track_id")) if resp.get("track_id") is not None else None,
                card_pan=str((resp.get("payment") or {}).get("card_no")) if isinstance(resp.get("payment"), dict) else None,
                amount_toman=amount_irt,
                message="Verified" if status_code == 100 else "Already verified",
                raw_response=resp,
            )
        return GatewayVerifyResult(
            provider=self.provider_name,
            verified=False,
            authority_track_id=authority_track_id,
            amount_toman=amount_irt,
            message=f"IDPay verification failed (status={status_code})",
            raw_response=resp,
        )


def get_iranian_gateway(
    provider: str,
    *,
    merchant_id: str | None = None,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> IranianGatewayAdapter:
    """Instantiate an Iranian payment gateway adapter by provider slug."""
    cfg = settings or get_settings()
    name = (provider or "").strip().lower()
    if name == "zarinpal":
        return ZarinPalGateway(
            merchant_id=(merchant_id or cfg.ZARINPAL_MERCHANT_ID),
            sandbox=cfg.ZARINPAL_SANDBOX,
            http_client=http_client,
        )
    if name == "zibal":
        return ZibalGateway(
            merchant=(merchant_id or cfg.ZIBAL_MERCHANT),
            http_client=http_client,
        )
    if name == "nextpay":
        return NextPayGateway(
            api_key=(merchant_id or cfg.NEXTPAY_API_KEY),
            http_client=http_client,
        )
    if name == "idpay":
        return IDPayGateway(
            api_key=(merchant_id or cfg.IDPAY_API_KEY),
            sandbox=cfg.ZARINPAL_SANDBOX,
            http_client=http_client,
        )
    raise ValidationError(f"Unsupported Iranian payment gateway provider: {provider}")
