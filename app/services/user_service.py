"""User lifecycle, search, RBAC, blocking, and atomic Decimal wallet ledger service."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.constants import Currency, SUPPORTED_LANGUAGES, WalletLedgerRefType
from app.core.exceptions import (
    InsufficientBalanceError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.database.models import User, WalletLedger
from app.locales.i18n import TranslationService
from app.services.dtos import (
    BalanceAdjustmentDTO,
    UserCreateOrUpdateDTO,
    UserDTO,
    WalletLedgerDTO,
)

logger = get_logger(__name__)

_USD_QUANT = Decimal("0.00000001")
_IRT_QUANT = Decimal("0.01")
_INT32_MAX = 2_147_483_647
_INT64_MAX = 9_223_372_036_854_775_807


def _escape_like(value: str) -> str:
    """Escape SQL LIKE/ILIKE wildcard characters."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clean_optional_str(value: str | None, *, max_len: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


class UserService:
    """Async PostgreSQL implementation of UserServiceProtocol with row-level locking."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._admin_ids: frozenset[int] = frozenset(self._settings.admin_ids)

    async def upsert_telegram_user(self, data: UserCreateOrUpdateDTO) -> UserDTO:
        """Create a new Telegram user or refresh profile metadata without clobbering chosen language."""
        if data.telegram_id <= 0:
            raise ValidationError("telegram_id must be a positive integer")

        normalized_lang = TranslationService.normalize_lang(data.language_code)
        cleaned_username = _clean_optional_str(
            data.username.lstrip("@") if data.username else None,
            max_len=64,
        )
        cleaned_first = _clean_optional_str(data.first_name, max_len=128)
        cleaned_last = _clean_optional_str(data.last_name, max_len=128)
        is_env_admin = data.telegram_id in self._admin_ids

        async with self._session_factory() as session:
            async with session.begin():
                insert_stmt = pg_insert(User).values(
                    telegram_id=data.telegram_id,
                    username=cleaned_username,
                    first_name=cleaned_first,
                    last_name=cleaned_last,
                    language_code=normalized_lang,
                    language_selected=False,
                    is_admin=is_env_admin,
                )
                update_values: dict[str, object] = {
                    "username": cleaned_username,
                    "first_name": cleaned_first,
                    "last_name": cleaned_last,
                    "updated_at": func.now(),
                }
                if is_env_admin:
                    update_values["is_admin"] = True

                stmt = insert_stmt.on_conflict_do_update(
                    index_elements=[User.telegram_id],
                    set_=update_values,
                ).returning(User)

                row = (await session.execute(stmt)).scalar_one()
                return UserDTO.model_validate(row)

    async def get_by_id(self, user_id: int) -> UserDTO:
        """Fetch user by internal database ID or raise NotFoundError."""
        async with self._session_factory() as session:
            row = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"User with id={user_id} was not found")
            return UserDTO.model_validate(row)

    async def get_by_telegram_id(self, telegram_id: int) -> UserDTO | None:
        """Fetch user by Telegram user ID or return None if unregistered."""
        async with self._session_factory() as session:
            row = (
                await session.execute(select(User).where(User.telegram_id == telegram_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            return UserDTO.model_validate(row)

    async def search_users(self, query: str, *, limit: int = 20, offset: int = 0) -> list[UserDTO]:
        """Search users by numeric telegram_id, @username, or first/last name."""
        cleaned = query.strip()
        if not cleaned:
            return await self.list_users(limit=limit, offset=offset)

        safe_limit = max(1, min(limit, 100))
        safe_offset = max(0, offset)

        async with self._session_factory() as session:
            if cleaned.isdigit():
                numeric_val = int(cleaned)
                escaped = _escape_like(cleaned)
                pattern = f"%{escaped}%"
                filters: list[object] = [User.username.ilike(pattern, escape="\\")]
                if numeric_val <= _INT64_MAX:
                    filters.append(User.telegram_id == numeric_val)
                if numeric_val <= _INT32_MAX:
                    filters.append(User.id == numeric_val)

                stmt = select(User).where(or_(*filters))
                if numeric_val <= _INT64_MAX:
                    stmt = stmt.order_by((User.telegram_id == numeric_val).desc(), User.id.desc())
                else:
                    stmt = stmt.order_by(User.id.desc())
                stmt = stmt.limit(safe_limit).offset(safe_offset)
            elif cleaned.startswith("@"):
                handle = cleaned[1:].strip()
                if not handle:
                    return []
                pattern = f"%{_escape_like(handle)}%"
                stmt = (
                    select(User)
                    .where(User.username.ilike(pattern, escape="\\"))
                    .order_by(User.id.desc())
                    .limit(safe_limit)
                    .offset(safe_offset)
                )
            else:
                pattern = f"%{_escape_like(cleaned)}%"
                full_name = func.concat(
                    func.coalesce(User.first_name, ""),
                    " ",
                    func.coalesce(User.last_name, ""),
                )
                stmt = (
                    select(User)
                    .where(
                        or_(
                            User.username.ilike(pattern, escape="\\"),
                            User.first_name.ilike(pattern, escape="\\"),
                            User.last_name.ilike(pattern, escape="\\"),
                            full_name.ilike(pattern, escape="\\"),
                        )
                    )
                    .order_by(User.id.desc())
                    .limit(safe_limit)
                    .offset(safe_offset)
                )

            rows = (await session.execute(stmt)).scalars().all()
            return [UserDTO.model_validate(row) for row in rows]

    async def list_users(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        blocked_only: bool = False,
    ) -> list[UserDTO]:
        """List registered users ordered newest-first, optionally filtered to blocked accounts."""
        safe_limit = max(1, min(limit, 200))
        safe_offset = max(0, offset)
        async with self._session_factory() as session:
            stmt = select(User)
            if blocked_only:
                stmt = stmt.where(User.is_blocked.is_(True))
            stmt = stmt.order_by(User.created_at.desc(), User.id.desc()).limit(safe_limit).offset(safe_offset)
            rows = (await session.execute(stmt)).scalars().all()
            return [UserDTO.model_validate(row) for row in rows]

    async def set_language(self, telegram_id: int, lang_code: str) -> UserDTO:
        """Persist a validated 12-language choice for an existing user."""
        normalized = lang_code.strip().lower()
        if normalized not in SUPPORTED_LANGUAGES:
            raise ValidationError(f"Unsupported language code: {lang_code}")

        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(User).where(User.telegram_id == telegram_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise NotFoundError(f"User with telegram_id={telegram_id} was not found")
                row.language_code = normalized
                row.language_selected = True
                await session.flush()
                await session.refresh(row)
                return UserDTO.model_validate(row)

    async def set_blocked(self, user_id: int, *, blocked: bool, admin_id: int) -> UserDTO:
        """Block or unblock a user account using a row-level lock."""
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(select(User).where(User.id == user_id).with_for_update())
                ).scalar_one_or_none()
                if row is None:
                    raise NotFoundError(f"User with id={user_id} was not found")
                if blocked and row.telegram_id == admin_id:
                    raise ValidationError("Administrators cannot block their own account")
                row.is_blocked = blocked
                await session.flush()
                await session.refresh(row)
                logger.info(
                    "Admin telegram_id=%s set user id=%s (telegram_id=%s) is_blocked=%s",
                    admin_id,
                    row.id,
                    row.telegram_id,
                    blocked,
                )
                return UserDTO.model_validate(row)

    async def adjust_balance(self, adjustment: BalanceAdjustmentDTO) -> tuple[UserDTO, WalletLedgerDTO]:
        """Atomically charge or deduct user USD/IRT balance with FOR UPDATE lock and immutable ledger."""
        raw_amount = adjustment.amount
        if not isinstance(raw_amount, Decimal) or not raw_amount.is_finite() or raw_amount == Decimal("0"):
            raise ValidationError("Balance adjustment amount must be a finite non-zero Decimal")

        quant = _USD_QUANT if adjustment.currency == Currency.USD else _IRT_QUANT
        delta = raw_amount.quantize(quant, rounding=ROUND_HALF_UP)
        if delta == Decimal("0"):
            raise ValidationError("Adjustment amount rounds to zero for currency precision")

        async with self._session_factory() as session:
            async with session.begin():
                user = (
                    await session.execute(
                        select(User).where(User.id == adjustment.user_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if user is None:
                    raise NotFoundError(f"User with id={adjustment.user_id} was not found")

                if adjustment.currency == Currency.USD:
                    before_balance = Decimal(user.balance_usd).quantize(_USD_QUANT, rounding=ROUND_HALF_UP)
                    after_balance = (before_balance + delta).quantize(_USD_QUANT, rounding=ROUND_HALF_UP)
                    if after_balance < Decimal("0"):
                        raise InsufficientBalanceError(
                            f"Deduction exceeds current USD balance ({before_balance})"
                        )
                    user.balance_usd = after_balance
                elif adjustment.currency == Currency.IRT:
                    before_balance = Decimal(user.balance_irt).quantize(_IRT_QUANT, rounding=ROUND_HALF_UP)
                    after_balance = (before_balance + delta).quantize(_IRT_QUANT, rounding=ROUND_HALF_UP)
                    if after_balance < Decimal("0"):
                        raise InsufficientBalanceError(
                            f"Deduction exceeds current IRT balance ({before_balance})"
                        )
                    user.balance_irt = after_balance
                else:
                    raise ValidationError(f"Unsupported currency: {adjustment.currency}")

                ref_type = (
                    WalletLedgerRefType.ADMIN_CHARGE.value
                    if delta > Decimal("0")
                    else WalletLedgerRefType.ADMIN_DEDUCT.value
                )
                ledger_row = WalletLedger(
                    user_id=user.id,
                    currency=adjustment.currency.value,
                    before_balance=before_balance,
                    change_amount=delta,
                    after_balance=after_balance,
                    reference_type=ref_type,
                    reference_id=None,
                    admin_id=adjustment.admin_id,
                    note=adjustment.note.strip() if adjustment.note else None,
                )
                session.add(ledger_row)
                await session.flush()
                await session.refresh(user)
                await session.refresh(ledger_row)

                logger.info(
                    "Admin %s adjusted user id=%s %s by %s (%s -> %s)",
                    adjustment.admin_id,
                    user.id,
                    adjustment.currency.value,
                    delta,
                    before_balance,
                    after_balance,
                )
                return UserDTO.model_validate(user), WalletLedgerDTO.model_validate(ledger_row)

    async def get_wallet_history(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[WalletLedgerDTO]:
        """Return immutable wallet ledger history for a user ordered newest-first."""
        safe_limit = max(1, min(limit, 100))
        safe_offset = max(0, offset)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(WalletLedger)
                    .where(WalletLedger.user_id == user_id)
                    .order_by(WalletLedger.created_at.desc(), WalletLedger.id.desc())
                    .limit(safe_limit)
                    .offset(safe_offset)
                )
            ).scalars().all()
            return [WalletLedgerDTO.model_validate(row) for row in rows]
