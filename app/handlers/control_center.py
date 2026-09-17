from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ForbiddenWord, Group, GroupModerator, RequiredChannel, SupportTicket
from app.keyboards.panel import (
    antiflood_preset_menu,
    channels_admin_menu,
    default_mute_menu,
    main_menu,
    role_edit_menu,
    role_remove_confirm,
    roles_menu,
    service_ticket_menu,
    settings_detail_menu,
    support_menu,
    warnings_limit_menu,
    words_admin_menu,
)
from app.services.access import DEFAULT_ROLE_PERMISSIONS, is_service_owner
from app.services.plans import plan_limit
from app.services.ui import panel_header

router = Router(name=__name__)


class ControlForm(StatesGroup):
    word_add = State()
    channel_add = State()
    welcome_text = State()
    rules_text = State()
    role_add = State()
    support_new = State()
    ticket_reply = State()


async def owned_group(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> Group | None:
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not is_service_owner(user_id):
        query = query.where(Group.owner_telegram_id == user_id)
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def moderator_row(session: AsyncSession, group_id: int, moderator_id: int) -> GroupModerator | None:
    return await session.scalar(
        select(GroupModerator).where(
            GroupModerator.id == moderator_id,
            GroupModerator.group_id == group_id,
        )
    )


def effective_permissions(item: GroupModerator) -> dict[str, bool]:
    return DEFAULT_ROLE_PERMISSIONS.get(item.role, {}) | (item.permissions or {})


# ---- content management without group commands ----

@router.callback_query(F.data.regexp(r"^word_add:\d+$"))
async def word_add(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[1])
    if not await owned_group(session, group_id, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(ControlForm.word_add)
    await state.update_data(group_id=group_id)
    await callback.message.edit_text(panel_header("Новое запрещённое слово", "Отправьте слово или фразу одним сообщением."))
    await callback.answer()


@router.message(ControlForm.word_add, F.chat.type == "private")
async def word_add_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    group_id = int(data["group_id"])
    group = await owned_group(session, group_id, message.from_user.id, for_update=True)
    if not group:
        await state.clear(); await message.answer("Доступ к группе потерян."); return
    current = int(await session.scalar(select(func.count()).select_from(ForbiddenWord).where(ForbiddenWord.group_id == group.id)) or 0)
    if current >= plan_limit(group, "words"):
        await state.clear(); await message.answer("Достигнут лимит запрещённых слов текущего тарифа."); return
    word = " ".join((message.text or "").lower().strip().split())[:255]
    if len(word) < 2:
        await message.answer("Слишком короткое значение. Отправьте слово или фразу ещё раз."); return
    session.add(ForbiddenWord(group_id=group.id, word=word))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback(); await message.answer("Такое слово или фраза уже есть в списке."); return
    await state.clear()
    words = (await session.scalars(select(ForbiddenWord.word).where(ForbiddenWord.group_id == group.id).order_by(ForbiddenWord.word))).all()
    await message.answer(panel_header("Добавлено", word), reply_markup=words_admin_menu(group.id, words))


@router.callback_query(F.data.regexp(r"^word_remove:\d+:\d+$"))
async def word_remove(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_index = callback.data.split(":")
    group_id, index = int(raw_group), int(raw_index)
    group = await owned_group(session, group_id, callback.from_user.id, for_update=True)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True); return
    rows = (await session.scalars(select(ForbiddenWord).where(ForbiddenWord.group_id == group.id).order_by(ForbiddenWord.word))).all()
    if index >= len(rows):
        await callback.answer("Список уже изменился. Откройте его заново.", show_alert=True); return
    await session.delete(rows[index]); await session.commit()
    words = (await session.scalars(select(ForbiddenWord.word).where(ForbiddenWord.group_id == group.id).order_by(ForbiddenWord.word))).all()
    await callback.message.edit_text(panel_header("Запрещённые слова", "Нажмите строку, чтобы удалить её, или добавьте новую."), reply_markup=words_admin_menu(group.id, words))
    await callback.answer("Удалено")


@router.callback_query(F.data.regexp(r"^channel_add:\d+$"))
async def channel_add(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[1])
    if not await owned_group(session, group_id, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    await state.set_state(ControlForm.channel_add)
    await state.update_data(group_id=group_id)
    await callback.message.edit_text(panel_header("Добавить обязательный канал", "Отправьте username канала, например @mychannel. Mimoru должна иметь возможность проверить подписку."))
    await callback.answer()


@router.message(ControlForm.channel_add, F.chat.type == "private")
async def channel_add_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data(); group_id = int(data["group_id"])
    group = await owned_group(session, group_id, message.from_user.id, for_update=True)
    if not group:
        await state.clear(); await message.answer("Доступ к группе потерян."); return
    current = int(await session.scalar(select(func.count()).select_from(RequiredChannel).where(RequiredChannel.group_id == group.id, RequiredChannel.active.is_(True))) or 0)
    if current >= plan_limit(group, "channels"):
        await state.clear(); await message.answer("Достигнут лимит обязательных каналов текущего тарифа."); return
    username = (message.text or "").strip().lower()
    import re
    if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", username):
        await message.answer("Нужен username в формате @channel_name."); return
    row = await session.scalar(select(RequiredChannel).where(RequiredChannel.group_id == group.id, RequiredChannel.channel_username == username))
    if row:
        row.active = True
    else:
        session.add(RequiredChannel(group_id=group.id, channel_username=username, active=True))
    await session.commit(); await state.clear()
    channels = (await session.scalars(select(RequiredChannel.channel_username).where(RequiredChannel.group_id == group.id, RequiredChannel.active.is_(True)).order_by(RequiredChannel.channel_username))).all()
    await message.answer(panel_header("Канал добавлен", username), reply_markup=channels_admin_menu(group.id, channels))


@router.callback_query(F.data.regexp(r"^channel_remove:\d+:\d+$"))
async def channel_remove(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_index = callback.data.split(":")
    group_id, index = int(raw_group), int(raw_index)
    group = await owned_group(session, group_id, callback.from_user.id, for_update=True)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True); return
    rows = (await session.scalars(select(RequiredChannel).where(RequiredChannel.group_id == group.id, RequiredChannel.active.is_(True)).order_by(RequiredChannel.channel_username))).all()
    if index >= len(rows):
        await callback.answer("Список уже изменился. Откройте его заново.", show_alert=True); return
    rows[index].active = False; await session.commit()
    channels = (await session.scalars(select(RequiredChannel.channel_username).where(RequiredChannel.group_id == group.id, RequiredChannel.active.is_(True)).order_by(RequiredChannel.channel_username))).all()
    await callback.message.edit_text(panel_header("Обязательные каналы", "Нажмите канал для удаления или добавьте новый."), reply_markup=channels_admin_menu(group.id, channels))
    await callback.answer("Удалено")


# ---- settings editor ----

@router.callback_query(F.data.regexp(r"^settings_detail:\d+$"))
async def settings_detail(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True); return
    await callback.message.edit_text(panel_header("Параметры группы", "Основные значения можно менять без команд."), reply_markup=settings_detail_menu(group))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setting_text:\d+:(welcome|rules)$"))
async def setting_text(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    _, raw_group, field = callback.data.split(":")
    group_id = int(raw_group); group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True); return
    await state.set_state(ControlForm.welcome_text if field == "welcome" else ControlForm.rules_text)
    await state.update_data(group_id=group_id)
    current = group.settings.welcome_text if field == "welcome" else group.settings.rules_text
    title = "Текст приветствия" if field == "welcome" else "Правила группы"
    hint = "Можно использовать {имя}." if field == "welcome" else "Отправьте новый текст правил одним сообщением."
    await callback.message.edit_text(panel_header(title, f"Сейчас:\n{current[:1200]}\n\n{hint}"))
    await callback.answer()


async def _save_setting_text(message: Message, session: AsyncSession, state: FSMContext, field: str) -> None:
    data = await state.get_data(); group_id = int(data["group_id"])
    group = await owned_group(session, group_id, message.from_user.id, for_update=True)
    if not group:
        await state.clear(); await message.answer("Доступ к группе потерян."); return
    value = (message.text or "").strip()[:3000]
    if len(value) < 2:
        await message.answer("Текст слишком короткий."); return
    if field == "welcome": group.settings.welcome_text = value
    else: group.settings.rules_text = value
    await session.commit(); await state.clear()
    await message.answer(panel_header("Сохранено", "Изменения применены."), reply_markup=settings_detail_menu(group))


@router.message(ControlForm.welcome_text, F.chat.type == "private")
async def save_welcome(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await _save_setting_text(message, session, state, "welcome")


@router.message(ControlForm.rules_text, F.chat.type == "private")
async def save_rules(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await _save_setting_text(message, session, state, "rules")


@router.callback_query(F.data.regexp(r"^setting_num:\d+:(warnings|defaultmute|antiflood)$"))
async def setting_num(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, field = callback.data.split(":")
    group_id = int(raw_group); group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True); return
    if field == "warnings":
        markup = warnings_limit_menu(group.id, group.settings.warnings_limit); title = "Лимит предупреждений"
    elif field == "defaultmute":
        markup = default_mute_menu(group.id, group.settings.default_mute_seconds); title = "Мут по умолчанию"
    else:
        markup = antiflood_preset_menu(group.id, group.settings.antiflood_limit, group.settings.antiflood_window_seconds); title = "Профиль антифлуда"
    await callback.message.edit_text(panel_header(title, "Выберите значение."), reply_markup=markup); await callback.answer()


@router.callback_query(F.data.regexp(r"^setting_set:\d+:(warnings|defaultmute):\d+$"))
async def setting_set(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, field, raw_value = callback.data.split(":")
    group_id, value = int(raw_group), int(raw_value); group = await owned_group(session, group_id, callback.from_user.id, for_update=True)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True); return
    if field == "warnings":
        if value not in {1, 2, 3, 4, 5}: await callback.answer("Недопустимое значение.", show_alert=True); return
        group.settings.warnings_limit = value
    else:
        if value not in {300, 900, 3600, 21600, 86400}: await callback.answer("Недопустимое значение.", show_alert=True); return
        group.settings.default_mute_seconds = value
    await session.commit(); await callback.message.edit_text(panel_header("Параметры группы", "Значение сохранено."), reply_markup=settings_detail_menu(group)); await callback.answer("Сохранено")


@router.callback_query(F.data.regexp(r"^setting_flood:\d+:(4|6|8):(5|10|15)$"))
async def setting_flood(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_limit, raw_window = callback.data.split(":")
    group_id, limit, window = int(raw_group), int(raw_limit), int(raw_window)
    if (limit, window) not in {(4, 5), (6, 10), (8, 15)}:
        await callback.answer("Недопустимый профиль.", show_alert=True); return
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True); return
    group.settings.antiflood_limit = limit; group.settings.antiflood_window_seconds = window
    await session.commit(); await callback.message.edit_text(panel_header("Параметры группы", "Профиль антифлуда сохранён."), reply_markup=settings_detail_menu(group)); await callback.answer("Сохранено")


# ---- roles and permissions ----

@router.callback_query(F.data.regexp(r"^role_add:\d+$"))
async def role_add(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True); return
    current = int(await session.scalar(select(func.count()).select_from(GroupModerator).where(GroupModerator.group_id == group.id, GroupModerator.active.is_(True))) or 0)
    if current >= plan_limit(group, "moderators"):
        await callback.answer("Достигнут лимит модераторов текущего тарифа.", show_alert=True); return
    await state.set_state(ControlForm.role_add); await state.update_data(group_id=group_id)
    await callback.message.edit_text(panel_header("Добавить модератора", "Отправьте Telegram ID администратора группы. После добавления его права можно настроить кнопками.")); await callback.answer()


@router.message(ControlForm.role_add, F.chat.type == "private")
async def role_add_id(message: Message, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data(); group_id = int(data["group_id"]); group = await owned_group(session, group_id, message.from_user.id)
    if not group:
        await state.clear(); await message.answer("Доступ к группе потерян."); return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой Telegram ID."); return
    user_id = int(raw)
    if user_id == group.owner_telegram_id:
        await message.answer("Владелец группы уже имеет все права."); return
    try:
        member = await bot.get_chat_member(group.telegram_chat_id, user_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("Не удалось проверить пользователя в группе. Проверьте ID и права Mimoru."); return
    if member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
        await message.answer("Сначала назначьте этого пользователя администратором Telegram-группы."); return
    item = await session.scalar(select(GroupModerator).where(GroupModerator.group_id == group.id, GroupModerator.user_telegram_id == user_id))
    if item:
        item.active = True; item.role = "moderator"; item.assigned_by_telegram_id = message.from_user.id
    else:
        item = GroupModerator(group_id=group.id, user_telegram_id=user_id, role="moderator", permissions={}, active=True, assigned_by_telegram_id=message.from_user.id)
        session.add(item)
    await session.commit(); await state.clear()
    await message.answer(panel_header("Модератор добавлен", f"Telegram ID: {user_id}"), reply_markup=role_edit_menu(group.id, item, effective_permissions(item)))


@router.callback_query(F.data.regexp(r"^role_edit:\d+:\d+$"))
async def role_edit(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":"); group_id, item_id = int(raw_group), int(raw_id)
    if not await owned_group(session, group_id, callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    item = await moderator_row(session, group_id, item_id)
    if not item: await callback.answer("Роль не найдена.", show_alert=True); return
    text = panel_header("Права модератора", f"Telegram ID: {item.user_telegram_id}\nРоль: {item.role}\nСтатус: {'включена' if item.active else 'выключена'}")
    await callback.message.edit_text(text, reply_markup=role_edit_menu(group_id, item, effective_permissions(item))); await callback.answer()


@router.callback_query(F.data.regexp(r"^role_set:\d+:\d+:(senior|moderator|helper)$"))
async def role_set(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id, role = callback.data.split(":"); group_id, item_id = int(raw_group), int(raw_id)
    if not await owned_group(session, group_id, callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    item = await moderator_row(session, group_id, item_id)
    if not item: await callback.answer("Роль не найдена.", show_alert=True); return
    item.role = role; item.permissions = {}; item.active = True; await session.commit()
    await callback.message.edit_reply_markup(reply_markup=role_edit_menu(group_id, item, effective_permissions(item))); await callback.answer("Роль изменена")


@router.callback_query(F.data.regexp(r"^role_perm:\d+:\d+:(warn|unwarn|mute|unmute|kick|ban|unban|delete|info|history|warnings)$"))
async def role_perm(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id, permission = callback.data.split(":"); group_id, item_id = int(raw_group), int(raw_id)
    if not await owned_group(session, group_id, callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    item = await moderator_row(session, group_id, item_id)
    if not item: await callback.answer("Роль не найдена.", show_alert=True); return
    current = effective_permissions(item); custom = dict(item.permissions or {}); custom[permission] = not bool(current.get(permission, False)); item.permissions = custom
    await session.commit(); await callback.message.edit_reply_markup(reply_markup=role_edit_menu(group_id, item, effective_permissions(item))); await callback.answer("Право изменено")


@router.callback_query(F.data.regexp(r"^role_reset:\d+:\d+$"))
async def role_reset(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":"); group_id, item_id = int(raw_group), int(raw_id)
    if not await owned_group(session, group_id, callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    item = await moderator_row(session, group_id, item_id)
    if not item: await callback.answer("Роль не найдена.", show_alert=True); return
    item.permissions = {}; await session.commit(); await callback.message.edit_reply_markup(reply_markup=role_edit_menu(group_id, item, effective_permissions(item))); await callback.answer("Права сброшены")


@router.callback_query(F.data.regexp(r"^role_toggle:\d+:\d+$"))
async def role_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":"); group_id, item_id = int(raw_group), int(raw_id)
    if not await owned_group(session, group_id, callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    item = await moderator_row(session, group_id, item_id)
    if not item: await callback.answer("Роль не найдена.", show_alert=True); return
    item.active = not item.active; await session.commit(); await callback.message.edit_reply_markup(reply_markup=role_edit_menu(group_id, item, effective_permissions(item))); await callback.answer("Статус изменён")


@router.callback_query(F.data.regexp(r"^role_remove_confirm:\d+:\d+$"))
async def role_remove_ask(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":"); group_id, item_id = int(raw_group), int(raw_id)
    if not await owned_group(session, group_id, callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    item = await moderator_row(session, group_id, item_id)
    if not item: await callback.answer("Роль не найдена.", show_alert=True); return
    await callback.message.edit_text(panel_header("Удалить роль?", f"Telegram ID: {item.user_telegram_id}"), reply_markup=role_remove_confirm(group_id, item_id)); await callback.answer()


@router.callback_query(F.data.regexp(r"^role_remove:\d+:\d+$"))
async def role_remove(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":"); group_id, item_id = int(raw_group), int(raw_id)
    if not await owned_group(session, group_id, callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    item = await moderator_row(session, group_id, item_id)
    if item: await session.delete(item); await session.commit()
    rows = (await session.scalars(select(GroupModerator).where(GroupModerator.group_id == group_id).order_by(GroupModerator.active.desc(), GroupModerator.role))).all()
    await callback.message.edit_text(panel_header("Роли модераторов", "Роль удалена."), reply_markup=await roles_menu(group_id, rows)); await callback.answer("Удалено")


# ---- support center ----

@router.callback_query(F.data == "support:new")
async def support_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ControlForm.support_new)
    await callback.message.edit_text(panel_header("Новое обращение", "Опишите вопрос одним сообщением. Не отправляйте пароли, токены и другие секреты.")); await callback.answer()


@router.message(ControlForm.support_new, F.chat.type == "private")
async def support_new_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    text = (message.text or "").strip()[:4000]
    if len(text) < 4: await message.answer("Опишите вопрос немного подробнее."); return
    ticket = SupportTicket(user_telegram_id=message.from_user.id, text=text, status="new")
    session.add(ticket); await session.commit(); await state.clear()
    await message.answer(panel_header("Обращение создано", f"Номер #{ticket.id}. Ответ появится в этом чате."), reply_markup=support_menu())
    # Best effort notification to service owners; the ticket remains stored if delivery fails.
    from app.core.config import get_settings
    cfg = get_settings()
    recipients = set(cfg.service_owner_ids)
    if cfg.support_chat_id:
        recipients.add(cfg.support_chat_id)
    for owner_id in recipients:
        try:
            await message.bot.send_message(owner_id, f"🆘 Новое обращение #{ticket.id}\nПользователь: <code>{message.from_user.id}</code>\n\n{escape(text)}", reply_markup=service_ticket_menu(ticket.id))
        except Exception as exc:
            import structlog
            structlog.get_logger().warning("support_owner_notification_failed", ticket_id=ticket.id, owner_id=owner_id, error=str(exc))


@router.callback_query(F.data == "support:mine")
async def support_mine(callback: CallbackQuery, session: AsyncSession) -> None:
    rows = (await session.scalars(select(SupportTicket).where(SupportTicket.user_telegram_id == callback.from_user.id).order_by(SupportTicket.created_at.desc()).limit(10))).all()
    lines = [f"• #{t.id} · {t.status} · {t.created_at:%d.%m %H:%M}\n  {escape(t.text[:120])}" for t in rows]
    await callback.message.edit_text(panel_header("Мои обращения") + "\n\n" + ("\n\n".join(lines) if lines else "Обращений пока нет."), reply_markup=support_menu()); await callback.answer()


@router.callback_query(F.data.regexp(r"^ticket:\d+$"))
async def ticket_open(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    ticket_id = int(callback.data.split(":")[1]); ticket = await session.get(SupportTicket, ticket_id)
    if not ticket: await callback.answer("Обращение не найдено.", show_alert=True); return
    text = panel_header(f"Обращение #{ticket.id}", f"Пользователь: {ticket.user_telegram_id}\nСтатус: {ticket.status}") + f"\n\n{escape(ticket.text)}"
    await callback.message.edit_text(text, reply_markup=service_ticket_menu(ticket.id)); await callback.answer()


@router.callback_query(F.data.regexp(r"^ticket_reply:\d+$"))
async def ticket_reply(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not is_service_owner(callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    ticket_id = int(callback.data.split(":")[1]); ticket = await session.get(SupportTicket, ticket_id)
    if not ticket: await callback.answer("Обращение не найдено.", show_alert=True); return
    await state.set_state(ControlForm.ticket_reply); await state.update_data(ticket_id=ticket_id)
    await callback.message.edit_text(panel_header(f"Ответ на #{ticket_id}", "Отправьте ответ пользователю одним сообщением.")); await callback.answer()


@router.message(ControlForm.ticket_reply, F.chat.type == "private")
async def ticket_reply_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not is_service_owner(message.from_user.id): await state.clear(); return
    data = await state.get_data(); ticket = await session.get(SupportTicket, int(data["ticket_id"]))
    if not ticket: await state.clear(); await message.answer("Обращение уже удалено."); return
    text = (message.text or "").strip()[:4000]
    if len(text) < 2: await message.answer("Ответ слишком короткий."); return
    try:
        await message.bot.send_message(ticket.user_telegram_id, panel_header(f"Ответ поддержки · #{ticket.id}") + f"\n\n{escape(text)}", reply_markup=support_menu())
    except Exception:
        await message.answer("Не удалось доставить ответ пользователю. Возможно, он заблокировал бота."); return
    ticket.status = "answered"; await session.commit(); await state.clear()
    await message.answer(panel_header("Ответ отправлен", f"Обращение #{ticket.id}."), reply_markup=service_ticket_menu(ticket.id))


@router.callback_query(F.data.regexp(r"^ticket_close:\d+$"))
async def ticket_close(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    ticket_id = int(callback.data.split(":")[1]); ticket = await session.get(SupportTicket, ticket_id)
    if not ticket: await callback.answer("Обращение не найдено.", show_alert=True); return
    ticket.status = "closed"; await session.commit()
    try: await bot.send_message(ticket.user_telegram_id, f"✅ Обращение #{ticket.id} закрыто.", reply_markup=support_menu())
    except Exception as exc:
        import structlog
        structlog.get_logger().warning("support_close_notification_failed", ticket_id=ticket.id, error=str(exc))
    await callback.message.edit_text(panel_header(f"Обращение #{ticket.id}", "Закрыто."), reply_markup=service_ticket_menu(ticket.id)); await callback.answer("Закрыто")
