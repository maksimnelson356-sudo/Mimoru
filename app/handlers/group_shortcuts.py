from __future__ import annotations

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Group, User
from app.db.rank_models import RankAssignment
from app.handlers.ad_market_v3 import _group_title_or_placeholder
from app.handlers.group_commands import group_complaint
from app.services.access import can_manage_group, is_service_owner
from app.services.ranks import RANK_CODES, RANK_LABELS
from app.services.ui import panel_header

log = structlog.get_logger(__name__)


router = Router(name=__name__)
GROUP_TYPES = {"group", "supergroup"}
PUBLIC_ROSTER_WORDS = {"кто админ", "кто админы", "кто администрация", "администрация"}


@router.message(F.chat.type.in_(GROUP_TYPES))
async def debug_any_message(message: Message) -> None:
    log.warning(
        "debug_any_message",
        chat_id=message.chat.id,
        chat_type=message.chat.type,
        from_user_id=message.from_user.id if message.from_user else None,
        text=message.text,
        entities=[str(e.type) for e in (message.entities or [])],
        entity_count=len(message.entities or []),
    )
    await message.reply("🛠 debug")


@router.message(Command("games"), F.chat.type.in_(GROUP_TYPES))
async def games_command(message: Message) -> None:
    log.warning(
        "games_command_called",
        chat_id=message.chat.id,
        chat_type=message.chat.type,
        from_user_id=message.from_user.id if message.from_user else None,
        text=message.text,
        entities=[str(e.type) for e in (message.entities or [])],
    )
    await message.reply("🎮 ping ok")


@router.message(Command("report"), F.chat.type.in_(GROUP_TYPES))
async def report_command(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.reply_to_message is None:
        await message.reply(
            "🚨 Пожаловаться на участника\n\n"
            "Чтобы отправить жалобу, ответьте на сообщение нарушителя одним из вариантов:\n"
            "жалоба · доложить · нарушитель\n\n"
            "Жалоба будет передана администрации группы для рассмотрения и принятия мер."
        )
        return
    await group_complaint(message, bot, session)


@router.message(Command("help"), F.chat.type.in_(GROUP_TYPES))
async def group_help_command(message: Message) -> None:
    await message.reply(
        "🆘 Помощь Mimoru\n\n"
        "Здесь можно быстро узнать, что умеет бот в этой группе.\n\n"
        "/games — развлечения и мини-игры.\n"
        "/report — пожаловаться на нарушение.\n"
        "/comands — посмотреть доступные команды.\n"
        "/oftop — связаться с владельцем Mimoru.\n\n"
        "Чтобы посмотреть структуру ролей группы, напишите: кто админ.\n\n"
        "Если вы не хотите, чтобы Mimoru сама выбирала вас для случайных игровых действий, "
        "отправьте /imunitet. Другие участники при этом всё равно смогут играть с вами."
    )


@router.message(Command("comands", "commands"), F.chat.type.in_(GROUP_TYPES))
async def commands_command(message: Message) -> None:
    await message.reply(
        "📋 Команды Mimoru в группе\n\n"
        "Для всех участников:\n"
        "/games — открыть развлечения.\n"
        "/report — пожаловаться ответом на сообщение.\n"
        "/imunitet — включить или выключить защиту от случайных действий самой Mimoru.\n"
        "/help — короткая помощь.\n"
        "/oftop текст — написать владельцу Mimoru.\n"
        "кто админ — посмотреть все роли Mimoru в этой группе.\n\n"
        "Обычные слова тоже работают: развлечения, игры, жалоба, доложить, нарушитель.\n\n"
        "Если у вас есть права модерации Mimoru, ответом на сообщение доступны: "
        "мут, бан, пред, снять пред, снять все предупреждения, говори. "
        "Разбан: разбан @username или разбан Telegram-ID."
    )


@router.message(F.chat.type.in_(GROUP_TYPES), F.text.casefold().in_(PUBLIC_ROSTER_WORDS))
async def public_role_roster(message: Message, session: AsyncSession) -> None:
    group = await session.scalar(
        select(Group).where(Group.telegram_chat_id == message.chat.id, Group.is_active.is_(True))
    )
    if group is None:
        return

    rows = list((await session.scalars(
        select(RankAssignment).where(
            RankAssignment.group_id == group.id,
            RankAssignment.active.is_(True),
        )
    )).all())

    async def label_for(user_id: int) -> str:
        user = await session.scalar(select(User).where(User.telegram_id == user_id))
        if user is None:
            return f"ID {user_id}"
        if user.username:
            return f"@{user.username}"
        full_name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
        return full_name or f"ID {user_id}"

    lines = ["👥 Роли Mimoru в этой группе"]
    if group.owner_telegram_id:
        lines += ["", "👑 Владелец", f"• {await label_for(group.owner_telegram_id)}"]

    by_rank: dict[str, list[RankAssignment]] = {code: [] for code in RANK_CODES}
    for row in rows:
        if row.rank_code in by_rank:
            by_rank[row.rank_code].append(row)

    for rank_code in RANK_CODES:
        assigned = by_rank[rank_code]
        if not assigned:
            continue
        lines += ["", RANK_LABELS.get(rank_code, rank_code)]
        for row in assigned:
            lines.append(f"• {await label_for(row.user_telegram_id)}")

    if not rows and not group.owner_telegram_id:
        lines += ["", "Назначенных ролей пока нет."]

    lines += [
        "",
        "В Telegram-списке администраторов могут отображаться Зам. владельца, Глав. админ, Администратор чата и Мажёр. Администратор войска, Помощник и Недотрога работают только через Mimoru.",
    ]
    await message.reply("\n".join(lines))


@router.message(Command("oftop"), F.chat.type.in_(GROUP_TYPES))
async def oftop_command(message: Message, bot: Bot) -> None:
    settings = get_settings()
    raw = message.text or ""
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "💬 Связаться с владельцем Mimoru\n\n"
            "Напишите сообщение после команды, например:\n"
            "/oftop Хочу предложить новую функцию\n\n"
            "Ваше сообщение будет передано владельцу бота. Для жалобы на участника используйте /report."
        )
        return

    if not settings.service_owner_ids:
        await message.reply("Связь с владельцем Mimoru сейчас не настроена.")
        return

    sender = message.from_user
    sender_label = sender.full_name if sender is not None else "Неизвестный пользователь"
    sender_id = sender.id if sender is not None else 0
    username = f"@{sender.username}" if sender is not None and sender.username else "без username"
    text = parts[1].strip()
    owner_text = (
        "Сообщение владельцу Mimoru\n\n"
        f"От: {sender_label} · {username} · ID {sender_id}\n"
        f"Группа: {message.chat.title or 'Без названия'} · ID {message.chat.id}\n\n"
        f"Сообщение:\n{text}"
    )

    delivered = 0
    for owner_id in settings.service_owner_ids:
        try:
            await bot.send_message(owner_id, owner_text)
            delivered += 1
        except Exception:
            continue

    if delivered:
        await message.reply("Сообщение отправлено владельцу Mimoru.")
    else:
        await message.reply("Не удалось доставить сообщение владельцу Mimoru. Попробуйте позже.")


async def _owned_group(session: AsyncSession, group_id: int, user_id: int) -> Group | None:
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not is_service_owner(user_id):
        query = query.where(Group.owner_telegram_id == user_id)
    return await session.scalar(query)


@router.callback_query(F.data.regexp(r"^group_disconnect:\d+$"))
async def group_disconnect_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await _owned_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Группа не найдена или у вас нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Отключить группу",
            f"{group.title}\n\nMimoru прекратит администрирование, сохранит настройки группы и покинет Telegram-группу. Подключить её снова можно будет позже.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⛔ Да, отключить группу", callback_data=f"group_disconnect_do:{group.id}")],
            [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group.id}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^group_disconnect_do:\d+$"))
async def group_disconnect_do(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await _owned_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Группа уже отключена или у вас нет доступа.", show_alert=True)
        return

    try:
        await bot.send_message(
            group.telegram_chat_id,
            "⛔ Mimoru больше не администрирует эту группу.\n\n"
            "Обслуживание отключено владельцем группы. Mimoru покидает группу.\n"
            "Чтобы подключить бота снова, добавьте Mimoru администратором и напишите «подключить»."
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass

    group.is_active = False
    await session.commit()

    left = True
    try:
        await bot.leave_chat(group.telegram_chat_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        left = False

    text = (
        "Группа отключена от Mimoru. Бот покинул группу. Настройки сохранены."
        if left
        else "Группа отключена от Mimoru, но Telegram не позволил боту автоматически покинуть чат. Удалите Mimoru из группы вручную. Настройки сохранены."
    )
    await callback.message.edit_text(
        panel_header("Группа отключена", text),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К моим группам", callback_data="panel:groups")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="panel:home")],
        ]),
    )
    await callback.answer("Группа отключена")

@router.message(F.chat.type.in_(GROUP_TYPES), F.text.casefold() == "реклама")
async def ad_group_entry(message: Message, bot: Bot, session: AsyncSession) -> None:
    """#4: начать создание ОП из группы (переход в личку)."""
    if message.from_user is None:
        return

    group = await session.scalar(
        select(Group).where(
            Group.telegram_chat_id == message.chat.id,
            Group.is_active.is_(True),
        )
    )
    if group is None:
        return

    user_id = message.from_user.id
    if not await can_manage_group(bot, group, user_id, session):
        return

    groups = list((await session.scalars(
        select(Group)
        .where(Group.owner_telegram_id == user_id, Group.is_active.is_(True))
        .order_by(Group.title)
    )).all())

    if not groups:
        await message.reply(
            "📭 У вас пока нет активных групп Mimoru, где вы владелец."
        )
        return

    rows = [
        [InlineKeyboardButton(
            text=_group_title_or_placeholder(group)[:58],
            callback_data=f"reqlist:group:{group.id}",
        )]
        for group in groups
    ]
    rows.append([InlineKeyboardButton(text="📥 Входящие заявки", callback_data="reqdeal:seller")])
    rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])

    try:
        await bot.send_message(
            user_id,
            panel_header(
                "Продать обязательную подписку",
                "Выберите свою группу. В каталоге покупателям не показываются ID и @username площадки.",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except TelegramForbiddenError:
        me = await bot.get_me()
        await message.reply(
            "⚠️ Чтобы настроить рекламу, откройте личку со мной:\n"
            f"👉 @{me.username}\n"
            "и отправьте /start, затем повторите «реклама» здесь."
        )
        return

    await message.reply("✅ Отправил настройки рекламы вам в личку.")