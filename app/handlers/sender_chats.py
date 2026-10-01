from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AllowedSenderChat, Group
from app.services.owner_management import managed_group_for_message

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
        denial_text="Изменять защиту сообщений от каналов может владелец или администратор группы.",
        for_update=for_update,
    )


@router.message(F.text.regexp(r"(?i)^каналы-отправители (?:вкл|выкл)$"))
async def toggle(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    enabled = (message.text or "").casefold().endswith("вкл")
    group.settings.sender_chat_filter_enabled = enabled
    await session.commit()
    await message.reply(f"{'✅' if enabled else '❌'} Фильтр сообщений от каналов {'включён' if enabled else 'выключен'}.")


@router.message(F.text.regexp(r"(?i)^каналы-отправители статус$"))
async def status(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session)
    if not group:
        return
    count = len((await session.scalars(select(AllowedSenderChat.id).where(AllowedSenderChat.group_id == group.id))).all())
    await message.reply(f"<b>Сообщения от каналов</b>\nСтатус: {'✅' if group.settings.sender_chat_filter_enabled else '❌'}\nРазрешённых каналов: {count}\nОт имени группы: {'разрешено' if group.settings.allow_group_sender_identity else 'запрещено'}")


@router.message(F.text.regexp(r"(?i)^разрешить канал-отправитель$"))
async def allow_sender(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    target = message.reply_to_message
    if not target or not target.sender_chat:
        await message.reply("Ответьте этой командой на сообщение, отправленное от имени канала.")
        return
    sc = target.sender_chat
    row = await session.scalar(select(AllowedSenderChat).where(AllowedSenderChat.group_id == group.id, AllowedSenderChat.sender_chat_id == sc.id))
    if row is None:
        session.add(AllowedSenderChat(group_id=group.id, sender_chat_id=sc.id, title=sc.title, username=sc.username, added_by_telegram_id=message.from_user.id))
        await session.commit()
    await message.reply(f"✅ Канал «{sc.title}» разрешён.")


@router.message(F.text.regexp(r"(?i)^запретить канал-отправитель$"))
async def deny_sender(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    target = message.reply_to_message
    if not target or not target.sender_chat:
        await message.reply("Ответьте этой командой на сообщение, отправленное от имени канала.")
        return
    await session.execute(delete(AllowedSenderChat).where(AllowedSenderChat.group_id == group.id, AllowedSenderChat.sender_chat_id == target.sender_chat.id))
    await session.commit()
    await message.reply("✅ Канал удалён из разрешённого списка.")


@router.message(F.text.regexp(r"(?i)^список каналов-отправителей$"))
async def list_senders(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session)
    if not group:
        return
    rows = (await session.scalars(select(AllowedSenderChat).where(AllowedSenderChat.group_id == group.id).order_by(AllowedSenderChat.id))).all()
    if not rows:
        await message.reply("Разрешённых каналов-отправителей пока нет.")
        return
    await message.reply("<b>Разрешённые каналы-отправители</b>\n" + "\n".join(f"• {x.title or x.sender_chat_id} (<code>{x.sender_chat_id}</code>)" for x in rows))
