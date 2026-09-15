from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select

from app.db.deleted_cleanup_retry_models import DeletedCleanupRetry
from app.db.models import AutomationLog, Group, GroupMember
from app.db.session import SessionFactory
from app.services.deleted_accounts import (
    CleanupResult,
    ScanResult,
    _telegram_call,
    is_deleted_profile,
    upsert_user,
)

BASE_RETRY_DELAY = timedelta(hours=1)
MAX_RETRY_DELAY = timedelta(hours=24)


def _retry_delay(attempts: int) -> timedelta:
    hours = min(24, 2 ** max(0, attempts - 1))
    return timedelta(hours=hours)


async def _schedule_retry(
    session,
    group_id: int,
    now: datetime,
    failed: int,
    existing: DeletedCleanupRetry | None,
) -> DeletedCleanupRetry:
    attempts = (existing.attempts + 1) if existing is not None else 1
    retry_at = now + min(_retry_delay(attempts), MAX_RETRY_DELAY)
    if existing is None:
        existing = DeletedCleanupRetry(
            group_id=group_id,
            retry_at=retry_at,
            attempts=attempts,
            last_error=f"{failed} удалений не выполнено",
        )
        session.add(existing)
    else:
        existing.retry_at = retry_at
        existing.attempts = attempts
        existing.last_error = f"{failed} удалений не выполнено"
    return existing


async def _scan_known_members_per_item(bot: Bot, group: Group) -> ScanResult:
    """Scan members with per-member DB sessions to avoid pool exhaustion.

    Each Telegram API call gets its own short-lived session. DB state is
    updated immediately after each check, so the connection is never held
    during Telegram round-trips.
    """
    log = structlog.get_logger(__name__)
    chat_id = group.telegram_chat_id
    group_id = group.id

    async with SessionFactory() as session:
        member_rows = list(
            (
                await session.scalars(
                    select(GroupMember)
                    .where(
                        GroupMember.group_id == group_id,
                        GroupMember.is_present.is_(True),
                    )
                    .with_for_update()
                    .order_by(GroupMember.id)
                )
            ).all()
        )

    checked = deleted = present = inaccessible = 0
    for row in member_rows:
        try:
            member = await _telegram_call(
                lambda uid=row.user_telegram_id: bot.get_chat_member(chat_id, uid)
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning(
                "deleted_cleanup_failed",
                group_id=group_id,
                user_id=row.user_telegram_id,
                error=str(exc),
            )
            inaccessible += 1
            continue

        checked += 1
        is_deleted = False
        if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
            is_present = False
        else:
            is_present = True
            user = member.user
            is_deleted = is_deleted_profile(user)
            if is_deleted:
                deleted += 1
            else:
                present += 1

        async with SessionFactory() as session:
            row = await session.scalar(
                select(GroupMember).where(GroupMember.id == row.id)
            )
            if row is not None:
                row.is_present = is_present
                row.is_deleted_account = is_deleted
                row.last_checked_at = datetime.now(UTC)
                if is_present and not is_deleted:
                    await upsert_user(session, member.user)
            await session.commit()

    return ScanResult(
        checked=checked,
        deleted=deleted,
        present=present,
        inaccessible=inaccessible,
    )


async def _remove_deleted_accounts_per_item(bot: Bot, group: Group) -> CleanupResult:
    """Remove deleted accounts with per-member DB sessions.

    Each ban call gets its own short-lived session. The connection is never
    held during Telegram API round-trips.
    """
    log = structlog.get_logger(__name__)
    chat_id = group.telegram_chat_id
    group_id = group.id

    async with SessionFactory() as session:
        rows = list(
            (
                await session.scalars(
                    select(GroupMember)
                    .where(
                        GroupMember.group_id == group_id,
                        GroupMember.is_present.is_(True),
                        GroupMember.is_deleted_account.is_(True),
                    )
                    .order_by(GroupMember.id)
                )
            ).all()
        )

    removed = failed = 0
    for row in rows:
        try:
            await _telegram_call(
                lambda uid=row.user_telegram_id: bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=uid,
                    revoke_messages=False,
                )
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning(
                "deleted_cleanup_failed",
                group_id=group_id,
                user_id=row.user_telegram_id,
                error=str(exc),
            )
            failed += 1
            continue

        async with SessionFactory() as session:
            member = await session.scalar(
                select(GroupMember).where(GroupMember.id == row.id)
            )
            if member is not None:
                member.is_present = False
                member.last_checked_at = datetime.now(UTC)
            await session.commit()
        removed += 1

    return CleanupResult(found=len(rows), removed=removed, failed=failed)


async def run_group_automation(bot: Bot) -> None:
    """Run current deleted-account cleanup under the owner mutation lock boundary.

    Session lifecycle: the Group FOR UPDATE lock is held only during validation
    and the final last_run_at update. Telegram API calls (scan + ban) happen
    between two separate short transactions so the pool is never held during
    potentially hundreds of sequential Telegram calls.
    """
    log = structlog.get_logger()
    async with SessionFactory() as session:
        candidate_ids = list(
            (
                await session.scalars(
                    select(Group.id).where(Group.is_active.is_(True)).order_by(Group.id)
                )
            ).all()
        )

    for group_id in candidate_ids:
        now = datetime.now(UTC)

        # --- Phase 1: Validate under Group FOR UPDATE lock, then release ---
        should_run = False
        telegram_chat_id = None
        async with SessionFactory() as session:
            group = await session.scalar(
                select(Group)
                .where(Group.id == group_id, Group.is_active.is_(True))
                .with_for_update()
            )
            if group is None:
                continue

            settings = group.settings
            if not settings.automation_enabled:
                continue
            schedule = settings.deleted_cleanup_schedule
            if schedule not in {"weekly", "monthly"}:
                continue

            retry = await session.get(DeletedCleanupRetry, group.id)
            if retry is not None:
                if retry.retry_at > now:
                    continue
            else:
                interval = timedelta(days=7 if schedule == "weekly" else 30)
                last_run = settings.deleted_cleanup_last_run_at
                if last_run is not None and now - last_run < interval:
                    continue

            telegram_chat_id = group.telegram_chat_id
            should_run = True

        if not should_run:
            continue

        # --- Phase 2: Telegram API calls with NO connection held ---
        scan = None
        cleanup = None
        error_info = None
        try:
            scan_group = Group(id=group_id, telegram_chat_id=telegram_chat_id)
            scan = await _scan_known_members_per_item(bot, scan_group)
            cleanup = await _remove_deleted_accounts_per_item(bot, scan_group)
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            error_info = ("telegram_error", str(error)[:500])
            log.warning(
                "automation_deleted_cleanup_failed", group_id=group_id, error=str(error)
            )
        except Exception as error:
            error_info = ("error", str(error)[:500])
            log.exception("automation_deleted_cleanup_unexpected", group_id=group_id)

        # --- Phase 3: Persist results under Group FOR UPDATE lock ---
        async with SessionFactory() as session:
            group = await session.scalar(
                select(Group)
                .where(Group.id == group_id, Group.is_active.is_(True))
                .with_for_update()
            )
            if group is None:
                continue

            retry = await session.get(DeletedCleanupRetry, group.id)

            if error_info is not None:
                status, error_text = error_info
                retry = await _schedule_retry(session, group.id, now, 1, retry)
                session.add(
                    AutomationLog(
                        group_id=group.id,
                        rule_code="deleted_cleanup",
                        status=status,
                        details={
                            "error": error_text,
                            "retry_at": retry.retry_at.isoformat(),
                            "attempts": retry.attempts,
                        },
                    )
                )
            else:
                details = {
                    "checked": scan.checked,
                    "found": scan.deleted,
                    "removed": cleanup.removed,
                    "failed": cleanup.failed,
                }
                if cleanup.failed > 0:
                    retry = await _schedule_retry(
                        session, group.id, now, cleanup.failed, retry
                    )
                    details["retry_at"] = retry.retry_at.isoformat()
                    details["attempts"] = retry.attempts
                    session.add(
                        AutomationLog(
                            group_id=group.id,
                            rule_code="deleted_cleanup",
                            status="partial",
                            details=details,
                        )
                    )
                else:
                    group.settings.deleted_cleanup_last_run_at = now
                    if retry is not None:
                        await session.delete(retry)
                    session.add(
                        AutomationLog(
                            group_id=group.id,
                            rule_code="deleted_cleanup",
                            status="ok",
                            details=details,
                        )
                    )

            await session.commit()
