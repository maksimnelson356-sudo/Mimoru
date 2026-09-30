from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, User
from app.db.rank_models import GroupRankPolicy, RankAssignment, RankAssignmentEvent
from app.services.public_identity import public_user_token, stored_visible_name
from app.services.ranks import (
    ADMIN_RANKS,
    CHAT_ADMIN,
    HELPER,
    PERMISSION_LABELS,
    RANK_CODES,
    RANK_LABELS,
    ROLE_CEILINGS,
    UNTOUCHABLE,
    actor_has_permission,
    add_rank_event,
    assignable_ranks,
    can_assign_rank,
    can_edit_assignment,
    can_moderate_target,
    can_remove_assignment,
    demote_if_managed,
    effective_permissions,
    ensure_telegram_rank,
    get_actor_rank,
    get_assignment,
    is_service_owner,
    telegram_rights_for_rank,
)
from app.services.telegram_admins import TELEGRAM_OWNER, sync_telegram_administrators
from app.services.ui import panel_header


router = Router(name=__name__)


class RankForm(StatesGroup):
    add_user = State()


MEDIA_BLOCKED = ChatPermissions(
    can_send_messages=True,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=True,
    can_invite_users=True,
)
MEDIA_ALLOWED = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)


def _rank_label(code: str) -> str:
    return RANK_LABELS.get(code, code)


def _user_label(user: User | None, user_id: int) -> str:
    if user is None:
        return f"ID {user_id}"
    if user.username:
        return f"@{user.username}"
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return name or f"ID {user_id}"


async def _group(session: AsyncSession, group_id: int) -> Group | None:
    return await session.scalar(
        select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    )


async def _group_for_actor(
    session: AsyncSession,
    group_id: int,
    user_id: int,
) -> tuple[Group | None, object | None]:
    group = await _group(session, group_id)
    if group is None:
        return None, None
    actor = await get_actor_rank(session, group, user_id)
    return group, actor


async def _assignment_rows(session: AsyncSession, group_id: int) -> list[RankAssignment]:
    return list(
        (
            await session.scalars(
                select(RankAssignment)
                .where(RankAssignment.group_id == group_id, RankAssignment.active.is_(True))
                .order_by(RankAssignment.rank_code, RankAssignment.created_at)
            )
        ).all()
    )


async def _roles_markup(
    session: AsyncSession,
    group: Group,
    actor_id: int,
    rows: list[RankAssignment],
) -> InlineKeyboardMarkup:
    actor = await get_actor_rank(session, group, actor_id)
    buttons: list[list[InlineKeyboardButton]] = []
    for item in sorted(rows, key=lambda row: (-__import__("app.services.ranks", fromlist=["rank_level"]).rank_level(row.rank_code), row.user_telegram_id)):
        buttons.append([
            InlineKeyboardButton(
                text=f"{_rank_label(item.rank_code)} · {item.user_telegram_id}",
                callback_data=f"rank_edit:{group.id}:{item.id}",
            )
        ])
    if actor is not None and assignable_ranks(actor):
        buttons.append([InlineKeyboardButton(text="➕ Назначить ранг", callback_data=f"rank_add:{group.id}")])
    if group.owner_telegram_id == actor_id or is_service_owner(actor_id):
        buttons.append([InlineKeyboardButton(text="⚙️ Права рангов группы", callback_data=f"rank_policies:{group.id}")])
    buttons.append([InlineKeyboardButton(text="📜 История назначений", callback_data=f"rank_history:{group.id}")])
    buttons.append([InlineKeyboardButton(text="◀️ К модерации", callback_data=f"group_section:{group.id}:moderation")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_roles(target: Message, bot: Bot, session: AsyncSession, group: Group, actor_id: int) -> None:
    sync = await sync_telegram_administrators(bot, session, group)
    rows = await _assignment_rows(session, group.id)
    await session.flush()
    telegram_lines = []
    for entry in sync.entries:
        telegram_role = "Владелец" if entry.role_code == TELEGRAM_OWNER else "Администратор"
        handle = f" @{entry.username}" if entry.username else ""
        telegram_lines.append(f"• {telegram_role}: {entry.name}{handle}")
    rank_lines = []
    for item in rows:
        assigner = (
            public_user_token(item.assigned_by_telegram_id)
            if item.assigned_by_telegram_id
            else "Mimoru"
        )
        rank_lines.append(
            f"• {_rank_label(item.rank_code)}"
            f" · {await stored_visible_name(item.user_telegram_id)}"
            f" · назначил {assigner}"
        )
    text = panel_header(
        "Ранги и администрация",
        "Ранги Mimoru настраиваются отдельно для этой группы. Один и тот же человек может иметь разные ранги в разных группах.",
    )
    text += "\n\nРанги Mimoru\n" + ("\n".join(rank_lines) if rank_lines else "Пока не назначены.")
    text += "\n\nTelegram-администраторы\n" + (
        "\n".join(telegram_lines) if telegram_lines else "Список Telegram-администраторов недоступен."
    )
    text += (
        "\n\nИерархия: Зам. владельца → Глав. админ → Администратор чата → "
        "Администратор войса → Помощник. Недотрога — отдельный статус иммунитета."
    )
    await target.edit_text(text, reply_markup=await _roles_markup(session, group, actor_id, rows))


@router.callback_query(F.data.regexp(r"^roles:\d+$"))
async def telegram_and_solivra_roles(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group, actor = await _group_for_actor(session, group_id, callback.from_user.id)
    if group is None or actor is None:
        await callback.answer("Нет доступа к рангам этой группы.", show_alert=True)
        return
    await _render_roles(callback.message, bot, session, group, callback.from_user.id)
    await callback.answer("Синхронизировано")


@router.callback_query(F.data.regexp(r"^rank_add:\d+$"))
@router.callback_query(F.data.regexp(r"^role_add:\d+$"))
async def rank_add(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group, actor = await _group_for_actor(session, group_id, callback.from_user.id)
    if group is None or actor is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    available = assignable_ranks(actor)
    if not available:
        await callback.answer("Ваш ранг не может назначать должности.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=_rank_label(code), callback_data=f"rank_add_choose:{group.id}:{code}")] for code in available]
    rows.append([InlineKeyboardButton(text="◀️ К рангам", callback_data=f"roles:{group.id}")])
    await callback.message.edit_text(
        panel_header("Новый ранг", "Сначала выберите должность. Затем Mimoru попросит Telegram ID участника."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^rank_add_choose:\d+:[a-z_]+$"))
async def rank_add_choose(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    _, raw_group, rank_code = callback.data.split(":")
    group_id = int(raw_group)
    group = await _group(session, group_id)
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    allowed, reason = await can_assign_rank(session, group, callback.from_user.id, rank_code)
    if not allowed:
        await callback.answer(reason, show_alert=True)
        return
    await state.set_state(RankForm.add_user)
    await state.update_data(group_id=group.id, rank_code=rank_code, _cancel_callback=f"roles:{group.id}")
    await callback.message.edit_text(
        panel_header(
            f"Назначить: {_rank_label(rank_code)}",
            "Отправьте числовой Telegram ID участника. Для административного ранга Mimoru сама попытается выдать необходимые Telegram-права.",
        )
    )
    await callback.answer()


async def _set_assignment(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    actor_id: int,
    target_id: int,
    rank_code: str,
) -> tuple[bool, str, RankAssignment | None]:
    allowed, reason = await can_assign_rank(session, group, actor_id, rank_code, target_id=target_id)
    if not allowed:
        return False, reason, None

    try:
        member = await bot.get_chat_member(group.telegram_chat_id, target_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False, "Не удалось найти участника в этой Telegram-группе.", None
    if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        return False, "Пользователь должен состоять в группе.", None

    existing = await get_assignment(session, group.id, target_id, active_only=False)
    old_rank = existing.rank_code if existing and existing.active else None
    old_managed = bool(existing and existing.telegram_admin_managed)

    if rank_code in ADMIN_RANKS:
        if old_managed and member.status == ChatMemberStatus.ADMINISTRATOR:
            try:
                await bot.promote_chat_member(
                    group.telegram_chat_id,
                    target_id,
                    **telegram_rights_for_rank(rank_code),
                )
                managed = True
            except (TelegramBadRequest, TelegramForbiddenError):
                return False, "Не удалось обновить Telegram-права администратора.", None
        else:
            ok, newly_managed, error = await ensure_telegram_rank(bot, group, target_id, rank_code)
            if not ok:
                return False, error, None
            managed = old_managed or newly_managed
    else:
        if existing is not None and old_managed:
            await demote_if_managed(bot, group, existing)
        managed = False

    helper_for = actor_id if rank_code == HELPER else None
    if existing is None:
        assignment = RankAssignment(
            group_id=group.id,
            user_telegram_id=target_id,
            rank_code=rank_code,
            permissions={},
            active=True,
            assigned_by_telegram_id=actor_id,
            helper_for_telegram_id=helper_for,
            telegram_admin_managed=managed,
        )
        session.add(assignment)
    else:
        assignment = existing
        assignment.rank_code = rank_code
        assignment.permissions = {}
        assignment.active = True
        assignment.assigned_by_telegram_id = actor_id
        assignment.helper_for_telegram_id = helper_for
        assignment.telegram_admin_managed = managed

    add_rank_event(
        session,
        group_id=group.id,
        actor_id=actor_id,
        target_id=target_id,
        action="assign" if old_rank is None else "change",
        old_rank=old_rank,
        new_rank=rank_code,
        details={"telegram_admin_managed": managed},
    )
    await session.flush()
    return True, "", assignment


@router.message(RankForm.add_user, F.chat.type == "private")
async def rank_add_user_text(message: Message, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужен числовой Telegram ID. Попробуйте ещё раз.")
        return
    group = await _group(session, int(data["group_id"]))
    if group is None:
        await state.clear()
        await message.answer("Группа больше недоступна.")
        return
    ok, error, assignment = await _set_assignment(
        bot, session, group, message.from_user.id, int(raw), str(data["rank_code"])
    )
    if not ok or assignment is None:
        await message.answer(error)
        return
    await session.commit()
    await state.clear()
    await message.answer(
        panel_header(
            "Ранг назначен",
            f"Telegram ID: {assignment.user_telegram_id}\nРанг: {_rank_label(assignment.rank_code)}",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Открыть ранг", callback_data=f"rank_edit:{group.id}:{assignment.id}")],
            [InlineKeyboardButton(text="◀️ К рангам", callback_data=f"roles:{group.id}")],
        ]),
    )


async def _rank_detail_markup(
    session: AsyncSession,
    group: Group,
    actor_id: int,
    assignment: RankAssignment,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    can_edit, _ = await can_edit_assignment(session, group, actor_id, assignment)
    if can_edit:
        perms = await effective_permissions(session, group.id, assignment)
        ceiling = ROLE_CEILINGS.get(assignment.rank_code, set())
        for name in sorted(ceiling, key=lambda key: PERMISSION_LABELS.get(key, key)):
            rows.append([
                InlineKeyboardButton(
                    text=f"{'✅' if perms.get(name, False) else '❌'} {PERMISSION_LABELS.get(name, name)}",
                    callback_data=f"rank_perm:{group.id}:{assignment.id}:{name}",
                )
            ])
        if ceiling:
            rows.append([InlineKeyboardButton(text="♻️ Сбросить личные права", callback_data=f"rank_reset:{group.id}:{assignment.id}")])
        actor = await get_actor_rank(session, group, actor_id)
        if actor is not None:
            choices = [code for code in assignable_ranks(actor) if code != assignment.rank_code]
            for code in choices:
                rows.append([InlineKeyboardButton(
                    text=f"↪️ Сделать: {_rank_label(code)}",
                    callback_data=f"rank_change:{group.id}:{assignment.id}:{code}",
                )])
    can_remove, _ = await can_remove_assignment(session, group, actor_id, assignment)
    if can_remove:
        rows.append([InlineKeyboardButton(text="🗑 Снять ранг", callback_data=f"rank_remove_confirm:{group.id}:{assignment.id}")])
    rows.append([InlineKeyboardButton(text="◀️ К рангам", callback_data=f"roles:{group.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^rank_edit:\d+:\d+$"))
@router.callback_query(F.data.regexp(r"^role_edit:\d+:\d+$"))
async def rank_edit(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":")
    group = await _group(session, int(raw_group))
    assignment = await session.get(RankAssignment, int(raw_id))
    if group is None or assignment is None or assignment.group_id != group.id or not assignment.active:
        await callback.answer("Ранг не найден.", show_alert=True)
        return
    actor = await get_actor_rank(session, group, callback.from_user.id)
    if actor is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    permissions = await effective_permissions(session, group.id, assignment)
    enabled = [PERMISSION_LABELS.get(name, name) for name, value in permissions.items() if value]
    helper = f"\nПомогает администратору: {assignment.helper_for_telegram_id}" if assignment.helper_for_telegram_id else ""
    text = panel_header(
        _rank_label(assignment.rank_code),
        f"Telegram ID: {assignment.user_telegram_id}\nНазначил: {assignment.assigned_by_telegram_id}{helper}",
    )
    text += "\n\nАктивные права: " + (", ".join(enabled) if enabled else "командных прав нет")
    await callback.message.edit_text(
        text,
        reply_markup=await _rank_detail_markup(session, group, callback.from_user.id, assignment),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^rank_perm:\d+:\d+:[a-z_]+$"))
async def rank_permission_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id, permission = callback.data.split(":")
    group = await _group(session, int(raw_group))
    assignment = await session.get(RankAssignment, int(raw_id))
    if group is None or assignment is None or assignment.group_id != group.id:
        await callback.answer("Ранг не найден.", show_alert=True)
        return
    allowed, reason = await can_edit_assignment(session, group, callback.from_user.id, assignment)
    if not allowed:
        await callback.answer(reason, show_alert=True)
        return
    if permission not in ROLE_CEILINGS.get(assignment.rank_code, set()):
        await callback.answer("Это право недоступно данному рангу.", show_alert=True)
        return
    current = await effective_permissions(session, group.id, assignment)
    custom = dict(assignment.permissions or {})
    custom[permission] = not bool(current.get(permission, False))
    assignment.permissions = custom
    add_rank_event(
        session,
        group_id=group.id,
        actor_id=callback.from_user.id,
        target_id=assignment.user_telegram_id,
        action="permission",
        old_rank=assignment.rank_code,
        new_rank=assignment.rank_code,
        details={permission: custom[permission]},
    )
    await session.commit()
    await callback.message.edit_reply_markup(
        reply_markup=await _rank_detail_markup(session, group, callback.from_user.id, assignment)
    )
    await callback.answer("Право изменено")


@router.callback_query(F.data.regexp(r"^rank_reset:\d+:\d+$"))
async def rank_permission_reset(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":")
    group = await _group(session, int(raw_group))
    assignment = await session.get(RankAssignment, int(raw_id))
    if group is None or assignment is None or assignment.group_id != group.id:
        await callback.answer("Ранг не найден.", show_alert=True)
        return
    allowed, reason = await can_edit_assignment(session, group, callback.from_user.id, assignment)
    if not allowed:
        await callback.answer(reason, show_alert=True)
        return
    assignment.permissions = {}
    await session.commit()
    await callback.message.edit_reply_markup(
        reply_markup=await _rank_detail_markup(session, group, callback.from_user.id, assignment)
    )
    await callback.answer("Личные права сброшены")


@router.callback_query(F.data.regexp(r"^rank_change:\d+:\d+:[a-z_]+$"))
async def rank_change(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, raw_group, raw_id, rank_code = callback.data.split(":")
    group = await _group(session, int(raw_group))
    assignment = await session.get(RankAssignment, int(raw_id))
    if group is None or assignment is None or assignment.group_id != group.id:
        await callback.answer("Ранг не найден.", show_alert=True)
        return
    ok, error, updated = await _set_assignment(
        bot, session, group, callback.from_user.id, assignment.user_telegram_id, rank_code
    )
    if not ok or updated is None:
        await callback.answer(error, show_alert=True)
        return
    await session.commit()
    await callback.message.edit_text(
        panel_header("Ранг изменён", f"Теперь: {_rank_label(updated.rank_code)}"),
        reply_markup=await _rank_detail_markup(session, group, callback.from_user.id, updated),
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data.regexp(r"^rank_remove_confirm:\d+:\d+$"))
@router.callback_query(F.data.regexp(r"^role_remove_confirm:\d+:\d+$"))
async def rank_remove_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":")
    group = await _group(session, int(raw_group))
    assignment = await session.get(RankAssignment, int(raw_id))
    if group is None or assignment is None or assignment.group_id != group.id:
        await callback.answer("Ранг не найден.", show_alert=True)
        return
    allowed, reason = await can_remove_assignment(session, group, callback.from_user.id, assignment)
    if not allowed:
        await callback.answer(reason, show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Снять ранг?",
            f"{await stored_visible_name(assignment.user_telegram_id)}\nРанг: {_rank_label(assignment.rank_code)}\n\nЕсли Telegram-права были выданы самой Mimoru, бот также попробует снять их.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, снять", callback_data=f"rank_remove:{group.id}:{assignment.id}")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"rank_edit:{group.id}:{assignment.id}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^rank_remove:\d+:\d+$"))
@router.callback_query(F.data.regexp(r"^role_remove:\d+:\d+$"))
async def rank_remove(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":")
    group = await _group(session, int(raw_group))
    assignment = await session.get(RankAssignment, int(raw_id))
    if group is None or assignment is None or assignment.group_id != group.id:
        await callback.answer("Ранг не найден.", show_alert=True)
        return
    allowed, reason = await can_remove_assignment(session, group, callback.from_user.id, assignment)
    if not allowed:
        await callback.answer(reason, show_alert=True)
        return
    old_rank = assignment.rank_code
    await demote_if_managed(bot, group, assignment)
    assignment.active = False
    assignment.permissions = {}
    assignment.helper_for_telegram_id = None
    assignment.telegram_admin_managed = False
    add_rank_event(
        session,
        group_id=group.id,
        actor_id=callback.from_user.id,
        target_id=assignment.user_telegram_id,
        action="remove",
        old_rank=old_rank,
        new_rank=None,
    )
    await session.commit()
    await _render_roles(callback.message, bot, session, group, callback.from_user.id)
    await callback.answer("Ранг снят")


@router.callback_query(F.data.regexp(r"^rank_policies:\d+$"))
async def rank_policies(callback: CallbackQuery, session: AsyncSession) -> None:
    group = await _group(session, int(callback.data.split(":")[1]))
    if group is None or not (group.owner_telegram_id == callback.from_user.id or is_service_owner(callback.from_user.id)):
        await callback.answer("Настраивать права рангов может только владелец группы.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=_rank_label(code), callback_data=f"rank_policy:{group.id}:{code}")] for code in RANK_CODES]
    rows.append([InlineKeyboardButton(text="◀️ К рангам", callback_data=f"roles:{group.id}")])
    await callback.message.edit_text(
        panel_header(
            "Права рангов группы",
            "Настройки действуют только в этой группе. Системная иерархия не меняется: нижнему рангу нельзя выдать право выше его безопасного предела.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


async def _policy_permissions(session: AsyncSession, group_id: int, rank_code: str) -> dict[str, bool]:
    row = await session.scalar(select(GroupRankPolicy).where(
        GroupRankPolicy.group_id == group_id,
        GroupRankPolicy.rank_code == rank_code,
    ))
    from app.services.ranks import DEFAULT_ROLE_PERMISSIONS
    result = dict(DEFAULT_ROLE_PERMISSIONS.get(rank_code, {}))
    if row is not None:
        for name, value in (row.permissions or {}).items():
            if name in ROLE_CEILINGS.get(rank_code, set()):
                result[name] = bool(value)
    return result


@router.callback_query(F.data.regexp(r"^rank_policy:\d+:[a-z_]+$"))
async def rank_policy(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, rank_code = callback.data.split(":")
    group = await _group(session, int(raw_group))
    if group is None or not (group.owner_telegram_id == callback.from_user.id or is_service_owner(callback.from_user.id)):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if rank_code not in RANK_CODES:
        await callback.answer("Неизвестный ранг.", show_alert=True)
        return
    permissions = await _policy_permissions(session, group.id, rank_code)
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if permissions.get(name, False) else '❌'} {PERMISSION_LABELS.get(name, name)}",
            callback_data=f"rank_policy_perm:{group.id}:{rank_code}:{name}",
        )]
        for name in sorted(ROLE_CEILINGS.get(rank_code, set()), key=lambda key: PERMISSION_LABELS.get(key, key))
    ]
    rows.append([InlineKeyboardButton(text="◀️ К рангам", callback_data=f"rank_policies:{group.id}")])
    await callback.message.edit_text(
        panel_header(
            _rank_label(rank_code),
            "Включите только те права, которые должны автоматически получать участники этого ранга в данной группе.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^rank_policy_perm:\d+:[a-z_]+:[a-z_]+$"))
async def rank_policy_perm(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, rank_code, permission = callback.data.split(":")
    group = await _group(session, int(raw_group))
    if group is None or not (group.owner_telegram_id == callback.from_user.id or is_service_owner(callback.from_user.id)):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if permission not in ROLE_CEILINGS.get(rank_code, set()):
        await callback.answer("Это право нельзя выдать данному рангу.", show_alert=True)
        return
    row = await session.scalar(select(GroupRankPolicy).where(
        GroupRankPolicy.group_id == group.id,
        GroupRankPolicy.rank_code == rank_code,
    ))
    current = await _policy_permissions(session, group.id, rank_code)
    if row is None:
        row = GroupRankPolicy(
            group_id=group.id,
            rank_code=rank_code,
            permissions={},
            updated_by_telegram_id=callback.from_user.id,
        )
        session.add(row)
    custom = dict(row.permissions or {})
    custom[permission] = not bool(current.get(permission, False))
    row.permissions = custom
    row.updated_by_telegram_id = callback.from_user.id
    await session.commit()
    await rank_policy(callback, session)


@router.callback_query(F.data.regexp(r"^rank_history:\d+$"))
async def rank_history(callback: CallbackQuery, session: AsyncSession) -> None:
    group, actor = await _group_for_actor(session, int(callback.data.split(":")[1]), callback.from_user.id)
    if group is None or actor is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    events = list((await session.scalars(
        select(RankAssignmentEvent)
        .where(RankAssignmentEvent.group_id == group.id)
        .order_by(RankAssignmentEvent.created_at.desc())
        .limit(30)
    )).all())
    lines = []
    for item in events:
        old = _rank_label(item.old_rank_code) if item.old_rank_code else "—"
        new = _rank_label(item.new_rank_code) if item.new_rank_code else "—"
        lines.append(
            f"• {item.created_at:%d.%m %H:%M} · {item.action} · {item.target_telegram_id} · {old} → {new} · кто: {item.actor_telegram_id}"
        )
    await callback.message.edit_text(
        panel_header("История рангов", "Последние 30 изменений.") + "\n\n" + ("\n".join(lines) if lines else "История пока пуста."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К рангам", callback_data=f"roles:{group.id}")]
        ]),
    )
    await callback.answer()


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text.casefold() == "ранги")
async def ranks_in_group(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await session.scalar(select(Group).where(
        Group.telegram_chat_id == message.chat.id,
        Group.is_active.is_(True),
    ))
    if group is None or await get_actor_rank(session, group, message.from_user.id) is None:
        return
    rows = await _assignment_rows(session, group.id)
    await message.answer(
        panel_header("Ранги группы", "Управление рангами Mimoru. Для быстрого назначения ответьте на сообщение участника словом «назначить»."),
        reply_markup=await _roles_markup(session, group, message.from_user.id, rows),
    )


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.reply_to_message,
    F.text.casefold() == "назначить",
)
async def quick_assign_menu(message: Message, session: AsyncSession) -> None:
    group = await session.scalar(select(Group).where(
        Group.telegram_chat_id == message.chat.id,
        Group.is_active.is_(True),
    ))
    if group is None or message.reply_to_message.from_user is None:
        return
    actor = await get_actor_rank(session, group, message.from_user.id)
    if actor is None:
        return
    target_id = message.reply_to_message.from_user.id
    choices = assignable_ranks(actor)
    if not choices:
        await message.reply("Ваш ранг не может назначать должности.")
        return
    rows = [[InlineKeyboardButton(
        text=_rank_label(code),
        callback_data=f"rank_quick:{group.id}:{target_id}:{code}",
    )] for code in choices]
    await message.reply(
        f"Какой ранг назначить участнику {target_id}?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.regexp(r"^rank_quick:\d+:\d+:[a-z_]+$"))
async def rank_quick(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, raw_group, raw_target, rank_code = callback.data.split(":")
    group = await _group(session, int(raw_group))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    ok, error, assignment = await _set_assignment(
        bot, session, group, callback.from_user.id, int(raw_target), rank_code
    )
    if not ok or assignment is None:
        await callback.answer(error, show_alert=True)
        return
    await session.commit()
    await callback.message.edit_text(
        f"✅ Участнику {assignment.user_telegram_id} назначен ранг «{_rank_label(assignment.rank_code)}»."
    )
    await callback.answer("Назначено")


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.reply_to_message,
    F.text.casefold().in_({"без медиа", "медиа выкл", "медиа вкл"}),
)
async def media_restriction(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.reply_to_message.from_user is None:
        return
    group = await session.scalar(select(Group).where(
        Group.telegram_chat_id == message.chat.id,
        Group.is_active.is_(True),
    ))
    if group is None:
        return
    if not await actor_has_permission(session, group, message.from_user.id, "restrict_media"):
        return
    target_id = message.reply_to_message.from_user.id
    target_rank = await get_assignment(session, group.id, target_id)
    if target_rank is not None and target_rank.rank_code in ADMIN_RANKS:
        await message.reply("Ограничение медиа применяется только к обычным участникам, не к администраторам.")
        return
    allowed, reason = await can_moderate_target(session, group, message.from_user.id, target_id)
    if not allowed:
        await message.reply(reason)
        return
    permissions = MEDIA_ALLOWED if message.text.casefold() == "медиа вкл" else MEDIA_BLOCKED
    try:
        await bot.restrict_chat_member(message.chat.id, target_id, permissions=permissions)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.reply("Telegram не позволил изменить медиа-права этого участника.")
        return
    await message.reply("✅ Медиа разрешены." if permissions is MEDIA_ALLOWED else "✅ Отправка медиа ограничена.")


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.reply_to_message,
    F.text.casefold().in_({"доложить", "нарушитель", "жалоба"}),
)
async def helper_report(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.reply_to_message.from_user is None:
        return
    group = await session.scalar(select(Group).where(
        Group.telegram_chat_id == message.chat.id,
        Group.is_active.is_(True),
    ))
    if group is None:
        return
    assignment = await get_assignment(session, group.id, message.from_user.id)
    if assignment is None or assignment.rank_code != HELPER or not assignment.helper_for_telegram_id:
        return
    target = message.reply_to_message.from_user

    def _plain_label(full_name: str | None, username: str | None, fallback: str) -> str:
        handle = f"@{username}" if username else ""
        name = (full_name or "").strip() or handle or fallback
        return f"{name} ({handle})" if handle and name != handle else name

    reporter_name = _plain_label(
        message.from_user.full_name, message.from_user.username, "Помощник"
    )
    target_name = _plain_label(
        target.full_name, target.username, "Участник без имени"
    )
    try:
        await bot.send_message(
            assignment.helper_for_telegram_id,
            panel_header(
                "Сообщение от помощника",
                f"Группа: {group.title}\nПомощник: {reporter_name}\nНарушитель: {target_name}\n\nПроверьте ситуацию и примите решение самостоятельно.",
            ),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.reply("Не удалось уведомить вашего администратора в личных сообщениях.")
        return
    await message.reply("✅ Ваш администратор получил уведомление.")
