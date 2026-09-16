from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, GroupMember, User
from app.db.rank_models import RankAssignment
from app.keyboards.home import group_home_menu
from app.services.access import is_service_owner
from app.services.client_access import set_group_service_active
from app.services.group_refs import group_reference_label
from app.services.plans import effective_plan, remaining_days, subscription_state
from app.services.telegram_admins import sync_telegram_administrators
from app.services.ui import panel_header

router = Router(name=__name__)

_ADMIN_RANKS_FOR_PANEL = {"deputy_owner", "chief_admin", "chat_admin"}

# Rank management needs promote_members; voice administrators also rely on the
# bot being able to configure video-chat rights when it promotes them.
GROUP_ADMIN_RIGHTS = (
    "delete_messages+restrict_members+invite_users+manage_chat+"
    "promote_members+manage_video_chats"
)


async def _label(bot: Bot, group: Group) -> str:
    return await group_reference_label(bot, group)


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


@router.callback_query(F.data == "panel:groups")
async def user_groups(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    admin_subq = (
        select(RankAssignment.id)
        .where(
            RankAssignment.group_id == Group.id,
            RankAssignment.user_telegram_id == callback.from_user.id,
            RankAssignment.active.is_(True),
            RankAssignment.rank_code.in_(_ADMIN_RANKS_FOR_PANEL),
        )
        .exists()
    )

    groups = list(
        (
            await session.scalars(
                select(Group)
                .where(
                    Group.is_active.is_(True),
                    or_(
                        Group.owner_telegram_id == callback.from_user.id,
                        admin_subq,
                    ),
                )
                .order_by(Group.created_at.desc())
            )
        ).all()
    )
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups:
        rows.append(
            [
                InlineKeyboardButton(
                    text=(await _label(bot, group))[:58],
                    callback_data=f"group:{group.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔎 Найти по ID / @username", callback_data="group_lookup:user"
            )
        ]
    )
    me = await bot.get_me()
    admin_url = f"https://t.me/{me.username or 'mimorubot'}?startgroup&admin={GROUP_ADMIN_RIGHTS}"
    rows.append(
        [InlineKeyboardButton(text="➕ Добавить Mimoru администратором", url=admin_url)]
    )
    rows.append(
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="panel:home")]
    )
    text = panel_header(
        "Мои группы",
        (
            "Группы показываются по Telegram ID и публичному @username. Нажмите нужную группу или найдите её вручную."
            if groups
            else "Подключённых групп пока нет. Добавьте Mimoru администратором в группу и напишите там «подключить»."
        ),
    )
    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^group:\d+$"))
async def user_group_card(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    group_id = int(callback.data.split(":")[-1])
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not is_service_owner(callback.from_user.id):
        admin_subq = (
            select(RankAssignment.id)
            .where(
                RankAssignment.group_id == Group.id,
                RankAssignment.user_telegram_id == callback.from_user.id,
                RankAssignment.active.is_(True),
                RankAssignment.rank_code.in_(_ADMIN_RANKS_FOR_PANEL),
            )
            .exists()
        )
        query = query.where(
            or_(
                Group.owner_telegram_id == callback.from_user.id,
                admin_subq,
            )
        )
    group = await session.scalar(query)
    if group is None:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return
    await sync_telegram_administrators(bot, session, group)
    await session.commit()
    identity = await _label(bot, group)
    await callback.message.edit_text(
        panel_header(
            "Группа",
            f"{identity}\n\n"
            "Выберите, что хотите сделать с этой группой.\n\n"
            "🛡 Защита — спам и фильтры.\n"
            "👮 Модерация — наказания, причины и роли.\n"
            "👥 Участники — поиск, карточки и ограничения.\n"
            "📊 Статистика — показатели именно этой группы.\n"
            "📝 Контент — слова и правила.\n"
            "⚙️ Настройки — поведение Mimoru.\n"
            f"\nТариф группы: {effective_plan(group).upper()}",
        ),
        reply_markup=group_home_menu(group.id),
    )
    await callback.answer()


async def _service_groups(
    callback: CallbackQuery, bot: Bot, session: AsyncSession, mode: str
) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    query = select(Group)
    if mode == "active":
        query = query.where(Group.is_active.is_(True))
    elif mode == "disabled":
        query = query.where(Group.is_active.is_(False))
    groups = list(
        (await session.scalars(query.order_by(Group.created_at.desc()).limit(50))).all()
    )
    rows = [
        [
            InlineKeyboardButton(
                text="✅ Активные", callback_data="service:groups:active"
            ),
            InlineKeyboardButton(
                text="⛔ Отключённые", callback_data="service:groups:disabled"
            ),
        ]
    ]
    for group in groups:
        status = "✅" if group.is_active else "⛔"
        identity = await _label(bot, group)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {identity[:45]} · {effective_plan(group).upper()}",
                    callback_data=f"service_group:{group.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔎 Найти по ID / @username", callback_data="group_lookup:service"
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="◀️ Панель Mimoru", callback_data="service:home")]
    )
    await callback.message.edit_text(
        panel_header(
            "Группы",
            "ID — это Telegram chat ID. Если у группы есть публичный username, он показан рядом.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data == "service:groups")
async def service_groups_all(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    await _service_groups(callback, bot, session, "all")


@router.callback_query(F.data.regexp(r"^service:groups:(active|disabled)$"))
async def service_groups_filtered(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    await _service_groups(callback, bot, session, callback.data.rsplit(":", 1)[1])


async def _render_service_group(
    callback: CallbackQuery, bot: Bot, session: AsyncSession, group: Group
) -> None:
    owner = (
        await session.scalar(
            select(User).where(User.telegram_id == group.owner_telegram_id)
        )
        if group.owner_telegram_id
        else None
    )
    members = int(
        await session.scalar(
            select(func.count())
            .select_from(GroupMember)
            .where(
                GroupMember.group_id == group.id,
                GroupMember.is_present.is_(True),
            )
        )
        or 0
    )
    identity = await _label(bot, group)
    days = remaining_days(group)
    expires = (
        group.plan_expires_at.strftime("%d.%m.%Y %H:%M UTC")
        if group.plan_expires_at
        else "без срока"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="💎 Управление тарифом", callback_data=f"service_plan:{group.id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="🩺 Проверить Telegram",
                callback_data=f"service_group_health:{group.id}",
            ),
            InlineKeyboardButton(
                text="📊 Статистика", callback_data=f"service_group_stats:{group.id}"
            ),
        ],
    ]
    if group.owner_telegram_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="👤 Открыть владельца",
                    callback_data=f"service_client:{group.owner_telegram_id}",
                )
            ]
        )
    action = "disable" if group.is_active else "enable"
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "⛔ Отключить обслуживание"
                    if group.is_active
                    else "✅ Включить обслуживание"
                ),
                callback_data=f"service_group_confirm:{group.id}:{action}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="◀️ Ко всем группам", callback_data="service:groups"
            )
        ]
    )
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
    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.regexp(r"^service_group:\d+$"))
async def service_group_card(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group = await session.get(Group, int(callback.data.split(":")[-1]))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    await _render_service_group(callback, bot, session, group)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_group_confirm:\d+:(enable|disable)$"))
async def service_group_confirm(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, gid, action = callback.data.split(":")
    group = await session.get(Group, int(gid))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    identity = await _label(bot, group)
    description = (
        "Mimoru перестанет обслуживать эту группу. Бот физически не удаляется из Telegram, данные и настройки сохраняются."
        if action == "disable"
        else "Mimoru снова начнёт обслуживать эту группу с сохранёнными настройками."
    )
    await callback.message.edit_text(
        panel_header("Подтверждение", f"{identity}\n\n{description}"),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Да, подтвердить",
                        callback_data=f"service_group_action:{group.id}:{action}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="◀️ Отмена", callback_data=f"service_group:{group.id}"
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service_group_action:\d+:(enable|disable)$"))
async def service_group_action(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, gid, action = callback.data.split(":")
    result = await set_group_service_active(
        session,
        group_id=int(gid),
        active=action == "enable",
    )
    if result.group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    if result.blocked_owner:
        await callback.answer(
            "Сначала разблокируйте клиента-владельца группы.", show_alert=True
        )
        return
    await _render_service_group(callback, bot, session, result.group)
    await callback.answer("Сохранено")
