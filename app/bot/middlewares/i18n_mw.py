"""Inner i18n middleware binding user language and TranslationService into handler data."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.bot.middlewares.logging_mw import extract_telegram_user
from app.locales.i18n import TranslationService
from app.services.dtos import UserDTO


class BoundTranslator:
    """Callable wrapper binding TranslationService to a fixed user language code."""

    def __init__(self, service: TranslationService, lang_code: str) -> None:
        self._service = service
        self.lang_code = lang_code

    async def __call__(self, key: str, **kwargs: Any) -> str:
        return await self._service.get_text(key, self.lang_code, **kwargs)


class I18nMiddleware(BaseMiddleware):
    """Resolve active language for current user and inject `lang`, `i18n`, `translation_service`, and `t`."""

    def __init__(self, translation_service: TranslationService) -> None:
        self._translation_service = translation_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: UserDTO | None = data.get("user")
        if user is not None:
            lang = TranslationService.normalize_lang(user.language_code)
        else:
            tg_user = extract_telegram_user(event, data)
            lang = TranslationService.normalize_lang(getattr(tg_user, "language_code", None))

        data["lang"] = lang
        data["lang_code"] = lang
        data["i18n"] = self._translation_service
        data["translation_service"] = self._translation_service
        data["t"] = BoundTranslator(self._translation_service, lang)
        return await handler(event, data)
