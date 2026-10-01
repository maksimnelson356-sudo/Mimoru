from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, Punishment
from app.db.pending_bans import PendingBan
from app.services.access import can_moderate
from app.services.message_ttl import send_group_notice
from app.services.permissions import target_is_protected
from app.services.ranks import can_moderate_target, get_assignment
from app.services.user_refs import resolve_known_user_reference, user_label


router = Router(name=__name__)
GROUP_TYPES = {"group", "supergroup"}


async def _group(
    session: AsyncSession,
    chat_id: int,
    *,
    for_update: bool = False,
) -> Group | None:
    query = select(Group).where(
        Group.telegram_chat_id == chat_id,
        Group.is_active.is_(True),
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def _pending_for(
    session: AsyncSession,
    group_id: int,
    *,
    user_id: int | None = None,
    username: str | None = None,
) -> PendingBan | None:
    conditions = []
    if user_id is not None:
        conditions.append(PendingBan.user_telegram_id == user_id)
    if username:
        conditions.append(func.lower(PendingBan.username) == username.casefold())
    if not conditions:
        return None
    return await session.scalar(
        select(PendingBan).where(
            PendingBan.group_id == group_id,
            PendingBan.active.is_(True),
            or_(*conditions),
        ).order_by(PendingBan.created_at.desc())
    )


async def _save_pending(
    session: AsyncSession,
    group: Group,
    moderator_id: int,
    *,
    user_id: int | None,
    username: str | None,
) -> PendingBan:
    row = await _pending_for(session, group.id, user_id=user_id, username=username)
    if row is None:
        row = PendingBan(
            group_id=group.id,
            user_telegram_id=user_id,
            username=username,
            moderator_telegram_id=moderator_id,
            reason="Предварительный бан администратора",
            active=True,
        )
        session.add(row)
    else:
        row.active = True
        row.moderator_telegram_id = moderator_id
        if user_id is not None:
            row.user_telegram_id = user_id
        if username:
            row.username = username
    await session.flush()
    return row


async def _ensure_punishment(
    session: AsyncSession,
    group: Group,
    target_id: int,
    moderator_id: int,
    reason: str,
) -> None:
    existing = await session.scalar(
        select(Punishment).where(
            Punishment.group_id == group.id,
            Punishment.user_telegram_id == target_id,
            Punishment.kind == "ban",
            Punishment.active.is_(True),
        ).order_by(Punishment.created_at.desc())
    )
    if existing is None:
        session.add(Punishment(
            group_id=group.id,
            user_telegram_id=target_id,
            moderator_telegram_id=moderator_id,
            kind="ban",
            reason=reason,
            ends_at=None,
            active=True,
        ))


async def _is_unmanaged_telegram_admin(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    target_id: int,
) -> bool:
    assignment = await get_assignment(session, group.id, target_id)
    if assignment is not None:
        return False
    try:
        return await target_is_protected(bot, group.telegram_chat_id, target_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


@router.message(F.chat.type.in_(GROUP_TYPES), F.text.regexp(r"(?i)^бан\s+(?:@\w{3,64}|\d{5,20})\s*$"))
async def ban_reference(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    group = await _group(session, message.chat.id, for_update=True)
    if group is None or not await can_moderate(bot, session, group, message.from_user.id, "ban"):
        return

    raw = (message.text or "").split(maxsplit=1)[1].strip()
    target_id, username = await resolve_known_user_reference(session, raw)
    if target_id is not None:
        if target_id == message.from_user.id:
            await message.reply("Нельзя забанить самого себя.")
            return
        allowed, reason = await can_moderate_target(session, group, message.from_user.id, target_id)
        if not allowed:
            await message.reply(reason)
            return
        if await _is_unmanaged_telegram_admin(bot, session, group, target_id):
            await message.reply(
                "Нельзя забанить Telegram-владельца или администратора, который не управляется рангами Mimoru."
            )
            return

    await _save_pending(
        session,
        group,
        message.from_user.id,
        user_id=target_id,
        username=username,
    )

    telegram_applied = False
    if target_id is not None:
        try:
            await bot.ban_chat_member(group.telegram_chat_id, target_id)
            telegram_applied = True
        except (TelegramBadRequest, TelegramForbiddenError):
            telegram_applied = False
        if telegram_applied:
            await _ensure_punishment(
                session,
                group,
                target_id,
                message.from_user.id,
                "Бан по ID/@username",
            )
    await session.commit()

    if target_id is None:
        await send_group_notice(
            bot,
            message.chat.id,
            f"🚫 @{username} добавлен в предварительный бан.\n"
            "Mimoru пока не знает Telegram ID этого аккаунта. Если он войдёт в группу с этим username, бот сразу забанит его.",
        )
        return
    label = await user_label(session, target_id)
    if telegram_applied:
        await send_group_notice(
            bot, message.chat.id, f"🚫 {label} забанен и сохранён в списке запретов группы."
        )
    else:
        await send_group_notice(
            bot,
            message.chat.id,
            f"🚫 Запрет для {label} сохранён. Если пользователь появится в группе, Mimoru применит бан автоматически.",
        )


@router.message(F.chat.type.in_(GROUP_TYPES), F.text.regexp(r"(?i)^разбан\s+(?:@\w{3,64}|\d{5,20})\s*$"))
async def unban_reference(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    group = await _group(session, message.chat.id, for_update=True)
    if group is None or not await can_moderate(bot, session, group, message.from_user.id, "unban"):
        return
    raw = (message.text or "").split(maxsplit=1)[1].strip()
    target_id, username = await resolve_known_user_reference(session, raw)

    conditions = []
    if target_id is not None:
        conditions.append(PendingBan.user_telegram_id == target_id)
    if username:
        conditions.append(func.lower(PendingBan.username) == username.casefold())
    if conditions:
        rows = list((await session.scalars(select(PendingBan).where(
            PendingBan.group_id == group.id,
            PendingBan.active.is_(True),
            or_(*conditions),
        ))).all())
        for row in rows:
            row.active = False

    if target_id is not None:
        punishments = list((await session.scalars(select(Punishment).where(
            Punishment.group_id == group.id,
            Punishment.user_telegram_id == target_id,
            Punishment.kind == "ban",
            Punishment.active.is_(True),
        ))).all())
        for row in punishments:
            row.active = False
        try:
            await bot.unban_chat_member(group.telegram_chat_id, target_id, only_if_banned=True)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    await session.commit()
    label = await user_label(session, target_id) if target_id is not None else f"@{username}"
    await send_group_notice(bot, message.chat.id, f"✅ Запрет для {label} снят.")


async def enforce_pending_ban_on_join(
    message: Message,
    bot: Bot,
    session: AsyncSession,
) -> set[int]:
    """Apply deferred bans first and return only members successfully banned.

    This is intentionally a helper, not a competing ``F.new_chat_members`` handler.
    The single live join pipeline in ``members.welcome`` calls it before CAPTCHA and
    welcome processing, then skips the returned member IDs.
    """
    group = await _group(session, message.chat.id, for_update=True)
    if group is None:
        return set()
    banned_user_ids: set[int] = set()
    for member in message.new_chat_members or []:
        if member.is_bot:
            continue
        username = member.username.casefold() if member.username else None
        pending = await _pending_for(session, group.id, user_id=member.id, username=username)
        active_ban = await session.scalar(select(Punishment).where(
            Punishment.group_id == group.id,
            Punishment.user_telegram_id == member.id,
            Punishment.kind == "ban",
            Punishment.active.is_(True),
        ).order_by(Punishment.created_at.desc()))
        if pending is None and active_ban is None:
            continue
        moderator_id = pending.moderator_telegram_id if pending is not None else active_ban.moderator_telegram_id
        if pending is not None and pending.user_telegram_id is None:
            pending.user_telegram_id = member.id
        try:
            await bot.ban_chat_member(group.telegram_chat_id, member.id)
        except (TelegramBadRequest, TelegramForbiddenError):
            continue
        await _ensure_punishment(session, group, member.id, moderator_id, "Автоматически применён предварительный бан")
        banned_user_ids.add(member.id)
        await message.answer(f"🚫 {member.full_name} автоматически забанен: пользователь находился в списке запретов Mimoru.")
    await session.commit()
    return banned_user_ids
