from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.db.rank_models import RankAssignment
from app.handlers.telegram_roles import _roles_markup
from app.services.ranks import (
    ADMIN_RANKS,
    HELPER,
    RANK_CODES,
    RANK_LABELS,
    UNTOUCHABLE,
    add_rank_event,
    can_assign_rank,
    demote_telegram_admin,
    get_actor_rank,
    get_assignment,
    is_service_owner,
    telegram_rights_for_rank,
)
from app.services.public_identity import stored_visible_name
from app.services.telegram_admins import TELEGRAM_ADMIN, sync_telegram_administrators
from app.services.ui import panel_header


router = Router(name=__name__)
TELEGRAM_MODE = "telegram"
BOT_ONLY_MODE = "bot_only"


class AccessRankForm(StatesGroup):
    add_user = State()


def _rank_label(code: str) -> str:
    return RANK_LABELS.get(code, code)


def _mode_label(mode: str) -> str:
    return "👮 Telegram + Mimoru" if mode == TELEGRAM_MODE else "🤖 Только Mimoru"


async def _group(
    session: AsyncSession,
    group_id: int,
    *,
    for_update: bool = False,
) -> Group | None:
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def _owner_group(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> Group | None:
    group = await _group(session, group_id, for_update=for_update)
    if group is None:
        return None
    if is_service_owner(user_id):
        return group
    if group.owner_telegram_id == user_id:
        return group
    from app.services.ranks import DEPUTY_OWNER, get_assignment

    assignment = await get_assignment(session, group.id, user_id)
    if assignment is not None and assignment.active and assignment.rank_code == DEPUTY_OWNER:
        return group
    return None


async def _telegram_provisioning_ready(bot: Bot, group: Group) -> tuple[bool, str]:
    """Validate Mimoru's concrete Telegram right before changing another admin."""
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(group.telegram_chat_id, me.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False, (
            "Не удалось проверить права Mimoru в Telegram. Убедитесь, что бот остаётся "
            "администратором группы, и повторите действие."
        )
    if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
        return False, (
            "Mimoru больше не является администратором группы. Сначала верните боту "
            "права администратора."
        )
    if not bool(getattr(bot_member, "can_promote_members", False)):
        return False, (
            "У Mimoru нет права назначать администраторов. Откройте настройки администратора "
            "Mimoru в Telegram и включите право «Добавлять администраторов», затем повторите действие."
        )
    return True, ""


async def _apply_assignment(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    actor_id: int,
    target_id: int,
    rank_code: str,
    access_mode: str,
) -> tuple[bool, str, RankAssignment | None]:
    allowed, reason = await can_assign_rank(session, group, actor_id, rank_code, target_id=target_id)
    if not allowed:
        return False, reason, None
    if target_id == group.owner_telegram_id:
        return False, "Владельцу группы внутренний ранг не назначается.", None
    if access_mode not in {TELEGRAM_MODE, BOT_ONLY_MODE}:
        return False, "Неизвестный способ доступа.", None
    if access_mode == TELEGRAM_MODE and rank_code not in ADMIN_RANKS:
        return False, "Этот ранг работает только внутри Mimoru и не требует Telegram-админки.", None
    if access_mode == TELEGRAM_MODE:
        ready, readiness_error = await _telegram_provisioning_ready(bot, group)
        if not ready:
            return False, readiness_error, None

    try:
        member = await bot.get_chat_member(group.telegram_chat_id, target_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False, "Не удалось найти участника в Telegram-группе.", None
    if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        return False, "Пользователь должен состоять в группе.", None

    existing = await get_assignment(session, group.id, target_id, active_only=False)
    old_rank = existing.rank_code if existing and existing.active else None
    old_mode = getattr(existing, "access_mode", BOT_ONLY_MODE) if existing else None

    if access_mode == TELEGRAM_MODE:
        try:
            await bot.promote_chat_member(
                group.telegram_chat_id,
                target_id,
                **telegram_rights_for_rank(rank_code),
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            return False, (
                "Telegram не позволил привести права администратора к выбранному рангу. "
                "Проверьте, что у Mimoru есть право назначать администраторов и нужные ей права."
            ), None
        managed = True
    else:
        if member.status == ChatMemberStatus.ADMINISTRATOR:
            if not await demote_telegram_admin(bot, group, target_id):
                return False, (
                    "Не удалось снять Telegram-админку. Чтобы оставить человеку доступ только через Mimoru, "
                    "сначала разрешите боту управлять администраторами или снимите админку вручную."
                ), None
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
            access_mode=access_mode,
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
        assignment.access_mode = access_mode
        assignment.telegram_admin_managed = managed

    add_rank_event(
        session,
        group_id=group.id,
        actor_id=actor_id,
        target_id=target_id,
        action="assign" if old_rank is None else "change",
        old_rank=old_rank,
        new_rank=rank_code,
        details={"access_mode": access_mode, "old_access_mode": old_mode},
    )
    await session.flush()
    return True, "", assignment


def _mode_markup(group_id: int, user_id: int, rank_code: str, back: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if rank_code in ADMIN_RANKS:
        rows.append([InlineKeyboardButton(
            text="👮 Telegram + Mimoru",
            callback_data=f"admin_access_apply:{group_id}:{user_id}:{rank_code}:telegram",
        )])
    rows.append([InlineKeyboardButton(
        text="🤖 Только внутри Mimoru",
        callback_data=f"admin_access_apply:{group_id}:{user_id}:{rank_code}:bot_only",
    )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^admin_access:\d+$"))
async def admin_access_home(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await _owner_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Настраивать администрацию может только владелец группы.", show_alert=True)
        return
    sync = await sync_telegram_administrators(bot, session, group)
    assignments = list((await session.scalars(
        select(RankAssignment).where(RankAssignment.group_id == group.id, RankAssignment.active.is_(True))
    )).all())
    by_user = {row.user_telegram_id: row for row in assignments}
    telegram_rows = []
    buttons: list[list[InlineKeyboardButton]] = []
    for entry in sync.entries:
        if entry.role_code != TELEGRAM_ADMIN:
            continue
        assignment = by_user.get(entry.user_id)
        state = _rank_label(assignment.rank_code) if assignment else "не согласован"
        telegram_rows.append(f"• {entry.name} · {state}")
        buttons.append([InlineKeyboardButton(
            text=f"👮 {entry.name[:32]} · {state}",
            callback_data=f"admin_access_user:{group.id}:{entry.user_id}",
        )])
    bot_only = [row for row in assignments if getattr(row, "access_mode", BOT_ONLY_MODE) == BOT_ONLY_MODE]
    bot_only_lines = []
    for row in bot_only:
        bot_only_lines.append(
            f"• {await stored_visible_name(row.user_telegram_id)}"
            f" · {_rank_label(row.rank_code)}"
        )
    text = panel_header(
        "Администрация и доступ",
        "Telegram-администраторы и управление через Mimoru теперь разделены. Для каждого человека владелец сам выбирает ранг и способ доступа.",
    )
    text += "\n\n👮 Telegram-администраторы\n" + ("\n".join(telegram_rows) if telegram_rows else "Нет несогласованных Telegram-администраторов.")
    text += "\n\n🤖 Только внутри Mimoru\n" + ("\n".join(bot_only_lines) if bot_only_lines else "Пока никого нет.")
    buttons.append([InlineKeyboardButton(text="➕ Добавить управление через Mimoru", callback_data=f"rank_add:{group.id}")])
    buttons.append([InlineKeyboardButton(text="◀️ К модерации", callback_data=f"group_section:{group.id}:moderation")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_access_user:\d+:\d+$"))
async def admin_access_user(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_user = callback.data.split(":")
    group = await _owner_group(session, int(raw_group), callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(
        text=_rank_label(code),
        callback_data=f"admin_access_rank:{group.id}:{raw_user}:{code}",
    )] for code in RANK_CODES]
    rows.append([InlineKeyboardButton(text="◀️ К администрации", callback_data=f"admin_access:{group.id}")])
    await callback.message.edit_text(
        panel_header("Выберите ранг", "После ранга Mimoru спросит, оставить человека администратором Telegram или дать ему доступ только через бота."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_access_rank:\d+:\d+:[a-z_]+$"))
async def admin_access_rank(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_user, rank_code = callback.data.split(":")
    group = await _owner_group(session, int(raw_group), callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            _rank_label(rank_code),
            "Выберите способ доступа.\n\n👮 Telegram + Mimoru — права будут приведены к выбранному рангу и человек останется в списке администраторов Telegram.\n\n🤖 Только Mimoru — Telegram-админка будет снята, но команды и управление через Mimoru останутся.",
        ),
        reply_markup=_mode_markup(group.id, int(raw_user), rank_code, f"admin_access_user:{group.id}:{raw_user}"),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_access_apply:\d+:\d+:[a-z_]+:(telegram|bot_only)$"))
async def admin_access_apply(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, raw_group, raw_user, rank_code, mode = callback.data.split(":")
    group = await _owner_group(
        session,
        int(raw_group),
        callback.from_user.id,
        for_update=True,
    )
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    ok, error, assignment = await _apply_assignment(
        bot, session, group, callback.from_user.id, int(raw_user), rank_code, mode
    )
    if not ok or assignment is None:
        await callback.answer(error, show_alert=True)
        return
    await session.commit()
    await callback.message.edit_text(
        panel_header("Доступ сохранён", f"Ранг: {_rank_label(assignment.rank_code)}\nРежим: {_mode_label(assignment.access_mode)}"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К администрации", callback_data=f"admin_access:{group.id}")]
        ]),
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data.regexp(r"^rank_quick:\d+:\d+:[a-z_]+$"))
async def rank_quick_choose_mode(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, raw_user, rank_code = callback.data.split(":")
    group = await _group(session, int(raw_group))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    allowed, reason = await can_assign_rank(session, group, callback.from_user.id, rank_code, target_id=int(raw_user))
    if not allowed:
        await callback.answer(reason, show_alert=True)
        return
    await callback.message.edit_text(
        f"Куда назначить ранг «{_rank_label(rank_code)}»?",
        reply_markup=_mode_markup(group.id, int(raw_user), rank_code, f"roles:{group.id}"),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^rank_add_choose:\d+:[a-z_]+$"))
async def rank_add_choose_mode(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    _, raw_group, rank_code = callback.data.split(":")
    group = await _group(session, int(raw_group))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    allowed, reason = await can_assign_rank(session, group, callback.from_user.id, rank_code)
    if not allowed:
        await callback.answer(reason, show_alert=True)
        return
    await state.set_state(AccessRankForm.add_user)
    await state.update_data(group_id=group.id, rank_code=rank_code, _cancel_callback=f"roles:{group.id}")
    await callback.message.edit_text(
        panel_header(f"Назначить: {_rank_label(rank_code)}", "Отправьте числовой Telegram ID участника. После этого выберете: Telegram + Mimoru или только Mimoru."),
    )
    await callback.answer()


@router.message(AccessRankForm.add_user, F.chat.type == "private")
async def rank_add_user_mode(message: Message, session: AsyncSession, state: FSMContext) -> None:
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
    rank_code = str(data["rank_code"])
    await state.clear()
    await message.answer(
        panel_header("Способ доступа", "Выберите, должен ли участник быть администратором Telegram или управлять группой только через Mimoru."),
        reply_markup=_mode_markup(group.id, int(raw), rank_code, f"roles:{group.id}"),
    )


@router.callback_query(F.data.regexp(r"^rank_change:\d+:\d+:[a-z_]+$"))
async def rank_change_keep_mode(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, raw_group, raw_id, rank_code = callback.data.split(":")
    group = await _group(session, int(raw_group), for_update=True)
    assignment = await session.get(RankAssignment, int(raw_id))
    if group is None or assignment is None or assignment.group_id != group.id:
        await callback.answer("Ранг не найден.", show_alert=True)
        return
    mode = getattr(assignment, "access_mode", BOT_ONLY_MODE)
    if rank_code not in ADMIN_RANKS:
        mode = BOT_ONLY_MODE
    ok, error, updated = await _apply_assignment(
        bot, session, group, callback.from_user.id, assignment.user_telegram_id, rank_code, mode
    )
    if not ok or updated is None:
        await callback.answer(error, show_alert=True)
        return
    await session.commit()
    await callback.message.edit_text(
        panel_header("Ранг изменён", f"Теперь: {_rank_label(updated.rank_code)}\nРежим: {_mode_label(updated.access_mode)}"),
        reply_markup=await _roles_markup(session, group, callback.from_user.id, [updated]),
    )
    await callback.answer("Сохранено")