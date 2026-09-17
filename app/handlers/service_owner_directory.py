from __future__ import annotations

import io

from aiogram import Bot, F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, GroupMember, User
from app.services.access import is_service_owner
from app.services.group_health import calculate_group_health
from app.services.plans import effective_plan, remaining_days, subscription_state
from app.services.ui import panel_header


router = Router(name=__name__)


def _name(user: User | None, telegram_id: int) -> str:
    if user and user.username:
        return f"@{user.username}"
    if user:
        full = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
        if full:
            return full
    return f"ID {telegram_id}"


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


def _format_dt(value) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else ""


async def _owner_exists(session: AsyncSession, telegram_id: int) -> bool:
    return bool(await session.scalar(select(func.count()).select_from(Group).where(Group.owner_telegram_id == telegram_id)))


async def _render_owners(callback: CallbackQuery, session: AsyncSession) -> None:
    rows = (await session.execute(
        select(
            Group.owner_telegram_id,
            User,
            func.count(Group.id).label("groups_count"),
        )
        .outerjoin(User, User.telegram_id == Group.owner_telegram_id)
        .where(Group.owner_telegram_id.is_not(None))
        .group_by(Group.owner_telegram_id, User.id)
        .order_by(func.max(Group.created_at).desc())
        .limit(100)
    )).all()
    buttons: list[list[InlineKeyboardButton]] = []
    blocked = 0
    for owner_id, user, groups_count in rows:
        owner_id = int(owner_id)
        if user is not None and user.service_blocked:
            blocked += 1
        mark = "🚫" if user is not None and user.service_blocked else "👤"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {_name(user, owner_id)[:34]} · {int(groups_count)} гр.",
            callback_data=f"owner_card:{owner_id}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Панель Mimoru", callback_data="service:home")])
    text = panel_header(
        "Владельцы групп",
        "Здесь показываются только пользователи, у которых есть хотя бы одна подключённая к Mimoru группа.",
    )
    text += f"\n\nВладельцев: {len(rows)}\nЗаблокированных владельцев: {blocked}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("service:clients"))
async def owners_directory(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await _render_owners(callback, session)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^owner_card:\d+$"))
async def owner_card(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[-1])
    groups = list((await session.scalars(
        select(Group).where(Group.owner_telegram_id == telegram_id).order_by(Group.created_at.desc())
    )).all())
    if not groups:
        await callback.answer("У этого пользователя больше нет подключённых групп.", show_alert=True)
        return
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    buttons = [[InlineKeyboardButton(
        text=f"🏠 {group.title[:30]} · {_plan_label(group)}",
        callback_data=f"owner_group:{telegram_id}:{group.id}",
    )] for group in groups]
    buttons.append([InlineKeyboardButton(text="◀️ К владельцам", callback_data="service:clients")])
    active = sum(1 for group in groups if group.is_active)
    text = panel_header("Владелец групп", _name(user, telegram_id))
    text += (
        f"\n\nTelegram ID: {telegram_id}"
        f"\nГрупп подключено: {len(groups)}"
        f"\nАктивных групп: {active}"
        f"\nОтключённых: {len(groups) - active}"
        f"\nСтатус доступа: {'🚫 заблокирован' if user and user.service_blocked else '✅ активен'}"
        "\n\nВыберите группу, чтобы увидеть количество участников и скачать пользователей этой группы."
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


async def _render_owner_group(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    owner_id: int,
    group: Group,
) -> None:
    if group.owner_telegram_id != owner_id:
        await callback.answer("Группа больше не принадлежит этому владельцу.", show_alert=True)
        return
    current_members = int(await session.scalar(
        select(func.count()).select_from(GroupMember).where(
            GroupMember.group_id == group.id,
            GroupMember.is_present.is_(True),
            GroupMember.is_deleted_account.is_(False),
        )
    ) or 0)
    known_members = int(await session.scalar(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id)
    ) or 0)
    usernames = int(await session.scalar(
        select(func.count()).select_from(GroupMember)
        .join(User, User.telegram_id == GroupMember.user_telegram_id)
        .where(GroupMember.group_id == group.id, User.username.is_not(None))
    ) or 0)
    days = remaining_days(group)
    expires = group.plan_expires_at.strftime("%d.%m.%Y %H:%M UTC") if group.plan_expires_at else "без срока"
    rows = [
        [InlineKeyboardButton(text="📥 Скачать пользователей Excel", callback_data=f"owner_export:{owner_id}:{group.id}")],
        [
            InlineKeyboardButton(text="🩺 Проверить Telegram", callback_data=f"owner_health:{owner_id}:{group.id}"),
            InlineKeyboardButton(text="📊 Статистика", callback_data=f"owner_stats:{owner_id}:{group.id}"),
        ],
        [InlineKeyboardButton(text="💎 Управление тарифом", callback_data=f"cp:c{owner_id}:{group.id}")],
        [InlineKeyboardButton(text="◀️ К владельцу", callback_data=f"owner_card:{owner_id}")],
    ]
    text = panel_header("Группа владельца", group.title)
    text += (
        f"\n\nСтатус Mimoru: {'✅ обслуживание включено' if group.is_active else '⛔ обслуживание отключено'}"
        f"\nУчастников сейчас в базе: {current_members}"
        f"\nВсего известных Mimoru пользователей: {known_members}"
        f"\nС @username: {usernames}"
        f"\nТариф: {_plan_label(group)}"
        f"\nСрок: {expires}"
    )
    if days is not None:
        text += f"\nОсталось дней: {days}"
    text += "\n\nExcel содержит всех известных Mimoru пользователей этой группы и их текущий статус."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^owner_group:\d+:\d+$"))
async def owner_group(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, raw_owner, raw_group = callback.data.split(":")
    group = await session.get(Group, int(raw_group))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    await _render_owner_group(callback, bot, session, int(raw_owner), group)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cg:c\d+:\d+$"))
async def owner_group_context_return(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, source, raw_group = callback.data.split(":")
    owner_id = int(source[1:])
    group = await session.get(Group, int(raw_group))
    if not is_service_owner(callback.from_user.id) or group is None:
        await callback.answer("Нет доступа или группа не найдена.", show_alert=True)
        return
    await _render_owner_group(callback, bot, session, owner_id, group)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^owner_export:\d+:\d+$"))
async def export_group_users(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, raw_owner, raw_group = callback.data.split(":")
    owner_id, group_id = int(raw_owner), int(raw_group)
    group = await session.get(Group, group_id)
    if group is None or group.owner_telegram_id != owner_id:
        await callback.answer("Группа не найдена у этого владельца.", show_alert=True)
        return
    records = (await session.execute(
        select(GroupMember, User)
        .outerjoin(User, User.telegram_id == GroupMember.user_telegram_id)
        .where(GroupMember.group_id == group.id)
        .order_by(GroupMember.is_present.desc(), GroupMember.last_seen_at.desc())
    )).all()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Пользователи"
    headers = [
        "Telegram ID",
        "Имя пользователя",
        "Имя",
        "Фамилия",
        "Статус",
        "Дата вступления",
        "Последняя активность",
    ]
    sheet.append(headers)

    for member, user in records:
        status = (
            "Удалённый аккаунт"
            if member.is_deleted_account
            else ("В группе" if member.is_present else "Вышел из группы")
        )
        sheet.append([
            member.user_telegram_id,
            f"@{user.username}" if user and user.username else "",
            user.first_name if user else "",
            user.last_name if user and user.last_name else "",
            status,
            _format_dt(member.joined_at),
            _format_dt(member.last_seen_at),
        ])

    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.row_dimensions[1].height = 22

    for column_cells in sheet.columns:
        column_index = column_cells[0].column
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        sheet.column_dimensions[get_column_letter(column_index)].width = min(max(max_length + 3, 12), 38)
        for cell in column_cells[1:]:
            cell.alignment = Alignment(vertical="center")

    output = io.BytesIO()
    workbook.save(output)
    data = output.getvalue()
    filename = f"mimoru_group_{group.id}_users.xlsx"
    await callback.message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption=f"Пользователи группы «{group.title}». Записей: {len(records)}.",
    )
    await callback.answer("Excel готов")


@router.callback_query(F.data.regexp(r"^owner_health:\d+:\d+$"))
async def owner_group_health(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, raw_owner, raw_group = callback.data.split(":")
    owner_id, group_id = int(raw_owner), int(raw_group)
    group = await session.get(Group, group_id)
    if group is None or group.owner_telegram_id != owner_id:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    health = await calculate_group_health(bot, session, group)
    lines = [
        f"{'✅' if health.bot_is_admin else '❌'} Бот администратор",
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
            [InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"owner_health:{owner_id}:{group.id}")],
            [InlineKeyboardButton(text="◀️ К группе", callback_data=f"owner_group:{owner_id}:{group.id}")],
        ]),
    )
    await callback.answer("Проверено")


@router.callback_query(F.data.regexp(r"^owner_stats:\d+:\d+$"))
async def owner_group_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, raw_owner, raw_group = callback.data.split(":")
    owner_id, group_id = int(raw_owner), int(raw_group)
    group = await session.get(Group, group_id)
    if group is None or group.owner_telegram_id != owner_id:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    current_members = int(await session.scalar(select(func.count()).select_from(GroupMember).where(
        GroupMember.group_id == group.id, GroupMember.is_present.is_(True), GroupMember.is_deleted_account.is_(False),
    )) or 0)
    known_members = int(await session.scalar(select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id)) or 0)
    await callback.message.edit_text(
        panel_header("Пользователи группы", group.title)
        + f"\n\nСейчас в базе: {current_members}\nВсего известных Mimoru: {known_members}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Скачать пользователей Excel", callback_data=f"owner_export:{owner_id}:{group.id}")],
            [InlineKeyboardButton(text="◀️ К группе", callback_data=f"owner_group:{owner_id}:{group.id}")],
        ]),
    )
    await callback.answer()
