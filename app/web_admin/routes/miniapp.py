"""Telegram Mini App Shop (/miniapp and /webapp) with initData HMAC, CSS Hex Colors, and 12-Language Routing."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.core.constants import (
    Currency,
    FIAT_ENABLED_LANGUAGES,
    LANGUAGE_ORDER,
    SUPPORTED_LANGUAGES,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.locales.i18n import TranslationService
from app.services.dtos import PurchaseRequestDTO, UserCreateOrUpdateDTO
from app.services.interfaces import (
    PaymentServiceProtocol,
    ShopServiceProtocol,
    UserServiceProtocol,
)
from app.web_admin.deps import (
    MiniAppPrincipal,
    get_app_settings,
    get_miniapp_principal,
    get_payment_service,
    get_shop_service,
    get_translation_service,
    get_user_service,
    templates,
)

router = APIRouter(tags=["miniapp"])


def _serialize_languages() -> list[dict[str, Any]]:
    return [
        {
            "code": code,
            "native_name": SUPPORTED_LANGUAGES[code].native_name,
            "english_name": SUPPORTED_LANGUAGES[code].english_name,
            "flag": SUPPORTED_LANGUAGES[code].flag,
            "is_rtl": SUPPORTED_LANGUAGES[code].is_rtl,
            "supports_fiat_irr": SUPPORTED_LANGUAGES[code].supports_fiat_irr,
        }
        for code in LANGUAGE_ORDER
    ]


def _enforce_region_payment_policy(
    lang_code: str,
    methods_dump: dict[str, Any],
) -> dict[str, Any]:
    """Guarantee non-Persian languages receive strictly crypto-only deposit methods."""
    normalized = TranslationService.normalize_lang(lang_code)
    if normalized not in FIAT_ENABLED_LANGUAGES:
        methods_dump["fiat_enabled"] = False
        methods_dump["fiat_methods"] = []
    else:
        methods_dump["fiat_enabled"] = True
    methods_dump["lang_code"] = normalized
    return methods_dump


@router.get("/miniapp")
@router.get("/miniapp/")
@router.get("/webapp")
@router.get("/webapp/")
async def miniapp_shell(
    request: Request,
    lang: str | None = None,
) -> Response:
    """Serve the Telegram Mini App Shop HTML interface."""
    settings = get_app_settings(request)
    initial_lang = TranslationService.normalize_lang(lang or settings.DEFAULT_LANGUAGE)
    lang_info = SUPPORTED_LANGUAGES[initial_lang]

    return templates.TemplateResponse(
        request=request,
        name="miniapp.html",
        context={
            "request": request,
            "app_name": settings.APP_NAME,
            "initial_lang": initial_lang,
            "is_rtl": lang_info.is_rtl,
            "languages": _serialize_languages(),
        },
    )


@router.get("/miniapp/api/bootstrap")
@router.get("/miniapp/api/catalog")
@router.get("/webapp/api/bootstrap")
async def miniapp_bootstrap_api(
    lang: str | None = None,
    principal: MiniAppPrincipal = Depends(get_miniapp_principal),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
    translation_service: TranslationService = Depends(get_translation_service),
) -> JSONResponse:
    """Authenticate Mini App via initData HMAC and return catalog, CSS Hex colors, 12-lang strings, and region-aware deposit methods."""
    active_lang = TranslationService.normalize_lang(lang or principal.language_code)
    lang_info = SUPPORTED_LANGUAGES[active_lang]

    categories = await shop_service.list_categories(active_only=True)
    products = await shop_service.list_products(active_only=True)
    available_methods = await payment_service.get_available_methods_for_language(active_lang)
    methods_payload = _enforce_region_payment_policy(
        active_lang,
        available_methods.model_dump(mode="json"),
    )

    buttons = await translation_service.get_main_menu_buttons(active_lang)
    translations = await translation_service.get_language_map(active_lang)

    return JSONResponse(
        {
            "user": (
                principal.user.model_dump(mode="json")
                if principal.user is not None
                else {
                    "telegram_id": principal.telegram_id,
                    "username": principal.username,
                    "first_name": principal.first_name,
                    "language_code": active_lang,
                    "balance_usd": "0",
                    "balance_irt": "0",
                }
            ),
            "lang_code": active_lang,
            "is_rtl": lang_info.is_rtl,
            "supports_fiat_irr": lang_info.supports_fiat_irr,
            "languages": _serialize_languages(),
            "categories": [c.model_dump(mode="json") for c in categories],
            "products": [p.model_dump(mode="json") for p in products],
            "ui_buttons": [b.model_dump(mode="json") for b in buttons],
            "payment_methods": methods_payload,
            "translations": translations,
        }
    )


@router.get("/miniapp/api/products/{product_id}")
@router.get("/webapp/api/products/{product_id}")
async def miniapp_product_detail_api(
    product_id: int,
    _principal: MiniAppPrincipal = Depends(get_miniapp_principal),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> JSONResponse:
    """Return ProductDetailDTO with bulk discount tiers and live CSS Hex color."""
    detail = await shop_service.get_product_detail(product_id)
    if not detail.is_active:
        raise NotFoundError("Product is not currently active")
    return JSONResponse({"product": detail.model_dump(mode="json")})


@router.post("/miniapp/api/language")
@router.post("/webapp/api/language")
async def miniapp_switch_language_api(
    request: Request,
    principal: MiniAppPrincipal = Depends(get_miniapp_principal),
    user_service: UserServiceProtocol = Depends(get_user_service),
    payment_service: PaymentServiceProtocol = Depends(get_payment_service),
    translation_service: TranslationService = Depends(get_translation_service),
) -> JSONResponse:
    """Switch the Mini App user's active language across the 12 supported locales."""
    body = await request.json()
    raw_lang = str(body.get("lang_code") or body.get("language_code") or "").strip().lower()
    if raw_lang not in SUPPORTED_LANGUAGES:
        raise ValidationError(f"Unsupported language code '{raw_lang}'")

    updated_user = await user_service.set_language(principal.telegram_id, raw_lang)
    available_methods = await payment_service.get_available_methods_for_language(raw_lang)
    methods_payload = _enforce_region_payment_policy(
        raw_lang,
        available_methods.model_dump(mode="json"),
    )
    translations = await translation_service.get_language_map(raw_lang)
    buttons = await translation_service.get_main_menu_buttons(raw_lang)
    lang_info = SUPPORTED_LANGUAGES[raw_lang]

    return JSONResponse(
        {
            "ok": True,
            "lang_code": raw_lang,
            "is_rtl": lang_info.is_rtl,
            "supports_fiat_irr": lang_info.supports_fiat_irr,
            "user": updated_user.model_dump(mode="json"),
            "payment_methods": methods_payload,
            "ui_buttons": [b.model_dump(mode="json") for b in buttons],
            "translations": translations,
        }
    )


@router.post("/miniapp/api/purchase")
@router.post("/webapp/api/purchase")
async def miniapp_purchase_api(
    request: Request,
    principal: MiniAppPrincipal = Depends(get_miniapp_principal),
    user_service: UserServiceProtocol = Depends(get_user_service),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> JSONResponse:
    """Execute an atomic product purchase from the Telegram Mini App."""
    body = await request.json()
    try:
        product_id = int(body.get("product_id") or 0)
        quantity = int(body.get("quantity") or 1)
    except (TypeError, ValueError) as exc:
        raise ValidationError("product_id and quantity must be valid integers") from exc

    if product_id <= 0 or quantity < 1 or quantity > 1000:
        raise ValidationError("quantity must be between 1 and 1000")

    raw_currency = str(body.get("currency") or "USD").strip().upper()
    if raw_currency not in {"USD", "IRT"}:
        raise ValidationError("currency must be USD or IRT")
    currency = Currency(raw_currency)

    user_dto = principal.user
    if user_dto is None:
        user_dto = await user_service.upsert_telegram_user(
            UserCreateOrUpdateDTO(
                telegram_id=principal.telegram_id,
                username=principal.username,
                first_name=principal.first_name,
                last_name=principal.last_name,
                language_code=principal.language_code,
            )
        )

    active_lang = TranslationService.normalize_lang(user_dto.language_code)
    if currency == Currency.IRT and active_lang not in FIAT_ENABLED_LANGUAGES:
        raise ValidationError("IRT purchases are only available for Persian (fa) language")

    fulfillment_input = str(body.get("fulfillment_input") or "").strip() or None

    purchase_result = await shop_service.purchase_product(
        PurchaseRequestDTO(
            user_id=user_dto.id,
            product_id=product_id,
            quantity=quantity,
            currency=currency,
            fulfillment_input=fulfillment_input,
        )
    )

    return JSONResponse(
        {
            "ok": True,
            "purchase": purchase_result.model_dump(mode="json"),
        }
    )


@router.get("/miniapp/api/orders")
@router.get("/webapp/api/orders")
async def miniapp_orders_api(
    principal: MiniAppPrincipal = Depends(get_miniapp_principal),
    user_service: UserServiceProtocol = Depends(get_user_service),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> JSONResponse:
    """List the authenticated Mini App user's order history."""
    user_dto = principal.user
    if user_dto is None:
        user_dto = await user_service.upsert_telegram_user(
            UserCreateOrUpdateDTO(
                telegram_id=principal.telegram_id,
                username=principal.username,
                first_name=principal.first_name,
                last_name=principal.last_name,
                language_code=principal.language_code,
            )
        )
    orders = await shop_service.list_user_orders(user_dto.id, limit=30)
    return JSONResponse({"orders": [o.model_dump(mode="json") for o in orders]})
