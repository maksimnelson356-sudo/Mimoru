from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    ForbiddenWord,
    Group,
    GroupModerator,
    ModerationLog,
    RequiredChannel,
)
from app.keyboards.panel import (
    analytics_menu,
    back_to_group,
    channels_admin_menu,
    content_menu,
    group_menu,
    groups_menu,
    main_menu,
    members_menu,
    moderation_menu,
    protection_menu,
    roles_menu,
    settings_menu,
    subscription_back,
    subscription_menu,
    words_admin_menu,
)
from app.services.access import is_service_owner
from app.services.plans import (
    effective_plan,
    feature_available,
    plan_limit,
    remaining_days,
    subscription_state,
)
from app.services.public_identity import public_user_token
from app.services.ui import panel_header

router = Router(name=__name__)
settings = get_settings()

COMMANDS_TEXT = (
    panel_header("Команды и помощь") + "\n\n<b>Модерация — ответом на сообщение</b>\n"
    "<code>бан</code> или <code>бан 7д</code>, <code>разбан</code>, <code>мут 2ч</code>, "
    "<code>размут</code>, <code>пред</code>, "
    "<code>снять пред</code>, <code>преды</code>, <code>инфо</code>, "
    "<code>история</code>, <code>удалить</code>.\n"
    "Для предупреждения, мута и бана Mimoru предложит причины кнопками.\n\n"
    "<b>Защита</b>\n"
    "<code>антифлуд вкл/выкл</code>, <code>антифлуд 6 за 10с</code>\n"
    "<code>ссылки вкл/выкл</code>, <code>повторы вкл/выкл</code>, "
    "<code>капс вкл/выкл</code>, <code>капс лимит 70</code>\n"
    "<code>голосовые/стикеры/пересылки вкл/выкл</code>\n"
    "<code>добавить слово ...</code>, <code>удалить слово ...</code>\n\n"
    "<b>Группа</b>\n"
    "<code>капча вкл/выкл</code>, <code>приветствие вкл/выкл</code>\n"
    "<code>изменить приветствие ТЕКСТ</code>, <code>изменить правила ТЕКСТ</code>\n"
    "<code>добавить подписку @channel</code>, <code>удалить подписку @channel</code>\n"
    "<code>статистика сегодня/неделя/месяц</code>, <code>жалоба</code>."
)


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


@router.message(
    F.chat.type == "private",
    F.text.casefold().in_({"панель", "меню", "настройки", "мои группы"}),
)
async def open_panel(message: Message) -> None:
    await message.answer(
        panel_header("Главная", "Управление Telegram-сообществами"),
        reply_markup=main_menu(message.from_user.id in settings.service_owner_ids),
    )


@router.callback_query(F.data == "panel:home")
async def panel_home(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        panel_header("Главная", "Выберите нужный раздел"),
        reply_markup=main_menu(callback.from_user.id in settings.service_owner_ids),
    )
    await callback.answer()


@router.callback_query(F.data == "panel:commands")
async def panel_commands(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        COMMANDS_TEXT,
        reply_markup=main_menu(callback.from_user.id in settings.service_owner_ids),
    )
    await callback.answer()


@router.callback_query(F.data == "panel:groups")
async def panel_groups(callback: CallbackQuery, session: AsyncSession) -> None:
    groups = (
        await session.scalars(
            select(Group)
            .where(
                Group.owner_telegram_id == callback.from_user.id,
                Group.is_active.is_(True),
            )
            .order_by(Group.title)
        )
    ).all()
    if not groups:
        await callback.message.edit_text(
            panel_header(
                "Мои группы",
                "Подключённых групп пока нет. Добавьте Mimoru администратором и напишите в группе «подключить».",
            ),
            reply_markup=main_menu(callback.from_user.id in settings.service_owner_ids),
        )
    else:
        await callback.message.edit_text(
            panel_header("Мои группы", "Выберите группу для управления"),
            reply_markup=groups_menu(groups),
        )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^group:\d+$"))
async def open_group(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            group.title,
            f"ID: {group.telegram_chat_id}\nТариф: {effective_plan(group).upper()}",
        ),
        reply_markup=group_menu(group),
    )
    await callback.answer()


@router.callback_query(
    F.data.regexp(
        r"^group_section:\d+:(analytics|protection|members|content|moderation|settings)$"
    )
)
async def group_section(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group_id, section = callback.data.split(":")
    group = await owned_group(session, int(raw_group_id), callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if section == "analytics":
        text = panel_header("Аналитика", f"Группа: {group.title}")
        keyboard = analytics_menu(group.id)
    elif section == "protection":
        text = panel_header("Защита", "Основные фильтры и вход новых участников")
        keyboard = protection_menu(group)
    elif section == "members":
        text = panel_header("Участники", "Активность, роли и журнал действий")
        keyboard = members_menu(group.id)
    elif section == "moderation":
        text = panel_header("Модерация", "Наказания, причины, роли и история действий")
        keyboard = moderation_menu(group.id)
    elif section == "settings":
        text = panel_header("Настройки", "Поведение Mimoru и параметры группы")
        keyboard = settings_menu(group)
    else:
        text = panel_header("Контент", "Слова, ссылки и обязательные каналы")
        keyboard = content_menu(group.id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(
    F.data.regexp(
        r"^toggle:\d+:(antiflood|links|captcha|welcome|repeats|caps|quarantine|edit|mentions|sender|raid|reports|night|joinreq)$"
    )
)
async def toggle_setting(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group_id, setting = callback.data.split(":")
    group = await owned_group(
        session, int(raw_group_id), callback.from_user.id, for_update=True
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if setting == "reports" and not feature_available(group, "daily_reports"):
        await callback.answer(
            "Ежедневные отчёты доступны на TRIAL, STANDARD и PRO.", show_alert=True
        )
        return
    attr = {
        "antiflood": "antiflood_enabled",
        "links": "links_enabled",
        "captcha": "captcha_enabled",
        "welcome": "welcome_enabled",
        "repeats": "repeats_enabled",
        "caps": "caps_enabled",
        "quarantine": "newcomer_quarantine_enabled",
        "edit": "edit_protection_enabled",
        "mentions": "mention_filter_enabled",
        "sender": "sender_chat_filter_enabled",
        "raid": "anti_raid_enabled",
        "reports": "reports_enabled",
        "night": "night_mode_enabled",
        "joinreq": "join_requests_enabled",
    }[setting]
    setattr(group.settings, attr, not getattr(group.settings, attr))
    await session.commit()
    if setting in {"welcome", "reports", "night", "joinreq"}:
        markup = settings_menu(group)
    else:
        markup = protection_menu(group)
    await callback.message.edit_reply_markup(reply_markup=markup)
    await callback.answer("Настройка изменена.")


@router.callback_query(F.data.regexp(r"^words:\d+$"))
async def show_words(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    words = (
        await session.scalars(
            select(ForbiddenWord.word)
            .where(ForbiddenWord.group_id == group.id)
            .order_by(ForbiddenWord.word)
        )
    ).all()
    text = (
        panel_header(
            "Запрещённые слова", "Нажмите строку, чтобы удалить её, или добавьте новую."
        )
        + "\n\n"
    )
    text += "\n".join(f"• {word}" for word in words[:50]) if words else "Список пуст."
    await callback.message.edit_text(
        text, reply_markup=words_admin_menu(group.id, words)
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^channels:\d+$"))
async def show_channels(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    channels = (
        await session.scalars(
            select(RequiredChannel.channel_username)
            .where(
                RequiredChannel.group_id == group.id,
                RequiredChannel.active.is_(True),
            )
            .order_by(RequiredChannel.channel_username)
        )
    ).all()
    text = (
        panel_header(
            "Обязательные каналы", "Нажмите канал для удаления или добавьте новый."
        )
        + "\n\n"
    )
    text += (
        "\n".join(f"• {channel}" for channel in channels)
        if channels
        else "Список пуст — проверка подписки не используется."
    )
    await callback.message.edit_text(
        text, reply_markup=channels_admin_menu(group.id, channels)
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^logs:\d+$"))
async def show_logs(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    logs = (
        await session.scalars(
            select(ModerationLog)
            .where(ModerationLog.group_id == group.id)
            .order_by(ModerationLog.created_at.desc())
            .limit(30)
        )
    ).all()
    names = {
        "ban": "бан",
        "unban": "разбан",
        "mute": "мут",
        "unmute": "размут",
        "kick": "кик",
        "warn": "предупреждение",
        "unwarn": "снято предупреждение",
        "auto_mute": "автомут",
        "remove_by_ban": "снятие ранга (бан)",
        "remove_by_kick": "снятие ранга (кик)",
        "unwarn_all": "сняты все предупреждения",
        "deleted_accounts_cleanup": "очистка удалённых аккаунтов",
        "complaint_resolved": "жалоба обработана",
        "complaint_rejected": "жалоба отклонена",
    }
    lines = []
    for item in logs:
        target = (
            public_user_token(item.target_telegram_id)
            if item.target_telegram_id
            else "—"
        )
        actor = (
            public_user_token(item.actor_telegram_id) if item.actor_telegram_id else "—"
        )
        action_label = names.get(item.action, item.action)
        reason_part = (
            f" · {item.reason[:50]}…"
            if item.reason and len(item.reason) > 50
            else (f" · {item.reason}" if item.reason else "")
        )
        lines.append(
            f"• {item.created_at:%d.%m %H:%M} — {action_label}: {target} "
            f"(модератор {actor}){reason_part}"
        )
    text = panel_header("Журнал модерации") + "\n\n"
    text += "\n".join(lines) if lines else "Журнал пока пуст."
    await callback.message.edit_text(text, reply_markup=back_to_group(group.id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^roles:\d+$"))
async def show_roles(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    items = (
        await session.scalars(
            select(GroupModerator)
            .where(GroupModerator.group_id == group.id)
            .order_by(
                GroupModerator.active.desc(),
                GroupModerator.role,
                GroupModerator.user_telegram_id,
            )
        )
    ).all()
    active_count = sum(1 for item in items if item.active)
    text = panel_header(
        "Роли модераторов",
        f"Активных: {active_count}. Права каждой роли можно настроить отдельно.",
    )
    await callback.message.edit_text(text, reply_markup=roles_menu(group.id, items))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plan:\d+$"))
async def show_plan(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    words_count = len(
        (
            await session.scalars(
                select(ForbiddenWord.id).where(ForbiddenWord.group_id == group.id)
            )
        ).all()
    )
    channels_count = len(
        (
            await session.scalars(
                select(RequiredChannel.id).where(
                    RequiredChannel.group_id == group.id,
                    RequiredChannel.active.is_(True),
                )
            )
        ).all()
    )
    plan = effective_plan(group)
    state = subscription_state(group)
    days = remaining_days(group)
    expires = (
        group.plan_expires_at.strftime("%d.%m.%Y %H:%M UTC")
        if group.plan_expires_at
        else "без срока"
    )
    state_label = {
        "active": "✅ активна",
        "trial": "🧪 тестовый период",
        "expired": "⌛ истекла",
        "free": "🆓 бесплатный",
    }[state]
    left = "" if days is None else f"\nОсталось: <b>{days} дн.</b>"
    text = (
        panel_header("Подписка и тариф", group.title)
        + f"\n\nТариф: <b>{plan.upper()}</b> · {state_label}\n"
        f"Действует до: {expires}{left}\n\n"
        f"🚫 Запрещённые слова: {words_count}/{plan_limit(group, 'words')}\n"
        f"📣 Обязательные каналы: {channels_count}/{plan_limit(group, 'channels')}\n"
        f"👮 Лимит модераторов: {plan_limit(group, 'moderators')}\n"
        f"📌 Лимит причин: {plan_limit(group, 'reasons')}\n\n"
        "Продление добавляет 30 дней к текущему сроку, если подписка ещё активна."
    )
    await callback.message.edit_text(text, reply_markup=subscription_menu(group.id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plan_compare:\d+$"))
async def plan_compare(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    text = panel_header("Сравнение тарифов", group.title) + (
        "\n\n<b>FREE</b>\nБазовая модерация и защита · 10 слов · 1 канал.\n"
        "\n<b>STANDARD · 250 ⭐ / 30 дней</b>\nРасширенная защита и аналитика, отчёты, реклама · 100 слов · 3 канала · 10 модераторов.\n"
        "\n<b>PRO · 500 ⭐ / 30 дней</b>\nВсе возможности Mimoru, максимальные лимиты, приоритетная поддержка · 1000 слов · 10 каналов · 50 модераторов."
    )
    await callback.message.edit_text(text, reply_markup=subscription_back(group.id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plan_history:\d+$"))
async def plan_history(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    from app.db.models import Payment

    rows = (
        await session.scalars(
            select(Payment)
            .where(Payment.group_id == group.id)
            .order_by(Payment.created_at.desc())
            .limit(15)
        )
    ).all()
    lines = []
    for row in rows:
        status = {"paid": "✅", "pending": "⏳", "failed": "❌"}.get(row.status, "•")
        date = (
            (row.paid_at or row.created_at).strftime("%d.%m.%Y")
            if (row.paid_at or row.created_at)
            else "—"
        )
        lines.append(
            f"{status} {date} · {row.plan_code.upper()} · {row.amount} ⭐ · {row.status}"
        )
    text = (
        panel_header("История платежей", group.title)
        + "\n\n"
        + ("\n".join(lines) if lines else "Платежей пока нет.")
    )
    await callback.message.edit_text(text, reply_markup=subscription_back(group.id))
    await callback.answer()
