from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, ModerationLog
from app.services.access import can_manage_group
from app.services.repositories import get_or_create_group

router = Router(name=__name__)
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


async def owner_group(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> Group | None:
    if not message.from_user:
        return None
    if for_update:
        # Do not first load the Group unlocked and then re-select it FOR UPDATE in
        # the same ORM Session. SQLAlchemy's identity map may otherwise retain the
        # earlier owner attributes. Resolve the mutation winner directly under lock.
        group = await session.scalar(
            select(Group)
            .where(
                Group.telegram_chat_id == message.chat.id,
                Group.is_active.is_(True),
            )
            .with_for_update()
        )
        if group is None:
            return None
    else:
        group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id, session):
        await message.reply("Настраивать журнал может владелец или администратор группы.")
        return None
    return group


@router.message(F.text.casefold() == "журнал сюда")
async def audit_here(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await owner_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    group.settings.audit_chat_id = message.chat.id
    group.settings.audit_topic_id = message.message_thread_id
    session.add(ModerationLog(
        group_id=group.id,
        actor_telegram_id=message.from_user.id,
        target_telegram_id=None,
        action="audit_destination_set",
        metadata_json={"chat_id": message.chat.id, "topic_id": message.message_thread_id},
    ))
    await session.commit()
    await message.reply(
        "✅ События модерации будут отправляться сюда"
        + (" в эту тему." if message.message_thread_id else ".")
    )


@router.message(F.text.casefold() == "журнал выкл")
async def audit_off(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await owner_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    group.settings.audit_chat_id = None
    group.settings.audit_topic_id = None
    await session.commit()
    await message.reply("Журнал в отдельный чат отключён. Записи продолжают храниться в базе.")


@router.message(F.text.casefold() == "журнал статус")
async def audit_status(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await owner_group(message, bot, session)
    if not group:
        return
    if group.settings.audit_chat_id is None:
        await message.reply("Отдельный чат журнала не настроен.")
        return
    topic = f"\nТема: {group.settings.audit_topic_id}" if group.settings.audit_topic_id else ""
    await message.reply(f"Чат журнала: {group.settings.audit_chat_id}{topic}")


@router.message(F.text.casefold() == "журнал тест")
async def audit_test(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await owner_group(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    if group.settings.audit_chat_id is None:
        await session.commit()
        await message.reply("Сначала отправьте команду «журнал сюда» в нужном чате или теме.")
        return

    chat_id = group.settings.audit_chat_id
    topic_id = group.settings.audit_topic_id
    try:
        await bot.send_message(
            chat_id,
            "✅ Тест журнала модерации",
            message_thread_id=topic_id,
        )
    except Exception as error:
        await session.rollback()
        await message.reply(f"Не удалось отправить тест: {error}")
        return

    # Release the Group serialization lock before the user-facing acknowledgement.
    await session.commit()
    await message.reply("Тестовое сообщение отправлено.")
