from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, ModerationLog, Punishment, Warning
from app.db.moderation_operation_models import ModerationOperationIntent
from app.db.rank_models import RankAssignment
from app.db.session import SessionFactory
from app.services.moderation import (
    deactivate_punishments,
    log_action,
    purge_user_messages,
)
from app.services.ranks import ADMIN_RANKS, add_rank_event, restore_telegram_rank


log = structlog.get_logger()
MUTE_EXPIRY_TOLERANCE_SECONDS = 10


async def create_moderation_intent(
    session: AsyncSession,
    *,
    group_id: int,
    target_id: int,
    actor_id: int,
    action: str,
    source: str,
    payload: dict,
) -> int | None:
    """Persist one external moderation operation before its Telegram side effect."""
    intent = ModerationOperationIntent(
        group_id=group_id,
        target_telegram_id=target_id,
        actor_telegram_id=actor_id,
        action=action,
        source=source,
        payload=payload,
    )
    session.add(intent)
    try:
        await session.commit()
        return intent.id
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(ModerationOperationIntent.id).where(
                ModerationOperationIntent.group_id == group_id,
                ModerationOperationIntent.target_telegram_id == target_id,
            )
        )
        if existing is not None:
            return None
        raise


async def drop_moderation_intent(session: AsyncSession, intent_id: int) -> None:
    intent = await session.get(ModerationOperationIntent, intent_id)
    if intent is not None:
        await session.delete(intent)
        await session.commit()


async def _matching_log_exists(
    session: AsyncSession,
    intent: ModerationOperationIntent,
) -> bool:
    actions = {
        "ban": ("ban",),
        "mute": ("mute",),
        "unban": ("unban",),
        "unmute": ("unmute",),
        "warn": ("auto_mute",),
    }.get(intent.action, (intent.action,))
    existing = await session.scalar(
        select(ModerationLog.id)
        .where(
            ModerationLog.group_id == intent.group_id,
            ModerationLog.target_telegram_id == intent.target_telegram_id,
            ModerationLog.actor_telegram_id == intent.actor_telegram_id,
            ModerationLog.action.in_(actions),
            ModerationLog.created_at >= intent.created_at,
        )
        .limit(1)
    )
    return existing is not None


def _member_is_muted(member: object) -> bool:
    if getattr(member, "status", None) != ChatMemberStatus.RESTRICTED:
        return False
    return getattr(member, "can_send_messages", True) is False


def _member_until(member: object) -> datetime | None:
    value = getattr(member, "until_date", None)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _payload_time(payload: dict, key: str) -> datetime | None:
    raw = payload.get(key)
    if not raw:
        return None
    value = datetime.fromisoformat(str(raw))
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _mute_matches_intent(member: object, payload: dict) -> bool:
    if not _member_is_muted(member):
        return False
    expected = _payload_time(payload, "ends_at")
    if expected is None:
        return not bool(payload.get("had_active_mute"))
    actual = _member_until(member)
    if actual is None:
        # Without an observable expiry, a pre-existing mute is ambiguous and must
        # not be mistaken for proof that this operation reached Telegram.
        return not bool(payload.get("had_active_mute"))
    return abs((actual - expected).total_seconds()) <= MUTE_EXPIRY_TOLERANCE_SECONDS


def _ban_transition_proves_applied(member: object, payload: dict) -> bool:
    if getattr(member, "status", None) != ChatMemberStatus.KICKED:
        return False
    if not bool(payload.get("pre_banned")):
        return True

    pre_until = _payload_time(payload, "pre_until")
    expected = _payload_time(payload, "ends_at")
    current_until = _member_until(member)
    duration = payload.get("duration")

    if duration:
        if expected is None or current_until is None:
            return False
        current_matches = (
            abs((current_until - expected).total_seconds())
            <= MUTE_EXPIRY_TOLERANCE_SECONDS
        )
        pre_matches = (
            pre_until is not None
            and abs((pre_until - expected).total_seconds())
            <= MUTE_EXPIRY_TOLERANCE_SECONDS
        )
        return current_matches and not pre_matches

    # Re-banning an already timed-banned target with no duration makes the ban
    # permanent. The status stays KICKED, so the disappearing until_date is the
    # only observable evidence that this operation reached Telegram.
    return pre_until is not None and current_until is None


def _state_transition_proves_applied(
    intent: ModerationOperationIntent,
    member: object,
    payload: dict,
) -> bool:
    """Require a change from the durable pre-side-effect Telegram snapshot.

    This prevents a crash immediately after intent creation from claiming an older
    ban/mute/release that already existed before the operation was delegated.
    """
    banned = getattr(member, "status", None) == ChatMemberStatus.KICKED
    muted = _member_is_muted(member)
    pre_banned = bool(payload.get("pre_banned"))
    pre_muted = bool(payload.get("pre_muted"))

    if intent.action == "ban":
        return _ban_transition_proves_applied(member, payload)
    if intent.action == "unban":
        return pre_banned and not banned
    if intent.action == "unmute":
        return pre_muted and not muted
    if intent.action in {"mute", "warn"}:
        if intent.action == "warn" and not payload.get("expect_auto_mute"):
            return False
        if not _mute_matches_intent(member, payload):
            return False
        if not pre_muted:
            return True
        pre_until = _payload_time(payload, "pre_until")
        expected = _payload_time(payload, "ends_at")
        if pre_until is None or expected is None:
            return False
        return abs((pre_until - expected).total_seconds()) > MUTE_EXPIRY_TOLERANCE_SECONDS
    return False


async def _restore_orphan_rank(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    intent: ModerationOperationIntent,
    member: object,
) -> bool:
    """Compensate only an observable demotion owned by this Mimoru rank.

    An intent can survive a crash before any Telegram call. If the target is still
    an administrator, no demotion happened and recovery must not rewrite its rights.
    If restoration is required but cannot be completed, retain the intent for retry.
    """
    payload = dict(intent.payload or {})
    if not payload.get("target_managed_admin"):
        return True
    if getattr(member, "status", None) == ChatMemberStatus.ADMINISTRATOR:
        return True
    assignment = await session.scalar(
        select(RankAssignment).where(
            RankAssignment.group_id == group.id,
            RankAssignment.user_telegram_id == intent.target_telegram_id,
            RankAssignment.active.is_(True),
        )
    )
    if assignment is None or assignment.rank_code not in ADMIN_RANKS:
        return True
    restored = await restore_telegram_rank(bot, group, assignment)
    if not restored:
        log.warning(
            "moderation_intent_rank_compensation_failed",
            intent_id=intent.id,
            group_id=group.id,
            target_id=intent.target_telegram_id,
        )
    return restored


async def _finalize_applied_intent(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    intent: ModerationOperationIntent,
) -> None:
    payload = dict(intent.payload or {})
    reason = str(payload.get("reason") or "Восстановлено после сбоя")
    action = intent.action

    if action == "ban":
        ends_at = _payload_time(payload, "ends_at")
        session.add(
            Punishment(
                group_id=group.id,
                user_telegram_id=intent.target_telegram_id,
                moderator_telegram_id=intent.actor_telegram_id,
                kind="ban",
                reason=reason,
                ends_at=ends_at,
            )
        )
        assignment = await session.scalar(
            select(RankAssignment).where(
                RankAssignment.group_id == group.id,
                RankAssignment.user_telegram_id == intent.target_telegram_id,
                RankAssignment.active.is_(True),
            )
        )
        if assignment is not None and assignment.rank_code in ADMIN_RANKS:
            old_rank = assignment.rank_code
            assignment.active = False
            assignment.telegram_admin_managed = False
            assignment.restore_after_mute = False
            add_rank_event(
                session,
                group_id=group.id,
                actor_id=intent.actor_telegram_id,
                target_id=intent.target_telegram_id,
                action="remove_by_ban",
                old_rank=old_rank,
                new_rank=None,
                details={"reason": reason, "moderation_intent_recovered": True},
            )
        log_action(
            session,
            group.id,
            intent.actor_telegram_id,
            intent.target_telegram_id,
            "ban",
            reason,
            {"duration": payload.get("duration"), "moderation_intent_recovered": True},
        )
        # Модератор просил «бан + очистку»: без этого шага восстановление
        # оставило бы сообщения навсегда и не оставило бы следов в логах.
        if payload.get("cleanup"):
            try:
                total, deleted, failed_batches = await purge_user_messages(
                    bot,
                    session,
                    chat_id=group.telegram_chat_id,
                    group_id=group.id,
                    target_id=intent.target_telegram_id,
                )
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                log.warning(
                    "moderation_intent_cleanup_failed",
                    group_id=group.id,
                    user_telegram_id=intent.target_telegram_id,
                    error=str(error),
                )
            else:
                if deleted:
                    log.info(
                        "moderation_intent_cleanup_deleted",
                        group_id=group.id,
                        user_telegram_id=intent.target_telegram_id,
                        deleted=deleted,
                        attempted=total,
                    )
                if failed_batches or deleted < total:
                    log.warning(
                        "moderation_intent_cleanup_incomplete",
                        group_id=group.id,
                        user_telegram_id=intent.target_telegram_id,
                        attempted=total,
                        deleted=deleted,
                        failed_batches=failed_batches,
                    )

    elif action == "mute":
        ends_at = _payload_time(payload, "ends_at") or (
            datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("duration") or 0))
        )
        session.add(
            Punishment(
                group_id=group.id,
                user_telegram_id=intent.target_telegram_id,
                moderator_telegram_id=intent.actor_telegram_id,
                kind="mute",
                reason=reason,
                ends_at=ends_at,
            )
        )
        assignment = await session.scalar(
            select(RankAssignment).where(
                RankAssignment.group_id == group.id,
                RankAssignment.user_telegram_id == intent.target_telegram_id,
                RankAssignment.active.is_(True),
            )
        )
        if assignment is not None and assignment.rank_code in ADMIN_RANKS:
            assignment.restore_after_mute = True
        log_action(
            session,
            group.id,
            intent.actor_telegram_id,
            intent.target_telegram_id,
            "mute",
            reason,
            {"duration": payload.get("duration"), "moderation_intent_recovered": True},
        )

    elif action == "unban":
        await deactivate_punishments(session, group.id, intent.target_telegram_id, "ban")
        log_action(
            session,
            group.id,
            intent.actor_telegram_id,
            intent.target_telegram_id,
            "unban",
            reason,
            {"moderation_intent_recovered": True},
        )

    elif action == "unmute":
        await deactivate_punishments(session, group.id, intent.target_telegram_id, "mute")
        log_action(
            session,
            group.id,
            intent.actor_telegram_id,
            intent.target_telegram_id,
            "unmute",
            reason,
            {"moderation_intent_recovered": True},
        )

    elif action == "warn" and payload.get("expect_auto_mute"):
        session.add(
            Warning(
                group_id=group.id,
                user_telegram_id=intent.target_telegram_id,
                moderator_telegram_id=intent.actor_telegram_id,
                reason=reason,
            )
        )
        duration = int(payload.get("default_mute") or 0)
        ends_at = _payload_time(payload, "ends_at") or (
            datetime.now(timezone.utc) + timedelta(seconds=duration)
        )
        session.add(
            Punishment(
                group_id=group.id,
                user_telegram_id=intent.target_telegram_id,
                moderator_telegram_id=intent.actor_telegram_id,
                kind="mute",
                reason="Лимит предупреждений",
                ends_at=ends_at,
            )
        )
        log_action(
            session,
            group.id,
            intent.actor_telegram_id,
            intent.target_telegram_id,
            "warn",
            reason,
            {"moderation_intent_recovered": True},
        )
        log_action(
            session,
            group.id,
            intent.actor_telegram_id,
            intent.target_telegram_id,
            "auto_mute",
            "Лимит предупреждений",
            {"duration": duration, "moderation_intent_recovered": True},
        )

    await session.delete(intent)
    await session.commit()


async def recover_moderation_operation_intents(bot: Bot, *, limit: int = 100) -> None:
    """Reconcile ambiguous Telegram moderation mutations conservatively.

    Release intents are never replayed: current Telegram state decides whether the
    corresponding DB release may be finalized. Punishment intents require an
    observable transition from the pre-side-effect snapshot. If no punishment side
    effect happened, an owned managed-admin demotion is compensated before retirement.
    """
    async with SessionFactory() as scan_session:
        intent_ids = list((await scan_session.scalars(
            select(ModerationOperationIntent.id)
            .order_by(ModerationOperationIntent.created_at, ModerationOperationIntent.id)
            .limit(limit)
        )).all())

    for intent_id in intent_ids:
        async with SessionFactory() as session:
            group_id = await session.scalar(
                select(ModerationOperationIntent.group_id).where(
                    ModerationOperationIntent.id == intent_id
                )
            )
            if group_id is None:
                continue
            group = await session.scalar(
                select(Group).where(Group.id == group_id).with_for_update()
            )
            intent = await session.scalar(
                select(ModerationOperationIntent)
                .where(ModerationOperationIntent.id == intent_id)
                .with_for_update()
            )
            if intent is None:
                continue
            if group is None or not group.is_active:
                await session.delete(intent)
                await session.commit()
                continue
            if await _matching_log_exists(session, intent):
                await session.delete(intent)
                await session.commit()
                continue

            try:
                member = await bot.get_chat_member(
                    group.telegram_chat_id,
                    intent.target_telegram_id,
                )
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                log.warning(
                    "moderation_intent_state_check_failed",
                    intent_id=intent.id,
                    group_id=group.id,
                    target_id=intent.target_telegram_id,
                    error=str(error),
                )
                continue

            payload = dict(intent.payload or {})
            if _state_transition_proves_applied(intent, member, payload):
                await _finalize_applied_intent(bot, session, group, intent)
                continue

            if intent.action in {"ban", "mute"}:
                compensated = await _restore_orphan_rank(bot, session, group, intent, member)
                if not compensated:
                    continue
            await session.delete(intent)
            await session.commit()
