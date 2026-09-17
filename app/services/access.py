from __future__ import annotations

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Group
from app.db.rank_models import RankAssignment

# Compatibility defaults for legacy handlers that still import this symbol.
# Runtime authorization is now based on app.services.ranks and rank_assignments.
DEFAULT_ROLE_PERMISSIONS = {
    "senior": {
        "ban": True,
        "unban": True,
        "mute": True,
        "unmute": True,
        "kick": False,
        "warn": True,
        "unwarn": True,
        "warnings": True,
        "info": True,
        "history": True,
        "delete": True,
    },
    "moderator": {
        "ban": False,
        "unban": False,
        "mute": True,
        "unmute": True,
        "kick": False,
        "warn": True,
        "unwarn": True,
        "warnings": True,
        "info": True,
        "history": True,
        "delete": True,
    },
    "helper": {
        "ban": False,
        "unban": False,
        "mute": False,
        "unmute": False,
        "kick": False,
        "warn": True,
        "unwarn": True,
        "warnings": True,
        "info": True,
        "history": True,
        "delete": False,
    },
}


def is_service_owner(user_id: int) -> bool:
    return user_id in get_settings().service_owner_ids


async def is_telegram_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


async def can_moderate(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    user_id: int,
    action: str,
) -> bool:
    # Kick was retired from Mimoru's moderation surface. Keep this deny before
    # owner/service-owner shortcuts so stale callbacks or cached payloads cannot
    # revive the action.
    if action == "kick":
        return False
    from app.services.rank_access import can_use_rank_permission

    return await can_use_rank_permission(bot, session, group, user_id, action)


async def can_manage_group(
    bot: Bot, group: Group, user_id: int, session: AsyncSession | None = None
) -> bool:
    if is_service_owner(user_id):
        return True
    if group.owner_telegram_id == user_id:
        return await is_telegram_admin(bot, group.telegram_chat_id, user_id)
    if session is None:
        return False
    from app.services.rank_access import get_actor_rank_with_access

    actor = await get_actor_rank_with_access(bot, session, group, user_id)
    return bool(actor is not None and actor.code == "deputy_owner")


ADMIN_RANKS_FOR_PANEL: frozenset[str] = frozenset(
    {
        "deputy_owner",
        "chief_admin",
        "chat_admin",
    }
)


async def _get_assignment(
    session: AsyncSession,
    group_id: int,
    user_id: int,
) -> RankAssignment | None:
    """Получить активное назначение ранга для пользователя в группе."""
    return await session.scalar(
        select(RankAssignment)
        .where(
            RankAssignment.group_id == group_id,
            RankAssignment.user_telegram_id == user_id,
            RankAssignment.active.is_(True),
            RankAssignment.rank_code.in_(ADMIN_RANKS_FOR_PANEL),
        )
        .limit(1)
    )


async def is_group_admin(
    session: AsyncSession,
    group: Group,
    telegram_id: int,
) -> bool:
    """True, если telegram_id — owner группы ИЛИ админ с активным рангом."""
    if group.owner_telegram_id == telegram_id:
        return True
    found = await session.scalar(
        select(RankAssignment.id)
        .where(
            RankAssignment.group_id == group.id,
            RankAssignment.user_telegram_id == telegram_id,
            RankAssignment.active.is_(True),
            RankAssignment.rank_code.in_(ADMIN_RANKS_FOR_PANEL),
        )
        .limit(1)
    )
    return found is not None


async def is_group_owner(group: Group, telegram_id: int) -> bool:
    """True, если telegram_id — прямой owner группы."""
    return group.owner_telegram_id == telegram_id


async def owned_group(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> Group | None:
    """Group доступная пользователю для управления (owner / service_owner).

    Владелец и service_owner — полный доступ.
    Остальные — None.
    """
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not is_service_owner(user_id):
        query = query.where(Group.owner_telegram_id == user_id)
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def accessible_group(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> Group | None:
    """Group доступная пользователю для просмотра.

    Владелец и service_owner — полный доступ.
    DEPUTY_OWNER, CHIEF_ADMIN, CHAT_ADMIN — read-only (просмотр).
    Остальные — None.
    """
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if for_update:
        query = query.with_for_update()
    group = await session.scalar(query)
    if group is None:
        return None
    if is_service_owner(user_id):
        return group
    if group.owner_telegram_id == user_id:
        return group
    assignment = await _get_assignment(session, group.id, user_id)
    if (
        assignment is not None
        and assignment.active
        and assignment.rank_code in ADMIN_RANKS_FOR_PANEL
    ):
        return group
    return None
