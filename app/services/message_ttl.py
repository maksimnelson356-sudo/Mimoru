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
# Private-dialog deletions live in a separate queue so the group queue can keep
# dropping stale private entries without ever touching someone's dialogue.
PRIVATE_TTL_QUEUE_KEY = "mimoru:msg:ttl:private"
# Panel confirmation prompts (ban mode, reason, duration) wait for a click; they
# are removed after this delay, which matches the pending payload expiry.
PROMPT_TTL_SECONDS = 600
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
    redis: Redis | None,
    chat_id: int,
    message_id: int,
    *,
    delay_seconds: int = AUTO_DELETE_SECONDS,
    allow_private: bool = False,
) -> bool:
    """Queue one Mimoru group message for removal after `delay_seconds`.

    Private dialogs are never cleaned up unless the caller opts in explicitly
    (panel confirmation prompts are removed even there, on the owner's request).
    A service message in a personal chat with Mimoru is the user's own record rather
    than chat noise, and removing it later only looks like the bot hiding what it
    said. A positive chat id is a user id, so the rule lives here instead of in
    every caller. Callers without a Redis client (helpers deep in a chain) pass None
    and nothing is queued. Returns True when the removal was actually queued.
    """
    if redis is None or (int(chat_id) > 0 and not allow_private):
        return False
    queue_key = PRIVATE_TTL_QUEUE_KEY if int(chat_id) > 0 else TTL_QUEUE_KEY
    try:
        await redis.zadd(queue_key, {_member(chat_id, message_id): time.time() + delay_seconds})
        return True
    except Exception as error:  # Redis outage must not break the moderation action itself.
        log.warning(
            "message_ttl_schedule_failed",
            chat_id=chat_id,
            message_id=message_id,
            error=str(error),
        )
        return False


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
    handled = 0
    for queue_key in (TTL_QUEUE_KEY, PRIVATE_TTL_QUEUE_KEY):
        handled += await _drain_queue(bot, redis, queue_key)
    return handled


async def _drain_queue(bot: Bot, redis: Redis, queue_key: str) -> int:
    try:
        due = await redis.zrange(
            queue_key,
            0,
            time.time(),
            byscore=True,
            offset=0,
            num=BATCH_LIMIT,
        )
    except Exception as error:
        log.warning("message_ttl_scan_failed", queue=queue_key, error=str(error))
        return 0

    handled = 0
    for member in due:
        parsed = _parse(member)
        if parsed is None:
            await redis.zrem(queue_key, member)
            continue
        chat_id, message_id = parsed
        if chat_id > 0 and queue_key != PRIVATE_TTL_QUEUE_KEY:
            # Written by an older build or a stale worker: drop the entry without
            # ever reaching into someone's personal dialogue.
            await redis.zrem(queue_key, member)
            continue
        # Claim before deleting: a second worker or a retry must not duplicate work.
        if not await redis.zrem(queue_key, member):
            continue
        try:
            await bot.delete_message(chat_id, message_id)
            handled += 1
        except TelegramRetryAfter:
            # Rate limited: put it back for the next loop instead of losing the message.
            await schedule_message_deletion(
                redis,
                chat_id,
                message_id,
                delay_seconds=5,
                allow_private=queue_key == PRIVATE_TTL_QUEUE_KEY,
            )
        except (TelegramBadRequest, TelegramForbiddenError, KeyError):
            # Already deleted, no rights, or the chat disappeared — nothing to retry.
            handled += 1
    return handled
