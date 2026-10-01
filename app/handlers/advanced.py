from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import ChatPermissions, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, ModerationLog, ModeratorNote, ScheduledMessage
from app.services.access import can_manage_group, can_moderate
from app.services.repositories import get_or_create_group
from app.services.scheduling import parse_scheduled_message
from app.services.timezones import to_local, validate_timezone
from app.utils.duration import parse_duration

router = Router(name=__name__)
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

LOCKED = ChatPermissions(can_send_messages=False)
OPEN = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True,
)


async def _group(
    message: Message,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> Group | None:
    if not message.from_user:
        return None
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if for_update:
        # Upgrade the already-loaded ORM identity to the current locked database
        # state before any owner/rank authorization reads its attributes.
        return await session.scalar(
            select(Group).where(
                Group.id == group.id,
                Group.is_active.is_(True),
            ).with_for_update().execution_options(populate_existing=True)
        )
    return group


async def _owner_group(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> Group | None:
    group = await _group(message, session, for_update=for_update)
    if not group or not message.from_user or not await can_manage_group(bot, group, message.from_user.id, session):
        await message.reply("Эта команда доступна владельцу или администратору группы.")
        return None
    return group


@router.message(F.text.regexp(r"(?i)^локдаун вкл(?:\s+\S+)?$"))
async def lockdown_on(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    parts = message.text.split(maxsplit=2)
    until = None
    if len(parts) == 3:
        seconds = parse_duration(parts[2])
        if seconds is None:
            await message.reply("Не удалось определить срок. Пример: локдаун вкл 30м")
            return
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    if group.settings.night_mode_active:
        previous = group.settings.night_mode_previous_permissions
        group.settings.night_mode_active = False
        group.settings.night_mode_previous_permissions = None
    else:
        previous = message.chat.permissions.model_dump(exclude_none=True) if message.chat.permissions else None
    await bot.set_chat_permissions(message.chat.id, LOCKED)
    group.settings.lockdown_enabled = True
    group.settings.lockdown_until = until
    group.settings.lockdown_previous_permissions = previous
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id,
                              target_telegram_id=None, action="lockdown_on",
                              reason=f"До {until.isoformat()}" if until else "Без срока"))
    await session.commit()
    local_until = to_local(until, group.settings.timezone_name) if until else None
    await message.reply("🔒 Группа закрыта для сообщений." + (f" До {local_until:%Y-%m-%d %H:%M} ({group.settings.timezone_name})." if local_until else ""))


@router.message(F.text.casefold() == "локдаун выкл")
async def lockdown_off(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    previous = group.settings.lockdown_previous_permissions
    permissions = ChatPermissions(**previous) if previous else OPEN
    await bot.set_chat_permissions(message.chat.id, permissions)
    group.settings.lockdown_enabled = False
    group.settings.lockdown_until = None
    group.settings.lockdown_previous_permissions = None
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id,
                              target_telegram_id=None, action="lockdown_off"))
    await session.commit()
    await message.reply("🔓 Группа снова открыта для сообщений.")


@router.message(F.text.casefold() == "локдаун статус")
async def lockdown_status(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session)
    if not group:
        return
    if not group.settings.lockdown_enabled:
        await message.reply("Локдаун выключен.")
    elif group.settings.lockdown_until:
        local_until = to_local(group.settings.lockdown_until, group.settings.timezone_name)
        await message.reply(f"Локдаун включён до {local_until:%Y-%m-%d %H:%M} ({group.settings.timezone_name}).")
    else:
        await message.reply("Локдаун включён без ограничения по времени.")


@router.message(F.text.regexp(r"(?is)^заметка\s+.+$"))
async def add_note(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _group(message, session, for_update=True)
    if not group or not message.from_user or not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Ответьте командой «заметка текст» на сообщение пользователя.")
        return
    if not await can_moderate(bot, session, group, message.from_user.id, "info"):
        await message.reply("У вас нет права добавлять заметки.")
        return
    text = message.text.split(maxsplit=1)[1].strip()
    if len(text) > 1000:
        await message.reply("Заметка не должна превышать 1000 символов.")
        return
    target_id = message.reply_to_message.from_user.id
    note = ModeratorNote(group_id=group.id, target_telegram_id=target_id,
                         author_telegram_id=message.from_user.id, text=text)
    session.add(note)
    await session.flush()
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id,
                              target_telegram_id=target_id, action="note_add",
                              reason=text, metadata_json={"note_id": note.id}))
    await session.commit()
    await message.reply(f"📝 Заметка #{note.id} сохранена.")


@router.message(F.text.casefold() == "заметки")
async def list_notes(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _group(message, session)
    if not group or not message.from_user or not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Ответьте командой «заметки» на сообщение пользователя.")
        return
    if not await can_moderate(bot, session, group, message.from_user.id, "info"):
        await message.reply("У вас нет права просматривать заметки.")
        return
    rows = (await session.scalars(select(ModeratorNote).where(
        ModeratorNote.group_id == group.id,
        ModeratorNote.target_telegram_id == message.reply_to_message.from_user.id,
    ).order_by(ModeratorNote.created_at.desc()).limit(10))).all()
    if not rows:
        await message.reply("Заметок о пользователе нет.")
        return
    lines = [f"#{row.id} · {row.created_at:%Y-%m-%d} · {row.text}" for row in rows]
    await message.reply("<b>Заметки модераторов</b>\n" + "\n".join(lines))


@router.message(F.text.regexp(r"(?i)^удалить заметку\s+\d+$"))
async def delete_note(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _group(message, session, for_update=True)
    if not group or not message.from_user or not await can_moderate(bot, session, group, message.from_user.id, "info"):
        await message.reply("У вас нет права удалять заметки.")
        return
    note_id = int(message.text.split()[-1])
    note = await session.scalar(select(ModeratorNote).where(ModeratorNote.id == note_id, ModeratorNote.group_id == group.id))
    if not note:
        await message.reply("Заметка не найдена.")
        return
    target_id = note.target_telegram_id
    await session.delete(note)
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id,
                              target_telegram_id=target_id, action="note_delete",
                              metadata_json={"note_id": note_id}))
    await session.commit()
    await message.reply(f"Заметка #{note_id} удалена.")


@router.message(F.text.regexp(r"(?i)^часовой пояс\s+\S+$"))
async def set_timezone(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    value = message.text.split(maxsplit=2)[2].strip()
    try:
        timezone_name = validate_timezone(value)
    except ValueError as error:
        await message.reply(str(error))
        return
    group.settings.timezone_name = timezone_name
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id,
                              target_telegram_id=None, action="timezone_change", reason=timezone_name))
    await session.commit()
    now_local = to_local(datetime.now(timezone.utc), timezone_name)
    await message.reply(f"✅ Часовой пояс: <b>{timezone_name}</b>. Сейчас {now_local:%Y-%m-%d %H:%M}.")


@router.message(F.text.casefold() == "часовой пояс")
async def show_timezone(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session)
    if not group:
        return
    now_local = to_local(datetime.now(timezone.utc), group.settings.timezone_name)
    await message.reply(f"Часовой пояс: <b>{group.settings.timezone_name}</b>\nЛокальное время: {now_local:%Y-%m-%d %H:%M}")


@router.message(F.text.regexp(r"(?is)^запланировать\s+.+\|.+$"))
async def schedule_message(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    try:
        send_at, text, recurrence, weekday, recurrence_time = parse_scheduled_message(message.text, group.settings.timezone_name)
    except ValueError as error:
        await message.reply(str(error))
        return
    row = ScheduledMessage(group_id=group.id, creator_telegram_id=message.from_user.id,
                           text=text, send_at=send_at, recurrence=recurrence,
                           recurrence_weekday=weekday, recurrence_time=recurrence_time)
    session.add(row)
    await session.flush()
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id,
                              target_telegram_id=None, action="scheduled_message_add",
                              metadata_json={"scheduled_message_id": row.id, "send_at": send_at.isoformat()}))
    await session.commit()
    local_send_at = to_local(send_at, group.settings.timezone_name)
    suffix = {"once": "однократно", "daily": "ежедневно", "weekly": "еженедельно"}[recurrence]
    await message.reply(f"📅 Публикация #{row.id}: {local_send_at:%Y-%m-%d %H:%M} ({group.settings.timezone_name}), {suffix}.")


@router.message(F.text.casefold() == "расписание")
async def schedule_list(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session)
    if not group:
        return
    rows = (await session.scalars(select(ScheduledMessage).where(
        ScheduledMessage.group_id == group.id, ScheduledMessage.status == "pending"
    ).order_by(ScheduledMessage.send_at).limit(20))).all()
    if not rows:
        await message.reply("Запланированных публикаций нет.")
        return
    lines = []
    for row in rows:
        local_time = to_local(row.send_at, group.settings.timezone_name)
        repeat = {"once": "однократно", "daily": "ежедневно", "weekly": "еженедельно"}.get(row.recurrence, row.recurrence)
        lines.append(f"#{row.id} · {local_time:%Y-%m-%d %H:%M} · {repeat} · {row.text[:80]}")
    await message.reply(f"<b>Запланированные публикации</b>\nЧасовой пояс: {group.settings.timezone_name}\n" + "\n".join(lines))


@router.message(F.text.regexp(r"(?i)^отменить публикацию\s+\d+$"))
async def cancel_scheduled(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    row_id = int(message.text.split()[-1])
    row = await session.scalar(select(ScheduledMessage).where(
        ScheduledMessage.id == row_id, ScheduledMessage.group_id == group.id,
        ScheduledMessage.status == "pending",
    ))
    if not row:
        await message.reply("Активная публикация не найдена.")
        return
    row.status = "cancelled"
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id,
                              target_telegram_id=None, action="scheduled_message_cancel",
                              metadata_json={"scheduled_message_id": row.id}))
    await session.commit()
    await message.reply(f"Публикация #{row.id} отменена.")


@router.message(F.text.regexp(r"(?i)^ночной режим вкл(?:\s+([01]\d|2[0-3]):[0-5]\d\s+([01]\d|2[0-3]):[0-5]\d)?$"))
async def night_mode_on(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    parts = message.text.split()
    if len(parts) == 5:
        from app.services.night_mode import parse_hhmm
        try:
            start = parse_hhmm(parts[3]).strftime("%H:%M")
            end = parse_hhmm(parts[4]).strftime("%H:%M")
        except ValueError as error:
            await message.reply(str(error))
            return
        group.settings.night_mode_start = start
        group.settings.night_mode_end = end
    group.settings.night_mode_enabled = True
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id,
                              target_telegram_id=None, action="night_mode_enable",
                              reason=f"{group.settings.night_mode_start}-{group.settings.night_mode_end}"))
    await session.commit()
    await message.reply(
        f"🌙 Ночной режим включён: {group.settings.night_mode_start}–{group.settings.night_mode_end} "
        f"({group.settings.timezone_name})."
    )


@router.message(F.text.casefold() == "ночной режим выкл")
async def night_mode_off(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    group.settings.night_mode_enabled = False
    if group.settings.night_mode_active and not group.settings.lockdown_enabled:
        previous = group.settings.night_mode_previous_permissions
        await bot.set_chat_permissions(message.chat.id, ChatPermissions(**previous) if previous else OPEN)
    group.settings.night_mode_active = False
    group.settings.night_mode_previous_permissions = None
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id,
                              target_telegram_id=None, action="night_mode_disable"))
    await session.commit()
    await message.reply("☀️ Ночной режим выключен.")


@router.message(F.text.casefold() == "ночной режим статус")
async def night_mode_status(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _owner_group(message, bot, session)
    if not group:
        return
    settings = group.settings
    state = "активен сейчас" if settings.night_mode_active else "сейчас не активен"
    enabled = "включён" if settings.night_mode_enabled else "выключен"
    await message.reply(
        f"🌙 Ночной режим {enabled}; {state}.\n"
        f"Расписание: {settings.night_mode_start}–{settings.night_mode_end} "
        f"({settings.timezone_name})."
    )
