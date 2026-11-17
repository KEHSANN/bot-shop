"""Shared pytest fixtures for async PostgreSQL 16, Redis 7, and Settings."""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings, clear_settings_cache, get_settings  # noqa: E402


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Return absolute path to the project root directory."""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def app_settings() -> Settings:
    """Return validated runtime Settings loaded from .env."""
    clear_settings_cache()
    settings = get_settings()
    settings.validate_runtime_secrets(require_bot_token=True)
    return settings


@pytest.fixture
async def db_engine(app_settings: Settings) -> AsyncGenerator[AsyncEngine, None]:
    """Provide an isolated NullPool AsyncEngine per test function."""
    engine = create_async_engine(
        app_settings.DATABASE_URL,
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session_factory(
    db_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Provide an async_sessionmaker bound to the per-test AsyncEngine."""
    return async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@pytest.fixture
async def db_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Yield a standalone AsyncSession for read queries or explicit transactions."""
    async with db_session_factory() as session:
        yield session


@pytest.fixture
async def redis_client(app_settings: Settings) -> AsyncGenerator[Redis, None]:
    """Yield an async Redis 7 client and close cleanly after the test."""
    client = Redis.from_url(app_settings.REDIS_URL, decode_responses=True)
    try:
        pong = await client.ping()
        assert pong is True
        yield client
    finally:
        await client.aclose()
