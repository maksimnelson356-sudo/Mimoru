import json
import secrets

import structlog
from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Filter
from aiogram.types import Message
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ForbiddenWord,
    GroupModerator,
    ModerationLog,
    Punishment,
    RequiredChannel,
    Warning,
)
from app.keyboards.panel import moderation_duration_picker, moderation_reason_picker
from app.services.access import can_manage_group, can_moderate
from app.services.moderation import execute
from app.services.moderation_reasons import active_reasons
from app.services.permissions import target_is_protected
from app.services.plans import plan_limit
from app.services.public_identity import public_user_token
from app.services.repositories import get_or_create_group
from app.utils.commands import ParsedCommand, parse_command


class TextCommandFilter(Filter):
    async def __call__(self, message: Message) -> dict | bool:
        if not message.text:
            return False
        command = parse_command(message.text)
        return {"command": command} if command else False


router = Router(name=__name__)
log = structlog.get_logger(__name__)
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


@router.message(F.text.regexp(r"(?i)^антифлуд (вкл|выкл)$"))
async def toggle_antiflood(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Изменять настройки может только владелец группы.")
        return
    group.settings.antiflood_enabled = message.text.lower().endswith("вкл")
    await session.commit()
    await message.reply(
        "✅ Антифлуд включён."
        if group.settings.antiflood_enabled
        else "❌ Антифлуд выключен."
    )


@router.message(F.text.regexp(r"(?i)^ссылки (вкл|выкл)$"))
async def toggle_links(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Изменять настройки может только владелец группы.")
        return
    group.settings.links_enabled = message.text.lower().endswith("вкл")
    await session.commit()
    await message.reply(
        "✅ Ссылки разрешены."
        if group.settings.links_enabled
        else "🚫 Ссылки запрещены."
    )


@router.message(F.text.regexp(r"(?i)^добавить слово .+"))
async def add_word(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Изменять настройки может только владелец группы.")
        return
    current_words = int(
        await session.scalar(
            select(func.count())
            .select_from(ForbiddenWord)
            .where(ForbiddenWord.group_id == group.id)
        )
        or 0
    )
    if current_words >= plan_limit(group, "words"):
        await message.reply("Достигнут лимит запрещённых слов текущего тарифа.")
        return
    word = message.text.split(maxsplit=2)[2].lower().strip()
    session.add(ForbiddenWord(group_id=group.id, word=word))
    try:
        await session.commit()
        await message.reply(f"✅ Запрещённое слово добавлено: {word}")
    except IntegrityError as exc:
        await session.rollback()
        log.warning("word_integrity_error", error=str(exc))
        await message.reply("Это слово уже есть в списке.")
    except Exception as exc:
        await session.rollback()
        log.exception("word_save_failed", error=str(exc))
        raise


@router.message(F.text.regexp(r"(?i)^добавить подписку @\w+$"))
async def add_required_channel(
    message: Message, bot: Bot, session: AsyncSession
) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Изменять настройки может только владелец группы.")
        return
    current_channels = int(
        await session.scalar(
            select(func.count())
            .select_from(RequiredChannel)
            .where(
                RequiredChannel.group_id == group.id,
                RequiredChannel.active.is_(True),
            )
        )
        or 0
    )
    if current_channels >= plan_limit(group, "channels"):
        await message.reply("Достигнут лимит обязательных каналов текущего тарифа.")
        return
    username = message.text.split()[-1].lower()
    session.add(RequiredChannel(group_id=group.id, channel_username=username))
    try:
        await session.commit()
        await message.reply(f"✅ Канал {username} добавлен.")
    except IntegrityError as exc:
        await session.rollback()
        log.warning("channel_integrity_error", error=str(exc))
        await message.reply("Канал уже добавлен.")
    except Exception as exc:
        await session.rollback()
        log.exception("channel_save_failed", error=str(exc))
        raise


@router.message(F.text.regexp(r"(?i)^капча (вкл|выкл)$"))
async def toggle_captcha(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Изменять настройки может только владелец группы.")
        return
    group.settings.captcha_enabled = message.text.lower().endswith("вкл")
    await session.commit()
    await message.reply(
        "✅ Капча включена."
        if group.settings.captcha_enabled
        else "❌ Капча выключена."
    )


@router.message(F.text.regexp(r"(?i)^приветствие (вкл|выкл)$"))
async def toggle_welcome(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Изменять настройки может только владелец группы.")
        return
    group.settings.welcome_enabled = message.text.lower().endswith("вкл")
    await session.commit()
    await message.reply(
        "✅ Приветствие включено."
        if group.settings.welcome_enabled
        else "❌ Приветствие выключено."
    )


@router.message(F.text.regexp(r"(?i)^удалить слово .+"))
async def remove_word(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Изменять настройки может только владелец группы.")
        return
    word = message.text.split(maxsplit=2)[2].lower().strip()
    item = await session.scalar(
        select(ForbiddenWord).where(
            ForbiddenWord.group_id == group.id, ForbiddenWord.word == word
        )
    )
    if not item:
        await message.reply("Такого слова нет в списке.")
        return
    await session.delete(item)
    await session.commit()
    await message.reply(f"✅ Слово удалено: {word}")


@router.message(F.text.regexp(r"(?i)^список слов$"))
async def list_words(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Изменять настройки может только владелец группы.")
        return
    words = (
        await session.scalars(
            select(ForbiddenWord.word)
            .where(ForbiddenWord.group_id == group.id)
            .order_by(ForbiddenWord.word)
        )
    ).all()
    await message.reply(
        "Запрещённые слова:\n"
        + ("\n".join(f"• {word}" for word in words) if words else "Список пуст.")
    )


@router.message(F.text.regexp(r"(?i)^удалить подписку @\w+$"))
async def remove_required_channel(
    message: Message, bot: Bot, session: AsyncSession
) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Изменять настройки может только владелец группы.")
        return
    username = message.text.split()[-1].lower()
    item = await session.scalar(
        select(RequiredChannel).where(
            RequiredChannel.group_id == group.id,
            RequiredChannel.channel_username == username,
        )
    )
    if not item:
        await message.reply("Такого канала нет в списке.")
        return
    await session.delete(item)
    await session.commit()
    await message.reply(f"✅ Канал {username} удалён.")


@router.message(F.text.regexp(r"(?i)^список подписок$"))
async def list_required_channels(
    message: Message, bot: Bot, session: AsyncSession
) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Изменять настройки может только владелец группы.")
        return
    channels = (
        await session.scalars(
            select(RequiredChannel.channel_username)
            .where(
                RequiredChannel.group_id == group.id, RequiredChannel.active.is_(True)
            )
            .order_by(RequiredChannel.channel_username)
        )
    ).all()
    await message.reply(
        "Обязательные каналы:\n"
        + (
            "\n".join(f"• {channel}" for channel in channels)
            if channels
            else "Список пуст."
        )
    )


@router.message(
    F.text.regexp(r"(?i)^(назначить|добавить) (старшего|модератора|помощника)$")
)
async def assign_moderator(message: Message, bot: Bot, session: AsyncSession) -> None:
    if (
        not message.from_user
        or not message.reply_to_message
        or not message.reply_to_message.from_user
    ):
        await message.reply("Ответьте этой командой на сообщение пользователя.")
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Назначать роли может только владелец группы.")
        return
    target = message.reply_to_message.from_user
    if target.id == group.owner_telegram_id:
        await message.reply("Владелец уже имеет все права.")
        return
    role_word = message.text.casefold().split()[-1]
    role = {"старшего": "senior", "модератора": "moderator", "помощника": "helper"}[
        role_word
    ]
    item = await session.scalar(
        select(GroupModerator).where(
            GroupModerator.group_id == group.id,
            GroupModerator.user_telegram_id == target.id,
        )
    )
    if item:
        item.role, item.active, item.assigned_by_telegram_id = (
            role,
            True,
            message.from_user.id,
        )
    else:
        session.add(
            GroupModerator(
                group_id=group.id,
                user_telegram_id=target.id,
                role=role,
                permissions={},
                active=True,
                assigned_by_telegram_id=message.from_user.id,
            )
        )
    await session.commit()
    await message.reply(f"✅ {target.full_name} назначен: {role}.")


@router.message(F.text.regexp(r"(?i)^(снять|удалить) (модератора|роль)$"))
async def remove_moderator(message: Message, bot: Bot, session: AsyncSession) -> None:
    if (
        not message.from_user
        or not message.reply_to_message
        or not message.reply_to_message.from_user
    ):
        await message.reply("Ответьте этой командой на сообщение пользователя.")
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Снимать роли может только владелец группы.")
        return
    target = message.reply_to_message.from_user
    item = await session.scalar(
        select(GroupModerator).where(
            GroupModerator.group_id == group.id,
            GroupModerator.user_telegram_id == target.id,
            GroupModerator.active.is_(True),
        )
    )
    if not item:
        await message.reply("У пользователя нет внутренней роли.")
        return
    item.active = False
    await session.commit()
    await message.reply(f"✅ Роль пользователя {target.full_name} снята.")


@router.message(F.text.casefold().in_({"модераторы", "роли"}))
async def list_moderators(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_manage_group(bot, group, message.from_user.id):
        await message.reply("Список ролей доступен владельцу группы.")
        return
    items = (
        await session.scalars(
            select(GroupModerator)
            .where(GroupModerator.group_id == group.id, GroupModerator.active.is_(True))
            .order_by(GroupModerator.role, GroupModerator.user_telegram_id)
        )
    ).all()
    labels = {"senior": "старший", "moderator": "модератор", "helper": "помощник"}
    lines = [
        f"• <code>{item.user_telegram_id}</code> — {labels.get(item.role, item.role)}"
        for item in items
    ]
    await message.reply(
        "<b>Внутренние роли</b>\n" + ("\n".join(lines) if lines else "Пока никого нет.")
    )


@router.message(F.text.casefold().in_({"удалить", "стереть", "удали"}))
async def delete_message_command(
    message: Message, bot: Bot, session: AsyncSession
) -> None:
    if not message.from_user or not message.reply_to_message:
        await message.reply("Ответьте командой на сообщение, которое нужно удалить.")
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_moderate(bot, session, group, message.from_user.id, "delete"):
        await message.reply("У вас нет права удалять сообщения.")
        return
    try:
        await message.reply_to_message.delete()
        await message.delete()
        session.add(
            ModerationLog(
                group_id=group.id,
                actor_telegram_id=message.from_user.id,
                target_telegram_id=(
                    message.reply_to_message.from_user.id
                    if message.reply_to_message.from_user
                    else None
                ),
                action="delete_message",
                reason="Удалено модератором",
                metadata_json={"message_id": message.reply_to_message.message_id},
            )
        )
        await session.commit()
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.reply("Не удалось удалить сообщение. Проверьте права бота.")


@router.message(TextCommandFilter())
async def moderation_command(
    message: Message,
    command: ParsedCommand,
    bot: Bot,
    session: AsyncSession,
    redis: Redis,
) -> None:
    if not message.from_user:
        return
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if not await can_moderate(
        bot, session, group, message.from_user.id, command.action
    ):
        await message.reply("У вас нет права выполнять эту команду.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Ответьте командой на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        await message.reply("Нельзя применить эту команду к себе.")
        return
    if target.id == group.owner_telegram_id or await target_is_protected(
        bot, message.chat.id, target.id
    ):
        await message.reply(
            "Нельзя применить действие к владельцу или администратору Telegram."
        )
        return
    if command.action == "info":
        warnings = int(
            await session.scalar(
                select(func.count())
                .select_from(Warning)
                .where(
                    Warning.group_id == group.id,
                    Warning.user_telegram_id == target.id,
                    Warning.active.is_(True),
                )
            )
            or 0
        )
        active = (
            await session.scalars(
                select(Punishment)
                .where(
                    Punishment.group_id == group.id,
                    Punishment.user_telegram_id == target.id,
                    Punishment.active.is_(True),
                )
                .order_by(Punishment.created_at.desc())
            )
        ).all()
        punishments = ", ".join(x.kind for x in active) or "нет"
        await message.reply(
            f"<b>{target.full_name}</b>\n"
            f"Telegram ID: <code>{target.id}</code>\n"
            f"Предупреждений: {warnings}\n"
            f"Активные наказания: {punishments}"
        )
        return
    if command.action == "history":
        logs = (
            await session.scalars(
                select(ModerationLog)
                .where(
                    ModerationLog.group_id == group.id,
                    ModerationLog.target_telegram_id == target.id,
                )
                .order_by(ModerationLog.created_at.desc())
                .limit(20)
            )
        ).all()
        lines = [
            f"• {x.created_at:%d.%m %H:%M} — {x.action}: {x.reason or 'без причины'}"
            for x in logs
        ]
        await message.reply(
            "<b>История пользователя</b>\n"
            + ("\n".join(lines) if lines else "История пуста.")
        )
        return

    if command.action in {"warn", "mute", "kick", "ban"}:
        token = secrets.token_hex(5)
        payload = {
            "group_id": group.id,
            "chat_id": message.chat.id,
            "target_id": target.id,
            "target_name": public_user_token(target.id),
            "moderator_id": message.from_user.id,
            "moderator_name": public_user_token(message.from_user.id),
            "action": command.action,
            "duration": command.duration,
            "warnings_limit": group.settings.warnings_limit,
            "default_mute": group.settings.default_mute_seconds,
            "origin": "group",
            "actor_role": (
                "owner" if message.from_user.id == group.owner_telegram_id else "admin"
            ),
        }
        await redis.setex(
            f"mimoru:modpending:{token}", 600, json.dumps(payload, ensure_ascii=False)
        )
        if command.action == "mute" and command.duration is None:
            await message.reply(
                f"🔇 На сколько ограничить <b>{public_user_token(target.id)}</b>?",
                reply_markup=moderation_duration_picker(token),
            )
            return
        reasons = await active_reasons(session, group.id, command.action)
        await session.commit()
        if not reasons:
            await redis.delete(f"mimoru:modpending:{token}")
            await message.reply(
                "Для этого действия нет активных причин. Владелец группы может добавить их в панели Mimoru."
            )
            return
        labels = {
            "warn": "предупреждения",
            "mute": "мута",
            "kick": "исключения",
            "ban": "блокировки",
        }
        await message.reply(
            f"📌 Выберите причину {labels[command.action]} для <b>{public_user_token(target.id)}</b>.",
            reply_markup=moderation_reason_picker(token, reasons),
        )
        return
    try:
        result = await execute(
            bot=bot,
            session=session,
            chat_id=message.chat.id,
            group_id=group.id,
            target_id=target.id,
            moderator_id=message.from_user.id,
            action=command.action,
            duration=command.duration,
            reason=command.reason,
            warnings_limit=group.settings.warnings_limit,
            default_mute=group.settings.default_mute_seconds,
            target_name=public_user_token(target.id),
            moderator_name=public_user_token(message.from_user.id),
            actor_role=(
                "owner" if message.from_user.id == group.owner_telegram_id else "admin"
            ),
        )
        await session.commit()
        await message.reply(result)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await session.rollback()
        await message.reply(f"Не удалось выполнить действие: {exc.message}")
