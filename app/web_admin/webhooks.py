"""FastAPI callback and webhook routes for Iranian fiat gateways and crypto gateways."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.constants import Currency, PaymentStatus
from app.core.exceptions import (
    AuthenticationError,
    DuplicateTransactionError,
    ExternalGatewayError,
    InvalidStateTransitionError,
    NotFoundError,
    ShopBotError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import escape_html
from app.services.dtos import PaymentDTO
from app.services.gateways.iranian_gateways import SUPPORTED_IRANIAN_GATEWAYS
from app.services.payment_service import PaymentService

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_SUPPORTED_CRYPTO_PROVIDERS: frozenset[str] = frozenset({"nowpayments", "oxapay"})


def get_payment_service(request: Request) -> PaymentService:
    """Resolve PaymentService from app.state or construct default instance."""
    svc = getattr(request.app.state, "payment_service", None)
    if svc is not None:
        return svc
    return PaymentService()


async def _extract_callback_params(request: Request) -> dict[str, str]:
    params: dict[str, str] = {str(k): str(v) for k, v in request.query_params.items()}
    if request.method.upper() == "POST":
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    for k, v in body.items():
                        if v is not None:
                            params[str(k)] = str(v)
            except ValueError:
                pass
        else:
            try:
                form = await request.form()
                for k, v in form.items():
                    params[str(k)] = str(v)
            except Exception:
                pass
    return params


def _resolve_iranian_authority(provider: str, params: dict[str, str]) -> str:
    candidates: tuple[str, ...]
    if provider == "zarinpal":
        candidates = ("Authority", "authority", "authority_track_id")
    elif provider == "zibal":
        candidates = ("trackId", "track_id", "authority_track_id")
    elif provider == "nextpay":
        candidates = ("trans_id", "authority_track_id")
    elif provider == "idpay":
        candidates = ("id", "track_id", "authority_track_id")
    else:
        candidates = ("authority_track_id", "Authority", "trackId", "trans_id", "id")

    for key in candidates:
        val = params.get(key)
        if val and val.strip():
            return val.strip()
    return ""


async def _notify_user_if_bot_available(request: Request, payment: PaymentDTO) -> None:
    """Best-effort Telegram DM to user when a webhook approves a deposit."""
    if payment.status != PaymentStatus.APPROVED:
        return
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        return
    try:
        from app.database.models import User
        from app.database.session import get_session_factory
        from app.locales.i18n import TranslationService

        session_factory = getattr(request.app.state, "session_factory", None) or get_session_factory()
        async with session_factory() as session:
            user = await session.get(User, payment.user_id)
        if user is None:
            return
        i18n = getattr(request.app.state, "i18n", None) or TranslationService(session_factory)
        amount_str = (
            f"${payment.amount_usd.normalize()}"
            if payment.currency == Currency.USD
            else f"{int(payment.amount_irt):,} IRT"
        )
        msg = await i18n.get_text("deposit.approved_notify", user.language_code, amount=escape_html(amount_str))
        await bot.send_message(chat_id=user.telegram_id, text=msg, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Failed to send webhook approval DM for payment %s: %s", payment.id, exc)


def _map_domain_error(exc: ShopBotError) -> HTTPException:
    if isinstance(exc, AuthenticationError):
        return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.message)
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message)
    if isinstance(exc, (DuplicateTransactionError, InvalidStateTransitionError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message)
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)
    if isinstance(exc, ExternalGatewayError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)


@router.api_route("/payment/{provider}", methods=["GET", "POST"])
async def iranian_gateway_callback(
    provider: str,
    request: Request,
    payment_service: PaymentService = Depends(get_payment_service),
) -> JSONResponse:
    """Handle Iranian Rial/Toman gateway verification callback idempotently."""
    provider_slug = provider.strip().lower()
    if provider_slug not in SUPPORTED_IRANIAN_GATEWAYS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown payment provider '{provider}'",
        )

    params = await _extract_callback_params(request)
    authority = _resolve_iranian_authority(provider_slug, params)
    if not authority:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing gateway authority or tracking parameter",
        )

    try:
        payment = await payment_service.verify_gateway_callback(
            authority,
            params,
            expected_provider=provider_slug,
        )
    except ShopBotError as exc:
        raise _map_domain_error(exc) from exc

    await _notify_user_if_bot_available(request, payment)

    approved = payment.status == PaymentStatus.APPROVED
    body: dict[str, Any] = {
        "ok": approved,
        "provider": provider_slug,
        "payment_id": payment.id,
        "status": payment.status.value,
        "currency": payment.currency.value,
        "amount_irt": str(payment.amount_irt),
        "amount_usd": str(payment.amount_usd),
        "authority_track_id": payment.authority_track_id,
    }
    return JSONResponse(status_code=status.HTTP_200_OK, content=body)


@router.post("/crypto/{provider}")
async def crypto_gateway_webhook(
    provider: str,
    request: Request,
    payment_service: PaymentService = Depends(get_payment_service),
) -> JSONResponse:
    """Verify HMAC-signed IPN/webhook notifications from NOWPayments or Oxapay."""
    provider_slug = provider.strip().lower()
    if provider_slug not in _SUPPORTED_CRYPTO_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown crypto webhook provider '{provider}'",
        )

    raw_body = await request.body()
    headers = {k: v for k, v in request.headers.items()}

    try:
        payment = await payment_service.process_crypto_webhook(
            provider_slug,
            raw_body,
            headers,
        )
    except ShopBotError as exc:
        raise _map_domain_error(exc) from exc

    await _notify_user_if_bot_available(request, payment)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "ok": payment.status == PaymentStatus.APPROVED,
            "provider": provider_slug,
            "payment_id": payment.id,
            "status": payment.status.value,
            "amount_usd": str(payment.amount_usd),
            "tx_hash": payment.tx_hash,
        },
    )
