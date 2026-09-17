from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyStat, Group, GroupMember, Payment, User
from app.services.access import is_service_owner
from app.services.client_access import set_client_blocked
from app.services.group_health import calculate_group_health
from app.services.manual_plans import apply_manual_plan
from app.services.plans import effective_plan, remaining_days, subscription_state
from app.services.ui import panel_header


router = Router(name=__name__)


def _owner(callback: CallbackQuery) -> bool:
    return is_service_owner(callback.from_user.id)


def _back(callback_data: str, text: str = "◀️ Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _user_name(user: User | None, telegram_id: int | None) -> str:
    if user is None:
        return f"ID {telegram_id or '—'}"
    if user.username:
        return f"@{user.username}"
    full = " ".join(x for x in (user.first_name, user.last_name) if x).strip()
    return full or f"ID {user.telegram_id}"


def _plan_label(group: Group) -> str:
    state = subscription_state(group)
    expires = ""
    if group.plan_expires_at:
        expires = f" · до {group.plan_expires_at:%d.%m.%Y}"
    if state == "trial":
        return f"🧪 TRIAL{expires}"
    if state == "active":
        return f"💎 {effective_plan(group).upper()}{expires}"
    if state == "expired":
        return "⌛ истёк"
    return "🆓 FREE"


async def _clients_screen(callback: CallbackQuery, session: AsyncSession) -> None:
    total = int(await session.scalar(select(func.count()).select_from(User)) or 0)
    blocked = int(await session.scalar(select(func.count()).select_from(User).where(User.service_blocked.is_(True))) or 0)
    owners = int(await session.scalar(
        select(func.count(func.distinct(Group.owner_telegram_id))).where(Group.owner_telegram_id.is_not(None))
    ) or 0)
    rows = [
        [InlineKeyboardButton(text="👤 Все пользователи", callback_data="service:clients:all")],
        [InlineKeyboardButton(text="🏠 Владельцы групп", callback_data="service:clients:owners")],
        [InlineKeyboardButton(text="🚫 Заблокированные", callback_data="service:clients:blocked")],
        [_back("service:home", "◀️ Панель Mimoru")],
    ]
    text = panel_header(
        "Клиенты",
        "Здесь находятся люди и управление их доступом. Платные подписки и TRIAL вынесены в отдельный раздел «Подписки и TRIAL».",
    )
    text += f"\n\n👤 Всего пользователей: {total}\n🏠 Владельцев групп: {owners}\n🚫 Заблокировано: {blocked}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "service:clients")
async def clients_home(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await _clients_screen(callback, session)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service:clients:(all|owners|blocked)$"))
async def clients_list(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    kind = callback.data.rsplit(":", 1)[1]
    group_counts = (
        select(
            Group.owner_telegram_id.label("owner_id"),
            func.count(Group.id).label("groups_count"),
        )
        .where(Group.owner_telegram_id.is_not(None))
        .group_by(Group.owner_telegram_id)
        .subquery()
    )
    query = (
        select(User, func.coalesce(group_counts.c.groups_count, 0))
        .outerjoin(group_counts, group_counts.c.owner_id == User.telegram_id)
    )
    if kind == "blocked":
        query = query.where(User.service_blocked.is_(True))
    elif kind == "owners":
        query = query.where(group_counts.c.owner_id.is_not(None))
    rows = list((await session.execute(query.order_by(User.created_at.desc()).limit(50))).all())
    buttons: list[list[InlineKeyboardButton]] = []
    for user, groups_count in rows:
        mark = "🚫" if user.service_blocked else "👤"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {_user_name(user, user.telegram_id)[:32]} · {int(groups_count)} гр.",
            callback_data=f"service_client:{user.telegram_id}",
        )])
    buttons.append([_back("service:clients", "◀️ К клиентам")])
    title = {"all": "Все пользователи", "owners": "Владельцы групп", "blocked": "Заблокированные"}[kind]
    await callback.message.edit_text(
        panel_header(title, f"Найдено: {len(rows)}. Нажмите человека, чтобы открыть карточку."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_client:\d+$"))
async def client_card(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[1])
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        await callback.answer("Клиент не найден.", show_alert=True)
        return
    groups = list((await session.scalars(
        select(Group).where(Group.owner_telegram_id == telegram_id).order_by(Group.created_at.desc()).limit(30)
    )).all())
    active_groups = sum(1 for group in groups if group.is_active)
    paid_groups = sum(1 for group in groups if subscription_state(group) == "active")
    trial_groups = sum(1 for group in groups if subscription_state(group) == "trial")
    paid_total = int(await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.user_telegram_id == telegram_id,
            Payment.status == "paid",
            Payment.currency == "XTR",
        )
    ) or 0)
    paid_count = int(await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.user_telegram_id == telegram_id,
            Payment.status == "paid",
            Payment.currency == "XTR",
        )
    ) or 0)
    last_payment = await session.scalar(
        select(Payment).where(
            Payment.user_telegram_id == telegram_id,
            Payment.status == "paid",
        ).order_by(Payment.paid_at.desc().nullslast(), Payment.created_at.desc()).limit(1)
    )
    last_payment_text = "—"
    if last_payment is not None:
        when = last_payment.paid_at or last_payment.created_at
        if when:
            last_payment_text = f"{when:%d.%m.%Y} · {last_payment.amount} Stars · {last_payment.plan_code.upper()}"

    rows = [[InlineKeyboardButton(
        text=f"🏠 {group.title[:30]} · {_plan_label(group)}",
        callback_data=f"service_group:{group.id}",
    )] for group in groups]
    action = "unblock" if user.service_blocked else "block"
    rows.append([InlineKeyboardButton(
        text="✅ Разблокировать клиента" if user.service_blocked else "🚫 Заблокировать клиента",
        callback_data=f"service_client_confirm:{telegram_id}:{action}",
    )])
    rows.append([_back("service:clients", "◀️ К клиентам")])

    text = panel_header("Карточка клиента", _user_name(user, telegram_id))
    text += (
        f"\n\nTelegram ID: {telegram_id}"
        f"\nСтатус доступа: {'🚫 заблокирован' if user.service_blocked else '✅ активен'}"
        f"\nГрупп всего: {len(groups)}"
        f"\nАктивных групп: {active_groups}"
        f"\nПлатных групп: {paid_groups}"
        f"\nГрупп на TRIAL: {trial_groups}"
        f"\nУспешных оплат: {paid_count}"
        f"\nОплачено всего: {paid_total} Stars"
        f"\nПоследняя оплата: {last_payment_text}"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_client_confirm:\d+:(block|unblock)$"))
async def client_confirm(callback: CallbackQuery) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, telegram_id, action = callback.data.split(":")
    tid = int(telegram_id)
    text = (
        "Блокировка запретит клиенту пользоваться Mimoru и отключит все его группы."
        if action == "block"
        else "Разблокировка вернёт доступ клиенту. Его группы останутся выключенными, пока вы не включите их отдельно."
    )
    await callback.message.edit_text(
        panel_header("Подтверждение", text),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, подтвердить", callback_data=f"service_client_action:{tid}:{action}")],
            [_back(f"service_client:{tid}", "◀️ Отмена")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_client_action:\d+:(block|unblock)$"))
async def client_action(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, telegram_id, action = callback.data.split(":")
    tid = int(telegram_id)
    result = await set_client_blocked(
        session,
        telegram_id=tid,
        blocked=action == "block",
    )
    if result is None:
        await callback.answer("Клиент не найден.", show_alert=True)
        return
    callback.data = f"service_client:{tid}"
    await client_card(callback, session)


async def _groups_screen(callback: CallbackQuery, session: AsyncSession, mode: str = "all") -> None:
    query = select(Group)
    if mode == "active":
        query = query.where(Group.is_active.is_(True))
    elif mode == "disabled":
        query = query.where(Group.is_active.is_(False))
    groups = list((await session.scalars(query.order_by(Group.created_at.desc()).limit(50))).all())
    total = int(await session.scalar(select(func.count()).select_from(Group)) or 0)
    active = int(await session.scalar(select(func.count()).select_from(Group).where(Group.is_active.is_(True))) or 0)
    rows = [[
        InlineKeyboardButton(text="✅ Активные", callback_data="service:groups:active"),
        InlineKeyboardButton(text="⛔ Отключённые", callback_data="service:groups:disabled"),
    ]]
    for group in groups:
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if group.is_active else '⛔'} {group.title[:28]} · {_plan_label(group)}",
            callback_data=f"service_group:{group.id}",
        )])
    rows.append([_back("service:home", "◀️ Панель Mimoru")])
    text = panel_header("Группы", "Нажмите группу — откроется полная карточка, диагностика, статистика, тариф и управление подключением.")
    text += f"\n\nВсего: {total}\nАктивных: {active}\nОтключённых: {total - active}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "service:groups")
async def groups_home(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await _groups_screen(callback, session)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service:groups:(active|disabled)$"))
async def groups_filtered(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await _groups_screen(callback, session, callback.data.rsplit(":", 1)[1])
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_group:\d+$"))
async def group_card(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group = await session.get(Group, int(callback.data.split(":")[1]))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    owner = await session.scalar(select(User).where(User.telegram_id == group.owner_telegram_id)) if group.owner_telegram_id else None
    members = int(await session.scalar(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id, GroupMember.is_present.is_(True))
    ) or 0)
    days = remaining_days(group)
    expires = group.plan_expires_at.strftime("%d.%m.%Y %H:%M UTC") if group.plan_expires_at else "без срока"
    rows = [
        [
            InlineKeyboardButton(text="🩺 Проверить Telegram", callback_data=f"service_group_health:{group.id}"),
            InlineKeyboardButton(text="📊 Статистика", callback_data=f"service_group_stats:{group.id}"),
        ],
        [InlineKeyboardButton(text="💎 Управление тарифом", callback_data=f"service_plan:{group.id}")],
    ]
    if group.owner_telegram_id:
        rows.append([InlineKeyboardButton(text="👤 Открыть владельца", callback_data=f"service_client:{group.owner_telegram_id}")])
    action = "disable" if group.is_active else "enable"
    rows.append([InlineKeyboardButton(
        text="⛔ Отключить обслуживание" if group.is_active else "✅ Включить обслуживание",
        callback_data=f"service_group_confirm:{group.id}:{action}",
    )])
    rows.append([_back("service:groups", "◀️ Ко всем группам")])
    text = panel_header("Карточка группы", group.title)
    text += (
        f"\n\nВнутренний ID: #{group.id}"
        f"\nTelegram chat ID: {group.telegram_chat_id}"
        f"\nОбслуживание Mimoru: {'✅ включено' if group.is_active else '⛔ выключено'}"
        f"\nВладелец: {_user_name(owner, group.owner_telegram_id)}"
        f"\nID владельца: {group.owner_telegram_id or '—'}"
        f"\nУчастников в базе: {members}"
        f"\nТариф: {_plan_label(group)}"
        f"\nСрок: {expires}"
    )
    if days is not None:
        text += f"\nОсталось дней: {days}"
    if group.created_at:
        text += f"\nДобавлена в Mimoru: {group.created_at:%d.%m.%Y}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_group_health:\d+$"))
async def service_group_health(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group = await session.get(Group, int(callback.data.split(":")[-1]))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    health = await calculate_group_health(bot, session, group)
    lines = [
        f"{'✅' if health.bot_is_admin else '❌'} Бот состоит в группе как администратор",
        f"{'✅' if health.can_delete_messages else '❌'} Может удалять сообщения",
        f"{'✅' if health.can_restrict_members else '❌'} Может ограничивать участников",
        f"{'✅' if health.can_invite_users else '❌'} Может управлять приглашениями",
        f"Оценка: {health.score}/100 · {health.level}",
    ]
    if health.recommendations:
        lines += ["", "Что исправить:"] + [f"• {item}" for item in health.recommendations]
    await callback.message.edit_text(
        panel_header("Проверка Telegram", group.title) + "\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"service_group_health:{group.id}")],
            [_back(f"service_group:{group.id}", "◀️ К карточке группы")],
        ]),
    )
    await callback.answer("Проверено")


@router.callback_query(F.data.regexp(r"^service_group_stats:\d+$"))
async def service_group_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group = await session.get(Group, int(callback.data.split(":")[-1]))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    messages = int(await session.scalar(
        select(func.coalesce(func.sum(DailyStat.messages_count), 0)).where(DailyStat.group_id == group.id)
    ) or 0)
    deleted = int(await session.scalar(
        select(func.coalesce(func.sum(DailyStat.deleted_count), 0)).where(DailyStat.group_id == group.id)
    ) or 0)
    active_users = int(await session.scalar(
        select(func.count(func.distinct(DailyStat.user_telegram_id))).where(
            DailyStat.group_id == group.id,
            DailyStat.messages_count > 0,
        )
    ) or 0)
    await callback.message.edit_text(
        panel_header("Служебная статистика группы", group.title)
        + f"\n\nСообщений учтено: {messages}\nАктивных участников в статистике: {active_users}\nУдалено сообщений: {deleted}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_back(f"service_group:{group.id}", "◀️ К карточке группы")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_group_confirm:\d+:(enable|disable)$"))
async def group_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, gid, action = callback.data.split(":")
    group = await session.get(Group, int(gid))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    description = (
        "Mimoru перестанет обслуживать эту группу. Бот физически не удаляется из Telegram-группы, а данные и настройки сохраняются."
        if action == "disable"
        else "Mimoru снова начнёт обслуживать эту группу с сохранёнными настройками."
    )
    await callback.message.edit_text(
        panel_header("Подтверждение", f"{group.title}\n\n{description}"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, подтвердить", callback_data=f"service_group_action:{group.id}:{action}")],
            [_back(f"service_group:{group.id}", "◀️ Отмена")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_group_action:\d+:(enable|disable)$"))
async def group_action(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, gid, action = callback.data.split(":")
    group = await session.get(Group, int(gid))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    group.is_active = action == "enable"
    await session.commit()
    await group_card(callback, session)


@router.callback_query(F.data.regexp(r"^service_plan:\d+$"))
async def service_plan(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group = await session.get(Group, int(callback.data.split(":")[-1]))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    expires = group.plan_expires_at.strftime("%d.%m.%Y %H:%M UTC") if group.plan_expires_at else "без срока"
    await callback.message.edit_text(
        panel_header(
            "Управление тарифом",
            f"{group.title}\n\nТекущий тариф: {effective_plan(group).upper()}\nСрок: {expires}\n\nВыберите тариф. Изменение требует подтверждения.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧪 TRIAL · 7 дней", callback_data=f"service_plan_confirm:{group.id}:trial:7")],
            [InlineKeyboardButton(text="⭐ STANDARD · 30 дней", callback_data=f"service_plan_confirm:{group.id}:standard:30")],
            [InlineKeyboardButton(text="💎 PRO · 30 дней", callback_data=f"service_plan_confirm:{group.id}:pro:30")],
            [InlineKeyboardButton(text="🆓 Перевести на FREE", callback_data=f"service_plan_confirm:{group.id}:free:0")],
            [_back(f"service_group:{group.id}", "◀️ К карточке группы")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_plan_confirm:\d+:(free|trial|standard|pro):(0|7|30)$"))
async def service_plan_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, raw_gid, plan_code, raw_days = callback.data.split(":")
    group = await session.get(Group, int(raw_gid))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Подтвердите тариф",
            f"Группа: {group.title}\nНовый тариф: {plan_code.upper()}\nСрок: {'без срока' if plan_code == 'free' else raw_days + ' дней'}",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, подтвердить", callback_data=f"service_plan_action:{group.id}:{plan_code}:{raw_days}")],
            [_back(f"service_plan:{group.id}", "◀️ Отмена")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_plan_action:\d+:(free|trial|standard|pro):(0|7|30)$"))
async def service_plan_action(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, raw_gid, plan_code, raw_days = callback.data.split(":")
    days = int(raw_days)
    group = await apply_manual_plan(
        session,
        group_id=int(raw_gid),
        actor_id=callback.from_user.id,
        plan_code=plan_code,
        days=days,
    )
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    callback.data = f"service_plan:{group.id}"
    await service_plan(callback, session)


@router.callback_query(F.data == "service:subscriptions")
async def subscriptions(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    now = datetime.now(timezone.utc)
    paid = list((await session.scalars(select(Group).where(
        Group.is_active.is_(True),
        Group.plan_code.in_(["standard", "pro"]),
        Group.plan_expires_at > now,
    ).order_by(Group.plan_expires_at.asc()))).all())
    trial = list((await session.scalars(select(Group).where(
        Group.is_active.is_(True),
        Group.plan_code == "trial",
        Group.plan_expires_at > now,
    ).order_by(Group.plan_expires_at.asc()))).all())
    rows = [
        [
            InlineKeyboardButton(text=f"💎 Платные ({len(paid)})", callback_data="service:subscriptions:paid"),
            InlineKeyboardButton(text=f"🧪 TRIAL ({len(trial)})", callback_data="service:subscriptions:trial"),
        ],
        [InlineKeyboardButton(text="⌛ Истекают ≤7 дней", callback_data="service:subscriptions:expiring")],
        [_back("service:home", "◀️ Панель Mimoru")],
    ]
    await callback.message.edit_text(
        panel_header("Подписки и TRIAL", "Коммерческие статусы находятся здесь отдельно от списка клиентов. Выберите категорию, затем группу."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service:subscriptions:(paid|trial|expiring)$"))
async def subscriptions_list(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _owner(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    kind = callback.data.rsplit(":", 1)[1]
    now = datetime.now(timezone.utc)
    query = select(Group).where(Group.is_active.is_(True), Group.plan_expires_at > now)
    if kind == "paid":
        query = query.where(Group.plan_code.in_(["standard", "pro"]))
    elif kind == "trial":
        query = query.where(Group.plan_code == "trial")
    else:
        query = query.where(
            Group.plan_expires_at <= now + timedelta(days=7),
            Group.plan_code.in_(["trial", "standard", "pro"]),
        )
    groups = list((await session.scalars(query.order_by(Group.plan_expires_at.asc()).limit(50))).all())
    rows = [[InlineKeyboardButton(
        text=f"{_plan_label(group)} · {group.title[:30]}",
        callback_data=f"service_group:{group.id}",
    )] for group in groups]
    rows.append([_back("service:subscriptions", "◀️ К подпискам")])
    title = {"paid": "Платные подписки", "trial": "Тестовые периоды", "expiring": "Истекают в течение 7 дней"}[kind]
    await callback.message.edit_text(
        panel_header(title, f"Найдено: {len(groups)}. Нажмите группу для карточки и управления тарифом."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()
