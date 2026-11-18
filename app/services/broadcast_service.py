"""Rate-limited mass broadcast service and direct messaging with FloodWait retry handling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.constants import BroadcastStatus, SUPPORTED_LANGUAGES
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.database.models import BroadcastJob, User
from app.database.session import get_session_factory
from app.services.dtos import BroadcastCreateDTO, BroadcastJobDTO

logger = get_logger(__name__)

_VALID_STATUSES: frozenset[str] = frozenset(s.value for s in BroadcastStatus)
_BALANCE_FILTER_ALIASES: frozenset[str] = frozenset({"has_balance", "with_balance", "balance>0"})
_ALL_FILTER_ALIASES: frozenset[str] = frozenset({"all", "*", ""})


def build_broadcast_inline_kb(payload: dict[str, Any]) -> InlineKeyboardMarkup | None:
    """Build an optional URL InlineKeyboardMarkup from a broadcast message_payload dict."""
    buttons_raw: list[dict[str, str]] = []
    if isinstance(payload.get("buttons"), list):
        for item in payload["buttons"]:
            if isinstance(item, dict) and item.get("text") and item.get("url"):
                buttons_raw.append({"text": str(item["text"]).strip(), "url": str(item["url"]).strip()})
    elif isinstance(payload.get("button"), dict):
        btn = payload["button"]
        if btn.get("text") and btn.get("url"):
            buttons_raw.append({"text": str(btn["text"]).strip(), "url": str(btn["url"]).strip()})
    elif payload.get("button_text") and payload.get("button_url"):
        buttons_raw.append(
            {
                "text": str(payload["button_text"]).strip(),
                "url": str(payload["button_url"]).strip(),
            }
        )

    rows: list[list[InlineKeyboardButton]] = []
    for btn in buttons_raw:
        text = btn["text"]
        url = btn["url"]
        if text and url and url.startswith(("http://", "https://", "tg://")):
            rows.append([InlineKeyboardButton(text=text, url=url)])

    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_broadcast_progress_bar(job: BroadcastJobDTO, *, width: int = 10) -> str:
    """Format a visual progress bar string for a BroadcastJobDTO."""
    total = max(0, job.total_targets)
    processed = min(total, job.sent_count + job.failed_count) if total > 0 else (job.sent_count + job.failed_count)
    if total > 0:
        ratio = min(1.0, max(0.0, processed / total))
        filled = int(round(ratio * width))
        pct = int(round(ratio * 100))
    else:
        filled = width if job.status in {BroadcastStatus.COMPLETED, BroadcastStatus.COMPLETED.value} else 0
        pct = 100 if filled == width else 0
    bar = "█" * filled + "░" * max(0, width - filled)
    return f"[{bar}] {pct}% ({processed}/{total} | ✅ {job.sent_count} | ❌ {job.failed_count})"


class BroadcastService:
    """Implements BroadcastServiceProtocol with ~25 msg/s rate limiting and FloodWait retries."""

    def __init__(
        self,
        session_or_factory: async_sessionmaker[AsyncSession] | AsyncSession | None = None,
        *,
        session: AsyncSession | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        bot: Bot | None = None,
        rate_per_sec: int | float | None = None,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        resolved_session: AsyncSession | None = session
        resolved_factory: async_sessionmaker[AsyncSession] | None = session_factory

        if isinstance(session_or_factory, AsyncSession):
            resolved_session = session_or_factory
        elif session_or_factory is not None:
            resolved_factory = session_or_factory

        self._external_session = resolved_session
        self._session_factory = resolved_factory
        self._bot = bot
        self._rate_per_sec = rate_per_sec
        self._sleep = sleep_func

    @asynccontextmanager
    async def _session_scope(self, *, write: bool = False) -> AsyncGenerator[AsyncSession, None]:
        """Yield an AsyncSession, committing on write when managing our own session."""
        if self._external_session is not None:
            yield self._external_session
            if write:
                await self._external_session.flush()
            return

        factory = self._session_factory or get_session_factory()
        async with factory() as session:
            if write:
                async with session.begin():
                    yield session
            else:
                yield session

    @staticmethod
    def normalize_target_filter(target_filter: str | None) -> str:
        """Validate and normalize an audience target filter string."""
        raw = (target_filter or "all").strip().lower()
        if raw in _ALL_FILTER_ALIASES:
            return "all"
        if raw in _BALANCE_FILTER_ALIASES:
            return "has_balance"
        if raw == "admins":
            return "admins"
        if raw in SUPPORTED_LANGUAGES:
            return f"lang:{raw}"
        if raw.startswith("lang:"):
            code = raw.split(":", 1)[1].strip()
            if code in SUPPORTED_LANGUAGES:
                return f"lang:{code}"
            raise ValidationError(f"Unsupported language code in broadcast filter: '{target_filter}'")
        if raw.startswith(("user:", "id:")):
            ident = raw.split(":", 1)[1].strip()
            if ident.lstrip("-").isdigit():
                return f"user:{int(ident)}"
            raise ValidationError(f"Invalid numeric user ID in broadcast filter: '{target_filter}'")
        raise ValidationError(f"Invalid broadcast target filter: '{target_filter}'")

    async def resolve_target_telegram_ids(self, target_filter: str) -> list[int]:
        """Resolve non-blocked recipient Telegram IDs matching the given audience filter."""
        normalized = self.normalize_target_filter(target_filter)

        stmt = select(User.telegram_id).where(User.is_blocked.is_(False))

        if normalized == "all":
            pass
        elif normalized == "has_balance":
            stmt = stmt.where(
                or_(
                    User.balance_usd > Decimal("0"),
                    User.balance_irt > Decimal("0"),
                )
            )
        elif normalized == "admins":
            stmt = stmt.where(User.is_admin.is_(True))
        elif normalized.startswith("lang:"):
            lang_code = normalized.split(":", 1)[1]
            stmt = stmt.where(User.language_code == lang_code)
        elif normalized.startswith("user:"):
            tg_id = int(normalized.split(":", 1)[1])
            stmt = stmt.where(User.telegram_id == tg_id)

        stmt = stmt.order_by(User.id.asc())

        async with self._session_scope(write=False) as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [int(tid) for tid in rows]

    async def create_job(self, data: BroadcastCreateDTO) -> BroadcastJobDTO:
        """Create a new BroadcastJob row initialized with resolved target count."""
        normalized_filter = self.normalize_target_filter(data.target_filter)
        if data.admin_id <= 0:
            raise ValidationError("admin_id must be a positive integer")

        payload = dict(data.message_payload or {})
        target_ids = await self.resolve_target_telegram_ids(normalized_filter)

        async with self._session_scope(write=True) as session:
            job = BroadcastJob(
                admin_id=data.admin_id,
                target_filter=normalized_filter,
                message_payload=payload,
                total_targets=len(target_ids),
                sent_count=0,
                failed_count=0,
                status=BroadcastStatus.PENDING.value,
            )
            session.add(job)
            await session.flush()
            await session.refresh(job)
            return BroadcastJobDTO.model_validate(job)

    async def update_progress(
        self,
        job_id: int,
        *,
        sent_count: int | None = None,
        failed_count: int | None = None,
        status: str | None = None,
    ) -> BroadcastJobDTO:
        """Update sent_count, failed_count, and/or status on an existing BroadcastJob."""
        if sent_count is not None and sent_count < 0:
            raise ValidationError("sent_count cannot be negative")
        if failed_count is not None and failed_count < 0:
            raise ValidationError("failed_count cannot be negative")

        normalized_status: str | None = None
        if status is not None:
            raw_status = status.value if isinstance(status, BroadcastStatus) else str(status).strip().lower()
            if raw_status not in _VALID_STATUSES:
                raise ValidationError(f"Invalid broadcast status: '{status}'")
            normalized_status = raw_status

        async with self._session_scope(write=True) as session:
            job = (
                await session.execute(select(BroadcastJob).where(BroadcastJob.id == job_id))
            ).scalar_one_or_none()
            if job is None:
                raise NotFoundError(f"Broadcast job #{job_id} not found")

            if sent_count is not None:
                job.sent_count = sent_count
            if failed_count is not None:
                job.failed_count = failed_count
            if normalized_status is not None:
                job.status = normalized_status
                if normalized_status in {
                    BroadcastStatus.COMPLETED.value,
                    BroadcastStatus.FAILED.value,
                    BroadcastStatus.CANCELLED.value,
                }:
                    if job.completed_at is None:
                        job.completed_at = datetime.now(timezone.utc)

            await session.flush()
            await session.refresh(job)
            return BroadcastJobDTO.model_validate(job)

    async def get_job(self, job_id: int) -> BroadcastJobDTO:
        """Fetch a single BroadcastJob by ID or raise NotFoundError."""
        async with self._session_scope(write=False) as session:
            job = (
                await session.execute(select(BroadcastJob).where(BroadcastJob.id == job_id))
            ).scalar_one_or_none()
            if job is None:
                raise NotFoundError(f"Broadcast job #{job_id} not found")
            return BroadcastJobDTO.model_validate(job)

    async def list_jobs(self, *, limit: int = 20, offset: int = 0) -> list[BroadcastJobDTO]:
        """List recent BroadcastJobs ordered newest first."""
        safe_limit = max(1, min(limit, 200))
        safe_offset = max(0, offset)
        async with self._session_scope(write=False) as session:
            rows = (
                await session.execute(
                    select(BroadcastJob)
                    .order_by(BroadcastJob.created_at.desc(), BroadcastJob.id.desc())
                    .limit(safe_limit)
                    .offset(safe_offset)
                )
            ).scalars().all()
            return [BroadcastJobDTO.model_validate(row) for row in rows]

    async def _send_single_payload(
        self,
        bot: Bot,
        chat_id: int,
        payload: dict[str, Any],
        *,
        max_retries: int = 3,
    ) -> bool:
        """Deliver a single message/media payload to chat_id with TelegramRetryAfter backoff."""
        content_type = str(
            payload.get("content_type")
            or payload.get("media_type")
            or payload.get("type")
            or "text"
        ).strip().lower()

        text = payload.get("text")
        if text is None:
            text = payload.get("caption")
        if text is None:
            text = payload.get("message")
        text_str = str(text) if text is not None else ""

        photo_id = payload.get("photo_file_id") or (payload.get("file_id") if content_type == "photo" else None)
        video_id = payload.get("video_file_id") or (payload.get("file_id") if content_type == "video" else None)
        doc_id = payload.get("document_file_id") or (payload.get("file_id") if content_type == "document" else None)

        parse_mode = payload.get("parse_mode", ParseMode.HTML)
        reply_markup = build_broadcast_inline_kb(payload)

        for attempt in range(max_retries + 1):
            try:
                if photo_id:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=str(photo_id),
                        caption=text_str or None,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
                elif video_id:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=str(video_id),
                        caption=text_str or None,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
                elif doc_id:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=str(doc_id),
                        caption=text_str or None,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text_str or " ",
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
                return True
            except TelegramRetryAfter as exc:
                wait_seconds = max(0.01, float(getattr(exc, "retry_after", 1.0)))
                logger.warning(
                    "Broadcast FloodWait for chat_id=%s (attempt %s/%s): sleeping %.2fs",
                    chat_id,
                    attempt + 1,
                    max_retries + 1,
                    wait_seconds,
                )
                if attempt >= max_retries:
                    return False
                await self._sleep(wait_seconds)
            except (TelegramForbiddenError, TelegramBadRequest, TelegramNotFound, TelegramAPIError) as exc:
                logger.info("Broadcast delivery skipped for chat_id=%s: %s", chat_id, exc)
                return False
            except Exception:
                logger.exception("Unexpected error delivering broadcast message to chat_id=%s", chat_id)
                return False
        return False

    async def send_direct_message(
        self,
        telegram_id: int,
        payload: dict[str, Any],
        *,
        bot: Bot | None = None,
        max_retries: int = 3,
    ) -> bool:
        """Send a single direct message (text/photo/video/document) to a specific user."""
        active_bot = bot or self._bot
        if active_bot is None:
            raise ValidationError("A Bot instance is required to send direct messages")
        return await self._send_single_payload(
            active_bot,
            telegram_id,
            payload,
            max_retries=max_retries,
        )

    async def execute_job(
        self,
        job_id: int,
        *,
        bot: Bot | None = None,
        rate_per_sec: int | float | None = None,
        progress_callback: Callable[[BroadcastJobDTO], Awaitable[None]] | None = None,
        progress_batch_size: int = 10,
    ) -> BroadcastJobDTO:
        """Execute a BroadcastJob asynchronously at ~25 msg/s with FloodWait retry handling."""
        active_bot = bot or self._bot
        if active_bot is None:
            raise ValidationError("A Bot instance is required to execute a broadcast job")

        job = await self.get_job(job_id)
        target_ids = await self.resolve_target_telegram_ids(job.target_filter)

        effective_rate = float(
            rate_per_sec
            if rate_per_sec is not None
            else (self._rate_per_sec if self._rate_per_sec is not None else get_settings().BROADCAST_RATE_PER_SEC)
        )
        interval = (1.0 / effective_rate) if effective_rate > 0 else 0.0

        async with self._session_scope(write=True) as session:
            db_job = (
                await session.execute(select(BroadcastJob).where(BroadcastJob.id == job_id))
            ).scalar_one_or_none()
            if db_job is None:
                raise NotFoundError(f"Broadcast job #{job_id} not found")
            db_job.total_targets = len(target_ids)
            db_job.sent_count = 0
            db_job.failed_count = 0
            db_job.status = BroadcastStatus.RUNNING.value
            await session.flush()
            await session.refresh(db_job)
            job = BroadcastJobDTO.model_validate(db_job)

        if progress_callback is not None:
            await progress_callback(job)

        sent_count = 0
        failed_count = 0
        total = len(target_ids)
        batch_step = max(1, progress_batch_size)

        try:
            for idx, tg_id in enumerate(target_ids, start=1):
                ok = await self._send_single_payload(active_bot, tg_id, job.message_payload)
                if ok:
                    sent_count += 1
                else:
                    failed_count += 1

                if idx % batch_step == 0 or idx == total:
                    job = await self.update_progress(
                        job_id,
                        sent_count=sent_count,
                        failed_count=failed_count,
                    )
                    if progress_callback is not None:
                        await progress_callback(job)

                if idx < total and interval > 0:
                    await self._sleep(interval)

            job = await self.update_progress(
                job_id,
                sent_count=sent_count,
                failed_count=failed_count,
                status=BroadcastStatus.COMPLETED.value,
            )
            if progress_callback is not None:
                await progress_callback(job)
            return job
        except Exception:
            logger.exception("Broadcast job #%s encountered a fatal error", job_id)
            job = await self.update_progress(
                job_id,
                sent_count=sent_count,
                failed_count=failed_count,
                status=BroadcastStatus.FAILED.value,
            )
            return job

    run_job = execute_job
    dispatch_job = execute_job


__all__ = [
    "BroadcastService",
    "build_broadcast_inline_kb",
    "format_broadcast_progress_bar",
]