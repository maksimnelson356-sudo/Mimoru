"""Pytest configuration.

Устанавливает фейковые env-переменные ДО импорта app.core.config,
чтобы pydantic-settings не падал при валидации.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_TESTS")
os.environ.setdefault("SERVICE_OWNER_IDS", "123456789")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("HEALTH_HOST", "127.0.0.1")
os.environ.setdefault("HEALTH_PORT", "8080")


@pytest.fixture
async def db_engine():
    """Per-test engine with pooling disabled, for tests marked ``db``.

    ``app.db.session.engine`` is built at import time and pools connections, but
    pytest-asyncio gives every test a fresh event loop. Once a pooled connection
    outlives its loop, asyncpg fails with "got Future attached to a different
    loop". NullPool keeps pooling out of these tests so each one gets connections
    bound to its own loop. The SQLAlchemy -> asyncpg -> PostgreSQL path under
    test is unchanged, and pooling is not what these tests verify.
    """
    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    """Session factory bound to the current test's engine and event loop."""
    return async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)