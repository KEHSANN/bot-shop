"""Two-way customer support ticket and message thread service."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.constants import SenderRole, TicketCategory, TicketStatus
from app.core.exceptions import InvalidStateTransitionError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.database.models import SupportMessage, SupportTicket, User
from app.services.dtos import SupportMessageDTO, SupportTicketDTO, SupportTicketDetailDTO

logger = get_logger(__name__)


def _generate_ticket_number() -> str:
    stamp = datetime.now(UTC).strftime("%y%m%d")
    suffix = secrets.token_hex(4).upper()
    return f"SUP-{stamp}-{suffix}"


class SupportService:
    """Async PostgreSQL implementation of SupportServiceProtocol."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_ticket(
        self,
        user_id: int,
        category: TicketCategory,
        *,
        subject: str | None = None,
        message_text: str | None = None,
        telegram_file_id: str | None = None,
        file_type: str | None = None,
    ) -> SupportTicketDetailDTO:
        """Create a new support ticket and optional initial message in one transaction."""
        cleaned_text = message_text.strip() if message_text else None
        cleaned_subject = subject.strip()[:256] if subject else None
        if not cleaned_text and not telegram_file_id and not cleaned_subject:
            raise ValidationError("Support ticket requires a message, attachment, or subject")

        async with self._session_factory() as session:
            async with session.begin():
                user = (
                    await session.execute(select(User).where(User.id == user_id))
                ).scalar_one_or_none()
                if user is None:
                    raise NotFoundError(f"User with id={user_id} was not found")

                ticket = SupportTicket(
                    ticket_number=_generate_ticket_number(),
                    user_id=user.id,
                    category=category.value,
                    subject=cleaned_subject,
                    status=TicketStatus.OPEN.value,
                )
                session.add(ticket)
                await session.flush()

                messages_out: list[SupportMessageDTO] = []
                if cleaned_text or telegram_file_id:
                    msg = SupportMessage(
                        ticket_id=ticket.id,
                        sender_role=SenderRole.USER.value,
                        sender_telegram_id=user.telegram_id,
                        message_text=cleaned_text,
                        telegram_file_id=telegram_file_id,
                        file_type=file_type,
                    )
                    session.add(msg)
                    await session.flush()
                    await session.refresh(msg)
                    messages_out.append(SupportMessageDTO.model_validate(msg))

                await session.refresh(ticket)
                base_dto = SupportTicketDTO.model_validate(ticket)
                detail = SupportTicketDetailDTO(
                    **base_dto.model_dump(),
                    messages=messages_out,
                )
                logger.info(
                    "Created support ticket %s for user id=%s category=%s",
                    ticket.ticket_number,
                    user.id,
                    category.value,
                )
                return detail

    async def add_message(
        self,
        ticket_id: int,
        sender_role: SenderRole,
        sender_telegram_id: int | None,
        *,
        message_text: str | None = None,
        telegram_file_id: str | None = None,
        file_type: str | None = None,
    ) -> SupportMessageDTO:
        """Append a user or admin message/attachment to an open ticket and update its status."""
        cleaned_text = message_text.strip() if message_text else None
        if not cleaned_text and not telegram_file_id:
            raise ValidationError("Support message must include text or an attachment")

        async with self._session_factory() as session:
            async with session.begin():
                ticket = (
                    await session.execute(
                        select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if ticket is None:
                    raise NotFoundError(f"Support ticket id={ticket_id} was not found")
                if ticket.status == TicketStatus.CLOSED.value:
                    raise InvalidStateTransitionError(
                        f"Ticket {ticket.ticket_number} is closed and cannot accept new messages"
                    )

                msg = SupportMessage(
                    ticket_id=ticket.id,
                    sender_role=sender_role.value,
                    sender_telegram_id=sender_telegram_id,
                    message_text=cleaned_text,
                    telegram_file_id=telegram_file_id,
                    file_type=file_type,
                )
                session.add(msg)
                ticket.status = (
                    TicketStatus.ANSWERED.value
                    if sender_role == SenderRole.ADMIN
                    else TicketStatus.OPEN.value
                )
                ticket.updated_at = func.now()
                await session.flush()
                await session.refresh(msg)
                return SupportMessageDTO.model_validate(msg)

    async def get_ticket_detail(self, ticket_id: int) -> SupportTicketDetailDTO:
        """Fetch full support ticket metadata and ordered message thread."""
        async with self._session_factory() as session:
            ticket = (
                await session.execute(
                    select(SupportTicket)
                    .options(selectinload(SupportTicket.messages))
                    .where(SupportTicket.id == ticket_id)
                )
            ).scalar_one_or_none()
            if ticket is None:
                raise NotFoundError(f"Support ticket id={ticket_id} was not found")
            return SupportTicketDetailDTO.model_validate(ticket)

    async def list_user_tickets(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[SupportTicketDTO]:
        """List tickets belonging to a specific user ordered by most recent update."""
        safe_limit = max(1, min(limit, 100))
        safe_offset = max(0, offset)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(SupportTicket)
                    .where(SupportTicket.user_id == user_id)
                    .order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc())
                    .limit(safe_limit)
                    .offset(safe_offset)
                )
            ).scalars().all()
            return [SupportTicketDTO.model_validate(row) for row in rows]

    async def list_tickets(
        self,
        *,
        status: TicketStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SupportTicketDTO]:
        """List all support tickets (for admin console), optionally filtered by status."""
        safe_limit = max(1, min(limit, 200))
        safe_offset = max(0, offset)
        async with self._session_factory() as session:
            stmt = select(SupportTicket)
            if status is not None:
                stmt = stmt.where(SupportTicket.status == status.value)
            stmt = stmt.order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc()).limit(
                safe_limit
            ).offset(safe_offset)
            rows = (await session.execute(stmt)).scalars().all()
            return [SupportTicketDTO.model_validate(row) for row in rows]

    async def close_ticket(self, ticket_id: int) -> SupportTicketDTO:
        """Close a support ticket atomically."""
        async with self._session_factory() as session:
            async with session.begin():
                ticket = (
                    await session.execute(
                        select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if ticket is None:
                    raise NotFoundError(f"Support ticket id={ticket_id} was not found")
                ticket.status = TicketStatus.CLOSED.value
                ticket.updated_at = func.now()
                await session.flush()
                await session.refresh(ticket)
                logger.info("Closed support ticket %s", ticket.ticket_number)
                return SupportTicketDTO.model_validate(ticket)
