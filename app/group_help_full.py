from __future__ import annotations

import math

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.handlers.group_action_aliases import (
    ADMIN_ROSTER_ALIASES,
    ALL_BANS_ALIASES,
    ALL_MUTES_ALIASES,
    ALL_WARNINGS_ALIASES,
    BOT_INFO_ALIASES,
    GROUP_STATS_ALIASES,
    LOOKUP_ALIASES,
    MY_BANS_ALIASES,
    MY_MUTES_ALIASES,
    MY_WARNINGS_ALIASES,
    REPORT_ALIASES,
    SELF_PROFILE_ALIASES,
)

router = Router(name=__name__)
GROUP_TYPES = {"group", "supergroup"}
OPEN_WORDS = {"помощь", "команды", "команды группы", "что умеет бот", "все команды", "все фразы"}
ADMIN_WORDS = {"админ команды", "команды админа", "админка команды", "все админ команды"}
PAGE_SIZE = 28


def _cb(owner_id: int, page: str) -> str:
    return f"grouphelpfull:{owner_id}:{page}"


def _join(title: str, phrases: set[str] | frozenset[str]) -> str:
    return title + "\n" + " · ".join(sorted(phrases, key=str.casefold))


def _home_text() -> str:
    return (
        "🤖 Mimoru · Полная справка группы\n\n"
        "Здесь собраны фразы, которые можно написать прямо в группе.\n\n"
        "👤 Участникам — профиль, правила, жалобы, статистика и все разговорные варианты.\n"
        "🎭 Развлечения — reply-действия, семья и отношения.\n"
        "🎮 Игры — отдельный раздел для полноценных игр; новые игры будут добавляться сюда.\n"
        "🛡 Админам — модерация, защита, управление, контент и сервисные команды.\n"
        "👑 Роли — кто что может.\n\n"
        "Выберите раздел."
    )


def _members_text() -> str:
    return (
        "👤 Участникам\n\n"
        "Быстрые команды:\n"
        "• кто я / мой профиль / моя стата — личная карточка\n"
        "• правила — правила группы\n"
        "• статистика — активность группы (расширенная статистика может требовать права)\n"
        "• развлечения — каталог развлекательных действий и отношений\n"
        "• пожениться / выйти замуж / сделать предложение — предложение брака ответом на сообщение\n"
        "• мой брак / мои отношения — текущий партнёр\n"
        "• развестись / подать на развод — завершить брак\n"
        "• браки — история браков группы\n"
        "• /games — отдельный раздел полноценных игр\n"
        "• моя стата игр / топ игр — игровая статистика; начнёт заполняться новыми играми\n\n"
        "🚨 Жалоба отправляется ответом на сообщение нарушителя. Нажмите «Все фразы участников», чтобы увидеть все варианты, которые понимает бот."
    )


def _member_alias_text() -> str:
    blocks = [
        _join("👤 Свой профиль:", SELF_PROFILE_ALIASES),
        _join("🔎 Информация об участнике (ответом):", LOOKUP_ALIASES),
        _join("🤖 О Mimoru:", BOT_INFO_ALIASES),
        _join("📊 Статистика группы:", GROUP_STATS_ALIASES),
        _join("🚨 Жалоба на сообщение:", REPORT_ALIASES | {"жалоба", "пожаловаться"}),
        _join("👑 Кто администрация:", ADMIN_ROSTER_ALIASES),
    ]
    return "🗣 Все разговорные фразы участников\n\n" + "\n\n".join(blocks)


def _admin_lists_text() -> str:
    blocks = [
        _join("🚫 Все баны:", ALL_BANS_ALIASES),
        _join("🔇 Все муты:", ALL_MUTES_ALIASES),
        _join("⚠️ Все предупреждения:", ALL_WARNINGS_ALIASES),
        _join("🚫 Мои баны:", MY_BANS_ALIASES),
        _join("🔇 Мои муты:", MY_MUTES_ALIASES),
        _join("⚠️ Мои предупреждения:", MY_WARNINGS_ALIASES),
    ]
    return "📋 Все фразы для списков модерации\n\n" + "\n\n".join(blocks)


def _moderation_text() -> str:
    return (
        "⚖️ Модерация\n\n"
        "Ответом на сообщение участника:\n"
        "• пред — предупреждение\n"
        "• снять пред — снять предупреждение\n"
        "• мут 10м / мут 2ч — ограничить на срок\n"
        "• размут — снять мут\n"
        "• бан — заблокировать\n"
        "• разбан — разблокировать\n"
        "• удалить — удалить сообщение\n"
        "• инфо — карточка участника\n"
        "• история — история модерации\n\n"
        "Для преда, мута и бана Mimoru предложит выбрать причину.\n\n"
        "Отдельной кнопкой ниже можно открыть все разговорные варианты списков банов, мутов и предупреждений."
    )


def _protection_text() -> str:
    return (
        "🛡 Защита\n\n"
        "антифлуд вкл / выкл\n"
        "антифлуд 6 за 10с\n"
        "ссылки вкл / выкл\n"
        "капча вкл\n"
        "капс вкл / выкл\n"
        "капс лимит 70\n"
        "повторы вкл / выкл\n"
        "голосовые вкл / выкл\n"
        "стикеры вкл / выкл\n"
        "пересылки вкл / выкл\n"
        "антирейд вкл / выкл\n"
        "антирейд 10 за 1м\n"
        "преды срок 30д / преды срок никогда\n"
        "карантин вкл 24ч / выкл / статус\n"
        "карантин ссылки вкл|выкл\n"
        "карантин медиа вкл|выкл\n"
        "карантин пересылки вкл|выкл\n"
        "медленный режим вкл 10с / выкл / статус\n"
        "слоумо вкл 30с\n"
        "массовый спам вкл / выкл / статус\n"
        "массовый спам 3 за 2м\n"
        "массовый спам наказание 1ч\n"
        "защита редактирования вкл / выкл / статус\n"
        "защита редактирования окно 2д\n"
        "упоминания вкл / выкл / статус\n"
        "упоминания лимит 5\n"
        "хэштеги лимит 10\n"
        "упоминания наказание 30м"
    )


def _management_text() -> str:
    return (
        "⚙️ Управление группой\n\n"
        "локдаун вкл [30м] / выкл / статус\n"
        "ночной режим вкл 23:00 07:00 / вкл / выкл / статус\n"
        "часовой пояс\n"
        "часовой пояс Europe/Warsaw\n"
        "запланировать 2026-08-20 12:00 | «текст»\n"
        "запланировать ежедневно 12:00 | «текст»\n"
        "запланировать еженедельно пн 09:00 | «текст»\n"
        "расписание\n"
        "отменить публикацию 5\n"
        "журнал сюда / статус / тест / выкл\n"
        "заметка текст — ответом на участника\n"
        "заметки — ответом на участника\n"
        "удалить заметку 5\n"
        "статистика неделя\n"
        "диагностика\n"
        "экспорт настроек"
    )


def _joins_text() -> str:
    return (
        "🔗 Приглашения и заявки\n\n"
        "создать ссылку реклама август\n"
        "создать ссылку-заявку партнёры\n"
        "ссылки приглашений\n"
        "отключить ссылку 5\n"
        "заявки вкл / выкл\n"
        "заявки авто вкл / выкл\n"
        "заявки\n"
        "одобрить заявку 15\n"
        "отклонить заявку 15"
    )


def _content_text() -> str:
    return (
        "📝 Контент, списки и автоответы\n\n"
        "изменить приветствие «текст»\n"
        "изменить правила «текст» или ответом на сообщение с правилами\n"
        "добавить триггер цена | Ответ\n"
        "удалить триггер цена\n"
        "триггеры / список автоответов\n"
        "добавить слово слово\n"
        "удалить слово слово\n"
        "добавить подписку @channel\n"
        "удалить подписку @channel\n"
        "список подписок\n"
        "разрешить ссылку example.com\n"
        "запретить ссылку example.com\n"
        "разрешённые ссылки\n"
        "доверять / не доверять — ответом на участника\n"
        "белый список\n"
        "каналы-отправители вкл / выкл / статус\n"
        "разрешить канал-отправитель\n"
        "запретить канал-отправитель\n"
        "список каналов-отправителей"
    )


def _admin_extra_text() -> str:
    return (
        "🧰 Дополнительные админ-функции\n\n"
        "жалобы — список жалоб\n"
        "закрыть жалобу 15 рассмотрено\n"
        "стата игр — статистика полноценных игр; пока пустая до добавления новых игр\n\n"
        "Старые авто-игры и их настройки удалены.\n\n"
        "Права конкретной команды зависят от ранга и настроек владельца."
    )


def _roles_text() -> str:
    return (
        "👑 Роли Mimoru\n\n"
        "Владелец — полный доступ.\n"
        "Зам. владельца — почти полный доступ и управление младшей администрацией.\n"
        "Глав. админ — старшая модерация и назначение младших ролей.\n"
        "Администратор чата — баны, муты, преды, удаление сообщений, помощники.\n"
        "Администратор войска — голосовые/видеочаты и информация.\n"
        "Помощник — информация и передача нарушений старшему.\n"
        "Мажёр — декоративный ранг без прав модерации Mimoru.\n"
        "Недотрога — иммунитет от наказаний Mimoru.\n\n"
        "Администратор не может наказывать или менять человека своего или более высокого уровня."
    )


def _home_markup(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Участникам", callback_data=_cb(owner_id, "members")), InlineKeyboardButton(text="🗣 Все фразы", callback_data=_cb(owner_id, "member_aliases"))],
        [InlineKeyboardButton(text="🎮 Игры", callback_data=_cb(owner_id, "games")), InlineKeyboardButton(text="🛡 Админам", callback_data=_cb(owner_id, "admin"))],
        [InlineKeyboardButton(text="👑 Роли", callback_data=_cb(owner_id, "roles")), InlineKeyboardButton(text="✖️ Закрыть", callback_data=_cb(owner_id, "close"))],
    ])


def _admin_markup(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Модерация", callback_data=_cb(owner_id, "moderation")), InlineKeyboardButton(text="📋 Списки", callback_data=_cb(owner_id, "admin_lists"))],
        [InlineKeyboardButton(text="🛡 Защита", callback_data=_cb(owner_id, "protection")), InlineKeyboardButton(text="⚙️ Управление", callback_data=_cb(owner_id, "management"))],
        [InlineKeyboardButton(text="🔗 Заявки/ссылки", callback_data=_cb(owner_id, "joins")), InlineKeyboardButton(text="📝 Контент", callback_data=_cb(owner_id, "content"))],
        [InlineKeyboardButton(text="🧰 Ещё", callback_data=_cb(owner_id, "admin_extra")), InlineKeyboardButton(text="👑 Роли", callback_data=_cb(owner_id, "roles"))],
        [InlineKeyboardButton(text="◀️ В справку", callback_data=_cb(owner_id, "home")), InlineKeyboardButton(text="✖️ Закрыть", callback_data=_cb(owner_id, "close"))],
    ])


def _back(owner_id: int, *, admin: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=_cb(owner_id, "admin" if admin else "home"))],
        [InlineKeyboardButton(text="✖️ Закрыть", callback_data=_cb(owner_id, "close"))],
    ])


async def _open(message: Message, *, admin: bool = False) -> None:
    if message.from_user is None:
        return
    owner_id = message.from_user.id
    await message.reply(
        _home_text() if not admin else "🛡 Mimoru · Полная справка администрации\n\nВыберите раздел.",
        reply_markup=_home_markup(owner_id) if not admin else _admin_markup(owner_id),
    )


@router.message(F.chat.type.in_(GROUP_TYPES), F.text.casefold().in_(OPEN_WORDS))
async def group_help(message: Message) -> None:
    await _open(message)


@router.message(F.chat.type.in_(GROUP_TYPES), F.text.casefold().in_(ADMIN_WORDS))
async def admin_help(message: Message) -> None:
    await _open(message, admin=True)


@router.message(F.chat.type.in_(GROUP_TYPES), Command("help"))
async def slash_help(message: Message) -> None:
    await _open(message)


@router.message(F.chat.type.in_(GROUP_TYPES), Command(commands=["commands"]))
async def slash_commands(message: Message) -> None:
    await _open(message)


@router.message(F.chat.type.in_(GROUP_TYPES), Command("comands"))  # legacy alias, keep for old clients
async def slash_commands_legacy_alias(message: Message) -> None:
    await _open(message)


async def _owner(callback: CallbackQuery) -> int | None:
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    owner_id = int(parts[1])
    if callback.from_user.id != owner_id:
        await callback.answer("Это меню открыл другой участник. Напишите «помощь», чтобы открыть своё.", show_alert=True)
        return None
    return owner_id


@router.callback_query(F.data.regexp(r"^grouphelpfull:\d+:[a-z_]+$"))
async def navigate(callback: CallbackQuery) -> None:
    owner_id = await _owner(callback)
    if owner_id is None or callback.message is None:
        return
    page = (callback.data or "").rsplit(":", 1)[1]
    if page == "close":
        await callback.message.delete()
        await callback.answer()
        return
    if page == "home":
        await callback.message.edit_text(_home_text(), reply_markup=_home_markup(owner_id))
        await callback.answer()
        return
    if page == "admin":
        await callback.message.edit_text("🛡 Mimoru · Полная справка администрации\n\nВыберите раздел.", reply_markup=_admin_markup(owner_id))
        await callback.answer()
        return
    if page == "games":
        await callback.message.edit_text(
            "🎮 Игры\n\n"
            "Это отдельный раздел для полноценных игр. Старые псевдоигры и автоматические игровые действия удалены.\n\n"
            "Для развлекательных reply-команд, брака и отношений напишите «развлечения».\n"
            "Новые игры будут добавляться в /games отдельно.",
            reply_markup=_back(owner_id),
        )
        await callback.answer()
        return
    pages = {
        "members": (_members_text(), False),
        "member_aliases": (_member_alias_text(), False),
        "moderation": (_moderation_text(), True),
        "admin_lists": (_admin_lists_text(), True),
        "protection": (_protection_text(), True),
        "management": (_management_text(), True),
        "joins": (_joins_text(), True),
        "content": (_content_text(), True),
        "admin_extra": (_admin_extra_text(), True),
        "roles": (_roles_text(), False),
    }
    item = pages.get(page)
    if item is None:
        await callback.answer("Раздел не найден.", show_alert=True)
        return
    text, admin = item
    await callback.message.edit_text(text, reply_markup=_back(owner_id, admin=admin))
    await callback.answer()
