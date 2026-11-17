"""Structured logging configuration with automatic redaction of tokens and secrets."""

from __future__ import annotations

import logging
import re
import sys

_MASK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b"), "[REDACTED_BOT_TOKEN]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]+"), r"\1[REDACTED_JWT]"),
    (re.compile(r"\bgAAAAA[A-Za-z0-9_=-]{20,}\b"), "[REDACTED_FERNET]"),
    (re.compile(r"(?i)((?:postgresql(?:\+asyncpg)?|redis)://[^:\s/]*:)[^@\s]+(@)"), r"\1[REDACTED]\2"),
)


class SecretMaskingFilter(logging.Filter):
    """Log filter that redacts known secret formats from log messages and args."""

    @staticmethod
    def _redact(text: str) -> str:
        masked = text
        for pattern, replacement in _MASK_PATTERNS:
            masked = pattern.sub(replacement, masked)
        return masked

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._redact(arg) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        return True


def configure_logging(level: str = "INFO") -> None:
    """Configure root logger with stdout handler and SecretMaskingFilter."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric_level)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(SecretMaskingFilter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(numeric_level)
    root_logger.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger with SecretMaskingFilter attached."""
    logger = logging.getLogger(name)
    if not any(isinstance(f, SecretMaskingFilter) for f in logger.filters):
        logger.addFilter(SecretMaskingFilter())
    return logger
