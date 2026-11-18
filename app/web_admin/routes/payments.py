"""Payments Queue, Unlimited Crypto Wallets, Iranian Fiat Methods, Exchange Rate, and QR/Receipt routes."""

from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import qrcode
from fastapi import APIRouter, Depends, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.constants import CryptoVerificationMode, FiatMethodType, PaymentStatus
from app.core.exceptions import NotFoundError, ValidationError
from app.core.security import detect_image_mime, escape_html, validate_receipt_image
from app.locales.i18n import TranslationService
from app.services.dtos import (
    CryptoWalletCreateDTO,
    CryptoWalletUpdateDTO,
    FiatMethodCreateDTO,
    FiatMethodUpdateDTO,
    PaymentReviewDTO,
)
from app.services.interfaces import PaymentServiceProtocol
from app.web_admin.deps import (
    AdminPrincipal,
    BotNotifierProtocol,
    get_app_settings,
    get_bot_notifier,
    get_payment_service,
    get_translation_service,
    get_user_service,
    parse_decimal_strict,
    prefers_json,
    render_admin_template,
    require_admin_csrf,
    try_resolve_optional_service,
)

router = APIRouter(tags=["admin-payments"])

_CARD_DIGITS_RE = re.compile(r"^\d{16}$")
_SHEBA_RE = re.compile(r"^IR\d{24}$")


def _parse_int_field(val: Any, field_name: str, default: int = 0) -> int:
    if val is None or str(val).strip() == "":
        return default
    try:
        return int(val)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be an integer") from exc


def _parse_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def _generate_qr_png_bytes(data_text: str) -> bytes:
    """Render a clean QR code PNG buffer in memory."""
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(data_text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _save_validated_qr_upload(
    upload: UploadFile,
    upload_dir: str,
    max_bytes: int,
) -> str:
    """Validate magic bytes and size of a QR image upload and store safely under UPLOAD_DIR/qr/."""
    raw = await upload.read()
    mime = validate_receipt_image(
        raw,
        filename=upload.filename,
        content_type=upload.content_type,
        max_bytes=max_bytes,
    )
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
    base_dir = Path(upload_dir).resolve()
    qr_dir = base_dir / "qr"
    qr_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"qr_{uuid.uuid4().hex}.{ext}"
    dest = (qr_dir / safe_name).resolve()
    if not dest.is_relative_to(base_dir):
        raise ValidationError("Invalid upload destination path")
    await asyncio.to_thread(dest.write_bytes, raw)
    return f"qr/{safe_name}"


def _resolve_safe_upload_path(upload_dir: str, relative_or_local_path: str) -> Path:
    """Resolve a stored file path and enforce containment within UPLOAD_DIR."""
    base_dir = Path(upload_dir).resolve()
    candidate = Path(relative_or_local_path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (base_dir / candidate).resolve()
        if (
            resolved.is_relative_to(base_dir)
            and not resolved.exists()
            and candidate.parts
            and candidate.parts[0] == base_dir.name
        ):
            alt = (base_dir.parent / candidate).resolve()
            if alt.is_relative_to(base_dir):
                resolved = alt
    if not resolved.is_relative_to(base_dir):
        raise ValidationError("File path traversal blocked")
    return resolved


@router.get("/admin/payments")
@router.get("/admin/api/payments")
async def payments_overview(
    request: Request,
    status: str = "all",
    limit: int = 50,
    offset: int = 0,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
) -> Response:
    """Display filterable Payments Queue, Crypto Wallets, Iranian Fiat Methods, and Exchange Rate."""
    status_clean = status.strip().lower()
    parsed_status: PaymentStatus | None = None
    if status_clean and status_clean != "all":
        if status_clean not in {s.value for s in PaymentStatus}:
            raise ValidationError(f"Invalid payment status filter '{status}'")
        parsed_status = PaymentStatus(status_clean)

    wallets = await payment_service.list_crypto_wallets(active_only=False)
    fiat_methods = await payment_service.list_fiat_methods(active_only=False)
    exchange_rate = await payment_service.get_exchange_rate()
    payments = await payment_service.list_payments(
        status=parsed_status,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )

    if prefers_json(request):
        return JSONResponse(
            {
                "status_filter": status_clean,
                "exchange_rate": exchange_rate.model_dump(mode="json"),
                "crypto_wallets": [w.model_dump(mode="json") for w in wallets],
                "fiat_methods": [m.model_dump(mode="json") for m in fiat_methods],
                "payments": [p.model_dump(mode="json") for p in payments],
            }
        )

    return render_admin_template(
        request,
        "payments.html",
        {
            "active_nav": "payments",
            "status_filter": status_clean,
            "exchange_rate": exchange_rate,
            "crypto_wallets": wallets,
            "fiat_methods": fiat_methods,
            "payments": payments,
            "verification_modes": [m.value for m in CryptoVerificationMode],
        },
        admin=admin,
    )


@router.post("/admin/payments/wallets")
async def create_crypto_wallet_route(
    request: Request,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
) -> Response:
    """Create a dynamic cryptocurrency deposit wallet with optional QR upload."""
    settings = get_app_settings(request)
    content_type = request.headers.get("content-type", "").lower()
    qr_image_path: str | None = None

    if "application/json" in content_type:
        payload = await request.json()
        qr_image_path = payload.get("qr_image_path")
    else:
        form = await request.form()
        payload = dict(form)
        qr_upload = form.get("qr_file")
        if isinstance(qr_upload, (StarletteUploadFile, UploadFile)) and qr_upload.filename:
            qr_image_path = await _save_validated_qr_upload(
                qr_upload,
                settings.UPLOAD_DIR,
                settings.MAX_UPLOAD_SIZE_BYTES,
            )

    coin_name = str(payload.get("coin_name") or "").strip()
    symbol = str(payload.get("symbol") or "").strip().upper()
    network = str(payload.get("network") or "").strip().upper()
    wallet_address = str(payload.get("wallet_address") or "").strip()
    if not (coin_name and symbol and network and len(wallet_address) >= 4):
        raise ValidationError("coin_name, symbol, network, and a valid wallet_address are required")

    min_deposit_usd = parse_decimal_strict(
        payload.get("min_deposit_usd") or "1",
        "min_deposit_usd",
        allow_zero=True,
    )
    raw_mode = str(payload.get("verification_mode") or "manual_txid").strip().lower()
    if raw_mode not in {m.value for m in CryptoVerificationMode}:
        raise ValidationError(f"Unsupported verification_mode '{raw_mode}'")

    explorer_config: dict[str, Any] = {}
    raw_explorer = payload.get("explorer_config")
    if isinstance(raw_explorer, dict):
        explorer_config = raw_explorer
    elif isinstance(raw_explorer, str) and raw_explorer.strip():
        try:
            parsed_cfg = json.loads(raw_explorer)
            if isinstance(parsed_cfg, dict):
                explorer_config = parsed_cfg
        except json.JSONDecodeError as exc:
            raise ValidationError("explorer_config must be valid JSON object") from exc

    created = await payment_service.create_crypto_wallet(
        CryptoWalletCreateDTO(
            coin_name=coin_name,
            symbol=symbol,
            network=network,
            wallet_address=wallet_address,
            qr_image_path=qr_image_path,
            min_deposit_usd=min_deposit_usd,
            fee_note=str(payload.get("fee_note") or "").strip() or None,
            instructions=str(payload.get("instructions") or "").strip() or None,
            verification_mode=CryptoVerificationMode(raw_mode),
            explorer_config=explorer_config,
            is_active=_parse_bool(payload.get("is_active"), default=True),
            sort_order=_parse_int_field(payload.get("sort_order"), "sort_order", default=0),
        )
    )

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "wallet": created.model_dump(mode="json")}, status_code=201)

    q = urlencode({"msg": f"Crypto wallet {created.symbol} ({created.network}) added"})
    return RedirectResponse(url=f"/admin/payments?{q}", status_code=303)


@router.post("/admin/payments/wallets/{wallet_id}")
@router.put("/admin/payments/wallets/{wallet_id}")
async def update_crypto_wallet_route(
    request: Request,
    wallet_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
) -> Response:
    """Update an existing cryptocurrency wallet configuration."""
    settings = get_app_settings(request)
    content_type = request.headers.get("content-type", "").lower()
    update_kwargs: dict[str, Any] = {}

    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)
        qr_upload = form.get("qr_file")
        if isinstance(qr_upload, (StarletteUploadFile, UploadFile)) and qr_upload.filename:
            update_kwargs["qr_image_path"] = await _save_validated_qr_upload(
                qr_upload,
                settings.UPLOAD_DIR,
                settings.MAX_UPLOAD_SIZE_BYTES,
            )

    for str_field in ("coin_name", "wallet_address", "fee_note", "instructions"):
        if str_field in payload and payload[str_field] is not None:
            val = str(payload[str_field]).strip()
            if val or str_field in {"fee_note", "instructions"}:
                update_kwargs[str_field] = val or None
    if payload.get("symbol"):
        update_kwargs["symbol"] = str(payload["symbol"]).strip().upper()
    if payload.get("network"):
        update_kwargs["network"] = str(payload["network"]).strip().upper()
    if payload.get("min_deposit_usd") is not None and str(payload.get("min_deposit_usd")).strip() != "":
        update_kwargs["min_deposit_usd"] = parse_decimal_strict(
            payload["min_deposit_usd"],
            "min_deposit_usd",
            allow_zero=True,
        )
    if payload.get("verification_mode"):
        raw_mode = str(payload["verification_mode"]).strip().lower()
        if raw_mode not in {m.value for m in CryptoVerificationMode}:
            raise ValidationError(f"Unsupported verification_mode '{raw_mode}'")
        update_kwargs["verification_mode"] = CryptoVerificationMode(raw_mode)
    if "is_active" in payload:
        update_kwargs["is_active"] = _parse_bool(payload.get("is_active"), default=True)
    if payload.get("sort_order") is not None and str(payload.get("sort_order")).strip() != "":
        update_kwargs["sort_order"] = _parse_int_field(payload["sort_order"], "sort_order")

    updated = await payment_service.update_crypto_wallet(wallet_id, CryptoWalletUpdateDTO(**update_kwargs))

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "wallet": updated.model_dump(mode="json")})

    q = urlencode({"msg": f"Crypto wallet {updated.symbol} ({updated.network}) updated"})
    return RedirectResponse(url=f"/admin/payments?{q}", status_code=303)


@router.post("/admin/payments/wallets/{wallet_id}/delete")
@router.delete("/admin/payments/wallets/{wallet_id}")
async def delete_crypto_wallet_route(
    request: Request,
    wallet_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
) -> Response:
    """Delete a cryptocurrency wallet."""
    await payment_service.delete_crypto_wallet(wallet_id)
    if prefers_json(request) or request.method == "DELETE":
        return JSONResponse({"ok": True, "deleted_wallet_id": wallet_id})

    q = urlencode({"msg": "Crypto wallet deleted"})
    return RedirectResponse(url=f"/admin/payments?{q}", status_code=303)


@router.get("/admin/payments/wallets/{wallet_id}/qr.png")
async def wallet_qr_image_route(
    request: Request,
    wallet_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
) -> Response:
    """Serve uploaded wallet QR image or auto-generate a PNG QR from the wallet address."""
    settings = get_app_settings(request)
    wallets = await payment_service.list_crypto_wallets(active_only=False)
    wallet = next((w for w in wallets if w.id == wallet_id), None)
    if wallet is None:
        raise NotFoundError(f"Crypto wallet {wallet_id} not found")

    if wallet.qr_image_path:
        resolved = _resolve_safe_upload_path(settings.UPLOAD_DIR, wallet.qr_image_path)
        if resolved.is_file():
            raw = await asyncio.to_thread(resolved.read_bytes)
            mime = detect_image_mime(raw) or "image/png"
            return Response(content=raw, media_type=mime)

    png_bytes = await asyncio.to_thread(_generate_qr_png_bytes, wallet.wallet_address)
    return Response(content=png_bytes, media_type="image/png")


@router.get("/admin/payments/qr-preview")
async def qr_preview_route(
    text: str,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
) -> Response:
    """Generate a live QR code PNG preview for an arbitrary wallet address string."""
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 512:
        raise ValidationError("QR preview text must be between 1 and 512 characters")
    png_bytes = await asyncio.to_thread(_generate_qr_png_bytes, cleaned)
    return Response(content=png_bytes, media_type="image/png")


@router.post("/admin/payments/fiat")
async def create_fiat_method_route(
    request: Request,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
) -> Response:
    """Create an Iranian Rial/Toman Fiat payment method (Card-to-Card or Gateway)."""
    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    raw_type = str(payload.get("method_type") or "card_to_card").strip().lower()
    if raw_type not in {"card_to_card", "gateway"}:
        raise ValidationError("method_type must be 'card_to_card' or 'gateway'")
    method_type = FiatMethodType(raw_type)

    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValidationError("Fiat method title is required")

    card_number: str | None = None
    raw_card = str(payload.get("card_number") or "").strip().replace("-", "").replace(" ", "")
    if raw_card:
        if not _CARD_DIGITS_RE.match(raw_card):
            raise ValidationError("Card number must be exactly 16 digits")
        card_number = raw_card
    elif method_type == FiatMethodType.CARD_TO_CARD:
        raise ValidationError("16-digit card_number is required for card_to_card method")

    sheba = str(payload.get("sheba") or "").strip().upper().replace(" ", "") or None
    if sheba is not None and not _SHEBA_RE.match(sheba):
        raise ValidationError("Sheba (IBAN) must match IR followed by 24 digits")
    cardholder_name = str(payload.get("cardholder_name") or "").strip() or None
    bank_name = str(payload.get("bank_name") or "").strip() or None
    gateway_provider = str(payload.get("gateway_provider") or "").strip() or None
    gateway_merchant_id = str(payload.get("gateway_merchant_id") or "").strip() or None
    min_deposit_irt = parse_decimal_strict(
        payload.get("min_deposit_irt") or "50000",
        "min_deposit_irt",
        allow_zero=True,
    )

    created = await payment_service.create_fiat_method(
        FiatMethodCreateDTO(
            method_type=method_type,
            title=title,
            card_number=card_number,
            sheba=sheba,
            cardholder_name=cardholder_name,
            bank_name=bank_name,
            gateway_provider=gateway_provider,
            gateway_merchant_id=gateway_merchant_id,
            min_deposit_irt=min_deposit_irt,
            instructions=str(payload.get("instructions") or "").strip() or None,
            is_active=_parse_bool(payload.get("is_active"), default=True),
            sort_order=_parse_int_field(payload.get("sort_order"), "sort_order", default=0),
        )
    )

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "fiat_method": created.model_dump(mode="json")}, status_code=201)

    q = urlencode({"msg": f"Fiat method '{created.title}' created"})
    return RedirectResponse(url=f"/admin/payments?{q}", status_code=303)


@router.post("/admin/payments/fiat/{method_id}")
@router.put("/admin/payments/fiat/{method_id}")
async def update_fiat_method_route(
    request: Request,
    method_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
) -> Response:
    """Update an Iranian Fiat payment method."""
    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    update_kwargs: dict[str, Any] = {}
    if payload.get("method_type"):
        raw_t = str(payload["method_type"]).strip().lower()
        if raw_t not in {"card_to_card", "gateway"}:
            raise ValidationError("method_type must be 'card_to_card' or 'gateway'")
        update_kwargs["method_type"] = FiatMethodType(raw_t)
    if payload.get("title"):
        update_kwargs["title"] = str(payload["title"]).strip()
    if "card_number" in payload and payload.get("card_number") is not None:
        raw_card = str(payload["card_number"]).strip().replace("-", "").replace(" ", "")
        if raw_card:
            if not _CARD_DIGITS_RE.match(raw_card):
                raise ValidationError("Card number must be exactly 16 digits")
            update_kwargs["card_number"] = raw_card
    for field_name in ("sheba", "cardholder_name", "bank_name", "gateway_provider", "gateway_merchant_id", "instructions"):
        if field_name in payload and payload[field_name] is not None:
            val = str(payload[field_name]).strip()
            if field_name == "sheba" and val:
                val = val.upper().replace(" ", "")
                if not _SHEBA_RE.match(val):
                    raise ValidationError("Sheba (IBAN) must match IR followed by 24 digits")
            if val or field_name == "instructions":
                update_kwargs[field_name] = val or None
    if payload.get("min_deposit_irt") is not None and str(payload.get("min_deposit_irt")).strip() != "":
        update_kwargs["min_deposit_irt"] = parse_decimal_strict(
            payload["min_deposit_irt"],
            "min_deposit_irt",
            allow_zero=True,
        )
    if "is_active" in payload:
        update_kwargs["is_active"] = _parse_bool(payload.get("is_active"), default=True)
    if payload.get("sort_order") is not None and str(payload.get("sort_order")).strip() != "":
        update_kwargs["sort_order"] = _parse_int_field(payload["sort_order"], "sort_order")

    updated = await payment_service.update_fiat_method(method_id, FiatMethodUpdateDTO(**update_kwargs))

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "fiat_method": updated.model_dump(mode="json")})

    q = urlencode({"msg": f"Fiat method '{updated.title}' updated"})
    return RedirectResponse(url=f"/admin/payments?{q}", status_code=303)


@router.post("/admin/payments/fiat/{method_id}/delete")
@router.delete("/admin/payments/fiat/{method_id}")
async def delete_fiat_method_route(
    request: Request,
    method_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
) -> Response:
    """Delete an Iranian Fiat payment method."""
    await payment_service.delete_fiat_method(method_id)
    if prefers_json(request) or request.method == "DELETE":
        return JSONResponse({"ok": True, "deleted_fiat_method_id": method_id})

    q = urlencode({"msg": "Fiat payment method deleted"})
    return RedirectResponse(url=f"/admin/payments?{q}", status_code=303)


@router.post("/admin/payments/rate")
async def update_exchange_rate_route(
    request: Request,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
) -> Response:
    """Update the USD-to-Toman (IRT) exchange rate and rate mode."""
    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    rate = parse_decimal_strict(
        payload.get("usd_to_toman_rate") or payload.get("rate"),
        "usd_to_toman_rate",
        allow_zero=False,
        allow_negative=False,
    )
    rate_mode = str(payload.get("rate_mode") or "manual").strip().lower()
    if rate_mode not in {"manual", "live"}:
        raise ValidationError("rate_mode must be 'manual' or 'live'")

    updated = await payment_service.update_exchange_rate(rate, rate_mode=rate_mode)

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "exchange_rate": updated.model_dump(mode="json")})

    q = urlencode({"msg": f"Exchange rate updated to {updated.usd_to_toman_rate} IRT ({updated.rate_mode})"})
    return RedirectResponse(url=f"/admin/payments?{q}", status_code=303)


@router.get("/admin/payments/{payment_id}/receipt")
async def view_payment_receipt_route(
    request: Request,
    payment_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
) -> Response:
    """Serve a validated payment receipt image stored under UPLOAD_DIR."""
    settings = get_app_settings(request)
    payment = await payment_service.get_payment(payment_id)
    if payment.receipt_local_path:
        resolved = _resolve_safe_upload_path(settings.UPLOAD_DIR, payment.receipt_local_path)
        if not resolved.is_file():
            raise NotFoundError("Receipt file not found on disk")
        raw = await asyncio.to_thread(resolved.read_bytes)
        mime = detect_image_mime(raw)
        if mime is None:
            raise ValidationError("Stored receipt file failed image signature check")
        return Response(content=raw, media_type=mime)

    bot = getattr(request.app.state, "bot", None)
    if payment.receipt_file_id and bot is not None:
        tg_file = await bot.get_file(payment.receipt_file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        raw = buf.getvalue()
        mime = detect_image_mime(raw)
        if mime is None:
            raise ValidationError("Telegram receipt file failed image signature check")
        return Response(content=raw, media_type=mime)

    raise NotFoundError("No receipt image stored for this payment")


async def _handle_payment_review(
    request: Request,
    payment_id: int,
    *,
    default_approve: bool | None,
    admin: AdminPrincipal,
    payment_service: PaymentServiceProtocol,
    translation_service: TranslationService,
    bot_notifier: BotNotifierProtocol,
) -> Response:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)

    if default_approve is not None:
        approve = default_approve
    elif "approve" in payload:
        approve = _parse_bool(payload["approve"])
    else:
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"approve", "reject"}:
            raise ValidationError("Review action must be 'approve' or 'reject'")
        approve = action == "approve"

    admin_note = str(payload.get("admin_note") or payload.get("reason") or "").strip() or None

    reviewed = await payment_service.review_payment(
        PaymentReviewDTO(
            payment_id=payment_id,
            admin_id=admin.admin_id,
            approve=approve,
            admin_note=admin_note,
        )
    )

    user_svc = await try_resolve_optional_service(request, get_user_service)
    if user_svc is not None:
        try:
            user = await user_svc.get_by_id(reviewed.user_id)
            key = "deposit.approved_notify" if approve else "deposit.rejected_notify"
            amt_str = str(reviewed.amount_usd if reviewed.currency.value == "USD" else reviewed.amount_irt)
            msg = await translation_service.get_text(
                key,
                user.language_code,
                payment_id=reviewed.id,
                amount=f"{amt_str} {reviewed.currency.value}",
                currency=reviewed.currency.value,
                reason=escape_html(admin_note or "-"),
            )
            await bot_notifier.send_direct_message(user.telegram_id, msg)
        except Exception as exc:
            logger.warning("Payment notification skipped for payment %s: %s", reviewed.id, exc)

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "payment": reviewed.model_dump(mode="json")})

    q = urlencode({"msg": f"Payment #{payment_id} {'approved' if approve else 'rejected'}"})
    return RedirectResponse(url=f"/admin/payments?{q}", status_code=303)


@router.post("/admin/payments/{payment_id}/review")
async def review_payment_route(
    request: Request,
    payment_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
    translation_service: TranslationService = Depends(get_translation_service),
    bot_notifier: BotNotifierProtocol = Depends(get_bot_notifier),
) -> Response:
    """Approve or reject a pending payment inside an ACID transaction."""
    return await _handle_payment_review(
        request,
        payment_id,
        default_approve=None,
        admin=admin,
        payment_service=payment_service,
        translation_service=translation_service,
        bot_notifier=bot_notifier,
    )


@router.post("/admin/payments/{payment_id}/approve")
async def approve_payment_route(
    request: Request,
    payment_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
    translation_service: TranslationService = Depends(get_translation_service),
    bot_notifier: BotNotifierProtocol = Depends(get_bot_notifier),
) -> Response:
    """1-click approve and credit a pending payment."""
    return await _handle_payment_review(
        request,
        payment_id,
        default_approve=True,
        admin=admin,
        payment_service=payment_service,
        translation_service=translation_service,
        bot_notifier=bot_notifier,
    )


@router.post("/admin/payments/{payment_id}/reject")
async def reject_payment_route(
    request: Request,
    payment_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
    translation_service: TranslationService = Depends(get_translation_service),
    bot_notifier: BotNotifierProtocol = Depends(get_bot_notifier),
) -> Response:
    """1-click reject a pending payment with optional rejection note."""
    return await _handle_payment_review(
        request,
        payment_id,
        default_approve=False,
        admin=admin,
        payment_service=payment_service,
        translation_service=translation_service,
        bot_notifier=bot_notifier,
    )
