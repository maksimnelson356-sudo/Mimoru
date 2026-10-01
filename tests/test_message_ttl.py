from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import DeleteMessage

from app.services.message_ttl import (
    AUTO_DELETE_SECONDS,
    PRIVATE_TTL_QUEUE_KEY,
    PROMPT_TTL_SECONDS,
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
async def test_private_dialog_messages_are_never_scheduled_for_removal() -> None:
    """Mimoru keeps her word in personal chats: nothing there is auto-deleted."""
    redis = SimpleNamespace(zadd=AsyncMock())

    assert await schedule_message_deletion(redis, 123456, 5) is False

    redis.zadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_notice_sent_to_a_private_chat_is_kept() -> None:
    redis = SimpleNamespace(zadd=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)))

    sent = await send_group_notice(bot, 123456, "🛠️ Бан", redis=redis)

    assert sent.message_id == 11
    redis.zadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_in_a_private_chat_is_kept() -> None:
    redis = SimpleNamespace(zadd=AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(id=123456),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=12)),
    )

    sent = await answer_group_notice(message, "🛠️", redis=redis)

    assert sent.message_id == 12
    redis.zadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_private_entries_are_dropped_without_deleting_anything() -> None:
    """A leftover queue entry pointing at a user must never reach Telegram."""
    redis = SimpleNamespace(
        # The second call drains the private queue, which stays empty here.
        zrange=AsyncMock(side_effect=[["123456:456"], []]),
        zrem=AsyncMock(return_value=1),
        zadd=AsyncMock(),
    )
    bot = SimpleNamespace(delete_message=AsyncMock())

    assert await process_message_deletions(bot, redis) == 0

    bot.delete_message.assert_not_awaited()
    redis.zrem.assert_awaited_once_with(TTL_QUEUE_KEY, "123456:456")
    redis.zadd.assert_not_awaited()


# --- wiring contracts: moderation results must go through the queue ----------

MODERATION_RESULT_SOURCES = {
    "app/handlers/group.py": (
        "await send_group_notice(bot, message.chat.id, result, redis=redis)"
    ),
    "app/handlers/moderation_command_modes.py": (
        "await send_group_notice(bot, message.chat.id, result, redis=redis)"
    ),
    "app/handlers/group_commands.py": (
        "await send_group_notice(bot, message.chat.id, notice)"
    ),
    "app/handlers/complaint_actions.py": (
        "await send_group_notice(bot, group.telegram_chat_id, msg)"
    ),
}


def test_moderation_result_notices_are_scheduled_for_removal() -> None:
    """Regression guard: text-command results used to reply plainly and stayed forever."""
    for path, expected in MODERATION_RESULT_SOURCES.items():
        source = Path(path).read_text(encoding="utf-8")
        assert expected in source, path

    group_src = Path("app/handlers/group.py").read_text(encoding="utf-8")
    assert "await message.reply(result)" not in group_src

    modes_src = Path("app/handlers/moderation_command_modes.py").read_text(
        encoding="utf-8"
    )
    assert "await message.reply(result)" not in modes_src

    commands_src = Path("app/handlers/group_commands.py").read_text(encoding="utf-8")
    assert "await message.reply(notice)" not in commands_src


def test_deferred_ban_results_no_longer_use_plain_replies() -> None:
    source = Path("app/handlers/deferred_bans.py").read_text(encoding="utf-8")
    assert source.count("await send_group_notice(") >= 4
    assert 'await message.reply(f"🚫 {label} забанен' not in source
    assert 'await message.reply(f"✅ Запрет для {label} снят.")' not in source


@pytest.mark.asyncio
async def test_none_redis_schedules_nothing() -> None:
    assert await schedule_message_deletion(None, -100123, 5) is False


# --- wiring contracts: the moderator's own command is cleaned up too ---------


COMMAND_DELETE_SOURCES = {
    "app/handlers/group.py": (
        "await schedule_message_deletion(redis, message.chat.id, message.message_id)"
    ),
    "app/handlers/moderation_command_modes.py": (
        "await schedule_message_deletion(redis, message.chat.id, message.message_id)"
    ),
    "app/handlers/group_commands.py": (
        "await schedule_message_deletion(bound_redis(), message.chat.id, message.message_id)"
    ),
    "app/handlers/deferred_bans.py": (
        "await schedule_message_deletion(bound_redis(), message.chat.id, message.message_id)"
    ),
}


def test_the_moderators_command_is_scheduled_for_removal() -> None:
    """Regression guard: the command that issued a punishment used to stay behind."""
    for path, expected in COMMAND_DELETE_SOURCES.items():
        source = Path(path).read_text(encoding="utf-8")
        assert expected in source, path

    commands_src = Path("app/handlers/group_commands.py").read_text(encoding="utf-8")
    assert (
        commands_src.count(
            "schedule_message_deletion(bound_redis(), message.chat.id, message.message_id)"
        )
        == 3
    )


def test_picker_flow_deletes_the_command_and_the_buttons() -> None:
    group_src = Path("app/handlers/group.py").read_text(encoding="utf-8")
    modes_src = Path("app/handlers/moderation_command_modes.py").read_text(
        encoding="utf-8"
    )
    reason_src = Path("app/handlers/reason_admin.py").read_text(encoding="utf-8")
    # The pending payload carries the command message id.
    assert '"command_message_id": message.message_id' in group_src
    assert '"command_message_id": message.message_id' in modes_src
    # After a successful picker execution the command message and the edited
    # picker message (the one that held the buttons) are both queued for removal.
    assert 'command_message_id = data.get("command_message_id")' in reason_src
    assert "callback.message.chat.id, callback.message.message_id" in reason_src


@pytest.mark.asyncio
async def test_private_deletions_need_an_explicit_opt_in() -> None:
    assert await schedule_message_deletion(None, 555, 5, allow_private=True) is False

    redis = MagicMock()
    redis.zadd = AsyncMock(return_value=True)

    # No opt-in: a private chat id is refused, even with a client.
    assert await schedule_message_deletion(redis, 555, 5) is False
    redis.zadd.assert_not_awaited()

    # Opted in (panel confirmation prompts): queued in the private queue.
    assert await schedule_message_deletion(redis, 555, 5, allow_private=True) is True
    redis.zadd.assert_awaited_once()
    assert redis.zadd.await_args.args[0] == PRIVATE_TTL_QUEUE_KEY


def test_panel_ban_prompts_are_removed_and_disclaimer_is_gone() -> None:
    """Regression guard: the ban-confirmation prompt used to linger forever."""
    for path in (
        "app/handlers/complaint_actions.py",
        "app/handlers/moderation_durable_guard.py",
    ):
        source = Path(path).read_text(encoding="utf-8")
        assert "allow_private=True" in source, path
        assert "Очистка необратима" not in source, path
        assert "удалит не все сообщения" not in source, path

    ttl_src = Path("app/services/message_ttl.py").read_text(encoding="utf-8")
    assert "PRIVATE_TTL_QUEUE_KEY" in ttl_src
    assert "PROMPT_TTL_SECONDS = 600" in ttl_src



@pytest.mark.asyncio
async def test_expired_messages_are_claimed_before_being_deleted() -> None:
    redis = SimpleNamespace(
        # The second call drains the private queue, which stays empty here.
        zrange=AsyncMock(side_effect=[["-100123:456", "-100123:457"], []]),
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
        # The second call drains the private queue, which stays empty here.
        zrange=AsyncMock(side_effect=[[b"-100123:456", "not-a-member", "1:2:3"], []]),
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
        # The second call drains the private queue, which stays empty here.
        zrange=AsyncMock(side_effect=[["-100123:456"], []]),
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
def test_ban_confirmation_prompts_never_fall_back_to_a_raw_id() -> None:
    """Regression guard: the moderation-command confirmation used to render the
    bare numeric target id when the pending payload carried no stored name."""
    guard = Path("app/handlers/moderation_durable_guard.py").read_text(encoding="utf-8")
    assert 'data.get("target_name") or public_user_token(target_id)' in guard
    assert 'data.get("target_name", target_id)' not in guard

    complaint = Path("app/handlers/complaint_actions.py").read_text(encoding="utf-8")
    assert "public_user_token(complaint.target_telegram_id)" in complaint
