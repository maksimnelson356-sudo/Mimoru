import json
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, ModerationLog
from app.services.access import can_manage_group
from app.services.repositories import get_or_create_group
from app.services.plans import feature_available
from app.services.settings_io import export_group_settings, import_group_settings
from app.services.ui import clean_ui_text

router = Router(name=__name__)
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


async def _managed(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> Group | None:
    if not message.from_user:
        return None
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if for_update:
        locked = await session.scalar(
            select(Group).where(
                Group.id == group.id,
                Group.is_active.is_(True),
            ).with_for_update()
        )
        if locked is None:
            await message.reply("Группа больше не обслуживается.")
            return None
        group = locked
    if not await can_manage_group(bot, group, message.from_user.id, session):
        await message.reply("Эта команда доступна владельцу или администратору группы.")
        return None
    return group


@router.message(F.text.casefold() == "экспорт настроек")
async def export_settings(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _managed(message, bot, session)
    if not group:
        return
    payload = json.dumps(export_group_settings(group), ensure_ascii=False, indent=2).encode("utf-8")
    file = BufferedInputFile(payload, filename=f"group_{group.id}_settings.json")
    await message.reply_document(file, caption="Резервная копия настроек группы.")


@router.message(F.text.regexp(r"(?is)^импорт настроек\s+\{.+\}$"))
async def import_settings(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _managed(message, bot, session, for_update=True)
    if not group:
        return
    try:
        payload = json.loads(message.text.split(maxsplit=2)[2])
        changed = import_group_settings(group, payload)
    except (ValueError, json.JSONDecodeError) as error:
        await message.reply(f"Не удалось импортировать настройки: {clean_ui_text(str(error))}")
        return
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id,
                              target_telegram_id=None, action="settings_import",
                              reason=f"Изменено полей: {len(changed)}"))
    await session.commit()
    await message.reply(f"✅ Настройки импортированы. Изменено полей: {len(changed)}.")


@router.message(F.text.regexp(r"(?i)^отчеты (вкл|выкл)$"))
async def toggle_reports(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _managed(message, bot, session, for_update=True)
    if not group:
        return
    enable = message.text.casefold().endswith("вкл")
    if enable and not feature_available(group, "daily_reports"):
        await message.reply("Ежедневные отчёты доступны на TRIAL, STANDARD и PRO.")
        return
    group.settings.reports_enabled = enable
    await session.commit()
    await message.reply("✅ Ежедневные отчёты включены." if group.settings.reports_enabled else "❌ Ежедневные отчёты выключены.")


@router.message(F.text.regexp(r"(?i)^отчет время ([01]?\d|2[0-3])$"))
async def report_hour(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _managed(message, bot, session, for_update=True)
    if not group:
        return
    if not feature_available(group, "daily_reports"):
        await message.reply("Настройка ежедневных отчётов доступна на TRIAL, STANDARD и PRO.")
        return
    hour = int(message.text.split()[-1])
    group.settings.report_hour_utc = hour
    await session.commit()
    await message.reply(f"✅ Ежедневный отчёт будет отправляться примерно в {hour:02d}:00 ({clean_ui_text(group.settings.timezone_name)}).")


@router.message(F.text.casefold().in_({"диагностика", "проверить права", "права бота"}))
async def diagnostics(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _managed(message, bot, session)
    if not group:
        return
    me = await bot.get_me()
    member = await bot.get_chat_member(message.chat.id, me.id)
    lines = [
        f"Статус бота: {member.status}",
        f"Удаление сообщений: {'✅' if getattr(member, 'can_delete_messages', False) else '❌'}",
        f"Ограничение участников: {'✅' if getattr(member, 'can_restrict_members', False) else '❌'}",
        f"Закрепление сообщений: {'✅' if getattr(member, 'can_pin_messages', False) else '❌'}",
        f"Тариф: {group.plan_code}",
        f"Отчёты: {'✅' if group.settings.reports_enabled else '❌'}",
    ]
    await message.reply("Диагностика группы\n" + "\n".join(lines))
