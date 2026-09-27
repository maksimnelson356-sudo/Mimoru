from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.db.models import Group
from app.db.required_reconcile_models import RequiredSubscriptionReconcile
from app.db.session import SessionFactory

log = structlog.get_logger(__name__)

RETRY_BASE_SECONDS = 30
MAX_RETRY_SECONDS = 300
PROCESSING_STALE_SECONDS = 900
BATCH_SIZE = 20


async def enqueue_required_reconcile(
    session,
    *,
    group_id: int,
    telegram_chat_id: int,
    channel_username: str,
    dedupe_key: str,
) -> bool:
    """Add one durable post-activation reconciliation intent to the transaction."""
    existing = await session.scalar(
        select(RequiredSubscriptionReconcile.id).where(
            RequiredSubscriptionReconcile.dedupe_key == dedupe_key
        )
    )
    if existing is not None:
        return False
    session.add(
        RequiredSubscriptionReconcile(
            group_id=group_id,
            telegram_chat_id=telegram_chat_id,
            channel_username=channel_username,
            dedupe_key=dedupe_key,
        )
    )
    await session.flush()
    return True


async def recover_stale_reconcile_intents() -> None:
    """Return intents abandoned by a crashed worker to the retry queue."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=PROCESSING_STALE_SECONDS)
    async with SessionFactory() as session:
        await session.execute(
            update(RequiredSubscriptionReconcile)
            .where(
                RequiredSubscriptionReconcile.status == "processing",
                RequiredSubscriptionReconcile.updated_at < cutoff,
            )
            .values(
                status="failed",
                next_attempt_at=now,
                last_error="worker_timeout",
            )
        )
        await session.commit()


async def _claim_due_intents() -> list[int]:
    now = datetime.now(timezone.utc)
    async with SessionFactory() as session:
        rows = list(
            (
                await session.scalars(
                    select(RequiredSubscriptionReconcile)
                    .where(
                        RequiredSubscriptionReconcile.status.in_(("pending", "failed")),
                        RequiredSubscriptionReconcile.next_attempt_at <= now,
                    )
                    .order_by(RequiredSubscriptionReconcile.next_attempt_at)
                    .limit(BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for row in rows:
            row.status = "processing"
            row.attempts += 1
        await session.commit()
        return [row.id for row in rows]


async def _mark_sent(intent_id: int) -> None:
    async with SessionFactory() as session:
        intent = await session.get(RequiredSubscriptionReconcile, intent_id)
        if intent is None:
            return
        intent.status = "sent"
        intent.last_error = None
        await session.commit()


async def _mark_retry(intent_id: int, error: Exception) -> None:
    async with SessionFactory() as session:
        intent = await session.get(RequiredSubscriptionReconcile, intent_id)
        if intent is None:
            return
        delay = min(
            MAX_RETRY_SECONDS,
            RETRY_BASE_SECONDS * (2 ** min(max(intent.attempts - 1, 0), 4)),
        )
        intent.status = "failed"
        intent.last_error = str(error)[:1000]
        intent.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        await session.commit()


async def process_required_subscription_reconciles(bot: Bot, redis: Redis) -> None:
    """Retry durable post-activation member restriction work."""
    await recover_stale_reconcile_intents()
    for intent_id in await _claim_due_intents():
        async with SessionFactory() as session:
            intent = await session.get(RequiredSubscriptionReconcile, intent_id)
            if intent is None:
                continue
            group = await session.get(Group, intent.group_id)
            if group is None or not group.is_active:
                await _mark_sent(intent_id)
                continue
            try:
                from app.handlers.required_direct import (
                    restrict_existing_unsubscribed_members,
                )

                await restrict_existing_unsubscribed_members(
                    bot,
                    redis,
                    group_id=intent.group_id,
                    telegram_chat_id=intent.telegram_chat_id,
                    channels=[intent.channel_username],
                )
            except Exception as error:
                log.warning(
                    "required_subscription_reconcile_failed",
                    intent_id=intent_id,
                    group_id=intent.group_id,
                    error=str(error),
                )
                await _mark_retry(intent_id, error)
            else:
                await _mark_sent(intent_id)
