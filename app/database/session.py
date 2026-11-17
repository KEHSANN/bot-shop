"""Async SQLAlchemy engine and session lifecycle management."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_async_engine_from_url(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create a configured AsyncEngine with pool pre-ping enabled."""
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async_sessionmaker bound to the given AsyncEngine."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def init_db(database_url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """Initialize module-level AsyncEngine and session factory."""
    global _engine, _session_factory
    url = database_url or get_settings().DATABASE_URL
    _engine = create_async_engine_from_url(url, echo=echo)
    _session_factory = create_session_factory(_engine)
    return _engine


def get_engine() -> AsyncEngine:
    """Return initialized AsyncEngine, initializing lazily from settings if needed."""
    if _engine is None:
        return init_db()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return initialized async_sessionmaker, initializing lazily if needed."""
    if _session_factory is None:
        init_db()
    assert _session_factory is not None
    return _session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession for FastAPI Depends or manual async context usage."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def close_db() -> None:
    """Dispose the module-level AsyncEngine cleanly on shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
