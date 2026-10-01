from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, ModerationLog
from app.db.rank_models import RankAssignment
from app.services.ranks import RANK_LABELS, add_rank_event

log = structlog.get_logger(__name__)

# SYSTEM_DISCONNECT_ACTOR_ID in app.services.group_disconnects uses the same zero
# actor for Telegram-observed, non-human driven state changes.
SYSTEM_ACTOR_ID = 0

LEAVE_EVENT = "remove_by_leave"
RETURN_EVENT = "return_without_rank"


async def active_assignment(
    session: AsyncSession,
    group: Group,
    user_id: int,
    *,
    for_update: bool = True,
) -> RankAssignment | None:
    """Any active Mimoru rank of a person in this group (all rank levels)."""
    query = select(RankAssignment).where(
        RankAssignment.group_id == group.id,
        RankAssignment.user_telegram_id == user_id,
        RankAssignment.active.is_(True),
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def deactivate_rank_on_leave(
    session: AsyncSession,
    group: Group,
    user_id: int,
    *,
    reason: str = "Выход из группы",
) -> str | None:
    """Remove the bot-side rank of a person who left the Telegram group.

    Telegram itself drops administrator rights when a member leaves, so only
    Mimoru's own RankAssignment has to be retired. The assignment row is kept
    (history and access-mode audits stay intact) but flagged inactive, so every
    permission resolver stops treating the person as staff.

    Returns the removed rank code, or None when there was nothing to remove.
    """
    assignment = await active_assignment(session, group, user_id)
    if assignment is None:
        return None

    old_rank = assignment.rank_code
    assignment.active = False
    assignment.telegram_admin_managed = False
    assignment.restore_after_mute = False

    add_rank_event(
        session,
        group_id=group.id,
        actor_id=SYSTEM_ACTOR_ID,
        target_id=user_id,
        action=LEAVE_EVENT,
        old_rank=old_rank,
        new_rank=None,
        details={"reason": reason},
    )
    session.add(
        ModerationLog(
            group_id=group.id,
            actor_telegram_id=SYSTEM_ACTOR_ID,
            target_telegram_id=user_id,
            action="rank_removed_by_leave",
            reason=reason,
            metadata_json={"rank_code": old_rank},
        )
    )
    log.info(
        "rank_removed_on_leave",
        group_id=group.id,
        user_id=user_id,
        rank_code=old_rank,
    )
    return old_rank


async def deactivate_all_ranks(
    session: AsyncSession,
    group: Group,
    *,
    reason: str = "Группа отключена от Mimoru",
) -> int:
    """Retire every active rank when a group leaves service.

    Otherwise the previous team silently comes back with full panel access the
    moment the group is reconnected, even though nobody re-appointed them.
    """
    rows = list((await session.scalars(
        select(RankAssignment)
        .where(
            RankAssignment.group_id == group.id,
            RankAssignment.active.is_(True),
        )
        .with_for_update()
    )).all())
    for assignment in rows:
        old_rank = assignment.rank_code
        assignment.active = False
        assignment.telegram_admin_managed = False
        assignment.restore_after_mute = False
        add_rank_event(
            session,
            group_id=group.id,
            actor_id=SYSTEM_ACTOR_ID,
            target_id=assignment.user_telegram_id,
            action=LEAVE_EVENT,
            old_rank=old_rank,
            new_rank=None,
            details={"reason": reason},
        )
    if rows:
        session.add(
            ModerationLog(
                group_id=group.id,
                actor_telegram_id=SYSTEM_ACTOR_ID,
                target_telegram_id=SYSTEM_ACTOR_ID,
                action="rank_removed_by_leave",
                reason=reason,
                metadata_json={"removed": len(rows)},
            )
        )
    return len(rows)


async def note_member_return(
    session: AsyncSession,
    group: Group,
    user_id: int,
    *,
    previous_rank: str | None,
) -> None:
    """Record that a former staff member came back without their rank."""
    if previous_rank is None:
        return
    add_rank_event(
        session,
        group_id=group.id,
        actor_id=SYSTEM_ACTOR_ID,
        target_id=user_id,
        action=RETURN_EVENT,
        old_rank=None,
        new_rank=None,
        details={"previous_rank_code": previous_rank},
    )


def rank_label(rank_code: str | None) -> str:
    if not rank_code:
        return "без ранга"
    return RANK_LABELS.get(rank_code, rank_code)
