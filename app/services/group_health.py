from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, GroupMember
from app.services.plans import feature_available


@dataclass(slots=True)
class GroupHealth:
    score: int
    level: str
    permission_score: int
    protection_score: int
    newcomer_score: int
    reporting_score: int
    hygiene_score: int
    known_members: int
    deleted_accounts: int
    recommendations: list[str]
    bot_is_admin: bool
    can_delete_messages: bool
    can_restrict_members: bool
    can_invite_users: bool
    can_manage_chat: bool



from app.services.group_health_scoring import health_level, hygiene_points, newcomer_points, protection_points


async def calculate_group_health(bot: Bot, session: AsyncSession, group: Group) -> GroupHealth:
    recommendations: list[str] = []
    bot_is_admin = False
    can_delete = False
    can_restrict = False
    can_invite = False
    can_manage = False

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(group.telegram_chat_id, me.id)
        bot_is_admin = member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
        if member.status == ChatMemberStatus.CREATOR:
            can_delete = can_restrict = can_invite = can_manage = True
        elif member.status == ChatMemberStatus.ADMINISTRATOR:
            can_delete = bool(getattr(member, "can_delete_messages", False))
            can_restrict = bool(getattr(member, "can_restrict_members", False))
            can_invite = bool(getattr(member, "can_invite_users", False))
            can_manage = bool(getattr(member, "can_manage_chat", False))
    except Exception:
        recommendations.append("Не удалось проверить права Mimoru в Telegram. Проверьте, что бот остаётся участником группы.")

    permission_score = 0
    if bot_is_admin:
        permission_score += 4
    else:
        recommendations.append("Назначьте Mimoru администратором группы.")
    if can_delete:
        permission_score += 10
    else:
        recommendations.append("Выдайте Mimoru право удалять сообщения — без него защита не сможет очищать нарушения.")
    if can_restrict:
        permission_score += 15
    else:
        recommendations.append("Выдайте Mimoru право блокировать и ограничивать участников — оно нужно для мутов, киков и банов.")
    if can_invite:
        permission_score += 3
    if can_manage:
        permission_score += 3

    settings = group.settings
    protection_score = protection_points(settings)
    newcomer_score = newcomer_points(settings)
    reports_available = feature_available(group, "daily_reports")
    reporting_score = 5 if (settings.reports_enabled or not reports_available) else 0

    if not settings.antiflood_enabled:
        recommendations.append("Включите антифлуд для защиты от массовой отправки сообщений.")
    if not settings.anti_raid_enabled:
        recommendations.append("Включите Anti-Raid, если в группу могут массово заходить новые аккаунты.")
    if not settings.captcha_enabled and not settings.newcomer_quarantine_enabled:
        recommendations.append("Для открытых групп полезно включить капчу или карантин новичков.")
    if reports_available and not settings.reports_enabled:
        recommendations.append("Включите ежедневные отчёты, чтобы не пропускать изменения активности и модерации.")

    known_members = int(
        await session.scalar(
            select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id)
        )
        or 0
    )
    deleted_accounts = int(
        await session.scalar(
            select(func.count()).select_from(GroupMember).where(
                GroupMember.group_id == group.id,
                GroupMember.is_present.is_(True),
                GroupMember.is_deleted_account.is_(True),
            )
        )
        or 0
    )
    hygiene_score = hygiene_points(known_members, deleted_accounts)
    if known_members and deleted_accounts / known_members > 0.03:
        recommendations.append(
            f"Найдено много удалённых аккаунтов ({deleted_accounts}). Откройте «Участники → Удалённые аккаунты» и выполните очистку."
        )

    score = min(
        100,
        permission_score + protection_score + newcomer_score + reporting_score + hygiene_score,
    )
    return GroupHealth(
        score=score,
        level=health_level(score),
        permission_score=permission_score,
        protection_score=protection_score,
        newcomer_score=newcomer_score,
        reporting_score=reporting_score,
        hygiene_score=hygiene_score,
        known_members=known_members,
        deleted_accounts=deleted_accounts,
        recommendations=recommendations[:6],
        bot_is_admin=bot_is_admin,
        can_delete_messages=can_delete,
        can_restrict_members=can_restrict,
        can_invite_users=can_invite,
        can_manage_chat=can_manage,
    )
