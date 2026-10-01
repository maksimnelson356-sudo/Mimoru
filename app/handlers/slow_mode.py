from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.services.owner_management import managed_group_for_message
from app.services.slow_mode import normalize_slow_mode_seconds
from app.utils.duration import human_duration, parse_duration

router = Router(name=__name__)
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


async def managed(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> Group | None:
    return await managed_group_for_message(
        message,
        bot,
        session,
        denial_text="Изменять медленный режим может владелец или администратор группы.",
        for_update=for_update,
    )


@router.message(F.text.regexp(r"(?i)^(?:медленный режим|слоумо) вкл(?: \S+)?$"))
async def slow_mode_enable(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    parts = (message.text or "").casefold().split()
    raw_duration = parts[-1] if parts[-1] != "вкл" else None
    duration = parse_duration(raw_duration) if raw_duration else group.settings.slow_mode_seconds
    if duration is None:
        await message.reply("Не удалось определить интервал. Пример: <code>медленный режим вкл 10с</code>.")
        return
    try:
        duration = normalize_slow_mode_seconds(duration)
    except ValueError as error:
        await message.reply(str(error))
        return
    group.settings.slow_mode_enabled = True
    group.settings.slow_mode_seconds = duration
    await session.commit()
    await message.reply(f"✅ Медленный режим включён: одно сообщение каждые {human_duration(duration)}.")


@router.message(F.text.regexp(r"(?i)^(?:медленный режим|слоумо) выкл$"))
async def slow_mode_disable(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    group.settings.slow_mode_enabled = False
    await session.commit()
    await message.reply("❌ Медленный режим выключен.")


@router.message(F.text.regexp(r"(?i)^(?:медленный режим|слоумо) статус$"))
async def slow_mode_status(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session)
    if not group:
        return
    settings = group.settings
    await message.reply(
        "<b>Медленный режим</b>\n"
        f"Статус: {'✅ включён' if settings.slow_mode_enabled else '❌ выключен'}\n"
        f"Интервал: {human_duration(settings.slow_mode_seconds)}\n"
        "Администраторы и доверенные пользователи не ограничиваются."
    )
