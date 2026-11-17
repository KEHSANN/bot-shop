"""Redis-cached multi-language translation and UI button lookup service."""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.constants import (
    DEFAULT_LANGUAGE,
    LANGUAGE_ORDER,
    MAIN_MENU_BUTTON_KEYS,
    SUPPORTED_LANGUAGES,
    MenuScope,
)
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.database.models import UIButton, UITranslation
from app.locales.seed_translations import TRANSLATIONS
from app.services.dtos import UIButtonDTO, UITranslationDTO

logger = get_logger(__name__)


class _SafeFormatDict(dict[str, Any]):
    """Prevents KeyError if an admin-edited CMS string omits or alters a format placeholder."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class TranslationService:
    """Provides Redis-cached i18n text and UI button retrieval with PostgreSQL persistence."""

    TEXTS_CACHE_PREFIX = "i18n:texts:"
    BUTTONS_CACHE_PREFIX = "i18n:buttons:"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: Redis | None = None,
        *,
        cache_ttl: int = 3600,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._cache_ttl = cache_ttl

    @staticmethod
    def normalize_lang(lang_code: str | None) -> str:
        """Resolve Telegram or user language code to one of the 12 supported languages."""
        if not lang_code:
            return DEFAULT_LANGUAGE
        cleaned = lang_code.strip().lower().split("-")[0].split("_")[0]
        if cleaned in SUPPORTED_LANGUAGES:
            return cleaned
        return DEFAULT_LANGUAGE

    async def get_language_map(self, lang_code: str) -> dict[str, str]:
        """Return full key->text dictionary for lang_code from Redis or PostgreSQL."""
        lang = self.normalize_lang(lang_code)
        cache_key = f"{self.TEXTS_CACHE_PREFIX}{lang}"

        if self._redis is not None:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    raw_map = json.loads(cached)
                    if isinstance(raw_map, dict):
                        return {str(k): str(v) for k, v in raw_map.items()}
            except Exception as exc:
                logger.warning("Redis read failed for %s: %s", cache_key, exc)

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(UITranslation.key, UITranslation.text_value).where(UITranslation.lang_code == lang)
                )
            ).all()

        result = {row.key: row.text_value for row in rows}
        if not result:
            result = {k: langs[lang] for k, langs in TRANSLATIONS.items() if lang in langs}

        if self._redis is not None and result:
            try:
                await self._redis.set(cache_key, json.dumps(result, ensure_ascii=False), ex=self._cache_ttl)
            except Exception as exc:
                logger.warning("Redis write failed for %s: %s", cache_key, exc)

        return result

    async def get_text(self, key: str, lang_code: str = DEFAULT_LANGUAGE, **kwargs: Any) -> str:
        """Fetch localized template for key with fallback to English and safe placeholder formatting."""
        lang = self.normalize_lang(lang_code)
        lang_map = await self.get_language_map(lang)
        template = lang_map.get(key)

        if template is None and lang != DEFAULT_LANGUAGE:
            en_map = await self.get_language_map(DEFAULT_LANGUAGE)
            template = en_map.get(key)

        if template is None:
            seed_entry = TRANSLATIONS.get(key)
            if seed_entry:
                template = seed_entry.get(lang) or seed_entry.get(DEFAULT_LANGUAGE)

        if template is None:
            template = key

        if kwargs:
            return template.format_map(_SafeFormatDict(kwargs))
        return template

    async def get_main_menu_buttons(self, lang_code: str = DEFAULT_LANGUAGE) -> list[UIButtonDTO]:
        """Return strictly scoped active main menu buttons sorted by (row_index, col_index)."""
        _ = self.normalize_lang(lang_code)
        cache_key = f"{self.BUTTONS_CACHE_PREFIX}{MenuScope.MAIN.value}"

        if self._redis is not None:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    raw_list = json.loads(cached)
                    if isinstance(raw_list, list):
                        buttons = [UIButtonDTO.model_validate(item) for item in raw_list]
                        return [b for b in buttons if b.button_key in MAIN_MENU_BUTTON_KEYS and b.is_active]
            except Exception as exc:
                logger.warning("Redis button cache read failed for %s: %s", cache_key, exc)

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(UIButton)
                    .where(UIButton.menu_scope == MenuScope.MAIN.value, UIButton.is_active.is_(True))
                    .order_by(UIButton.row_index.asc(), UIButton.col_index.asc(), UIButton.id.asc())
                )
            ).scalars().all()

        dtos = [
            UIButtonDTO.model_validate(row)
            for row in rows
            if row.button_key in MAIN_MENU_BUTTON_KEYS
        ]

        if self._redis is not None and dtos:
            try:
                serialized = [d.model_dump(mode="json") for d in dtos]
                await self._redis.set(cache_key, json.dumps(serialized, ensure_ascii=False), ex=self._cache_ttl)
            except Exception as exc:
                logger.warning("Redis button cache write failed for %s: %s", cache_key, exc)

        return dtos

    async def set_translation(self, key: str, lang_code: str, text_value: str) -> UITranslationDTO:
        """Create or update a translation entry in PostgreSQL and invalidate Redis cache."""
        lang = lang_code.strip().lower()
        if lang not in SUPPORTED_LANGUAGES:
            raise ValidationError(f"Unsupported language code: {lang_code}")
        if not key or not key.strip():
            raise ValidationError("Translation key must not be empty")

        async with self._session_factory() as session:
            async with session.begin():
                existing = (
                    await session.execute(
                        select(UITranslation).where(
                            UITranslation.key == key.strip(),
                            UITranslation.lang_code == lang,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    existing.text_value = text_value
                    await session.flush()
                    await session.refresh(existing)
                    dto = UITranslationDTO.model_validate(existing)
                else:
                    row = UITranslation(key=key.strip(), lang_code=lang, text_value=text_value)
                    session.add(row)
                    await session.flush()
                    await session.refresh(row)
                    dto = UITranslationDTO.model_validate(row)

        await self.invalidate_cache(lang)
        return dto

    async def invalidate_cache(self, lang_code: str | None = None) -> None:
        """Invalidate cached translation dictionaries and UI button layouts in Redis."""
        if self._redis is None:
            return
        try:
            if lang_code:
                lang = self.normalize_lang(lang_code)
                await self._redis.delete(f"{self.TEXTS_CACHE_PREFIX}{lang}")
            else:
                keys = [f"{self.TEXTS_CACHE_PREFIX}{code}" for code in LANGUAGE_ORDER]
                keys.extend(f"{self.BUTTONS_CACHE_PREFIX}{scope.value}" for scope in MenuScope)
                await self._redis.delete(*keys)
        except Exception as exc:
            logger.warning("Redis cache invalidation warning: %s", exc)
