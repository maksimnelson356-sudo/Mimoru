"""Pytest configuration.

Устанавливает фейковые env-переменные ДО импорта app.core.config,
чтобы pydantic-settings не падал при валидации.
"""

from __future__ import annotations

import os


os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_TESTS")
os.environ.setdefault("SERVICE_OWNER_IDS", "123456789")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("HEALTH_HOST", "127.0.0.1")
os.environ.setdefault("HEALTH_PORT", "8080")