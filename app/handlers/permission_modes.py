from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.services.access import can_manage_group
from app.services.chat_permission_transitions import (
    LOCKED,
    apply_permission_transition,
    permissions_dict,
    permissions_from_dict,
)
from app.services.repositories import get_or_create_group
from app.services.timezones import to_local
from app.utils.duration import parse_duration

router = Router(name=__name__)
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


async def _owner_group(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    for_update: bool = False,
):
    if message.from_user is None:
        return None
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if for_update:
        group = await session.scalar(
            select(Group)
            .where(Group.id == group.id, Group.is_active.is_(True))
            .with_for_update()
        )
        if group is None:
            await message.reply("Группа больше не обслуживается.")
            return None
    if not await can_manage_group(bot, group, message.from_user.id, session):
        await message.reply("Эта команда доступна владельцу или администратору группы.")
        return None
    return group


async def _current_permissions(bot: Bot, chat_id: int) -> dict | None:
    try:
        chat = await bot.get_chat(chat_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return None
    return permissions_dict(chat.permissions)


@router.message(F.text.regexp(r"(?i)^локдаун вкл(?:\s+\S+)?$"))
async def safe_lockdown_on(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session, for_update=True)
    if group is None or message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=2)
    until = None
    if len(parts) == 3:
        seconds = parse_duration(parts[2])
        if seconds is None:
            await message.reply("Не удалось определить срок. Пример: локдаун вкл 30м")
            return
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    current = await _current_permissions(bot, group.telegram_chat_id)
    if current is None:
        await message.reply("Не удалось прочитать текущие права группы в Telegram.")
        return
    clear_night = bool(group.settings.night_mode_active)
    previous = group.settings.night_mode_previous_permissions if clear_night else current
    ok, error = await apply_permission_transition(
        bot,
        session,
        group,
        actor_id=message.from_user.id,
        operation="lockdown_on",
        desired_permissions=LOCKED,
        previous_permissions=previous,
        payload={"until": until.isoformat() if until else None, "clear_night": clear_night},
    )
    if not ok:
        await message.reply(error)
        return
    local_until = to_local(until, group.settings.timezone_name) if until else None
    await message.reply(
        "🔒 Группа закрыта для сообщений."
        + (f" До {local_until:%Y-%m-%d %H:%M} ({group.settings.timezone_name})." if local_until else "")
    )


@router.message(F.text.casefold() == "локдаун выкл")
async def safe_lockdown_off(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session, for_update=True)
    if group is None or message.from_user is None:
        return
    if not group.settings.lockdown_enabled:
        await message.reply("Локдаун уже выключен.")
        return
    current = await _current_permissions(bot, group.telegram_chat_id)
    if current is None:
        await message.reply("Не удалось прочитать текущие права группы в Telegram.")
        return
    desired = permissions_from_dict(group.settings.lockdown_previous_permissions)
    ok, error = await apply_permission_transition(
        bot,
        session,
        group,
        actor_id=message.from_user.id,
        operation="lockdown_off",
        desired_permissions=desired,
        previous_permissions=current,
    )
    if not ok:
        await message.reply(error)
        return
    await message.reply("🔓 Группа снова открыта для сообщений.")


@router.message(F.text.casefold() == "ночной режим выкл")
async def safe_night_mode_off(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session, for_update=True)
    if group is None or message.from_user is None:
        return
    group.settings.night_mode_enabled = False
    if not group.settings.night_mode_active:
        await session.commit()
        await message.reply("Ночной режим выключен.")
        return
    if group.settings.lockdown_enabled:
        group.settings.night_mode_active = False
        group.settings.night_mode_previous_permissions = None
        await session.commit()
        await message.reply("Ночной режим выключен. Локдаун остаётся активным.")
        return

    current = await _current_permissions(bot, group.telegram_chat_id)
    if current is None:
        await message.reply("Не удалось прочитать текущие права группы в Telegram.")
        return
    desired = permissions_from_dict(group.settings.night_mode_previous_permissions)
    ok, error = await apply_permission_transition(
        bot,
        session,
        group,
        actor_id=message.from_user.id,
        operation="night_unlock",
        desired_permissions=desired,
        previous_permissions=current,
    )
    if not ok:
        await message.reply(error)
        return
    await message.reply("Ночной режим выключен, права группы восстановлены.")
