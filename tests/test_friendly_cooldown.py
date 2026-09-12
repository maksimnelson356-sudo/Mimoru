"""Тесты для Redis-based кулдауна social actions.

Проверяют чистую функцию _check_cooldown, которая работает через
Redis SET NX EX — атомарно ставит ключ mimoru:action-cooldown:... на 3 секунды.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.game_friendly_results import (
    ACTION_COOLDOWN_SECONDS,
    COOLDOWN_KEY_TEMPLATE,
    _check_cooldown,
)


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


class TestCooldownBasics:
    async def test_first_call_accepted(self, redis) -> None:
        assert await _check_cooldown(redis, chat_id=1, user_id=2, action="обнять") is True

    async def test_second_call_rejected(self, redis) -> None:
        await _check_cooldown(redis, 1, 2, "обнять")
        assert await _check_cooldown(redis, 1, 2, "обнять") is False

    async def test_third_call_also_rejected(self, redis) -> None:
        await _check_cooldown(redis, 1, 2, "обнять")
        await _check_cooldown(redis, 1, 2, "обнять")
        assert await _check_cooldown(redis, 1, 2, "обнять") is False


class TestCooldownScoping:
    """Cooldown per (chat, user, action) — независимые ключи."""

    async def test_different_action_not_blocked(self, redis) -> None:
        await _check_cooldown(redis, 1, 2, "обнять")
        assert await _check_cooldown(redis, 1, 2, "поцеловать") is True

    async def test_different_user_not_blocked(self, redis) -> None:
        await _check_cooldown(redis, 1, 2, "обнять")
        assert await _check_cooldown(redis, 1, 3, "обнять") is True

    async def test_different_chat_not_blocked(self, redis) -> None:
        await _check_cooldown(redis, 1, 2, "обнять")
        assert await _check_cooldown(redis, 2, 2, "обнять") is True


class TestCooldownKey:
    async def test_key_format(self, redis) -> None:
        await _check_cooldown(redis, 111, 222, "обнять")
        expected = COOLDOWN_KEY_TEMPLATE.format(chat_id=111, user_id=222, action="обнять")
        assert await redis.exists(expected)

    async def test_key_has_ttl(self, redis) -> None:
        await _check_cooldown(redis, 111, 222, "обнять")
        key = COOLDOWN_KEY_TEMPLATE.format(chat_id=111, user_id=222, action="обнять")
        ttl = await redis.ttl(key)
        assert 0 < ttl <= ACTION_COOLDOWN_SECONDS

    async def test_ttl_equals_cooldown_seconds(self, redis) -> None:
        assert ACTION_COOLDOWN_SECONDS == 3


class TestCooldownExpiry:
    async def test_after_delete_can_call_again(self, redis) -> None:
        """Эмуляция «прошло 3 секунды» — удаляем ключ вручную."""
        assert await _check_cooldown(redis, 1, 2, "обнять") is True
        key = COOLDOWN_KEY_TEMPLATE.format(chat_id=1, user_id=2, action="обнять")
        await redis.delete(key)
        assert await _check_cooldown(redis, 1, 2, "обнять") is True

    async def test_different_actions_have_independent_ttl(self, redis) -> None:
        await _check_cooldown(redis, 1, 2, "обнять")
        await _check_cooldown(redis, 1, 2, "поцеловать")
        key_a = COOLDOWN_KEY_TEMPLATE.format(chat_id=1, user_id=2, action="обнять")
        key_b = COOLDOWN_KEY_TEMPLATE.format(chat_id=1, user_id=2, action="поцеловать")
        assert await redis.exists(key_a)
        assert await redis.exists(key_b)


class TestCooldownAtomicity:
    async def test_concurrent_calls_only_one_succeeds(self, redis) -> None:
        """10 параллельных вызовов — только 1 успешный (SET NX атомарен)."""
        import asyncio

        results = await asyncio.gather(*[
            _check_cooldown(redis, 1, 2, "обнять") for _ in range(10)
        ])
        assert sum(1 for r in results if r) == 1
        assert sum(1 for r in results if not r) == 9