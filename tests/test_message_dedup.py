"""Тесты для MessageDeduplicationMiddleware (#5).

Проверяют, что:
- повторный Message с тем же (chat_id, message_id) — пропускается;
- разные message_id — оба обработаны;
- разные чаты с одинаковым message_id — оба обработаны;
- повторный CallbackQuery с тем же callback.id — пропускается;
- Message и CallbackQuery не мешают друг другу;
- EditedMessage / InlineQuery не дедуплицируются;
- TTL ключа выставлен правильно.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from app.middlewares_dedup import (
    CALLBACK_DEDUP_KEY,
    CALLBACK_DEDUP_TTL,
    MESSAGE_DEDUP_KEY,
    MESSAGE_DEDUP_TTL,
    MessageDeduplicationMiddleware,
)


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def middleware(redis) -> MessageDeduplicationMiddleware:
    return MessageDeduplicationMiddleware(redis)


def _make_message(*, chat_id: int = -1001, message_id: int = 42) -> Message:
    return Message(
        message_id=message_id,
        date=0,
        chat=Chat(id=chat_id, type="supergroup"),
        from_user=User(id=111, is_bot=False, first_name="Test"),
        text="привет",
    )


def _make_callback(*, user_id: int = 222, callback_id: str = "cbq-abc") -> CallbackQuery:
    return CallbackQuery(
        id=callback_id,
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        chat_instance="x",
        data="test",
    )


@pytest.fixture
def handler() -> AsyncMock:
    return AsyncMock(return_value="HANDLED")


class TestMessageDedup:
    async def test_first_message_processed(self, middleware, handler, redis) -> None:
        message = _make_message(chat_id=-1001, message_id=1)
        result = await middleware(handler, message, {})
        assert result == "HANDLED"
        handler.assert_awaited_once()

    async def test_duplicate_message_skipped(self, middleware, handler) -> None:
        message = _make_message(chat_id=-1001, message_id=1)
        await middleware(handler, message, {})
        result = await middleware(handler, message, {})

        assert result is None
        # handler должен быть вызван РОВНО 1 раз
        assert handler.await_count == 1

    async def test_third_duplicate_also_skipped(self, middleware, handler) -> None:
        message = _make_message(chat_id=-1001, message_id=1)
        await middleware(handler, message, {})
        await middleware(handler, message, {})
        result = await middleware(handler, message, {})

        assert result is None
        assert handler.await_count == 1

    async def test_different_message_id_not_blocked(self, middleware, handler) -> None:
        await middleware(handler, _make_message(chat_id=-1001, message_id=1), {})
        await middleware(handler, _make_message(chat_id=-1001, message_id=2), {})

        assert handler.await_count == 2

    async def test_different_chat_not_blocked(self, middleware, handler) -> None:
        # Один и тот же message_id, но разные чаты
        await middleware(handler, _make_message(chat_id=-1001, message_id=1), {})
        await middleware(handler, _make_message(chat_id=-2002, message_id=1), {})

        assert handler.await_count == 2

    async def test_key_format_for_message(self, middleware, handler, redis) -> None:
        await middleware(handler, _make_message(chat_id=-1001, message_id=1), {})
        expected_key = MESSAGE_DEDUP_KEY.format(chat_id=-1001, message_id=1)
        assert await redis.exists(expected_key)

    async def test_message_key_has_ttl(self, middleware, handler, redis) -> None:
        await middleware(handler, _make_message(chat_id=-1001, message_id=1), {})
        key = MESSAGE_DEDUP_KEY.format(chat_id=-1001, message_id=1)
        ttl = await redis.ttl(key)
        assert 0 < ttl <= MESSAGE_DEDUP_TTL


class TestCallbackDedup:
    async def test_first_callback_processed(self, middleware, handler) -> None:
        callback = _make_callback(user_id=222, callback_id="cbq-1")
        result = await middleware(handler, callback, {})
        assert result == "HANDLED"

    async def test_duplicate_callback_skipped(self, middleware, handler) -> None:
        callback = _make_callback(user_id=222, callback_id="cbq-1")
        await middleware(handler, callback, {})
        result = await middleware(handler, callback, {})

        assert result is None
        assert handler.await_count == 1

    async def test_different_callback_id_not_blocked(self, middleware, handler) -> None:
        await middleware(handler, _make_callback(user_id=222, callback_id="cbq-1"), {})
        await middleware(handler, _make_callback(user_id=222, callback_id="cbq-2"), {})

        assert handler.await_count == 2

    async def test_callback_key_format(self, middleware, handler, redis) -> None:
        await middleware(handler, _make_callback(user_id=222, callback_id="cbq-1"), {})
        expected_key = CALLBACK_DEDUP_KEY.format(user_id=222, callback_id="cbq-1")
        assert await redis.exists(expected_key)

    async def test_callback_key_has_ttl(self, middleware, handler, redis) -> None:
        await middleware(handler, _make_callback(user_id=222, callback_id="cbq-1"), {})
        key = CALLBACK_DEDUP_KEY.format(user_id=222, callback_id="cbq-1")
        ttl = await redis.ttl(key)
        assert 0 < ttl <= CALLBACK_DEDUP_TTL


class TestCrossTypeIsolation:
    async def test_message_and_callback_independent(self, middleware, handler) -> None:
        """Message и CallbackQuery не блокируют друг друга."""
        message = _make_message(chat_id=-1001, message_id=1)
        callback = _make_callback(user_id=222, callback_id="cbq-1")

        await middleware(handler, message, {})
        await middleware(handler, callback, {})

        assert handler.await_count == 2


class TestUnsupportedTypes:
    async def test_unknown_event_not_deduplicated(self, middleware, handler) -> None:
        """Для событий без ключа — middleware не дедуплицирует."""
        # Используем простой объект — не Message и не CallbackQuery
        event = MagicMock()
        result = await middleware(handler, event, {})
        assert result == "HANDLED"
        # Второй вызов — тоже пройдёт (нет дедупликации)
        result = await middleware(handler, event, {})
        assert result == "HANDLED"
        assert handler.await_count == 2


class TestConcurrency:
    async def test_concurrent_same_message_only_one_processed(self, middleware) -> None:
        """10 параллельных обработок одного message — только 1 пройдёт."""
        import asyncio

        handler = AsyncMock(return_value="HANDLED")
        message = _make_message(chat_id=-1001, message_id=1)

        results = await asyncio.gather(*[
            middleware(handler, message, {}) for _ in range(10)
        ])

        assert results.count("HANDLED") == 1
        assert results.count(None) == 9
        assert handler.await_count == 1


class TestConstants:
    def test_ttl_values(self) -> None:
        assert MESSAGE_DEDUP_TTL == 24 * 60 * 60
        assert CALLBACK_DEDUP_TTL == 60 * 60

    def test_key_patterns_contain_namespace(self) -> None:
        assert MESSAGE_DEDUP_KEY.startswith("mimoru:dedup:msg:")
        assert CALLBACK_DEDUP_KEY.startswith("mimoru:dedup:cbq:")