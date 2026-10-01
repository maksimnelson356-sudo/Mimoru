from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.services.edit_protection import normalize_edit_window
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
        denial_text="Изменять защиту редактирования может владелец или администратор группы.",
        for_update=for_update,
    )


@router.message(F.text.regexp(r"(?i)^защита редактирования (?:вкл|выкл)$"))
async def edit_toggle(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    enabled = (message.text or "").casefold().endswith("вкл")
    group.settings.edit_protection_enabled = enabled
    await session.commit()
    await message.reply(
        f"{'✅' if enabled else '❌'} Проверка отредактированных сообщений "
        f"{'включена' if enabled else 'выключена'}."
    )


@router.message(F.text.regexp(r"(?i)^защита редактирования статус$"))
async def edit_status(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session)
    if not group:
        return
    settings = group.settings
    await message.reply(
        "<b>Защита редактирования</b>\n"
        f"Статус: {'✅ включена' if settings.edit_protection_enabled else '❌ выключена'}\n"
        f"Проверять изменения в течение: {human_duration(settings.edit_protection_window_seconds)}"
    )


@router.message(F.text.regexp(r"(?i)^защита редактирования окно \S+$"))
async def edit_window(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    raw = (message.text or "").split()[-1]
    seconds = parse_duration(raw)
    if seconds is None:
        await message.reply("Не удалось определить срок. Пример: <code>защита редактирования окно 2д</code>.")
        return
    try:
        seconds = normalize_edit_window(seconds)
    except ValueError as error:
        await message.reply(str(error))
        return
    group.settings.edit_protection_window_seconds = seconds
    await session.commit()
    await message.reply(f"✅ Отредактированные сообщения проверяются в течение {human_duration(seconds)}.")
