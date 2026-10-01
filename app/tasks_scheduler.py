from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog
from aiogram import Bot
from redis.asyncio import Redis

from app.games.recovery import process_game_timeouts, recover_active_games
from app.services.audit import deliver_pending_logs
from app.services.message_ttl import process_message_deletions
from app.services.punishment_expiry import expire_punishments
from app.services.required_reconciles import process_required_subscription_reconciles
from app.tasks_ad_cleanup import complete_ad_orders
from app.tasks_captcha import expire_captcha_sessions
from app.tasks_deleted_cleanup import run_group_automation
from app.tasks_delivery import (
    recover_interrupted_scheduled_messages,
    send_daily_reports,
    send_scheduled_messages,
    send_subscription_notices,
)
from app.tasks_permission_modes import apply_night_modes, expire_lockdowns
from app.tasks_warning_expiry import expire_warnings


FAST_LOOP_SECONDS = 5.0
PERMISSION_TASK_SECONDS = 30.0
AD_CLEANUP_SECONDS = 30.0
WARNING_TASK_SECONDS = 60.0
REPORT_TASK_SECONDS = 60.0
SUBSCRIPTION_TASK_SECONDS = 60.0
GROUP_AUTOMATION_SECONDS = 300.0
REQUIRED_RECONCILE_SECONDS = 30.0


async def _run_job(name: str, job: Callable[[], Awaitable[None]]) -> bool:
    """Run one scheduler job without allowing it to block sibling jobs on failure."""
    try:
        await job()
    except Exception:
        structlog.get_logger().exception("background_job_failed", job=name)
        return False
    return True


async def background_loop(bot: Bot, redis: Redis, stop_event: asyncio.Event) -> None:
    """Run background work at a cadence matching each job's actual urgency.

    Previously every task ran on the same five-second loop. That forced daily,
    subscription and weekly/monthly maintenance jobs to rescan the database up
    to twelve times per minute. Urgent expiry/delivery work remains on the fast
    loop while heavier maintenance runs much less often.
    """
    await recover_interrupted_scheduled_messages()
    await _run_job("recover_active_games", lambda: recover_active_games(bot))

    last_run = {
        "permissions": 0.0,
        "ad_cleanup": 0.0,
        "warnings": 0.0,
        "reports": 0.0,
        "subscriptions": 0.0,
        "group_automation": 0.0,
        "required_reconcile": 0.0,
    }

    while not stop_event.is_set():
        now = time.monotonic()

        # Fast, user-visible work: keep the existing five-second response bound.
        await _run_job("expire_punishments", lambda: expire_punishments(bot, redis))
        await _run_job("expire_captcha_sessions", lambda: expire_captcha_sessions(bot, redis))
        await _run_job("send_scheduled_messages", lambda: send_scheduled_messages(bot))
        await _run_job("deliver_pending_logs", lambda: deliver_pending_logs(bot))
        await _run_job("process_message_deletions", lambda: process_message_deletions(bot, redis))
        await _run_job("process_game_timeouts", lambda: process_game_timeouts(bot))

        if now - last_run["permissions"] >= PERMISSION_TASK_SECONDS:
            ok_lockdowns = await _run_job("expire_lockdowns", lambda: expire_lockdowns(bot))
            ok_night = await _run_job("apply_night_modes", lambda: apply_night_modes(bot))
            if ok_lockdowns and ok_night:
                last_run["permissions"] = now

        if now - last_run["ad_cleanup"] >= AD_CLEANUP_SECONDS:
            if await _run_job("complete_ad_orders", lambda: complete_ad_orders(bot)):
                last_run["ad_cleanup"] = now

        if now - last_run["warnings"] >= WARNING_TASK_SECONDS:
            if await _run_job("expire_warnings", expire_warnings):
                last_run["warnings"] = now

        if now - last_run["reports"] >= REPORT_TASK_SECONDS:
            if await _run_job("send_daily_reports", lambda: send_daily_reports(bot)):
                last_run["reports"] = now

        if now - last_run["subscriptions"] >= SUBSCRIPTION_TASK_SECONDS:
            if await _run_job("send_subscription_notices", lambda: send_subscription_notices(bot)):
                last_run["subscriptions"] = now

        if now - last_run["group_automation"] >= GROUP_AUTOMATION_SECONDS:
            if await _run_job("run_group_automation", lambda: run_group_automation(bot)):
                last_run["group_automation"] = now

        if now - last_run["required_reconcile"] >= REQUIRED_RECONCILE_SECONDS:
            if await _run_job(
                "process_required_subscription_reconciles",
                lambda: process_required_subscription_reconciles(bot, redis),
            ):
                last_run["required_reconcile"] = now

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=FAST_LOOP_SECONDS)
        except TimeoutError:
            continue
