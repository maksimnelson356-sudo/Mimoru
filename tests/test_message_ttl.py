from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import DeleteMessage

from app.services.message_ttl import (
    AUTO_DELETE_SECONDS,
    TTL_QUEUE_KEY,
    answer_group_notice,
    bind_redis,
    process_message_deletions,
    schedule_message_deletion,
    send_group_notice,
)


@pytest.fixture(autouse=True)
def _unbound_redis():
    """Each test starts without a globally bound Redis client."""
    bind_redis(None)
    yield
    bind_redis(None)


def _queued(zadd: AsyncMock) -> tuple[str, str, float]:
    (key, mapping), _ = zadd.call_args
    member, score = next(iter(mapping.items()))
    return key, member, score


def test_notice_ttl_is_25_seconds() -> None:
    assert AUTO_DELETE_SECONDS == 25


@pytest.mark.asyncio
async def test_schedule_queues_chat_and_message_with_deadline() -> None:
    redis = SimpleNamespace(zadd=AsyncMock())

    await schedule_message_deletion(redis, -100123, 456)

    key, member, score = _queued(redis.zadd)
    assert key == TTL_QUEUE_KEY
    assert member == "-100123:456"
    assert time.time() + AUTO_DELETE_SECONDS - 5 <= score <= time.time() + AUTO_DELETE_SECONDS + 5


@pytest.mark.asyncio
async def test_group_notice_is_scheduled_for_auto_removal() -> None:
    redis = SimpleNamespace(zadd=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)))

    await send_group_notice(bot, -100123, "🛠️ Бан", redis=redis)

    _, member, _ = _queued(redis.zadd)
    assert member == "-100123:456"


@pytest.mark.asyncio
async def test_group_notice_falls_back_to_bound_redis_client() -> None:
    redis = SimpleNamespace(zadd=AsyncMock())
    bind_redis(redis)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)))

    await send_group_notice(bot, -100123, "🛠️ Бан")

    assert redis.zadd.await_count == 1


@pytest.mark.asyncio
async def test_group_notice_survives_redis_outage() -> None:
    redis = SimpleNamespace(zadd=AsyncMock(side_effect=ConnectionError("redis is down")))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=8)))

    sent = await send_group_notice(bot, -100123, "🛠️ Бан", redis=redis)

    assert sent.message_id == 8


@pytest.mark.asyncio
async def test_answer_group_notice_schedules_its_own_removal() -> None:
    redis = SimpleNamespace(zadd=AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=9)),
    )

    sent = await answer_group_notice(message, "Флуд: замучен на 15 мин", redis=redis)

    assert sent.message_id == 9
    _, member, _ = _queued(redis.zadd)
    assert member == "-100123:9"


@pytest.mark.asyncio
async def test_answer_group_notice_is_quiet_without_chat_rights() -> None:
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        answer=AsyncMock(
            side_effect=TelegramForbiddenError(
                method=DeleteMessage(chat_id=-100123, message_id=1),
                message="bot is not a member of the chat",
            )
        ),
    )

    assert await answer_group_notice(message, "🛠️", redis=SimpleNamespace(zadd=AsyncMock())) is None


@pytest.mark.asyncio
async def test_expired_messages_are_claimed_before_being_deleted() -> None:
    redis = SimpleNamespace(
        zrange=AsyncMock(return_value=["-100123:456", "-100123:457"]),
        # Second member was already claimed by another worker.
        zrem=AsyncMock(side_effect=[1, 0]),
        zadd=AsyncMock(),
    )
    bot = SimpleNamespace(delete_message=AsyncMock())

    handled = await process_message_deletions(bot, redis)

    assert handled == 1
    bot.delete_message.assert_awaited_once_with(-100123, 456)


@pytest.mark.asyncio
async def test_garbage_and_bytes_members_are_handled() -> None:
    redis = SimpleNamespace(
        zrange=AsyncMock(return_value=[b"-100123:456", "not-a-member", "1:2:3"]),
        zrem=AsyncMock(return_value=1),
        zadd=AsyncMock(),
    )
    bot = SimpleNamespace(delete_message=AsyncMock())

    handled = await process_message_deletions(bot, redis)

    assert handled == 1
    bot.delete_message.assert_awaited_once_with(-100123, 456)
    assert redis.zrem.await_count == 3  # the parsed member plus both broken ones


@pytest.mark.asyncio
async def test_gone_messages_are_dropped_instead_of_retried() -> None:
    redis = SimpleNamespace(
        zrange=AsyncMock(return_value=["-100123:456"]),
        zrem=AsyncMock(return_value=1),
        zadd=AsyncMock(),
    )
    bot = SimpleNamespace(
        delete_message=AsyncMock(
            side_effect=TelegramBadRequest(
                method=DeleteMessage(chat_id=-100123, message_id=456),
                message="message to delete not found",
            )
        )
    )

    assert await process_message_deletions(bot, redis) == 1
    assert redis.zadd.await_count == 0