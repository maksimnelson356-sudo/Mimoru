from datetime import datetime, timedelta, timezone

import structlog
from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AutoResponse, Complaint, DailyStat, Group, ModerationLog, User
from app.services.access import can_manage_group
from app.services.complaints import (
    COMPLAINT_DUPLICATE_TEXT,
    complaint_exists_for_message,
)
from app.services.message_ttl import (
    COMPLAINT_MESSAGE_TTL_SECONDS,
    bound_redis,
    schedule_message_deletion,
)
from app.services.repositories import get_or_create_group
from app.services.ui import clean_ui_text

router = Router(name=__name__)
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


async def managed(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> Group | None:
    if not message.from_user:
        return None
    group = await get_or_create_group(session, message.chat, message.from_user.id)
    if for_update:
        locked = await session.scalar(
            select(Group).where(
                Group.id == group.id,
                Group.is_active.is_(True),
            ).with_for_update()
        )
        if locked is None:
            await message.reply("Группа больше не обслуживается.")
            return None
        group = locked
    if not await can_manage_group(bot, group, message.from_user.id, session):
        await message.reply("Изменять настройки может владелец или администратор группы.")
        return None
    return group


@router.message(F.text.regexp(r"(?i)^антифлуд \d+ за \d+[смч]$"))
async def antiflood_config(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    parts = message.text.casefold().split()
    limit = int(parts[1])
    raw = parts[3]
    number = int(raw[:-1])
    unit = raw[-1]
    seconds = number * {"с": 1, "м": 60, "ч": 3600}[unit]
    if not 2 <= limit <= 100 or not 1 <= seconds <= 3600:
        await message.reply("Допустимо: 2–100 сообщений, интервал от 1 секунды до 1 часа.")
        return
    group.settings.antiflood_limit = limit
    group.settings.antiflood_window_seconds = seconds
    await session.commit()
    await message.reply(f"✅ Антифлуд: {limit} сообщений за {number}{unit}.")


@router.message(F.text.regexp(r"(?i)^(повторы|капс|голосовые|стикеры|пересылки) (вкл|выкл)$"))
async def toggle_extended(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    name, state = message.text.casefold().split()
    enabled = state == "вкл"
    attrs = {
        "повторы": "repeats_enabled",
        "капс": "caps_enabled",
        "голосовые": "voices_allowed",
        "стикеры": "stickers_allowed",
        "пересылки": "forwards_allowed",
    }
    setattr(group.settings, attrs[name], enabled)
    await session.commit()
    await message.reply(f"✅ Настройка «{name}» изменена: {'включено' if enabled else 'выключено'}.")


@router.message(F.text.regexp(r"(?i)^капс лимит \d{1,3}$"))
async def caps_limit(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    value = int(message.text.split()[-1])
    if not 10 <= value <= 100:
        await message.reply("Укажите процент от 10 до 100.")
        return
    group.settings.caps_percent = value
    await session.commit()
    await message.reply(f"✅ Лимит капса: {value}%.")


# Подсказку из справки («изменить правила текст») можно скопировать буквально и
# сохранить слово «текст» вместо настоящих правил — такой ввод в настройки не идёт.
_PLACEHOLDER_WORDS = {"текст", "text", "…", "..."}


def _is_placeholder_text(value: str) -> bool:
    cleaned = value.strip().strip("«»<>\"'").casefold()
    if cleaned in _PLACEHOLDER_WORDS:
        return True
    bare = cleaned.strip(".,;: ")
    return bool(bare) and bare in _PLACEHOLDER_WORDS


@router.message(F.text.regexp(r"(?is)^изменить приветствие\s+.+"))
async def welcome_text(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    text = message.text.split(maxsplit=2)[2].strip()
    if not text:
        await message.reply("Текст приветствия не может быть пустым.")
        return
    if _is_placeholder_text(text):
        await message.reply(
            "Это слово из подсказки, а не текст приветствия. Напишите сам текст после команды."
        )
        return
    group.settings.welcome_text = clean_ui_text(text[:2000])
    await session.commit()
    await message.reply("✅ Приветствие сохранено. Доступные переменные: {имя} и {группа}.")


def rules_input_text(message: Message) -> str:
    """Текст правил из самой команды либо из сообщения, на которое она отвечает."""
    parts = (message.text or "").split(maxsplit=2)
    inline_text = parts[2].strip() if len(parts) > 2 else ""
    quoted_text = ""
    if message.reply_to_message is not None:
        quoted_text = (
            message.reply_to_message.text or message.reply_to_message.caption or ""
        ).strip()
    return inline_text or quoted_text


@router.message(F.text.regexp(r"(?is)^изменить правила(\s+.*)?$"))
async def rules_text(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    text = rules_input_text(message)
    if not text:
        await message.reply(
            "Ответьте сообщением «изменить правила» на сообщение с текстом правил "
            "или напишите: изменить правила «текст»."
        )
        return
    if _is_placeholder_text(text):
        await message.reply(
            "Это слово из подсказки, а не текст правил. "
            "Пришлите настоящие правила ответом на это сообщение."
        )
        return
    group.settings.rules_text = clean_ui_text(text[:4000])
    await session.commit()
    await message.reply("✅ Правила сохранены.")


@router.message(F.text.casefold().in_({"правила", "показать правила"}))
async def rules(message: Message, session: AsyncSession) -> None:
    group = await session.scalar(
        select(Group).where(Group.telegram_chat_id == message.chat.id, Group.is_active.is_(True))
    )
    if group:
        await message.reply(clean_ui_text(group.settings.rules_text))


@router.message(F.text.regexp(r"(?is)^добавить триггер [^|]+\|.+"))
async def add_trigger(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group or not message.from_user:
        return
    raw = message.text[len("добавить триггер ") :]
    trigger, response = [item.strip() for item in raw.split("|", 1)]
    if not trigger or not response:
        await message.reply("Формат: добавить триггер цена | Ответ")
        return
    trigger = clean_ui_text(trigger.casefold()[:255])
    response = clean_ui_text(response[:4000])
    item = await session.scalar(
        select(AutoResponse).where(AutoResponse.group_id == group.id, AutoResponse.trigger == trigger)
    )
    if item:
        item.response_text = response
        item.active = True
    else:
        session.add(AutoResponse(group_id=group.id, trigger=trigger, response_text=response, created_by_telegram_id=message.from_user.id))
    await session.commit()
    await message.reply("✅ Автоответ сохранён.")


@router.message(F.text.regexp(r"(?i)^удалить триггер .+"))
async def remove_trigger(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session, for_update=True)
    if not group:
        return
    trigger = message.text.split(maxsplit=2)[2].casefold().strip()
    item = await session.scalar(
        select(AutoResponse).where(AutoResponse.group_id == group.id, AutoResponse.trigger == trigger)
    )
    if not item:
        await message.reply("Триггер не найден.")
        return
    await session.delete(item)
    await session.commit()
    await message.reply("✅ Триггер удалён.")


@router.message(F.text.casefold().in_({"триггеры", "список автоответов"}))
async def list_triggers(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await managed(message, bot, session)
    if not group:
        return
    rows = (await session.scalars(select(AutoResponse).where(AutoResponse.group_id == group.id).order_by(AutoResponse.trigger))).all()
    text = "Автоответы\n"
    text += "\n".join(f"• {clean_ui_text(item.trigger)} → {clean_ui_text(item.response_text[:80])}" for item in rows) if rows else "Список пуст."
    await message.reply(text)


async def _reply_complaint_notice(message: Message, text: str) -> None:
    """Ответ о жалобе в группе: показываем и снимаем через 45 секунд."""
    sent = await message.reply(text)
    await schedule_message_deletion(
        bound_redis(),
        sent.chat.id,
        sent.message_id,
        delay_seconds=COMPLAINT_MESSAGE_TTL_SECONDS,
    )


@router.message(F.text.casefold().in_({"жалоба", "пожаловаться"}))
async def complaint(message: Message, session: AsyncSession) -> None:
    if not message.from_user or not message.reply_to_message or not message.reply_to_message.from_user:
        await _reply_complaint_notice(message, "Ответьте командой «жалоба» на сообщение нарушителя.")
        return
    group = await session.scalar(select(Group).where(Group.telegram_chat_id == message.chat.id, Group.is_active.is_(True)))
    if not group:
        return
    if await complaint_exists_for_message(session, group_id=group.id, message_id=message.reply_to_message.message_id):
        await _reply_complaint_notice(message, COMPLAINT_DUPLICATE_TEXT)
        return
    target = message.reply_to_message.from_user
    item = Complaint(group_id=group.id, reporter_telegram_id=message.from_user.id, target_telegram_id=target.id, message_id=message.reply_to_message.message_id, message_text=message.reply_to_message.text or message.reply_to_message.caption)
    session.add(item)
    session.add(ModerationLog(group_id=group.id, actor_telegram_id=message.from_user.id, target_telegram_id=target.id, action="complaint", reason="Жалоба участника"))
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await _reply_complaint_notice(message, COMPLAINT_DUPLICATE_TEXT)
        return
    await session.commit()
    await _reply_complaint_notice(message, f"✅ Жалоба #{item.id} принята и передана владельцу группы.")
    if group.owner_telegram_id:
        target_label = clean_ui_text(target.full_name) + (f" (@{target.username})" if target.username else "")
        reporter_label = clean_ui_text(message.from_user.full_name) + (f" (@{message.from_user.username})" if message.from_user.username else "")
        try:
            await message.bot.send_message(group.owner_telegram_id, f"⚠️ Новая жалоба #{item.id} в «{clean_ui_text(group.title)}»\nНа пользователя: {target_label}\nОт: {reporter_label}\n\nКоманда в группе: жалобы")
        except Exception as error:
            structlog.get_logger().warning("complaint_owner_notification_failed", complaint_id=item.id, error=str(error))


@router.message(F.text.regexp(r"(?i)^(статистика|топ)(?: (сегодня|неделя|месяц))?$"))
async def statistics(message: Message, session: AsyncSession) -> None:
    group = await session.scalar(select(Group).where(Group.telegram_chat_id == message.chat.id, Group.is_active.is_(True)))
    if not group:
        return
    parts = message.text.casefold().split()
    period = parts[1] if len(parts) > 1 else "сегодня"
    days = {"сегодня": 1, "неделя": 7, "месяц": 30}.get(period, 1)
    start = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    total = int(await session.scalar(select(func.coalesce(func.sum(DailyStat.messages_count), 0)).where(DailyStat.group_id == group.id, DailyStat.date >= start)) or 0)
    active = int(await session.scalar(select(func.count(func.distinct(DailyStat.user_telegram_id))).where(DailyStat.group_id == group.id, DailyStat.date >= start, DailyStat.messages_count > 0)) or 0)
    rows = (await session.execute(select(DailyStat.user_telegram_id, User.username, User.first_name, func.sum(DailyStat.messages_count).label("count")).outerjoin(User, User.telegram_id == DailyStat.user_telegram_id).where(DailyStat.group_id == group.id, DailyStat.date >= start, DailyStat.messages_count > 0).group_by(DailyStat.user_telegram_id, User.username, User.first_name).order_by(func.sum(DailyStat.messages_count).desc()).limit(10))).all()
    lines = []
    for index, (user_id, username, first_name, count) in enumerate(rows, 1):
        label = f"@{clean_ui_text(username)}" if username else clean_ui_text(first_name or str(user_id))
        lines.append(f"{index}. {label} — {int(count)}")
    await message.reply(f"📊 Статистика: {period}\n\n💬 Сообщений: {total}\n👥 Активных участников: {active}\n\n🏆 Топ участников\n" + ("\n".join(lines) if lines else "Данных пока нет."))
