"""Dynamic 12-language CMS, UI button customization, and application settings service."""

from __future__ import annotations

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.constants import (
    SUPPORTED_LANGUAGES,
    is_valid_hex_color,
    normalize_hex_color,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.database.models import AppSetting, UIButton, UITranslation
from app.locales.i18n import TranslationService
from app.services.dtos import AppSettingDTO, UIButtonDTO, UITranslationDTO

logger = get_logger(__name__)


class CMSService:
    """Implements CMSServiceProtocol with Redis i18n and UI button cache invalidation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: Redis | None = None,
        translation_service: TranslationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._translation_service = translation_service or TranslationService(
            session_factory, redis_client
        )

    async def get_translation(self, key: str, lang_code: str) -> str:
        """Retrieve a localized UI text string via TranslationService."""
        return await self._translation_service.get_text(key, lang_code)

    async def list_translations(
        self,
        *,
        lang_code: str | None = None,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[UITranslationDTO]:
        """List CMS translation entries with optional language and substring search filters."""
        async with self._session_factory() as session:
            stmt = select(UITranslation)
            if lang_code is not None and lang_code.strip():
                normalized = lang_code.strip().lower()
                if normalized not in SUPPORTED_LANGUAGES:
                    raise ValidationError(f"Unsupported language code: {lang_code}")
                stmt = stmt.where(UITranslation.lang_code == normalized)
            if search is not None and search.strip():
                term = f"%{search.strip()}%"
                stmt = stmt.where(
                    or_(
                        UITranslation.key.ilike(term),
                        UITranslation.text_value.ilike(term),
                    )
                )
            stmt = (
                stmt.order_by(UITranslation.key.asc(), UITranslation.lang_code.asc())
                .limit(limit)
                .offset(offset)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [UITranslationDTO.model_validate(r) for r in rows]

    async def upsert_translation(
        self, key: str, lang_code: str, text_value: str
    ) -> UITranslationDTO:
        """Create or update a translation string and invalidate Redis cache immediately."""
        return await self._translation_service.set_translation(key, lang_code, text_value)

    async def list_buttons(self, *, menu_scope: str | None = None) -> list[UIButtonDTO]:
        """List CMS UI buttons sorted by menu_scope, row_index, col_index, and id."""
        async with self._session_factory() as session:
            stmt = select(UIButton)
            if menu_scope is not None and menu_scope.strip():
                stmt = stmt.where(UIButton.menu_scope == menu_scope.strip().lower())
            stmt = stmt.order_by(
                UIButton.menu_scope.asc(),
                UIButton.row_index.asc(),
                UIButton.col_index.asc(),
                UIButton.id.asc(),
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [UIButtonDTO.model_validate(r) for r in rows]

    async def update_button(
        self,
        button_key: str,
        *,
        color_emoji: str | None = None,
        color_hex: str | None = None,
        labels_json: dict[str, str] | None = None,
        is_active: bool | None = None,
    ) -> UIButtonDTO:
        """Update a UI button's dual-layer color, labels, or active status and clear Redis cache."""
        cleaned_key = (button_key or "").strip()
        if not cleaned_key:
            raise ValidationError("button_key must not be empty")

        async with self._session_factory() as session:
            async with session.begin():
                button = (
                    await session.execute(
                        select(UIButton).where(UIButton.button_key == cleaned_key)
                    )
                ).scalar_one_or_none()
                if button is None:
                    raise NotFoundError(f"UI button '{cleaned_key}' not found")

                if color_emoji is not None:
                    cleaned_emoji = color_emoji.strip()
                    if not cleaned_emoji:
                        raise ValidationError("color_emoji must not be empty")
                    button.color_emoji = cleaned_emoji

                if color_hex is not None:
                    if not is_valid_hex_color(color_hex):
                        raise ValidationError("color_hex must match #RRGGBB")
                    button.color_hex = normalize_hex_color(color_hex)

                if labels_json is not None:
                    merged_labels = dict(button.labels_json or {})
                    for lang_k, label_v in labels_json.items():
                        norm_lang = str(lang_k).strip().lower()
                        if norm_lang not in SUPPORTED_LANGUAGES:
                            raise ValidationError(
                                f"Unsupported language code in labels_json: {lang_k}"
                            )
                        merged_labels[norm_lang] = str(label_v)
                    button.labels_json = merged_labels

                if is_active is not None:
                    button.is_active = bool(is_active)

                await session.flush()
                await session.refresh(button)
                dto = UIButtonDTO.model_validate(button)

        await self._invalidate_button_cache(dto.menu_scope.value)
        return dto

    async def _invalidate_button_cache(self, menu_scope: str) -> None:
        if self._redis is not None:
            try:
                await self._redis.delete(f"{TranslationService.BUTTONS_CACHE_PREFIX}{menu_scope}")
            except Exception as exc:
                logger.warning("Redis button cache invalidation failed for %s: %s", menu_scope, exc)
        await self._translation_service.invalidate_cache()

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Read an application setting value or return default if absent."""
        cleaned_key = (key or "").strip()
        if not cleaned_key:
            raise ValidationError("Setting key must not be empty")
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AppSetting).where(AppSetting.key == cleaned_key)
                )
            ).scalar_one_or_none()
            return row.value if row is not None else default

    async def set_setting(
        self, key: str, value: str, *, description: str | None = None
    ) -> AppSettingDTO:
        """Create or update an application setting entry in PostgreSQL."""
        cleaned_key = (key or "").strip()
        if not cleaned_key:
            raise ValidationError("Setting key must not be empty")

        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AppSetting).where(AppSetting.key == cleaned_key)
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.value = str(value)
                    if description is not None:
                        row.description = description
                else:
                    row = AppSetting(
                        key=cleaned_key,
                        value=str(value),
                        description=description,
                    )
                    session.add(row)
                await session.flush()
                await session.refresh(row)
                return AppSettingDTO.model_validate(row)
