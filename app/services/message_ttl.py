from __future__ import annotations

import time

import structlog
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Message
from redis.asyncio import Redis

log = structlog.get_logger(__name__)

# Delay before Mimoru removes her own service reply from a group chat.
AUTO_DELETE_SECONDS = 25

# A Redis sorted set is used instead of asyncio.create_task(...): a restart or a
# redeploy must not leave moderation notices standing in group chats forever.
TTL_QUEUE_KEY = "mimoru:msg:ttl"
BATCH_LIMIT = 100

# Bound once at startup (app.main) so deep call chains — protection, moderation
# helpers — can schedule their own notices without threading Redis through every
# function signature. Explicit `redis=` arguments always win.
_bound_redis: Redis | None = None


def bind_redis(redis: Redis | None) -> None:
    global _bound_redis
    _bound_redis = redis


def bound_redis() -> Redis | None:
    return _bound_redis


def _member(chat_id: int, message_id: int) -> str:
    return f"{int(chat_id)}:{int(message_id)}"


def _parse(member: str | bytes) -> tuple[int, int] | None:
    if isinstance(member, bytes):  # decode_responses=False clients hand back bytes
        try:
            member = member.decode()
        except UnicodeDecodeError:
            return None
    try:
        raw_chat_id, raw_message_id = member.split(":", 1)
        return int(raw_chat_id), int(raw_message_id)
    except (AttributeError, TypeError, ValueError):
        return None


async def schedule_message_deletion(
    redis: Redis,
    chat_id: int,
    message_id: int,
    *,
    delay_seconds: int = AUTO_DELETE_SECONDS,
) -> None:
    """Queue one Mimoru message for removal after `delay_seconds`."""
    try:
        await redis.zadd(TTL_QUEUE_KEY, {_member(chat_id, message_id): time.time() + delay_seconds})
    except Exception as error:  # Redis outage must not break the moderation action itself.
        log.warning(
            "message_ttl_schedule_failed",
            chat_id=chat_id,
            message_id=message_id,
            error=str(error),
        )


async def send_group_notice(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    redis: Redis | None = None,
    reply_to_message_id: int | None = None,
    delay_seconds: int = AUTO_DELETE_SECONDS,
    **kwargs,
):
    """Post a service notice into a group chat and schedule its own removal.

    Moderation notices are informational: after a few seconds they only clutter
    the chat, so every one of them is removed automatically. Telegram errors are
    re-raised for callers that already handle chat-availability failures.
    """
    sent = await bot.send_message(
        chat_id,
        text,
        reply_to_message_id=reply_to_message_id,
        **kwargs,
    )
    client = redis if redis is not None else _bound_redis
    if client is not None:
        await schedule_message_deletion(
            client,
            chat_id,
            sent.message_id,
            delay_seconds=delay_seconds,
        )
    return sent


async def answer_group_notice(
    message: Message,
    text: str,
    *,
    redis: Redis | None = None,
    delay_seconds: int = AUTO_DELETE_SECONDS,
) -> Message | None:
    """`message.answer` for group notices that must disappear on their own."""
    try:
        sent = await message.answer(text)
    except (TelegramBadRequest, TelegramForbiddenError):
        return None
    client = redis if redis is not None else _bound_redis
    if client is not None:
        await schedule_message_deletion(
            client,
            message.chat.id,
            sent.message_id,
            delay_seconds=delay_seconds,
        )
    return sent


async def process_message_deletions(bot: Bot, redis: Redis) -> int:
    """Delete due messages; returns how many were removed or dropped."""
    try:
        due = await redis.zrange(
            TTL_QUEUE_KEY,
            0,
            time.time(),
            byscore=True,
            offset=0,
            num=BATCH_LIMIT,
        )
    except Exception as error:
        log.warning("message_ttl_scan_failed", error=str(error))
        return 0

    handled = 0
    for member in due:
        parsed = _parse(member)
        if parsed is None:
            await redis.zrem(TTL_QUEUE_KEY, member)
            continue
        chat_id, message_id = parsed
        # Claim before deleting: a second worker or a retry must not duplicate work.
        if not await redis.zrem(TTL_QUEUE_KEY, member):
            continue
        try:
            await bot.delete_message(chat_id, message_id)
            handled += 1
        except TelegramRetryAfter:
            # Rate limited: put it back for the next loop instead of losing the message.
            await schedule_message_deletion(redis, chat_id, message_id, delay_seconds=5)
        except (TelegramBadRequest, TelegramForbiddenError, KeyError):
            # Already deleted, no rights, or the chat disappeared — nothing to retry.
            handled += 1
    return handled
