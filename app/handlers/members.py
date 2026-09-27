from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, ChatMemberUpdated, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.safety import should_force_verification
from app.db.models import Group, NewMemberRecord, RequiredChannel, TrustedUser
from app.handlers.deferred_bans import enforce_pending_ban_on_join
from app.services.captcha_state import claim_verified_captcha
from app.services.captcha_verification import (
    VERIFICATION_MUTED,
    VERIFICATION_RETRY,
    VERIFICATION_UNAVAILABLE,
    finalize_verified_captcha,
)
from app.services.deleted_accounts import track_group_member
from app.services.public_identity import public_user_token
from app.services.required_resources import resolve_channel_url

router = Router(name=__name__)
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
settings = get_settings()

RESTRICTED = ChatPermissions(can_send_messages=False)


async def required_channels(session: AsyncSession, group_id: int) -> list[str]:
    return list((await session.scalars(
        select(RequiredChannel.channel_username).where(
            RequiredChannel.group_id == group_id,
            RequiredChannel.active.is_(True),
        ).order_by(RequiredChannel.channel_username)
    )).all())


async def verification_keyboard(chat_id: int, user_id: int, channels: list[str], *, bot: Bot | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        if bot is not None:
            url = await resolve_channel_url(bot, channel)
        else:
            username = channel.lstrip("@")
            url = f"https://t.me/{username}" if not username.lstrip("-").isdigit() else None
        if url is None:
            display = channel if channel.startswith("@") else f"{channel}"
            rows.append([InlineKeyboardButton(text=f"📢 {display}", callback_data=f"noop:ch:{channel}")])
        else:
            rows.append([InlineKeyboardButton(text=f"📢 {channel}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Проверить", callback_data=f"verify:{chat_id}:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def is_subscribed(bot: Bot, user_id: int, channels: list[str]) -> tuple[bool, str | None]:
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel, user_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            return False, f"Бот не может проверить канал {channel}. Добавьте бота администратором канала."
        if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
            return False, None
    return True, None


@router.message(F.new_chat_members)
async def welcome(message: Message, bot: Bot, session: AsyncSession, redis: Redis) -> None:
    banned_user_ids = await enforce_pending_ban_on_join(message, bot, session)
    group = await session.scalar(
        select(Group).where(
            Group.telegram_chat_id == message.chat.id,
            Group.is_active.is_(True),
        ).with_for_update()
    )
    if not group:
        return
    channels = await required_channels(session, group.id)
    raid_key = f"joins:{message.chat.id}"
    join_count = await redis.incrby(raid_key, len([m for m in message.new_chat_members if not m.is_bot]))
    if join_count == len([m for m in message.new_chat_members if not m.is_bot]):
        await redis.expire(raid_key, group.settings.anti_raid_window_seconds)
    raid_mode = should_force_verification(
        join_count,
        group.settings.anti_raid_limit,
        group.settings.anti_raid_enabled,
    )
    for member in message.new_chat_members:
        if member.is_bot or member.id in banned_user_ids:
            continue
        await track_group_member(session, group.id, member, present=True, checked=True)
        record = await session.scalar(select(NewMemberRecord).where(
            NewMemberRecord.group_id == group.id,
            NewMemberRecord.user_telegram_id == member.id,
        ))
        if record is None:
            session.add(NewMemberRecord(group_id=group.id, user_telegram_id=member.id, source="join"))
        else:
            record.joined_at = datetime.now(timezone.utc)
            record.source = "rejoin"
        trusted = await session.scalar(select(TrustedUser.id).where(
            TrustedUser.group_id == group.id,
            TrustedUser.user_telegram_id == member.id,
        ))
        needs_verification = trusted is None and (group.settings.captcha_enabled or bool(channels) or raid_mode)
        if needs_verification:
            try:
                await bot.restrict_chat_member(message.chat.id, member.id, permissions=RESTRICTED)
            except (TelegramBadRequest, TelegramForbiddenError):
                await message.answer("⚠️ Не удалось ограничить нового участника. Проверьте права бота.")
                continue
            deadline = datetime.now(timezone.utc) + timedelta(seconds=settings.captcha_timeout_seconds)
            await redis.set(
                f"captcha:{message.chat.id}:{member.id}",
                str(int(deadline.timestamp())),
                ex=settings.captcha_timeout_seconds + 120,
            )
            text = f"{public_user_token(member.id)}, подтвердите вход в течение {settings.captcha_timeout_seconds} сек."
            if channels:
                text += "\nПодпишитесь на обязательные каналы и нажмите «Проверить»."
            elif group.settings.captcha_enabled:
                text += "\nНажмите кнопку, чтобы подтвердить, что вы не робот."
            await message.answer(text, reply_markup=await verification_keyboard(message.chat.id, member.id, channels, bot=bot))
        elif group.settings.welcome_enabled:
            await message.answer(
                group.settings.welcome_text
                .replace("{имя}", public_user_token(member.id))
                .replace("{группа}", message.chat.title or "группа")
            )
    await session.commit()


@router.callback_query(F.data.startswith("verify:"))
async def verification_callback(callback, bot: Bot, session: AsyncSession, redis: Redis) -> None:
    _, raw_chat_id, raw_user_id = callback.data.split(":")
    chat_id, user_id = int(raw_chat_id), int(raw_user_id)
    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка не для вас.", show_alert=True)
        return
    group = await session.scalar(select(Group).where(Group.telegram_chat_id == chat_id))
    if not group:
        await callback.answer("Группа не подключена.", show_alert=True)
        return
    channels = await required_channels(session, group.id)
    subscribed, error = await is_subscribed(bot, user_id, channels)
    if error:
        await callback.answer(error, show_alert=True)
        return
    if not subscribed:
        await callback.answer("Подписка найдена не на всех каналах.", show_alert=True)
        return

    captcha_key = f"captcha:{chat_id}:{user_id}"
    claim_result = await claim_verified_captcha(
        redis,
        captcha_key,
        int(datetime.now(timezone.utc).timestamp()),
    )
    if claim_result <= 0:
        await callback.answer("Время проверки истекло. Дождитесь повторного входа в группу.", show_alert=True)
        return

    result = await finalize_verified_captcha(
        bot,
        redis,
        session,
        key=captcha_key,
        chat_id=chat_id,
        user_id=user_id,
    )
    await session.commit()

    if result == VERIFICATION_RETRY:
        await callback.answer("Не удалось снять ограничения. Mimoru повторит безопасно.", show_alert=True)
        return
    if result == VERIFICATION_UNAVAILABLE:
        await callback.answer("Группа больше не обслуживается Mimoru.", show_alert=True)
        return

    if result == VERIFICATION_MUTED:
        await callback.message.edit_text("✅ Проверка пройдена. Активный мут сохранён.")
    else:
        await callback.message.edit_text("✅ Проверка пройдена.")
    if group.settings.welcome_enabled:
        welcome_text = (
            group.settings.welcome_text
            .replace("{имя}", public_user_token(callback.from_user.id))
            .replace("{группа}", group.title or "группа")
        )
        await bot.send_message(chat_id, welcome_text)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^noop(?::|$)"))
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer("Ссылка недоступна. Обратитесь к администратору группы.", show_alert=True)


@router.chat_member()
async def track_chat_member_update(event: ChatMemberUpdated, session: AsyncSession) -> None:
    group = await session.scalar(select(Group).where(
        Group.telegram_chat_id == event.chat.id,
        Group.is_active.is_(True),
    ))
    if group is None:
        return
    target = event.new_chat_member.user
    present = event.new_chat_member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    await track_group_member(session, group.id, target, present=present, checked=True)
