from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.group_disconnect_models import GroupDisconnectIntent
from app.db.models import Group, User
from app.services.access import is_service_owner
from app.services.group_disconnects import (
    attempt_group_disconnect,
    request_group_disconnect,
    request_system_group_disconnect,
)
from app.services.permissions import is_creator
from app.services.repositories import (
    GroupOwnerServiceBlockedError,
    get_or_create_group,
    upsert_user,
)
from app.services.telegram_admins import sync_telegram_administrators
from app.services.ui import clean_ui_text, panel_header


router = Router(name=__name__)
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
ACTIVE_BOT_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
INACTIVE_BOT_STATUSES = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
START_GROUP_RE = re.compile(r"^/start(?:@\w+)?\s+group_(\d+)$", re.IGNORECASE)
CONNECT_COMMAND = "подключить"
ADMIN_PROMOTION_RIGHTS = (
    "manage_chat",
    "delete_messages",
    "restrict_members",
    "invite_users",
    "pin_messages",
    "promote_members",
)

# Legacy onboarding markers retained for compatibility tests/documentation:
# import_existing_admin_ranks
# setup_start_menu(group.id)


def _connect_command_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📋 подключить",
            copy_text=CopyTextButton(text=CONNECT_COMMAND),
        )
    ]])


def _admin_promotion_markup(bot_username: str) -> InlineKeyboardMarkup:
    rights = "+".join(ADMIN_PROMOTION_RIGHTS)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🛡 Назначить администратором",
            url=f"https://t.me/{bot_username}?startgroup=mimoru&admin={rights}",
        )
    ]])


def _private_setup_markup(bot_username: str, group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⚙️ Открыть Mimoru в личных сообщениях",
            url=f"https://t.me/{bot_username}?start=group_{group_id}",
        )
    ]])


def _connected_private_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👮 Согласовать администрацию", callback_data=f"admin_access:{group_id}")],
        [InlineKeyboardButton(text="🚀 Настроить Mimoru", callback_data=f"setup:{group_id}:start")],
        [InlineKeyboardButton(text="⭐ Проверить состояние", callback_data=f"health:{group_id}")],
        [InlineKeyboardButton(text="🏠 Открыть группу", callback_data=f"group:{group_id}")],
    ])


def _disconnected_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К моим группам", callback_data="panel:groups")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="panel:home")],
    ])


async def _bot_admin_status(bot: Bot, chat_id: int) -> bool | None:
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return None
    return member.status == ChatMemberStatus.ADMINISTRATOR


async def _reactivate_group(session: AsyncSession, group: Group) -> bool:
    """Bring a previously disconnected group back into service.

    A group leaves service either by the owner's «отключиться» or automatically
    after the bot loses administrator rights (app.services.group_disconnects).
    Only the word «подключить» from the owner used to flip is_active back, so in
    the meantime every panel action answered «Доступ к группе потерян» even after
    the bot was added again. Re-adding Mimoru as administrator restores service
    here; it never changes the owner and never lifts a service block.
    """
    if group.is_active:
        return False
    if group.owner_telegram_id:
        owner = await session.get(User, group.owner_telegram_id)
        if owner is not None and owner.service_blocked:
            return False
    group.is_active = True
    await session.execute(
        delete(GroupDisconnectIntent).where(GroupDisconnectIntent.group_id == group.id)
    )
    return True


@router.my_chat_member()
async def bot_group_membership_changed(event: ChatMemberUpdated, bot: Bot, session: AsyncSession) -> None:
    if event.chat.type not in GROUP_TYPES or event.new_chat_member.user.id != bot.id:
        return
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    if new_status == ChatMemberStatus.ADMINISTRATOR and old_status != ChatMemberStatus.ADMINISTRATOR:
        group = await session.scalar(
            select(Group).where(Group.telegram_chat_id == event.chat.id)
        )
        reconnected = group is not None and await _reactivate_group(session, group)
        if reconnected:
            await session.commit()
        try:
            if reconnected:
                await bot.send_message(
                    event.chat.id,
                    "🔌 Mimoru снова администратор — группа возвращена в обслуживание.\n\n"
                    "Настройки, ранги и защита остались там, где были. "
                    "Управлять группой можно в личном диалоге с Mimoru.",
                )
            else:
                await bot.send_message(
                    event.chat.id,
                    "👋 Mimoru получила права администратора и готова к подключению группы.\n\n"
                    "1️⃣ Убедитесь, что Mimoru назначена администратором.\n"
                    "2️⃣ Владелец группы должен отправить команду «подключить».\n"
                    "3️⃣ После подключения дальнейшая настройка продолжится в личном диалоге с Mimoru.\n\n"
                    "Нажмите кнопку ниже — слово «подключить» скопируется в буфер обмена. Затем вставьте его в чат и отправьте.",
                    reply_markup=_connect_command_markup(),
                )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        if reconnected and group is not None and group.owner_telegram_id:
            try:
                await bot.send_message(
                    group.owner_telegram_id,
                    f"🔌 Группа «{group.title}» снова подключена к Mimoru: "
                    "боту вернули права администратора.\n"
                    "Отключенные ранее ранги администраторов не возвращаются — "
                    "назначьте нужных людей заново.",
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
        return
    if old_status in INACTIVE_BOT_STATUSES and new_status == ChatMemberStatus.MEMBER:
        try:
            username = event.new_chat_member.user.username
            await bot.send_message(
                event.chat.id,
                "👋 Mimoru добавлена в группу, но пока без прав администратора.\n\n"
                "Для подключения сначала назначьте Mimoru администратором.\n"
                "Нажмите кнопку ниже и выдайте боту необходимые права.\n\n"
                "После назначения администратором Mimoru сама пришлёт следующий шаг с командой «подключить».",
                reply_markup=_admin_promotion_markup(username) if username else None,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        return

    group = await session.scalar(select(Group).where(Group.telegram_chat_id == event.chat.id))
    if group is None or not group.is_active:
        return
    if old_status == ChatMemberStatus.ADMINISTRATOR and new_status == ChatMemberStatus.MEMBER:
        # Persist a system-owned intent before any leave attempt. Recovery rechecks
        # the bot's current membership so a quickly repaired/reconnected group is
        # never disconnected by a stale admin-loss event.
        await request_system_group_disconnect(session, group)
        try:
            await bot.send_message(
                event.chat.id,
                "⚠️ Mimoru больше не может администрировать эту группу: у бота сняты необходимые права. Группа отключается, бот покидает чат.",
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        completed = await attempt_group_disconnect(bot, group.id)
        try:
            text = (
                f"⚠️ Группа «{clean_ui_text(group.title)}» отключена от Mimoru: у бота сняли права администратора."
                if completed
                else f"⚠️ В группе «{clean_ui_text(group.title)}» у Mimoru сняли права администратора. Состояние отключения будет проверено автоматически."
            )
            if group.owner_telegram_id is not None:
                await bot.send_message(group.owner_telegram_id, text)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        return
    if new_status in INACTIVE_BOT_STATUSES and old_status in ACTIVE_BOT_STATUSES:
        group.is_active = False
        await session.commit()
        try:
            if group.owner_telegram_id is not None:
                await bot.send_message(group.owner_telegram_id, f"⚠️ Mimoru больше не администрирует группу «{clean_ui_text(group.title)}»: бот был удалён из группы. Настройки сохранены.")
        except (TelegramBadRequest, TelegramForbiddenError):
            pass


@router.callback_query(F.data.regexp(r"^group_disconnect_do:\d+$"))
async def disconnect_group_crash_safe(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
) -> None:
    group_id = int(callback.data.split(":")[-1])
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not is_service_owner(callback.from_user.id):
        query = query.where(Group.owner_telegram_id == callback.from_user.id)
    group = await session.scalar(query)
    if group is None:
        await callback.answer("Группа уже отключена или у вас нет доступа.", show_alert=True)
        return

    await request_group_disconnect(session, group, callback.from_user.id)
    try:
        await bot.send_message(
            group.telegram_chat_id,
            "⛔ Mimoru больше не администрирует эту группу.\n\n"
            "Обслуживание отключается владельцем группы. Mimoru покидает группу.\n"
            "Чтобы подключить бота снова, добавьте Mimoru администратором и напишите «подключить».",
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass

    completed = await attempt_group_disconnect(bot, group.id)
    if completed:
        text = "Группа отключена от Mimoru. Бот покинул группу. Настройки сохранены."
        answer = "Группа отключена"
    else:
        text = (
            "Запрос на отключение сохранён, но Telegram пока не подтвердил выход бота. "
            "Mimoru будет повторять безопасную попытку автоматически."
        )
        answer = "Отключение ожидает Telegram"
    await callback.message.edit_text(
        panel_header("Отключение группы", text),
        reply_markup=_disconnected_menu(),
    )
    await callback.answer(answer)


@router.message(F.chat.type.in_(GROUP_TYPES), F.text.casefold() == "подключить")
async def connect_group_private_first(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    if not await is_creator(bot, message.chat.id, message.from_user.id):
        await message.reply("Подключить группу может только её владелец.")
        return
    bot_admin = await _bot_admin_status(bot, message.chat.id)
    if bot_admin is None:
        await message.reply(
            "Не удалось проверить права Mimoru в Telegram. Повторите подключение позже."
        )
        return
    if not bot_admin:
        await message.reply(
            "⛔ Сначала назначьте Mimoru администратором группы, затем снова напишите «подключить»."
        )
        return

    await upsert_user(session, message.from_user)
    try:
        group = await get_or_create_group(session, message.chat, message.from_user.id, create=True)
    except GroupOwnerServiceBlockedError:
        await session.rollback()
        await message.reply(
            "⛔ Подключение недоступно: владелец группы заблокирован в Mimoru. "
            "Сначала обратитесь к администрации сервиса для разблокировки."
        )
        return
    sync = await sync_telegram_administrators(bot, session, group)
    await session.commit()
    existing_admins = sum(1 for item in sync.entries if item.role_code == "telegram_admin")
    me = await bot.get_me()
    suffix = (
        f"\n\nВ группе найдено Telegram-администраторов: {existing_admins}. В личном кабинете Mimoru предложит назначить каждому единый ранг и выбрать способ доступа."
        if existing_admins else ""
    )
    if not me.username:
        await message.reply("✅ Группа подключена к Mimoru." + suffix + "\n\nТеперь откройте личный диалог с Mimoru.")
        return
    await message.reply(
        "✅ Группа подключена к Mimoru." + suffix + "\n\nТеперь откройте личный диалог с Mimoru — дальнейшая настройка группы будет проходить там.",
        reply_markup=_private_setup_markup(me.username, group.id),
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(START_GROUP_RE))
async def open_connected_group_setup(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.from_user is None or message.text is None:
        return
    match = START_GROUP_RE.match(message.text.strip())
    if match is None:
        return
    group_id = int(match.group(1))
    group = await session.scalar(select(Group).where(Group.id == group_id, Group.is_active.is_(True)))
    if group is None:
        await message.answer("Эта группа не найдена или уже отключена от Mimoru.")
        return
    if group.owner_telegram_id != message.from_user.id and not is_service_owner(message.from_user.id):
        await message.answer("Настраивать эту группу в личном кабинете может только её владелец.")
        return
    sync = await sync_telegram_administrators(bot, session, group)
    await session.commit()
    admin_count = sum(1 for item in sync.entries if item.role_code == "telegram_admin")
    hint = (
        f"\n\nНайдено Telegram-администраторов: {admin_count}. Начните с «Согласовать администрацию», чтобы Telegram-права и ранги Mimoru не противоречили друг другу."
        if admin_count else ""
    )
    await message.answer(
        panel_header("Группа подключена", f"{clean_ui_text(group.title)}\n\nПродолжайте настройку здесь, в личном диалоге с Mimoru.{hint}"),
        reply_markup=_connected_private_menu(group.id),
    )
