"""Catalog CRUD: Categories, Products, Dual-Layer Color Picker, Bulk Tiers, and Stock Import."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.constants import DeliveryType, LANGUAGE_ORDER
from app.core.exceptions import ValidationError
from app.core.security import validate_stock_upload_file
from app.services.dtos import (
    CategoryCreateDTO,
    CategoryUpdateDTO,
    ProductBulkTierDTO,
    ProductCreateDTO,
    ProductUpdateDTO,
)
from app.services.interfaces import ShopServiceProtocol
from app.web_admin.deps import (
    AdminPrincipal,
    get_app_settings,
    get_shop_service,
    parse_color_emoji_strict,
    parse_color_hex_strict,
    parse_decimal_strict,
    prefers_json,
    render_admin_template,
    require_admin_csrf,
)

router = APIRouter(tags=["admin-catalog"])


def _extract_localized_map(
    payload: dict[str, Any],
    *,
    json_key: str,
    prefix: str,
    fallback_field: str | None = None,
) -> dict[str, str]:
    """Extract 12-language text map from JSON dictionary or form fields (`<prefix>_<lang>`)."""
    result: dict[str, str] = {}
    raw_map = payload.get(json_key)
    if isinstance(raw_map, dict):
        for lang, text in raw_map.items():
            if lang in LANGUAGE_ORDER and isinstance(text, str) and text.strip():
                result[lang] = text.strip()

    for lang in LANGUAGE_ORDER:
        form_val = payload.get(f"{prefix}_{lang}")
        if isinstance(form_val, str) and form_val.strip():
            result[lang] = form_val.strip()

    if fallback_field and fallback_field in payload:
        fb = str(payload[fallback_field] or "").strip()
        if fb and "en" not in result:
            result["en"] = fb
    return result


def _parse_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


@router.get("/admin/catalog")
@router.get("/admin/api/catalog")
async def catalog_overview(
    request: Request,
    category_id: int | None = None,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """List categories and products with dual-layer color badges and stock levels."""
    categories = await shop_service.list_categories(active_only=False)
    products = await shop_service.list_products(
        category_id=category_id,
        active_only=False,
        pinned_only=False,
    )

    if prefers_json(request):
        return JSONResponse(
            {
                "categories": [c.model_dump(mode="json") for c in categories],
                "products": [p.model_dump(mode="json") for p in products],
            }
        )

    return render_admin_template(
        request,
        "catalog.html",
        {
            "active_nav": "catalog",
            "categories": categories,
            "products": products,
            "selected_category_id": category_id,
        },
        admin=admin,
    )


@router.post("/admin/catalog/categories")
async def create_category_route(
    request: Request,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Create a new product category with 12-language names and dual-layer color config."""
    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    slug = str(payload.get("slug") or "").strip()
    if not slug:
        raise ValidationError("Category slug is required")

    color_hex = parse_color_hex_strict(str(payload.get("color_hex") or "#3b82f6"))
    color_emoji = parse_color_emoji_strict(str(payload.get("color_emoji") or "🔵"), default="🔵")
    try:
        sort_order = int(payload.get("sort_order") or 0)
    except (TypeError, ValueError) as exc:
        raise ValidationError("sort_order must be an integer") from exc

    is_active = _parse_bool(payload.get("is_active"), default=True)
    names_json = _extract_localized_map(
        payload,
        json_key="names_json",
        prefix="name",
        fallback_field="name",
    )
    descriptions_json = _extract_localized_map(
        payload,
        json_key="descriptions_json",
        prefix="description",
        fallback_field="description",
    )

    created = await shop_service.create_category(
        CategoryCreateDTO(
            slug=slug,
            sort_order=sort_order,
            is_active=is_active,
            color_emoji=color_emoji,
            color_hex=color_hex,
            names_json=names_json,
            descriptions_json=descriptions_json,
        )
    )

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "category": created.model_dump(mode="json")}, status_code=201)

    q = urlencode({"msg": f"Category '{created.slug}' created"})
    return RedirectResponse(url=f"/admin/catalog?{q}", status_code=303)


@router.post("/admin/catalog/categories/{category_id}")
@router.put("/admin/catalog/categories/{category_id}")
async def update_category_route(
    request: Request,
    category_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Update an existing category's localized names, sort order, or dual-layer colors."""
    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    update_kwargs: dict[str, Any] = {}
    if payload.get("slug"):
        update_kwargs["slug"] = str(payload["slug"]).strip()
    if payload.get("sort_order") is not None and str(payload.get("sort_order")).strip() != "":
        try:
            update_kwargs["sort_order"] = int(payload["sort_order"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("sort_order must be an integer") from exc
    if "is_active" in payload:
        update_kwargs["is_active"] = _parse_bool(payload.get("is_active"), default=True)
    if payload.get("color_emoji"):
        update_kwargs["color_emoji"] = parse_color_emoji_strict(str(payload["color_emoji"]))
    if payload.get("color_hex"):
        update_kwargs["color_hex"] = parse_color_hex_strict(str(payload["color_hex"]))

    names_json = _extract_localized_map(
        payload,
        json_key="names_json",
        prefix="name",
        fallback_field="name",
    )
    if names_json:
        update_kwargs["names_json"] = names_json

    descriptions_json = _extract_localized_map(
        payload,
        json_key="descriptions_json",
        prefix="description",
        fallback_field="description",
    )
    if descriptions_json:
        update_kwargs["descriptions_json"] = descriptions_json

    updated = await shop_service.update_category(category_id, CategoryUpdateDTO(**update_kwargs))

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "category": updated.model_dump(mode="json")})

    q = urlencode({"msg": f"Category '{updated.slug}' updated"})
    return RedirectResponse(url=f"/admin/catalog?{q}", status_code=303)


@router.post("/admin/catalog/categories/{category_id}/delete")
@router.delete("/admin/catalog/categories/{category_id}")
async def delete_category_route(
    request: Request,
    category_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Delete a category by ID."""
    await shop_service.delete_category(category_id)
    if prefers_json(request) or request.method == "DELETE":
        return JSONResponse({"ok": True, "deleted_category_id": category_id})

    q = urlencode({"msg": "Category deleted"})
    return RedirectResponse(url=f"/admin/catalog?{q}", status_code=303)


@router.get("/admin/catalog/products/{product_id}")
@router.get("/admin/api/catalog/products/{product_id}")
async def product_detail_view(
    request: Request,
    product_id: int,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """View and edit product details, 12-lang texts, dual-layer colors, bulk tiers, and stock."""
    product = await shop_service.get_product_detail(product_id)
    categories = await shop_service.list_categories(active_only=False)

    if prefers_json(request):
        return JSONResponse(
            {
                "product": product.model_dump(mode="json"),
                "categories": [c.model_dump(mode="json") for c in categories],
            }
        )

    return render_admin_template(
        request,
        "product_detail.html",
        {
            "active_nav": "catalog",
            "product": product,
            "categories": categories,
        },
        admin=admin,
    )


@router.post("/admin/catalog/products")
async def create_product_route(
    request: Request,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Create a new digital product with Decimal pricing and dual-layer color styling."""
    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    try:
        category_id = int(payload.get("category_id") or 0)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Valid category_id is required") from exc
    if category_id <= 0:
        raise ValidationError("Valid category_id is required")

    slug = str(payload.get("slug") or "").strip()
    if not slug:
        raise ValidationError("Product slug is required")

    raw_delivery = str(payload.get("delivery_type") or "auto").strip().lower()
    if raw_delivery not in {"auto", "manual"}:
        raise ValidationError("delivery_type must be 'auto' or 'manual'")

    price_usd = parse_decimal_strict(payload.get("price_usd"), "price_usd", allow_zero=True)
    raw_irt = payload.get("price_irt")
    price_irt = (
        parse_decimal_strict(raw_irt, "price_irt", allow_zero=True)
        if raw_irt is not None and str(raw_irt).strip() != ""
        else None
    )

    color_hex = parse_color_hex_strict(str(payload.get("color_hex") or "#22c55e"))
    color_emoji = parse_color_emoji_strict(str(payload.get("color_emoji") or "🟢"), default="🟢")

    try:
        sort_order = int(payload.get("sort_order") or 0)
        manual_stock_count = int(payload.get("manual_stock_count") or 0)
        low_stock_threshold = int(payload.get("low_stock_threshold") or 5)
    except (TypeError, ValueError) as exc:
        raise ValidationError("sort_order, manual_stock_count, and low_stock_threshold must be integers") from exc

    is_pinned = _parse_bool(payload.get("is_pinned"), default=False)
    is_active = _parse_bool(payload.get("is_active"), default=True)
    manual_prompt_key = str(payload.get("manual_prompt_key") or "").strip() or None

    titles_json = _extract_localized_map(
        payload,
        json_key="titles_json",
        prefix="title",
        fallback_field="title",
    )
    descriptions_json = _extract_localized_map(
        payload,
        json_key="descriptions_json",
        prefix="description",
        fallback_field="description",
    )
    rules_json = _extract_localized_map(
        payload,
        json_key="rules_json",
        prefix="rules",
        fallback_field="rules",
    )
    manual_prompts_json = _extract_localized_map(
        payload,
        json_key="manual_prompts_json",
        prefix="manual_prompt",
    )

    created = await shop_service.create_product(
        ProductCreateDTO(
            category_id=category_id,
            slug=slug,
            delivery_type=DeliveryType(raw_delivery),
            manual_prompt_key=manual_prompt_key,
            price_usd=price_usd,
            price_irt=price_irt,
            color_emoji=color_emoji,
            color_hex=color_hex,
            is_pinned=is_pinned,
            is_active=is_active,
            sort_order=sort_order,
            manual_stock_count=manual_stock_count,
            low_stock_threshold=low_stock_threshold,
            titles_json=titles_json,
            descriptions_json=descriptions_json,
            rules_json=rules_json,
            manual_prompts_json=manual_prompts_json,
        )
    )

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "product": created.model_dump(mode="json")}, status_code=201)

    q = urlencode({"msg": f"Product '{created.slug}' created"})
    return RedirectResponse(url=f"/admin/catalog/products/{created.id}?{q}", status_code=303)


@router.post("/admin/catalog/products/{product_id}")
@router.put("/admin/catalog/products/{product_id}")
async def update_product_route(
    request: Request,
    product_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Update product pricing, localized content, pin status, or dual-layer color settings."""
    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    update_kwargs: dict[str, Any] = {}
    if payload.get("category_id") is not None and str(payload.get("category_id")).strip() != "":
        try:
            update_kwargs["category_id"] = int(payload["category_id"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("category_id must be an integer") from exc
    if payload.get("slug"):
        update_kwargs["slug"] = str(payload["slug"]).strip()
    if payload.get("delivery_type"):
        raw_dt = str(payload["delivery_type"]).strip().lower()
        if raw_dt not in {"auto", "manual"}:
            raise ValidationError("delivery_type must be 'auto' or 'manual'")
        update_kwargs["delivery_type"] = DeliveryType(raw_dt)
    if "manual_prompt_key" in payload:
        update_kwargs["manual_prompt_key"] = str(payload.get("manual_prompt_key") or "").strip() or None
    if payload.get("price_usd") is not None and str(payload.get("price_usd")).strip() != "":
        update_kwargs["price_usd"] = parse_decimal_strict(
            payload["price_usd"],
            "price_usd",
            allow_zero=True,
        )
    if "price_irt" in payload:
        raw_irt = payload.get("price_irt")
        update_kwargs["price_irt"] = (
            parse_decimal_strict(raw_irt, "price_irt", allow_zero=True)
            if raw_irt is not None and str(raw_irt).strip() != ""
            else None
        )
    if payload.get("color_emoji"):
        update_kwargs["color_emoji"] = parse_color_emoji_strict(str(payload["color_emoji"]))
    if payload.get("color_hex"):
        update_kwargs["color_hex"] = parse_color_hex_strict(str(payload["color_hex"]))
    if "is_pinned" in payload:
        update_kwargs["is_pinned"] = _parse_bool(payload.get("is_pinned"))
    if "is_active" in payload:
        update_kwargs["is_active"] = _parse_bool(payload.get("is_active"), default=True)
    try:
        if payload.get("sort_order") is not None and str(payload.get("sort_order")).strip() != "":
            update_kwargs["sort_order"] = int(payload["sort_order"])
        if payload.get("manual_stock_count") is not None and str(payload.get("manual_stock_count")).strip() != "":
            update_kwargs["manual_stock_count"] = int(payload["manual_stock_count"])
        if payload.get("low_stock_threshold") is not None and str(payload.get("low_stock_threshold")).strip() != "":
            update_kwargs["low_stock_threshold"] = int(payload["low_stock_threshold"])
    except (TypeError, ValueError) as exc:
        raise ValidationError("sort_order, manual_stock_count, and low_stock_threshold must be integers") from exc

    titles_json = _extract_localized_map(
        payload,
        json_key="titles_json",
        prefix="title",
        fallback_field="title",
    )
    if titles_json:
        update_kwargs["titles_json"] = titles_json

    descriptions_json = _extract_localized_map(
        payload,
        json_key="descriptions_json",
        prefix="description",
        fallback_field="description",
    )
    if descriptions_json:
        update_kwargs["descriptions_json"] = descriptions_json

    rules_json = _extract_localized_map(
        payload,
        json_key="rules_json",
        prefix="rules",
        fallback_field="rules",
    )
    if rules_json:
        update_kwargs["rules_json"] = rules_json

    manual_prompts_json = _extract_localized_map(
        payload,
        json_key="manual_prompts_json",
        prefix="manual_prompt",
    )
    if manual_prompts_json:
        update_kwargs["manual_prompts_json"] = manual_prompts_json

    updated = await shop_service.update_product(product_id, ProductUpdateDTO(**update_kwargs))

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "product": updated.model_dump(mode="json")})

    q = urlencode({"msg": f"Product '{updated.slug}' updated"})
    return RedirectResponse(url=f"/admin/catalog/products/{product_id}?{q}", status_code=303)


@router.post("/admin/catalog/products/{product_id}/pin")
async def toggle_product_pin_route(
    request: Request,
    product_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Pin or unpin a featured product on the Main Menu."""
    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    if "is_pinned" in payload:
        is_pinned = _parse_bool(payload["is_pinned"])
    else:
        detail = await shop_service.get_product_detail(product_id)
        is_pinned = not detail.is_pinned

    updated = await shop_service.update_product(product_id, ProductUpdateDTO(is_pinned=is_pinned))

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "product": updated.model_dump(mode="json")})

    q = urlencode({"msg": f"Product {'pinned' if updated.is_pinned else 'unpinned'}"})
    return RedirectResponse(url=f"/admin/catalog?{q}", status_code=303)


@router.post("/admin/catalog/products/{product_id}/delete")
@router.delete("/admin/catalog/products/{product_id}")
async def delete_product_route(
    request: Request,
    product_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Delete a product by ID."""
    await shop_service.delete_product(product_id)
    if prefers_json(request) or request.method == "DELETE":
        return JSONResponse({"ok": True, "deleted_product_id": product_id})

    q = urlencode({"msg": "Product deleted"})
    return RedirectResponse(url=f"/admin/catalog?{q}", status_code=303)


@router.post("/admin/catalog/products/{product_id}/tiers")
async def set_product_bulk_tiers_route(
    request: Request,
    product_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Configure bulk quantity discount tiers using strict Decimal pricing."""
    content_type = request.headers.get("content-type", "").lower()
    tiers: list[ProductBulkTierDTO] = []

    if "application/json" in content_type:
        body = await request.json()
        raw_tiers = body.get("tiers", []) if isinstance(body, dict) else body
        if not isinstance(raw_tiers, list):
            raise ValidationError("tiers must be a list")
        for item in raw_tiers:
            if not isinstance(item, dict) or "min_qty" not in item or "unit_price_usd" not in item:
                raise ValidationError("Each bulk tier must specify min_qty and unit_price_usd")
            try:
                min_qty = int(item["min_qty"])
            except (TypeError, ValueError) as exc:
                raise ValidationError("Bulk tier min_qty must be an integer >= 2") from exc
            if min_qty < 2:
                raise ValidationError("Bulk tier min_qty must be >= 2")
            usd = parse_decimal_strict(item["unit_price_usd"], "unit_price_usd", allow_zero=True)
            irt = (
                parse_decimal_strict(item["unit_price_irt"], "unit_price_irt", allow_zero=True)
                if item.get("unit_price_irt") is not None and str(item.get("unit_price_irt")).strip() != ""
                else None
            )
            tiers.append(ProductBulkTierDTO(min_qty=min_qty, unit_price_usd=usd, unit_price_irt=irt))
    else:
        form = await request.form()
        tiers_text = str(form.get("tiers_text") or "").strip()
        if tiers_text:
            for raw_line in tiers_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.replace("=", ":").split(":")]
                if len(parts) < 2:
                    raise ValidationError(f"Invalid tier line '{line}'; expected min_qty:usd_price[:irt_price]")
                try:
                    min_qty = int(parts[0])
                except ValueError as exc:
                    raise ValidationError(f"Invalid min_qty in tier line '{line}'") from exc
                if min_qty < 2:
                    raise ValidationError("Bulk tier min_qty must be >= 2")
                usd = parse_decimal_strict(parts[1], "unit_price_usd", allow_zero=True)
                irt = (
                    parse_decimal_strict(parts[2], "unit_price_irt", allow_zero=True)
                    if len(parts) >= 3 and parts[2]
                    else None
                )
                tiers.append(ProductBulkTierDTO(min_qty=min_qty, unit_price_usd=usd, unit_price_irt=irt))
        else:
            min_qtys = form.getlist("min_qty")
            usd_prices = form.getlist("unit_price_usd")
            irt_prices = form.getlist("unit_price_irt")
            for idx, raw_q in enumerate(min_qtys):
                if not str(raw_q).strip():
                    continue
                try:
                    min_qty = int(str(raw_q).strip())
                except ValueError as exc:
                    raise ValidationError("Bulk tier min_qty must be an integer >= 2") from exc
                if min_qty < 2:
                    raise ValidationError("Bulk tier min_qty must be >= 2")
                raw_u = usd_prices[idx] if idx < len(usd_prices) else ""
                usd = parse_decimal_strict(raw_u, "unit_price_usd", allow_zero=True)
                raw_i = irt_prices[idx] if idx < len(irt_prices) else ""
                irt = (
                    parse_decimal_strict(raw_i, "unit_price_irt", allow_zero=True)
                    if str(raw_i).strip()
                    else None
                )
                tiers.append(ProductBulkTierDTO(min_qty=min_qty, unit_price_usd=usd, unit_price_irt=irt))

    saved_tiers = await shop_service.set_bulk_tiers(product_id, tiers)

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse(
            {
                "ok": True,
                "product_id": product_id,
                "tiers": [t.model_dump(mode="json") for t in saved_tiers],
            }
        )

    q = urlencode({"msg": f"Saved {len(saved_tiers)} bulk discount tiers"})
    return RedirectResponse(url=f"/admin/catalog/products/{product_id}?{q}", status_code=303)


@router.post("/admin/catalog/products/{product_id}/stock")
async def import_product_stock_route(
    request: Request,
    product_id: int,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    shop_service: ShopServiceProtocol = Depends(get_shop_service),
) -> Response:
    """Import auto-delivery stock credentials via bulk paste or validated .txt/.csv upload."""
    settings = get_app_settings(request)
    content_type = request.headers.get("content-type", "").lower()
    payloads: list[str] = []
    deduplicate = False

    if "application/json" in content_type:
        body = await request.json()
        raw_items = body.get("payloads") or body.get("lines") or []
        if isinstance(raw_items, str):
            payloads.extend(line.strip() for line in raw_items.splitlines() if line.strip())
        elif isinstance(raw_items, list):
            payloads.extend(str(item).strip() for item in raw_items if str(item).strip())
        deduplicate = _parse_bool(body.get("deduplicate"), default=False)
    else:
        form = await request.form()
        deduplicate = _parse_bool(form.get("deduplicate"), default=False)
        stock_lines = str(form.get("stock_lines") or "")
        if stock_lines.strip():
            payloads.extend(line.strip() for line in stock_lines.splitlines() if line.strip())

        upload = form.get("stock_file")
        if isinstance(upload, (StarletteUploadFile, UploadFile)) and upload.filename:
            file_bytes = await upload.read()
            if file_bytes:
                file_lines = validate_stock_upload_file(
                    file_bytes,
                    filename=upload.filename,
                    max_bytes=settings.MAX_UPLOAD_SIZE_BYTES,
                )
                payloads.extend(file_lines)

    if not payloads:
        raise ValidationError("No stock lines provided via paste or file upload")

    result = await shop_service.add_auto_stock(
        product_id,
        payloads,
        deduplicate=deduplicate,
    )

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "import_result": result.model_dump(mode="json")})

    q = urlencode(
        {
            "msg": f"Imported {result.added_count} stock items "
            f"(skipped {result.skipped_duplicates}, available: {result.total_available})"
        }
    )
    return RedirectResponse(url=f"/admin/catalog/products/{product_id}?{q}", status_code=303)
