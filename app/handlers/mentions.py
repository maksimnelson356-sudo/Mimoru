from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.services.mentions import (
    normalize_hashtag_limit,
    normalize_mention_limit,
    normalize_mention_mute,
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
        denial_text="Изменять защиту упоминаний может владелец или администратор группы.",
        for_update=for_update,
    )


@router.message(F.text.regexp(r"(?i)^(?:упоминания|антиупоминания) (?:вкл|выкл)$"))
async def mentions_toggle(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    enabled = (message.text or "").casefold().endswith("вкл")
    group.settings.mention_filter_enabled = enabled
    await session.commit()
    await message.reply(
        f"{'✅' if enabled else '❌'} Защита от массовых упоминаний "
        f"{'включена' if enabled else 'выключена'}."
    )


@router.message(F.text.regexp(r"(?i)^(?:упоминания|антиупоминания) статус$"))
async def mentions_status(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session)
    if not group:
        return
    settings = group.settings
    await message.reply(
        "<b>Упоминания и хэштеги</b>\n"
        f"Статус: {'✅ включена' if settings.mention_filter_enabled else '❌ выключена'}\n"
        f"Лимит упоминаний: {settings.mention_limit}\n"
        f"Лимит хэштегов: {settings.hashtag_limit}\n"
        f"Наказание: мут на {human_duration(settings.mention_mute_seconds)}"
    )


@router.message(F.text.regexp(r"(?i)^упоминания лимит \d+$"))
async def mentions_limit(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    try:
        value = normalize_mention_limit(int((message.text or "").split()[-1]))
    except ValueError as error:
        await message.reply(str(error))
        return
    group.settings.mention_limit = value
    await session.commit()
    await message.reply(f"✅ Лимит упоминаний в одном сообщении: {value}.")


@router.message(F.text.regexp(r"(?i)^хэштеги лимит \d+$"))
async def hashtags_limit(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    try:
        value = normalize_hashtag_limit(int((message.text or "").split()[-1]))
    except ValueError as error:
        await message.reply(str(error))
        return
    group.settings.hashtag_limit = value
    await session.commit()
    await message.reply(f"✅ Лимит хэштегов в одном сообщении: {value}.")


@router.message(F.text.regexp(r"(?i)^упоминания наказание \S+$"))
async def mentions_punishment(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    duration = parse_duration((message.text or "").split()[-1])
    if duration is None:
        await message.reply("Не удалось определить срок. Пример: <code>упоминания наказание 30м</code>.")
        return
    try:
        duration = normalize_mention_mute(duration)
    except ValueError as error:
        await message.reply(str(error))
        return
    group.settings.mention_mute_seconds = duration
    await session.commit()
    await message.reply(f"✅ Наказание: мут на {human_duration(duration)}.")
