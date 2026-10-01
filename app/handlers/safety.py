from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, TrustedUser
from app.services.owner_management import managed_group_for_message
from app.services.public_identity import public_user_token

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
        denial_text="Изменять эти настройки может только владелец группы.",
        for_update=for_update,
    )


def replied_user_id(message: Message) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return None


@router.message(F.text.casefold().in_({"доверять", "в белый список", "добавить в белый список"}))
async def trust_user(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    target_id = replied_user_id(message)
    if not group or not message.from_user or target_id is None:
        if group:
            await message.reply("Ответьте этой командой на сообщение пользователя.")
        return
    session.add(TrustedUser(group_id=group.id, user_telegram_id=target_id, added_by_telegram_id=message.from_user.id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await message.reply("Пользователь уже находится в белом списке.")
        return
    await message.reply(f"✅ Пользователь {public_user_token(target_id)} добавлен в белый список.")


@router.message(F.text.casefold().in_({"не доверять", "из белого списка", "убрать из белого списка"}))
async def untrust_user(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    target_id = replied_user_id(message)
    if not group or target_id is None:
        if group:
            await message.reply("Ответьте этой командой на сообщение пользователя.")
        return
    row = await session.scalar(select(TrustedUser).where(TrustedUser.group_id == group.id, TrustedUser.user_telegram_id == target_id))
    if row is None:
        await message.reply("Пользователя нет в белом списке.")
        return
    await session.delete(row)
    await session.commit()
    await message.reply(f"✅ Пользователь {public_user_token(target_id)} удалён из белого списка.")


@router.message(F.text.casefold().in_({"белый список", "доверенные пользователи"}))
async def trusted_users(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session)
    if not group:
        return
    rows = (await session.scalars(select(TrustedUser.user_telegram_id).where(TrustedUser.group_id == group.id).order_by(TrustedUser.created_at))).all()
    text = "\n".join(f"• {public_user_token(user_id)}" for user_id in rows) if rows else "Список пуст."
    await message.reply("Доверенные пользователи\n" + text)


@router.message(F.text.regexp(r"(?i)^антирейд (вкл|выкл)$"))
async def toggle_antiraid(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    group.settings.anti_raid_enabled = message.text.casefold().endswith("вкл")
    await session.commit()
    await message.reply("✅ Антирейд включён." if group.settings.anti_raid_enabled else "❌ Антирейд выключен.")


@router.message(F.text.regexp(r"(?i)^антирейд \d+ за \d+[см]$"))
async def configure_antiraid(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    parts = message.text.casefold().split()
    limit = int(parts[1])
    raw = parts[3]
    amount, unit = int(raw[:-1]), raw[-1]
    window = amount * (1 if unit == "с" else 60)
    if not 2 <= limit <= 100 or not 10 <= window <= 3600:
        await message.reply("Допустимо: 2–100 входов за период от 10 секунд до 60 минут.")
        return
    group.settings.anti_raid_limit = limit
    group.settings.anti_raid_window_seconds = window
    await session.commit()
    await message.reply(f"✅ Антирейд: более {limit} входов за {window} сек. включает усиленную проверку.")


@router.message(F.text.regexp(r"(?i)^преды срок (никогда|\d+д)$"))
async def warning_expiry(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    value = message.text.casefold().split()[-1]
    days = 0 if value == "никогда" else int(value[:-1])
    if days != 0 and not 1 <= days <= 3650:
        await message.reply("Укажите срок от 1 до 3650 дней либо «никогда».")
        return
    group.settings.warning_expire_days = days
    await session.commit()
    await message.reply("✅ Предупреждения не будут истекать." if days == 0 else f"✅ Предупреждения действуют {days} дн.")
