from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
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
        denial_text="Изменять карантин может владелец или администратор группы.",
        for_update=for_update,
    )


@router.message(F.text.regexp(r"(?i)^карантин вкл(?: \S+)?$"))
async def quarantine_enable(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    parts = (message.text or "").split()
    duration = parse_duration(parts[2]) if len(parts) > 2 else group.settings.newcomer_quarantine_seconds
    if duration is None or not 300 <= duration <= 2_592_000:
        await message.reply("Укажите срок от 5 минут до 30 дней. Например: <code>карантин вкл 24ч</code>.")
        return
    group.settings.newcomer_quarantine_enabled = True
    group.settings.newcomer_quarantine_seconds = duration
    await session.commit()
    await message.reply(f"✅ Карантин новичков включён на {human_duration(duration)}")


@router.message(F.text.casefold() == "карантин выкл")
async def quarantine_disable(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    group.settings.newcomer_quarantine_enabled = False
    await session.commit()
    await message.reply("❌ Карантин новичков выключен.")


@router.message(F.text.casefold() == "карантин статус")
async def quarantine_status(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session)
    if not group:
        return
    s = group.settings
    await message.reply(
        "<b>Карантин новичков</b>\n"
        f"Статус: {'✅ включён' if s.newcomer_quarantine_enabled else '❌ выключен'}\n"
        f"Срок: {human_duration(s.newcomer_quarantine_seconds)}\n"
        f"Ссылки: {'🚫 запрещены' if s.newcomer_quarantine_block_links else '✅ разрешены'}\n"
        f"Медиа: {'🚫 запрещены' if s.newcomer_quarantine_block_media else '✅ разрешены'}\n"
        f"Пересылки: {'🚫 запрещены' if s.newcomer_quarantine_block_forwards else '✅ разрешены'}"
    )


@router.message(F.text.regexp(r"(?i)^карантин (ссылки|медиа|пересылки) (вкл|выкл)$"))
async def quarantine_rule(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    _, kind, state = (message.text or "").casefold().split()
    enabled = state == "вкл"
    field = {
        "ссылки": "newcomer_quarantine_block_links",
        "медиа": "newcomer_quarantine_block_media",
        "пересылки": "newcomer_quarantine_block_forwards",
    }[kind]
    setattr(group.settings, field, enabled)
    await session.commit()
    await message.reply(f"✅ Правило карантина «{kind}» {'включено' if enabled else 'выключено'}.")
