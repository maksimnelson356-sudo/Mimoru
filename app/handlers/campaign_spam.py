from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.services.campaign_spam import (
    normalize_campaign_limit,
    normalize_campaign_mute,
    normalize_campaign_window,
)
from app.services.owner_management import managed_group_for_message
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
        denial_text="Изменять защиту от массового спама может владелец или администратор группы.",
        for_update=for_update,
    )


@router.message(F.text.regexp(r"(?i)^(?:массовый спам|антикампания) (?:вкл|выкл)$"))
async def campaign_toggle(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    enabled = (message.text or "").casefold().endswith("вкл")
    group.settings.campaign_spam_enabled = enabled
    await session.commit()
    await message.reply(f"{'✅' if enabled else '❌'} Защита от массового спама {'включена' if enabled else 'выключена'}.")


@router.message(F.text.regexp(r"(?i)^(?:массовый спам|антикампания) статус$"))
async def campaign_status(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session)
    if not group:
        return
    settings = group.settings
    await message.reply(
        "Защита от массового спама\n"
        f"Статус: {'✅ включена' if settings.campaign_spam_enabled else '❌ выключена'}\n"
        f"Порог: {settings.campaign_spam_limit} разных пользователей\n"
        f"Окно: {human_duration(settings.campaign_spam_window_seconds)}\n"
        f"Мут: {human_duration(settings.campaign_spam_mute_seconds)}"
    )


@router.message(F.text.regexp(r"(?i)^массовый спам \d+ за \S+$"))
async def campaign_threshold(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    parts = (message.text or "").casefold().split()
    try:
        limit = normalize_campaign_limit(int(parts[2]))
    except (ValueError, IndexError) as error:
        await message.reply(str(error) if str(error) else "Пример: массовый спам 3 за 2м.")
        return
    window = parse_duration(parts[4]) if len(parts) > 4 else None
    if window is None:
        await message.reply("Не удалось определить интервал. Пример: массовый спам 3 за 2м.")
        return
    try:
        window = normalize_campaign_window(window)
    except ValueError as error:
        await message.reply(str(error))
        return
    group.settings.campaign_spam_limit = limit
    group.settings.campaign_spam_window_seconds = window
    await session.commit()
    await message.reply(f"✅ Порог: {limit} пользователей за {human_duration(window)}.")


@router.message(F.text.regexp(r"(?i)^массовый спам наказание \S+$"))
async def campaign_punishment(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    raw = (message.text or "").casefold().split()[-1]
    duration = parse_duration(raw)
    if duration is None:
        await message.reply("Не удалось определить срок. Пример: массовый спам наказание 1ч.")
        return
    try:
        duration = normalize_campaign_mute(duration)
    except ValueError as error:
        await message.reply(str(error))
        return
    group.settings.campaign_spam_mute_seconds = duration
    await session.commit()
    await message.reply(f"✅ Наказание за массовый спам: мут на {human_duration(duration)}.")
