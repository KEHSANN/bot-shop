"""12-language localization dictionaries and Redis-cached i18n service."""

from app.locales.i18n import TranslationService
from app.locales.seed_translations import TRANSLATIONS

__all__ = ["TRANSLATIONS", "TranslationService"]
