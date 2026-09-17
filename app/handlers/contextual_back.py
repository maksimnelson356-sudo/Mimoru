from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyStat, Group, GroupMember, Payment, User
from app.handlers.member_center import _member_card
from app.services.access import accessible_group, is_service_owner
from app.services.client_access import set_client_blocked, set_group_service_active
from app.services.group_health import calculate_group_health
from app.services.group_refs import group_reference_label
from app.services.manual_plans import apply_manual_plan
from app.services.plans import effective_plan, remaining_days, subscription_state
from app.services.ui import panel_header

router = Router(name=__name__)


def _title(text: str | None) -> str:
    first = (text or "").splitlines()[0].strip()
    prefix = "🟣 Mimoru · "
    return first[len(prefix):] if first.startswith(prefix) else first


def _plan_label(group: Group) -> str:
    state = subscription_state(group)
    if state == "trial":
        return "🧪 TRIAL"
    if state == "active":
        return f"💎 {effective_plan(group).upper()}"
    if state == "expired":
        return "⌛ истёк"
    return "🆓 FREE"


def _user_name(user: User | None, telegram_id: int | None) -> str:
    if user is None:
        return f"ID {telegram_id or '—'}"
    if user.username:
        return f"@{user.username}"
    full = " ".join(x for x in (user.first_name, user.last_name) if x).strip()
    return full or f"ID {user.telegram_id}"


def _service_source(callback: CallbackQuery, group: Group) -> str:
    title = _title(callback.message.text if callback.message else None)
    if title == "Карточка клиента" and group.owner_telegram_id:
        return f"c{group.owner_telegram_id}"
    return {
        "Платные подписки": "sp",
        "Тестовые периоды": "st",
        "Истекают в течение 7 дней": "se",
    }.get(title, "g")


def _service_back(source: str) -> tuple[str, str]:
    if source.startswith("c") and source[1:].isdigit():
        return f"cl:{source[1:]}", "◀️ К карточке клиента"
    if source == "sp":
        return "service:subscriptions:paid", "◀️ К платным подпискам"
    if source == "st":
        return "service:subscriptions:trial", "◀️ К TRIAL"
    if source == "se":
        return "service:subscriptions:expiring", "◀️ К истекающим"
    return "service:groups", "◀️ Ко всем группам"


async def _render_group(callback: CallbackQuery, bot: Bot, session: AsyncSession, group: Group, source: str) -> None:
    owner = await session.scalar(select(User).where(User.telegram_id == group.owner_telegram_id)) if group.owner_telegram_id else None
    members = int(await session.scalar(select(func.count()).select_from(GroupMember).where(
        GroupMember.group_id == group.id,
        GroupMember.is_present.is_(True),
    )) or 0)
    identity = await group_reference_label(bot, group)
    days = remaining_days(group)
    expires = group.plan_expires_at.strftime("%d.%m.%Y %H:%M UTC") if group.plan_expires_at else "без срока"
    back_callback, back_text = _service_back(source)
    rows = [
        [InlineKeyboardButton(text="💎 Управление тарифом", callback_data=f"cp:{source}:{group.id}")],
        [
            InlineKeyboardButton(text="🩺 Проверить Telegram", callback_data=f"ch:{source}:{group.id}"),
            InlineKeyboardButton(text="📊 Статистика", callback_data=f"cs:{source}:{group.id}"),
        ],
    ]
    if group.owner_telegram_id:
        rows.append([InlineKeyboardButton(text="👤 Открыть владельца", callback_data=f"cc:{source}:{group.id}:{group.owner_telegram_id}")])
    action = "disable" if group.is_active else "enable"
    rows.append([InlineKeyboardButton(
        text="⛔ Отключить обслуживание" if group.is_active else "✅ Включить обслуживание",
        callback_data=f"gc:{source}:{group.id}:{action}",
    )])
    rows.append([InlineKeyboardButton(text=back_text, callback_data=back_callback)])
    text = panel_header("Карточка группы", identity)
    text += (
        f"\n\nСтатус Mimoru: {'✅ обслуживание включено' if group.is_active else '⛔ обслуживание отключено'}"
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


@router.callback_query(F.data.regexp(r"^service_group:\d+$"))
async def service_group_context(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group = await session.get(Group, int(callback.data.split(":")[-1]))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    await _render_group(callback, bot, session, group, _service_source(callback, group))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cg:(g|sp|st|se|c\d+):\d+$"))
async def service_group_explicit(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, source, raw_gid = callback.data.split(":")
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group = await session.get(Group, int(raw_gid))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    await _render_group(callback, bot, session, group, source)
    await callback.answer()


async def _render_client(
    callback: CallbackQuery,
    session: AsyncSession,
    telegram_id: int,
    *,
    source: str = "l",
    group_id: int = 0,
) -> None:
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
    paid_total = int(await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.user_telegram_id == telegram_id, Payment.status == "paid", Payment.currency == "XTR",
    )) or 0)
    paid_count = int(await session.scalar(select(func.count()).select_from(Payment).where(
        Payment.user_telegram_id == telegram_id, Payment.status == "paid", Payment.currency == "XTR",
    )) or 0)
    last_payment = await session.scalar(select(Payment).where(
        Payment.user_telegram_id == telegram_id, Payment.status == "paid",
    ).order_by(Payment.paid_at.desc().nullslast(), Payment.created_at.desc()).limit(1))
    last_payment_text = "—"
    if last_payment is not None:
        when = last_payment.paid_at or last_payment.created_at
        if when:
            last_payment_text = f"{when:%d.%m.%Y} · {last_payment.amount} Stars · {last_payment.plan_code.upper()}"
    rows = [[InlineKeyboardButton(
        text=f"🏠 {group.title[:30]} · {_plan_label(group)}",
        callback_data=f"cg:c{telegram_id}:{group.id}",
    )] for group in groups]
    action = "unblock" if user.service_blocked else "block"
    rows.append([InlineKeyboardButton(
        text="✅ Разблокировать клиента" if user.service_blocked else "🚫 Заблокировать клиента",
        callback_data=f"uc:{source}:{group_id}:{telegram_id}:{action}",
    )])
    if source == "l":
        back_callback = "service:clients"
    else:
        back_callback = f"cg:{source}:{group_id}"
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)])
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


@router.callback_query(F.data.regexp(r"^service_client:\d+$"))
async def service_client_context(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await _render_client(callback, session, int(callback.data.split(":")[-1]))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cl:\d+$"))
async def service_client_from_list(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await _render_client(callback, session, int(callback.data.split(":")[-1]))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cc:(g|sp|st|se|c\d+):\d+:\d+$"))
async def service_client_from_group(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_gid, raw_tid = callback.data.split(":")
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await _render_client(callback, session, int(raw_tid), source=source, group_id=int(raw_gid))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^uc:(l|g|sp|st|se|c\d+):\d+:\d+:(block|unblock)$"))
async def service_client_confirm_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_gid, raw_tid, action = callback.data.split(":")
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    text = (
        "Блокировка запретит клиенту пользоваться Mimoru и отключит все его группы."
        if action == "block" else
        "Разблокировка вернёт доступ клиенту. Его группы останутся выключенными, пока вы не включите их отдельно."
    )
    back = f"cl:{raw_tid}" if source == "l" else f"cc:{source}:{raw_gid}:{raw_tid}"
    await callback.message.edit_text(
        panel_header("Подтверждение", text),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, подтвердить", callback_data=f"ua:{source}:{raw_gid}:{raw_tid}:{action}")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=back)],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ua:(l|g|sp|st|se|c\d+):\d+:\d+:(block|unblock)$"))
async def service_client_apply_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_gid, raw_tid, action = callback.data.split(":")
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    result = await set_client_blocked(session, telegram_id=int(raw_tid), blocked=action == "block")
    if result is None:
        await callback.answer("Клиент не найден.", show_alert=True)
        return
    await _render_client(
        callback,
        session,
        int(raw_tid),
        source=source,
        group_id=int(raw_gid),
    )
    await callback.answer("Сохранено")


async def _render_plan(callback: CallbackQuery, group: Group, source: str) -> None:
    expires = group.plan_expires_at.strftime("%d.%m.%Y %H:%M UTC") if group.plan_expires_at else "без срока"
    await callback.message.edit_text(
        panel_header("Управление тарифом", f"{group.title}\n\nТекущий тариф: {effective_plan(group).upper()}\nСрок: {expires}\n\nВыберите тариф. Изменение требует подтверждения."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧪 TRIAL · 7 дней", callback_data=f"pc:{source}:{group.id}:trial:7")],
            [InlineKeyboardButton(text="⭐ STANDARD · 30 дней", callback_data=f"pc:{source}:{group.id}:standard:30")],
            [InlineKeyboardButton(text="💎 PRO · 30 дней", callback_data=f"pc:{source}:{group.id}:pro:30")],
            [InlineKeyboardButton(text="🆓 Перевести на FREE", callback_data=f"pc:{source}:{group.id}:free:0")],
            [InlineKeyboardButton(text="◀️ К карточке группы", callback_data=f"cg:{source}:{group.id}")],
        ]),
    )


@router.callback_query(F.data.regexp(r"^cp:(g|sp|st|se|c\d+):\d+$"))
async def service_plan_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_gid = callback.data.split(":")
    group = await session.get(Group, int(raw_gid))
    if not is_service_owner(callback.from_user.id) or group is None:
        await callback.answer("Нет доступа или группа не найдена.", show_alert=True)
        return
    await _render_plan(callback, group, source)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^pc:(g|sp|st|se|c\d+):\d+:(free|trial|standard|pro):(0|7|30)$"))
async def service_plan_confirm_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_gid, plan_code, raw_days = callback.data.split(":")
    group = await session.get(Group, int(raw_gid))
    if not is_service_owner(callback.from_user.id) or group is None:
        await callback.answer("Нет доступа или группа не найдена.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header("Подтвердите тариф", f"Группа: {group.title}\nНовый тариф: {plan_code.upper()}\nСрок: {'без срока' if plan_code == 'free' else raw_days + ' дней'}"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, подтвердить", callback_data=f"pa:{source}:{group.id}:{plan_code}:{raw_days}")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cp:{source}:{group.id}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^pa:(g|sp|st|se|c\d+):\d+:(free|trial|standard|pro):(0|7|30)$"))
async def service_plan_apply_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_gid, plan_code, raw_days = callback.data.split(":")
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group = await apply_manual_plan(
        session,
        group_id=int(raw_gid),
        actor_id=callback.from_user.id,
        plan_code=plan_code,
        days=int(raw_days),
    )
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    await _render_plan(callback, group, source)
    await callback.answer("Сохранено")


@router.callback_query(F.data.regexp(r"^ch:(g|sp|st|se|c\d+):\d+$"))
async def service_health_context(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, source, raw_gid = callback.data.split(":")
    group = await session.get(Group, int(raw_gid))
    if not is_service_owner(callback.from_user.id) or group is None:
        await callback.answer("Нет доступа или группа не найдена.", show_alert=True)
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
            [InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"ch:{source}:{group.id}")],
            [InlineKeyboardButton(text="◀️ К карточке группы", callback_data=f"cg:{source}:{group.id}")],
        ]),
    )
    await callback.answer("Проверено")


@router.callback_query(F.data.regexp(r"^cs:(g|sp|st|se|c\d+):\d+$"))
async def service_stats_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_gid = callback.data.split(":")
    group = await session.get(Group, int(raw_gid))
    if not is_service_owner(callback.from_user.id) or group is None:
        await callback.answer("Нет доступа или группа не найдена.", show_alert=True)
        return
    messages = int(await session.scalar(select(func.coalesce(func.sum(DailyStat.messages_count), 0)).where(DailyStat.group_id == group.id)) or 0)
    deleted = int(await session.scalar(select(func.coalesce(func.sum(DailyStat.deleted_count), 0)).where(DailyStat.group_id == group.id)) or 0)
    active_users = int(await session.scalar(select(func.count(func.distinct(DailyStat.user_telegram_id))).where(DailyStat.group_id == group.id, DailyStat.messages_count > 0)) or 0)
    await callback.message.edit_text(
        panel_header("Служебная статистика группы", group.title)
        + f"\n\nСообщений учтено: {messages}\nАктивных участников в статистике: {active_users}\nУдалено сообщений: {deleted}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К карточке группы", callback_data=f"cg:{source}:{group.id}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^gc:(g|sp|st|se|c\d+):\d+:(enable|disable)$"))
async def service_group_confirm_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, source, raw_gid, action = callback.data.split(":")
    group = await session.get(Group, int(raw_gid))
    if not is_service_owner(callback.from_user.id) or group is None:
        await callback.answer("Нет доступа или группа не найдена.", show_alert=True)
        return
    description = (
        "Mimoru перестанет обслуживать эту группу. Бот физически не удаляется из Telegram, данные и настройки сохраняются."
        if action == "disable" else
        "Mimoru снова начнёт обслуживать эту группу с сохранёнными настройками."
    )
    await callback.message.edit_text(
        panel_header("Подтверждение", f"{group.title}\n\n{description}"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, подтвердить", callback_data=f"ga:{source}:{group.id}:{action}")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cg:{source}:{group.id}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ga:(g|sp|st|se|c\d+):\d+:(enable|disable)$"))
async def service_group_apply_context(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, source, raw_gid, action = callback.data.split(":")
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    result = await set_group_service_active(session, group_id=int(raw_gid), active=action == "enable")
    if result.group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    if result.blocked_owner:
        await callback.answer("Сначала разблокируйте клиента-владельца группы.", show_alert=True)
        return
    await _render_group(callback, bot, session, result.group, source)
    await callback.answer("Сохранено")


def _member_back(callback: CallbackQuery, group_id: int) -> tuple[str, str]:
    title = _title(callback.message.text if callback.message else None)
    mapping = {
        "Недавно активные": (f"people_active:{group_id}", "◀️ К активным"),
        "Неактивные 30+ дней": (f"people_inactive:{group_id}", "◀️ К неактивным"),
        "Новички · 7 дней": (f"people_new:{group_id}", "◀️ К новичкам"),
        "Требуют внимания": (f"people_suspicious:{group_id}", "◀️ К списку"),
        "Активные предупреждения": (f"active_punishments:{group_id}:warn", "◀️ К предупреждениям"),
        "Активные муты": (f"active_punishments:{group_id}:mute", "◀️ К мутам"),
        "Активные блокировки": (f"active_punishments:{group_id}:ban", "◀️ К блокировкам"),
    }
    if title in mapping:
        return mapping[title]
    if title == "Жалоба":
        match = re.search(r"#(\d+)", callback.message.text or "")
        if match:
            return f"complaint:{group_id}:{match.group(1)}", "◀️ К жалобе"
    return f"group_section:{group_id}:members", "◀️ К участникам"


@router.callback_query(F.data.regexp(r"^member_card:\d+:-?\d+$"))
async def member_card_context(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_user = callback.data.split(":")
    group = await accessible_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    text, markup = await _member_card(session, group, int(raw_user))
    back_callback, back_text = _member_back(callback, group.id)
    rows = [list(row) for row in markup.inline_keyboard]
    if rows:
        rows[-1] = [InlineKeyboardButton(text=back_text, callback_data=back_callback)]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()
