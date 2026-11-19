"""CMS routes: 12-Language UI Translations, UI Buttons with Dual-Layer Colors, and App Settings."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.bot.middlewares.force_join_mw import validate_force_join_channels_raw
from app.core.constants import (
    FORBIDDEN_MAIN_MENU_FEATURES,
    LANGUAGE_ORDER,
    SUPPORTED_LANGUAGES,
    SettingKey,
)
from app.core.exceptions import ValidationError
from app.services.interfaces import CMSServiceProtocol
from app.web_admin.deps import (
    AdminPrincipal,
    get_cms_service,
    parse_color_emoji_strict,
    parse_color_hex_strict,
    parse_decimal_strict,
    prefers_json,
    render_admin_template,
    require_admin_csrf,
)

router = APIRouter(tags=["admin-cms"])


def _parse_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


@router.get("/admin/cms")
@router.get("/admin/api/cms")
async def cms_overview(
    request: Request,
    lang: str = "all",
    q: str = "",
    tab: str = "texts",
    limit: int = 300,
    offset: int = 0,
    admin: AdminPrincipal = Depends(require_admin_csrf),
    cms_service: CMSServiceProtocol = Depends(get_cms_service),
) -> Response:
    """Render searchable 12-language CMS text editor, UI buttons color editor, and app settings."""
    lang_clean = lang.strip().lower()
    lang_filter: str | None = None
    if lang_clean and lang_clean != "all":
        if lang_clean not in SUPPORTED_LANGUAGES:
            raise ValidationError(f"Unsupported language code '{lang}'")
        lang_filter = lang_clean

    search_query = q.strip() or None
    translations = await cms_service.list_translations(
        lang_code=lang_filter,
        search=search_query,
        limit=max(1, min(limit, 1000)),
        offset=max(0, offset),
    )
    buttons = await cms_service.list_buttons()

    settings_map: dict[str, str] = {}
    for sk in SettingKey:
        val = await cms_service.get_setting(sk.value, default="")
        settings_map[sk.value] = val if val is not None else ""

    if prefers_json(request):
        return JSONResponse(
            {
                "lang_filter": lang_clean,
                "search": search_query or "",
                "translations": [t.model_dump(mode="json") for t in translations],
                "buttons": [b.model_dump(mode="json") for b in buttons],
                "settings": settings_map,
            }
        )

    return render_admin_template(
        request,
        "cms.html",
        {
            "active_nav": "cms",
            "active_tab": tab,
            "lang_filter": lang_clean,
            "search_query": search_query or "",
            "translations": translations,
            "buttons": buttons,
            "settings_map": settings_map,
        },
        admin=admin,
    )


@router.post("/admin/cms/translations")
@router.put("/admin/cms/translations")
async def upsert_translation_route(
    request: Request,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    cms_service: CMSServiceProtocol = Depends(get_cms_service),
) -> Response:
    """Create or update a localized UI translation string and invalidate Redis cache."""
    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    key = str(payload.get("key") or "").strip()
    lang_code = str(payload.get("lang_code") or "").strip().lower()
    text_value = str(payload.get("text_value") if payload.get("text_value") is not None else "")

    if not key:
        raise ValidationError("Translation key is required")
    if lang_code not in SUPPORTED_LANGUAGES:
        raise ValidationError(f"Unsupported language code '{lang_code}'")

    updated = await cms_service.upsert_translation(key, lang_code, text_value)

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "translation": updated.model_dump(mode="json")})

    q = urlencode({"msg": f"Updated translation '{key}' [{lang_code}]", "lang": lang_code, "tab": "texts"})
    return RedirectResponse(url=f"/admin/cms?{q}", status_code=303)


@router.post("/admin/cms/buttons/{button_key}")
@router.put("/admin/cms/buttons/{button_key}")
async def update_ui_button_route(
    request: Request,
    button_key: str,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    cms_service: CMSServiceProtocol = Depends(get_cms_service),
) -> Response:
    """Update a UI button's dual-layer color (Hex + Emoji), active flag, and 12-language labels."""
    cleaned_key = button_key.strip().lower()
    if cleaned_key in FORBIDDEN_MAIN_MENU_FEATURES:
        raise ValidationError(f"Button key '{cleaned_key}' is forbidden on the Main Menu")

    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    color_emoji: str | None = None
    if payload.get("color_emoji") is not None and str(payload.get("color_emoji")).strip() != "":
        color_emoji = parse_color_emoji_strict(str(payload["color_emoji"]))

    color_hex: str | None = None
    if payload.get("color_hex") is not None and str(payload.get("color_hex")).strip() != "":
        color_hex = parse_color_hex_strict(str(payload["color_hex"]))

    is_active: bool | None = None
    if "is_active" in payload:
        is_active = _parse_bool(payload.get("is_active"), default=True)

    labels_json: dict[str, str] = {}
    if isinstance(payload.get("labels_json"), dict):
        for lang, text in payload["labels_json"].items():
            if lang in LANGUAGE_ORDER and isinstance(text, str) and text.strip():
                labels_json[lang] = text.strip()
    for lang in LANGUAGE_ORDER:
        form_label = payload.get(f"label_{lang}")
        if isinstance(form_label, str) and form_label.strip():
            labels_json[lang] = form_label.strip()

    updated = await cms_service.update_button(
        cleaned_key,
        color_emoji=color_emoji,
        color_hex=color_hex,
        labels_json=labels_json if labels_json else None,
        is_active=is_active,
    )

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse({"ok": True, "button": updated.model_dump(mode="json")})

    q = urlencode({"msg": f"Button '{cleaned_key}' updated", "tab": "buttons"})
    return RedirectResponse(url=f"/admin/cms?{q}", status_code=303)


def _validate_setting_pair(key: str, value: str) -> tuple[str, str]:
    k = key.strip()
    v = value.strip()
    if not k:
        raise ValidationError("Setting key must not be empty")
    if k == SettingKey.MAINTENANCE_MODE.value:
        if v.lower() not in {"true", "false"}:
            raise ValidationError("maintenance_mode must be 'true' or 'false'")
        v = v.lower()
    elif k in {SettingKey.PINNED_LIMIT.value, SettingKey.LOW_STOCK_DEFAULT_THRESHOLD.value}:
        try:
            num = int(v)
        except ValueError as exc:
            raise ValidationError(f"{k} must be a non-negative integer") from exc
        if num < 0:
            raise ValidationError(f"{k} must be >= 0")
        v = str(num)
    elif k == SettingKey.USD_TO_TOMAN_RATE.value:
        dec = parse_decimal_strict(v, k, allow_zero=False, allow_negative=False)
        v = str(dec)
    elif k == SettingKey.RATE_MODE.value:
        if v.lower() not in {"manual", "live"}:
            raise ValidationError("rate_mode must be 'manual' or 'live'")
        v = v.lower()
    elif k == SettingKey.GUIDE_URL.value:
        if v:
            if len(v) > 500 or not v.lower().startswith(("https://", "http://")):
                raise ValidationError("guide_url must start with https:// or http:// (or be empty)")
    elif k == SettingKey.FORCE_JOIN_ENABLED.value:
        if v.lower() not in {"true", "false"}:
            raise ValidationError("force_join_enabled must be 'true' or 'false'")
        v = v.lower()
    elif k == SettingKey.FORCE_JOIN_CHANNELS.value:
        v = validate_force_join_channels_raw(v)
    return k, v


@router.post("/admin/cms/settings")
async def update_app_settings_route(
    request: Request,
    _admin: AdminPrincipal = Depends(require_admin_csrf),
    cms_service: CMSServiceProtocol = Depends(get_cms_service),
) -> Response:
    """Update one or more runtime application settings."""
    content_type = request.headers.get("content-type", "").lower()
    payload = await request.json() if "application/json" in content_type else dict(await request.form())

    updated_items = []
    if "key" in payload and "value" in payload:
        k, v = _validate_setting_pair(str(payload["key"]), str(payload["value"]))
        desc = str(payload.get("description") or "").strip() or None
        dto = await cms_service.set_setting(k, v, description=desc)
        updated_items.append(dto)
    else:
        for sk in SettingKey:
            if sk.value in payload and payload[sk.value] is not None:
                k, v = _validate_setting_pair(sk.value, str(payload[sk.value]))
                dto = await cms_service.set_setting(k, v)
                updated_items.append(dto)

    if not updated_items:
        raise ValidationError("No valid setting key/value pairs supplied")

    if prefers_json(request) or "application/json" in content_type:
        return JSONResponse(
            {
                "ok": True,
                "settings": [item.model_dump(mode="json") for item in updated_items],
            }
        )

    q = urlencode({"msg": "Application settings saved", "tab": "settings"})
    return RedirectResponse(url=f"/admin/cms?{q}", status_code=303)
