from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select

from app.db.models import Group, UserMessage
from app.db.session import SessionFactory
from app.services.plans import effective_plan, remaining_days, subscription_state
from app.services.runtime import stop_task

LEASE_KEY = "mimoru:background-loop:leader"
LEASE_SECONDS = 30
RENEW_SECONDS = 10
RETRY_SECONDS = 5
GROUP_DISCONNECT_RECOVERY_SECONDS = 30
CHAT_PERMISSION_RECOVERY_SECONDS = 30
RANK_PROVISIONING_RECOVERY_SECONDS = 30
JOIN_REVIEW_RECOVERY_SECONDS = 30
INVITE_OPERATION_RECOVERY_SECONDS = 30
DUPLICATE_REFUND_RECOVERY_SECONDS = 30
SUBSCRIPTION_REFUND_RECOVERY_SECONDS = 30
MODERATION_OPERATION_RECOVERY_SECONDS = 30
USER_MESSAGES_CLEANUP_SECONDS = 3600
PLAN_NOTICE_SECONDS = 3600  # раз в час

# Limit how many recovery tasks can execute concurrently. Each recovery
# function may open DB connections during Telegram API calls. Capping
# concurrency prevents pool exhaustion when many recovery loops fire
# simultaneously. The main background_loop is a separate caller and
# does not use this semaphore.
RECOVERY_CONCURRENCY = 4
_recovery_semaphore: asyncio.Semaphore | None = None


def _get_recovery_semaphore() -> asyncio.Semaphore:
    global _recovery_semaphore
    if _recovery_semaphore is None:
        _recovery_semaphore = asyncio.Semaphore(RECOVERY_CONCURRENCY)
    return _recovery_semaphore


_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


async def _acquire_lease(redis: Redis, token: str) -> bool:
    return bool(await redis.set(LEASE_KEY, token, nx=True, ex=LEASE_SECONDS))


async def _renew_lease_once(redis: Redis, token: str) -> bool:
    return bool(await redis.eval(_RENEW_SCRIPT, 1, LEASE_KEY, token, LEASE_SECONDS))


async def _release_lease(redis: Redis, token: str) -> None:
    await redis.eval(_RELEASE_SCRIPT, 1, LEASE_KEY, token)


async def _renew_lease(redis: Redis, token: str, local_stop: asyncio.Event) -> bool:
    log = structlog.get_logger()
    while not local_stop.is_set():
        try:
            await asyncio.wait_for(local_stop.wait(), timeout=RENEW_SECONDS)
            return True
        except TimeoutError:
            pass
        try:
            renewed = await _renew_lease_once(redis, token)
        except RedisError as error:
            # Lease ownership is uncertain after a Redis transport/server error.
            # Stop local work and let the outer loop reacquire before resuming.
            log.warning("background_leader_renew_failed", error=str(error))
            return False
        if not renewed:
            return False
    return True


async def _recover_group_disconnects_periodically(
    bot: Bot, local_stop: asyncio.Event
) -> None:
    """Retry durable group disconnect intents only while this replica is leader."""
    from app.services.group_disconnects import recover_group_disconnects

    log = structlog.get_logger()
    sem = _get_recovery_semaphore()
    while not local_stop.is_set():
        try:
            async with sem:
                await recover_group_disconnects(bot)
        except Exception:
            log.exception("group_disconnect_recovery_iteration_failed")
        try:
            await asyncio.wait_for(
                local_stop.wait(), timeout=GROUP_DISCONNECT_RECOVERY_SECONDS
            )
        except TimeoutError:
            continue


async def _recover_chat_permissions_periodically(
    bot: Bot, local_stop: asyncio.Event
) -> None:
    """Reconcile durable chat-permission intents only while this replica is leader."""
    from app.services.chat_permission_transitions import (
        recover_chat_permission_transitions,
    )

    log = structlog.get_logger()
    sem = _get_recovery_semaphore()
    while not local_stop.is_set():
        try:
            async with sem:
                await recover_chat_permission_transitions(bot)
        except Exception:
            log.exception("chat_permission_recovery_iteration_failed")
        try:
            await asyncio.wait_for(
                local_stop.wait(), timeout=CHAT_PERMISSION_RECOVERY_SECONDS
            )
        except TimeoutError:
            continue


async def _recover_rank_provisioning_periodically(
    bot: Bot, local_stop: asyncio.Event
) -> None:
    """Reconcile durable rank-provisioning intents only while this replica is leader."""
    from app.services.rank_provisioning import recover_rank_provisioning_intents

    log = structlog.get_logger()
    sem = _get_recovery_semaphore()
    while not local_stop.is_set():
        try:
            async with sem:
                await recover_rank_provisioning_intents(bot)
        except Exception:
            log.exception("rank_provisioning_recovery_iteration_failed")
        try:
            await asyncio.wait_for(
                local_stop.wait(), timeout=RANK_PROVISIONING_RECOVERY_SECONDS
            )
        except TimeoutError:
            continue


async def _recover_join_reviews_periodically(
    bot: Bot, local_stop: asyncio.Event
) -> None:
    """Reconcile stale review claims only while this replica owns the leader lease."""
    from app.services.join_request_transitions import recover_join_request_reviews

    log = structlog.get_logger()
    sem = _get_recovery_semaphore()
    while not local_stop.is_set():
        try:
            async with sem:
                await recover_join_request_reviews(bot)
        except Exception:
            log.exception("join_review_recovery_iteration_failed")
        try:
            await asyncio.wait_for(
                local_stop.wait(), timeout=JOIN_REVIEW_RECOVERY_SECONDS
            )
        except TimeoutError:
            continue


async def _recover_invite_operations_periodically(local_stop: asyncio.Event) -> None:
    """Quarantine stale invite operations only while this replica owns the leader lease."""
    from app.services.join_request_transitions import recover_invite_operations

    log = structlog.get_logger()
    sem = _get_recovery_semaphore()
    while not local_stop.is_set():
        try:
            async with sem:
                await recover_invite_operations()
        except Exception:
            log.exception("invite_operation_recovery_iteration_failed")
        try:
            await asyncio.wait_for(
                local_stop.wait(), timeout=INVITE_OPERATION_RECOVERY_SECONDS
            )
        except TimeoutError:
            continue


async def _recover_duplicate_refunds_periodically(
    bot: Bot, local_stop: asyncio.Event
) -> None:
    """Retry durable duplicate Stars refunds only while this replica is leader."""
    from app.services.global_post_refunds import recover_pending_duplicate_refunds

    log = structlog.get_logger()
    sem = _get_recovery_semaphore()
    while not local_stop.is_set():
        try:
            async with sem:
                await recover_pending_duplicate_refunds(bot)
        except Exception:
            log.exception("global_post_duplicate_refund_recovery_iteration_failed")
        try:
            await asyncio.wait_for(
                local_stop.wait(), timeout=DUPLICATE_REFUND_RECOVERY_SECONDS
            )
        except TimeoutError:
            continue


async def _recover_subscription_refunds_periodically(
    bot: Bot, local_stop: asyncio.Event
) -> None:
    """Retry durable subscription refunds only while this replica is leader."""
    from app.services.subscription_duplicate_refunds import (
        recover_pending_subscription_duplicate_refunds,
    )
    from app.services.subscription_refunds import recover_pending_subscription_refunds

    log = structlog.get_logger()
    sem = _get_recovery_semaphore()
    while not local_stop.is_set():
        try:
            async with sem:
                await recover_pending_subscription_refunds(bot)
                await recover_pending_subscription_duplicate_refunds(bot)
        except Exception:
            log.exception("subscription_refund_recovery_iteration_failed")
        try:
            await asyncio.wait_for(
                local_stop.wait(), timeout=SUBSCRIPTION_REFUND_RECOVERY_SECONDS
            )
        except TimeoutError:
            continue


async def _recover_moderation_operations_periodically(
    bot: Bot, local_stop: asyncio.Event
) -> None:
    """Reconcile durable moderation side-effect intents only while leader."""
    from app.services.moderation_operations import recover_moderation_operation_intents

    log = structlog.get_logger()
    sem = _get_recovery_semaphore()
    while not local_stop.is_set():
        try:
            async with sem:
                await recover_moderation_operation_intents(bot)
        except Exception:
            log.exception("moderation_operation_recovery_iteration_failed")
        try:
            await asyncio.wait_for(
                local_stop.wait(), timeout=MODERATION_OPERATION_RECOVERY_SECONDS
            )
        except TimeoutError:
            continue


async def _cleanup_user_messages_periodically(local_stop: asyncio.Event) -> None:
    """Delete old user messages (>49 hours) to keep the table bounded."""
    log = structlog.get_logger()
    while not local_stop.is_set():
        try:
            async with SessionFactory() as session:
                cutoff = datetime.now(UTC) - timedelta(hours=49)
                result = await session.execute(
                    delete(UserMessage).where(UserMessage.created_at < cutoff)
                )
                await session.commit()
                if result.rowcount:
                    log.info("user_messages_cleanup", deleted=result.rowcount)
        except Exception as exc:
            log.warning("user_messages_cleanup_failed", error=str(exc))
        try:
            await asyncio.wait_for(
                local_stop.wait(), timeout=USER_MESSAGES_CLEANUP_SECONDS
            )
        except TimeoutError:
            continue


async def _notify_plan_expiry_periodically(
    bot: Bot, redis: Redis, local_stop: asyncio.Event
) -> None:
    """Раз в час проверять группы с remaining_days in {3,2,1} и слать ЛС владельцу."""
    log = structlog.get_logger()
    while not local_stop.is_set():
        try:
            async with SessionFactory() as session:
                groups = (
                    await session.scalars(
                        select(Group).where(Group.is_active.is_(True))
                    )
                ).all()
            for group in groups:
                state = subscription_state(group)
                if state not in {"active", "trial"}:
                    continue
                days = remaining_days(group)
                if days not in {3, 2, 1}:
                    continue
                if not group.owner_telegram_id:
                    continue
                key = f"mimoru:plan_notice:{group.id}:{days}"
                if not await redis.set(key, "1", nx=True, ex=20 * 3600):
                    continue
                try:
                    await bot.send_message(
                        group.owner_telegram_id,
                        f"⏰ Напоминание о подписке\n\n"
                        f"Группа «{group.title}» — тариф "
                        f"{effective_plan(group).upper()} истекает через {days} дн.\n\n"
                        f"Продлить: /start в ЛС бота",
                    )
                except (TelegramBadRequest, TelegramForbiddenError) as exc:
                    log.warning(
                        "plan_notice_failed",
                        group_id=group.id,
                        owner_id=group.owner_telegram_id,
                        error=str(exc),
                    )
        except Exception as exc:
            log.warning("plan_notice_loop_failed", error=str(exc))
        try:
            await asyncio.wait_for(local_stop.wait(), timeout=PLAN_NOTICE_SECONDS)
        except TimeoutError:
            continue


async def _run_leader_worker(bot: Bot, redis: Redis, local_stop: asyncio.Event) -> None:
    """Recover durable external-side-effect intents before normal scheduled work."""
    from app.services.group_disconnects import recover_group_disconnects
    from app.tasks_scheduler import background_loop

    await recover_group_disconnects(bot)
    disconnect_recovery = asyncio.create_task(
        _recover_group_disconnects_periodically(bot, local_stop),
        name="group-disconnect-recovery",
    )
    chat_permission_recovery = asyncio.create_task(
        _recover_chat_permissions_periodically(bot, local_stop),
        name="chat-permission-recovery",
    )
    rank_provisioning_recovery = asyncio.create_task(
        _recover_rank_provisioning_periodically(bot, local_stop),
        name="rank-provisioning-recovery",
    )
    review_recovery = asyncio.create_task(
        _recover_join_reviews_periodically(bot, local_stop),
        name="join-review-recovery",
    )
    invite_recovery = asyncio.create_task(
        _recover_invite_operations_periodically(local_stop),
        name="invite-operation-recovery",
    )
    refund_recovery = asyncio.create_task(
        _recover_duplicate_refunds_periodically(bot, local_stop),
        name="global-post-duplicate-refund-recovery",
    )
    subscription_refund_recovery = asyncio.create_task(
        _recover_subscription_refunds_periodically(bot, local_stop),
        name="subscription-refund-recovery",
    )
    moderation_recovery = asyncio.create_task(
        _recover_moderation_operations_periodically(bot, local_stop),
        name="moderation-operation-recovery",
    )
    user_messages_cleanup = asyncio.create_task(
        _cleanup_user_messages_periodically(local_stop),
        name="user-messages-cleanup",
    )
    plan_notice = asyncio.create_task(
        _notify_plan_expiry_periodically(bot, redis, local_stop),
        name="plan-expiry-notice",
    )
    try:
        await background_loop(bot, redis, local_stop)
    finally:
        local_stop.set()
        await stop_task(disconnect_recovery, timeout=2.0)
        await stop_task(chat_permission_recovery, timeout=2.0)
        await stop_task(rank_provisioning_recovery, timeout=2.0)
        await stop_task(review_recovery, timeout=2.0)
        await stop_task(invite_recovery, timeout=2.0)
        await stop_task(refund_recovery, timeout=2.0)
        await stop_task(subscription_refund_recovery, timeout=2.0)
        await stop_task(moderation_recovery, timeout=2.0)
        await stop_task(user_messages_cleanup, timeout=2.0)
        await stop_task(plan_notice, timeout=2.0)


async def leader_background_loop(
    bot: Bot, redis: Redis, stop_event: asyncio.Event
) -> None:
    """Run the core scheduler on at most one application replica at a time.

    The lease is renewed independently from the worker loop. If ownership is lost or
    Redis makes ownership uncertain, local work is stopped before this process tries
    to become leader again. A crashed process releases ownership when the TTL expires.
    """
    log = structlog.get_logger()
    while not stop_event.is_set():
        token = uuid4().hex
        try:
            acquired = await _acquire_lease(redis, token)
        except RedisError as error:
            log.warning("background_leader_acquire_failed", error=str(error))
            acquired = False
        if not acquired:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=RETRY_SECONDS)
            except TimeoutError:
                continue
            break

        local_stop = asyncio.Event()
        worker = asyncio.create_task(
            _run_leader_worker(bot, redis, local_stop), name="background-worker"
        )
        renewer = asyncio.create_task(
            _renew_lease(redis, token, local_stop), name="background-lease-renewer"
        )
        global_stop = asyncio.create_task(
            stop_event.wait(), name="background-global-stop"
        )
        try:
            done, _ = await asyncio.wait(
                {worker, renewer, global_stop},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if global_stop in done:
                local_stop.set()
            elif renewer in done:
                renewed = renewer.result()
                if not renewed:
                    log.warning("background_leader_lease_lost")
                local_stop.set()
            elif worker in done:
                try:
                    worker.result()
                except Exception:
                    log.exception("background_worker_stopped_unexpectedly")
                local_stop.set()
        finally:
            local_stop.set()
            await stop_task(worker, timeout=10.0)
            await stop_task(renewer, timeout=2.0)
            if not global_stop.done():
                global_stop.cancel()
            try:
                await global_stop
            except asyncio.CancelledError:
                pass
            try:
                await _release_lease(redis, token)
            except Exception:
                log.exception("background_leader_release_failed")

        if stop_event.is_set():
            break
