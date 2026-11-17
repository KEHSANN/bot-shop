"""Inner authentication/session context middleware upserting UserDTO and RBAC flags."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.middlewares.logging_mw import extract_telegram_user
from app.config import Settings, get_settings
from app.core.constants import DEFAULT_LANGUAGE, SettingKey
from app.core.logging import get_logger
from app.database.models import AppSetting
from app.locales.i18n import TranslationService
from app.locales.seed_translations import TRANSLATIONS
from app.services.dtos import UserCreateOrUpdateDTO
from app.services.interfaces import UserServiceProtocol

logger = get_logger(__name__)


class AuthContextMiddleware(BaseMiddleware):
    """Upsert Telegram user into PostgreSQL, resolve is_admin, and enforce maintenance mode."""

    def __init__(
        self,
        user_service: UserServiceProtocol,
        *,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        translation_service: TranslationService | None = None,
    ) -> None:
        self._user_service = user_service
        self._settings = settings or get_settings()
        self._session_factory = session_factory
        self._i18n = translation_service

    async def _is_maintenance_enabled(self) -> bool:
        if self._session_factory is None:
            return False
        try:
            async with self._session_factory() as session:
                val = (
                    await session.execute(
                        select(AppSetting.value).where(AppSetting.key == SettingKey.MAINTENANCE_MODE.value)
                    )
                ).scalar_one_or_none()
            return str(val or "").strip().lower() in {"true", "1", "yes", "on"}
        except Exception as exc:
            logger.warning("Failed checking maintenance_mode setting: %s", exc)
            return False

    async def _reply_maintenance(self, event: TelegramObject, lang: str) -> None:
        i18n = self._i18n
        if i18n is not None:
            text = await i18n.get_text("err.maintenance", lang)
        else:
            entry = TRANSLATIONS.get("err.maintenance", {})
            text = entry.get(lang) or entry.get(DEFAULT_LANGUAGE) or "Maintenance mode."

        cq: CallbackQuery | None = event.callback_query if isinstance(event, Update) else (
            event if isinstance(event, CallbackQuery) else None
        )
        msg: Message | None = event.message if isinstance(event, Update) else (
            event if isinstance(event, Message) else None
        )
        try:
            if cq is not None:
                await cq.answer(text, show_alert=True)
            elif msg is not None:
                await msg.answer(text)
        except Exception as exc:
            logger.warning("Failed sending maintenance notice: %s", exc)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data.setdefault("user_service", self._user_service)
        data.setdefault("settings", self._settings)
        if self._session_factory is not None:
            data.setdefault("session_factory", self._session_factory)

        tg_user = extract_telegram_user(event, data)
        if tg_user is None or getattr(tg_user, "is_bot", False):
            return await handler(event, data)

        user_dto = await self._user_service.upsert_telegram_user(
            UserCreateOrUpdateDTO(
                telegram_id=tg_user.id,
                username=getattr(tg_user, "username", None),
                first_name=getattr(tg_user, "first_name", None),
                last_name=getattr(tg_user, "last_name", None),
                language_code=getattr(tg_user, "language_code", None),
            )
        )

        if user_dto.is_blocked:
            logger.info("AuthContextMiddleware dropped blocked user telegram_id=%s", tg_user.id)
            return None

        is_admin = bool(user_dto.is_admin or tg_user.id in self._settings.admin_ids)
        data["user"] = user_dto
        data["db_user"] = user_dto
        data["is_admin"] = is_admin

        if not is_admin and await self._is_maintenance_enabled():
            await self._reply_maintenance(event, user_dto.language_code)
            return None

        return await handler(event, data)


DbSessionMiddleware = AuthContextMiddleware
