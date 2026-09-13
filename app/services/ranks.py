from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Group
from app.db.rank_models import GroupRankPolicy, RankAssignment, RankAssignmentEvent

DEPUTY_OWNER = "deputy_owner"
CHIEF_ADMIN = "chief_admin"
CHAT_ADMIN = "chat_admin"
VOICE_ADMIN = "voice_admin"
HELPER = "helper"
MAJOR = "major"
UNTOUCHABLE = "untouchable"

RANK_CODES = (
    DEPUTY_OWNER,
    CHIEF_ADMIN,
    CHAT_ADMIN,
    VOICE_ADMIN,
    HELPER,
    MAJOR,
    UNTOUCHABLE,
)
RANK_LABELS = {
    DEPUTY_OWNER: "Зам. владельца",
    CHIEF_ADMIN: "Глав. админ",
    CHAT_ADMIN: "Администратор чата",
    VOICE_ADMIN: "Администратор войска",
    HELPER: "Помощник",
    MAJOR: "Мажёр",
    UNTOUCHABLE: "Недотрога",
}
RANK_LEVELS = {
    DEPUTY_OWNER: 90,
    CHIEF_ADMIN: 80,
    CHAT_ADMIN: 60,
    VOICE_ADMIN: 50,
    HELPER: 20,
    MAJOR: 10,
    UNTOUCHABLE: 0,
}

PERMISSION_LABELS = {
    "ban": "Бан",
    "unban": "Разбан",
    "mute": "Мут",
    "unmute": "Снять мут",
    "kick": "Кик",
    "warn": "Предупреждение",
    "unwarn": "Снять предупреждение",
    "delete": "Удалять сообщения",
    "restrict_media": "Ограничивать медиа",
    "warnings": "Смотреть предупреждения",
    "info": "Смотреть информацию",
    "history": "Смотреть историю",
    "promote_admins": "Назначать администраторов",
    "edit_admin_permissions": "Менять права администраторов",
    "assign_helper": "Назначать помощника",
    "voice_warn": "Предупреждение в войсе",
    "manage_video_chats": "Управлять видеочатами",
    "report_violation": "Сообщать своему администратору",
}

ROLE_CEILINGS: dict[str, set[str]] = {
    DEPUTY_OWNER: set(PERMISSION_LABELS),
    CHIEF_ADMIN: {
        "ban",
        "unban",
        "mute",
        "unmute",
        "kick",
        "warn",
        "unwarn",
        "delete",
        "restrict_media",
        "warnings",
        "info",
        "history",
        "promote_admins",
        "edit_admin_permissions",
        "assign_helper",
        "voice_warn",
        "manage_video_chats",
    },
    CHAT_ADMIN: {
        "ban",
        "unban",
        "mute",
        "unmute",
        "kick",
        "warn",
        "unwarn",
        "delete",
        "restrict_media",
        "warnings",
        "info",
        "history",
        "assign_helper",
    },
    VOICE_ADMIN: {"voice_warn", "manage_video_chats", "info", "history"},
    HELPER: {"report_violation", "info"},
    MAJOR: set(),
    UNTOUCHABLE: set(),
}

DEFAULT_ROLE_PERMISSIONS: dict[str, dict[str, bool]] = {
    DEPUTY_OWNER: {name: True for name in ROLE_CEILINGS[DEPUTY_OWNER]},
    CHIEF_ADMIN: {name: True for name in ROLE_CEILINGS[CHIEF_ADMIN]},
    CHAT_ADMIN: {name: True for name in ROLE_CEILINGS[CHAT_ADMIN]},
    VOICE_ADMIN: {name: True for name in ROLE_CEILINGS[VOICE_ADMIN]},
    HELPER: {name: True for name in ROLE_CEILINGS[HELPER]},
    MAJOR: {},
    UNTOUCHABLE: {},
}

# Real Telegram administrators managed by Mimoru. Major is deliberately
# Telegram-visible, but receives no moderation permissions. Voice admin,
# Helper and Untouchable remain internal bot roles.
ADMIN_RANKS = {DEPUTY_OWNER, CHIEF_ADMIN, CHAT_ADMIN, MAJOR}


@dataclass(frozen=True)
class ActorRank:
    code: str
    level: int
    assignment: RankAssignment | None


def is_service_owner(user_id: int) -> bool:
    return user_id in get_settings().service_owner_ids


async def get_assignment(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    active_only: bool = True,
) -> RankAssignment | None:
    query = select(RankAssignment).where(
        RankAssignment.group_id == group_id,
        RankAssignment.user_telegram_id == user_id,
    )
    if active_only:
        query = query.where(RankAssignment.active.is_(True))
    return await session.scalar(query)


async def get_actor_rank(
    session: AsyncSession, group: Group, user_id: int
) -> ActorRank | None:
    if is_service_owner(user_id):
        return ActorRank("service_owner", 110, None)
    if group.owner_telegram_id == user_id:
        return ActorRank("owner", 100, None)
    assignment = await get_assignment(session, group.id, user_id)
    if assignment is None or assignment.rank_code not in RANK_LEVELS:
        return None
    return ActorRank(
        assignment.rank_code, RANK_LEVELS[assignment.rank_code], assignment
    )


async def effective_permissions(
    session: AsyncSession,
    group_id: int,
    assignment: RankAssignment,
) -> dict[str, bool]:
    rank = assignment.rank_code
    ceiling = ROLE_CEILINGS.get(rank, set())
    defaults = DEFAULT_ROLE_PERMISSIONS.get(rank, {})
    policy = await session.scalar(
        select(GroupRankPolicy).where(
            GroupRankPolicy.group_id == group_id,
            GroupRankPolicy.rank_code == rank,
        )
    )
    result = {name: bool(defaults.get(name, False)) for name in ceiling}
    if policy is not None:
        for name, value in (policy.permissions or {}).items():
            if name in ceiling:
                result[name] = bool(value)
    for name, value in (assignment.permissions or {}).items():
        if name in ceiling:
            result[name] = bool(value)
    return result


async def actor_has_permission(
    session: AsyncSession,
    group: Group,
    user_id: int,
    permission: str,
) -> bool:
    actor = await get_actor_rank(session, group, user_id)
    if actor is None:
        return False
    if actor.level >= 100:
        return True
    if actor.assignment is None:
        return False
    permissions = await effective_permissions(session, group.id, actor.assignment)
    return bool(permissions.get(permission, False))


async def is_untouchable(session: AsyncSession, group_id: int, user_id: int) -> bool:
    assignment = await get_assignment(session, group_id, user_id)
    return bool(assignment and assignment.rank_code == UNTOUCHABLE)


def rank_level(rank_code: str | None) -> int:
    return RANK_LEVELS.get(rank_code or "", -1)


def assignable_ranks(actor: ActorRank) -> tuple[str, ...]:
    if actor.level >= 100:
        return RANK_CODES
    if actor.code == DEPUTY_OWNER:
        return (CHIEF_ADMIN, CHAT_ADMIN, VOICE_ADMIN, HELPER, MAJOR, UNTOUCHABLE)
    if actor.code == CHIEF_ADMIN:
        return (CHAT_ADMIN, VOICE_ADMIN, HELPER, MAJOR, UNTOUCHABLE)
    if actor.code == CHAT_ADMIN:
        return (HELPER, MAJOR)
    return ()


async def can_assign_rank(
    session: AsyncSession,
    group: Group,
    actor_id: int,
    rank_code: str,
    *,
    target_id: int | None = None,
) -> tuple[bool, str]:
    actor = await get_actor_rank(session, group, actor_id)
    if actor is None or rank_code not in assignable_ranks(actor):
        return False, "Ваш ранг не позволяет назначить эту должность."
    if actor.level < 100:
        if rank_code == HELPER:
            if not await actor_has_permission(
                session, group, actor_id, "assign_helper"
            ):
                return False, "У вашего ранга отключено назначение помощников."
        elif rank_code in ADMIN_RANKS and rank_code != MAJOR:
            if not await actor_has_permission(
                session, group, actor_id, "promote_admins"
            ):
                return False, "У вашего ранга отключено назначение администраторов."
    if target_id is not None:
        if target_id == group.owner_telegram_id:
            return False, "Владелец группы не нуждается во внутреннем ранге."
        target = await get_assignment(session, group.id, target_id)
        if target and target.active and actor.level <= rank_level(target.rank_code):
            return (
                False,
                "Нельзя изменить ранг участника с равным или более высоким уровнем.",
            )
    return True, ""


async def can_edit_assignment(
    session: AsyncSession,
    group: Group,
    actor_id: int,
    target: RankAssignment,
) -> tuple[bool, str]:
    actor = await get_actor_rank(session, group, actor_id)
    if actor is None:
        return False, "Нет доступа."
    if actor.level >= 100:
        return True, ""
    if actor.level <= rank_level(target.rank_code):
        return False, "Нельзя изменять администратора своего или более высокого ранга."
    if actor.code == CHAT_ADMIN:
        if target.rank_code not in {HELPER, MAJOR}:
            return (
                False,
                "Администратор чата может управлять только своим помощником или назначенным Мажёром.",
            )
        if target.assigned_by_telegram_id != actor_id:
            return False, "Эту должность назначил другой администратор."
        if target.rank_code == HELPER and not await actor_has_permission(
            session, group, actor_id, "assign_helper"
        ):
            return False, "У вашего ранга отключено управление помощниками."
        return True, ""
    if not await actor_has_permission(
        session, group, actor_id, "edit_admin_permissions"
    ):
        return False, "У вашего ранга отключено управление правами администраторов."
    return True, ""


async def can_remove_assignment(
    session: AsyncSession,
    group: Group,
    actor_id: int,
    target: RankAssignment,
) -> tuple[bool, str]:
    actor = await get_actor_rank(session, group, actor_id)
    if actor is None:
        return False, "Нет доступа."
    if actor.level >= 100:
        return True, ""

    if target.rank_code == UNTOUCHABLE:
        if actor.level < RANK_LEVELS[CHIEF_ADMIN]:
            return (
                False,
                "Снять Недотрогу может только Глав. админ, Зам. владельца или владелец.",
            )
        return True, ""

    if target.rank_code == HELPER:
        if actor.code == CHAT_ADMIN:
            if target.assigned_by_telegram_id != actor_id:
                return False, "Этого помощника назначил другой администратор."
            if not await actor_has_permission(
                session, group, actor_id, "assign_helper"
            ):
                return False, "У вашего ранга отключено управление помощниками."
            return True, ""
        if actor.level > RANK_LEVELS[CHAT_ADMIN]:
            return True, ""
        return False, "Недостаточно прав для снятия помощника."

    target_level = rank_level(target.rank_code)
    if actor.level <= target_level:
        return False, "Администратора равного или более высокого ранга снять нельзя."
    if target.assigned_by_telegram_id == actor_id:
        return True, ""

    assigner = await get_actor_rank(session, group, target.assigned_by_telegram_id)
    assigner_level = assigner.level if assigner is not None else 100
    if actor.level > assigner_level:
        return True, ""
    return (
        False,
        "Снять этого администратора может тот, кто его назначил, либо руководитель выше назначившего.",
    )


async def can_moderate_target(
    session: AsyncSession,
    group: Group,
    actor_id: int,
    target_id: int,
) -> tuple[bool, str]:
    if target_id == group.owner_telegram_id:
        return False, "Владельца группы нельзя ограничивать через Mimoru."

    if actor_id == target_id:
        return False, "Нельзя наказать самого себя."

    target = await get_assignment(session, group.id, target_id)
    if target is None:
        return True, ""
    if target.rank_code == UNTOUCHABLE:
        return False, "У участника активен ранг «Недотрога». Сначала снимите иммунитет."
    actor = await get_actor_rank(session, group, actor_id)
    if actor is None:
        return False, "Нет прав на это действие."
    if actor.level <= rank_level(target.rank_code):
        return (
            False,
            "Нельзя наказывать администратора своего или более высокого ранга.",
        )
    return True, ""


async def ensure_telegram_rank(
    bot: Bot,
    group: Group,
    user_id: int,
    rank_code: str,
) -> tuple[bool, bool, str]:
    if rank_code not in ADMIN_RANKS:
        return True, False, ""
    try:
        member = await bot.get_chat_member(group.telegram_chat_id, user_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False, False, "Не удалось проверить участника в Telegram-группе."
    if member.status == ChatMemberStatus.CREATOR:
        return False, False, "Владельцу Telegram-группы внутренний ранг не назначается."
    if member.status == ChatMemberStatus.ADMINISTRATOR:
        return True, False, ""
    if member.status not in {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED}:
        return False, False, "Пользователь должен состоять в группе."
    try:
        await bot.promote_chat_member(
            group.telegram_chat_id,
            user_id,
            **telegram_rights_for_rank(rank_code),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        return (
            False,
            False,
            (
                "Mimoru не смогла назначить Telegram-администратора. Проверьте право бота назначать администраторов."
            ),
        )
    return True, True, ""


def telegram_rights_for_rank(rank_code: str) -> dict[str, bool]:
    """Telegram privileges actually required by a Mimoru rank.

    Major is a display-only rank: Telegram still requires administrator status
    for the user to appear in the administrator list, so can_manage_chat is the
    unavoidable minimum. All actionable moderation privileges stay disabled.
    """
    base = {
        "can_manage_chat": True,
        "can_change_info": False,
        "can_delete_messages": False,
        "can_invite_users": True,
        "can_restrict_members": False,
        "can_pin_messages": False,
        "can_promote_members": False,
        "can_manage_video_chats": False,
        "can_manage_topics": False,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
    }
    if rank_code == MAJOR:
        base["can_invite_users"] = False
    elif rank_code in {DEPUTY_OWNER, CHIEF_ADMIN}:
        base.update(
            can_delete_messages=True,
            can_restrict_members=True,
            can_promote_members=True,
            can_manage_video_chats=True,
        )
    elif rank_code == CHAT_ADMIN:
        base.update(
            can_delete_messages=True,
            can_restrict_members=True,
        )
    return base


async def demote_telegram_admin(bot: Bot, group: Group, user_id: int) -> bool:
    try:
        await bot.promote_chat_member(
            group.telegram_chat_id,
            user_id,
            can_manage_chat=False,
            can_change_info=False,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_promote_members=False,
            can_manage_video_chats=False,
            can_manage_topics=False,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def restore_telegram_rank(
    bot: Bot, group: Group, assignment: RankAssignment
) -> bool:
    if assignment.rank_code not in ADMIN_RANKS or not assignment.active:
        return False
    try:
        await bot.promote_chat_member(
            group.telegram_chat_id,
            assignment.user_telegram_id,
            **telegram_rights_for_rank(assignment.rank_code),
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def demote_if_managed(bot: Bot, group: Group, assignment: RankAssignment) -> None:
    if not assignment.telegram_admin_managed:
        return
    await demote_telegram_admin(bot, group, assignment.user_telegram_id)


def add_rank_event(
    session: AsyncSession,
    *,
    group_id: int,
    actor_id: int,
    target_id: int,
    action: str,
    old_rank: str | None,
    new_rank: str | None,
    details: dict | None = None,
) -> None:
    session.add(
        RankAssignmentEvent(
            group_id=group_id,
            actor_telegram_id=actor_id,
            target_telegram_id=target_id,
            action=action,
            old_rank_code=old_rank,
            new_rank_code=new_rank,
            details=details,
        )
    )
