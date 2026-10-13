from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.db.rank_models import RankAssignment
from app.middlewares_rank_access import RankAccessModeMiddleware
from app.services.rank_provisioning import BOT_ONLY_MODE, TELEGRAM_MODE, provision_assignment, remove_assignment
from app.services.ranks import ADMIN_RANKS, RANK_LABELS, add_rank_event, can_assign_rank
from app.services.ui import panel_header


router = Router(name=__name__)
router.callback_query.middleware(RankAccessModeMiddleware())

logger = structlog.get_logger(__name__)


def _rank_label(code: str) -> str:
    return RANK_LABELS.get(code, code)


def _mode_label(mode: str) -> str:
    return "👮 Telegram + Mimoru" if mode == TELEGRAM_MODE else "🤖 Только Mimoru"


async def _group(session: AsyncSession, group_id: int) -> Group | None:
    return await session.scalar(
        select(Group)
        .where(Group.id == group_id, Group.is_active.is_(True))
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _attach_existing_unmanaged_telegram_admin(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    actor_id: int,
    target_id: int,
    rank_code: str,
    mode: str,
) -> tuple[bool, bool, str, RankAssignment | None]:
    """Bind a Mimoru rank to an existing Telegram admin without rewriting Telegram rights."""
    if mode != TELEGRAM_MODE or rank_code not in ADMIN_RANKS:
        return False, False, "", None
    try:
        member = await bot.get_chat_member(group.telegram_chat_id, target_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False, False, "", None
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return False, False, "", None

    assignment = await session.scalar(
        select(RankAssignment)
        .where(
            RankAssignment.group_id == group.id,
            RankAssignment.user_telegram_id == target_id,
        )
        .with_for_update()
    )
    if assignment is not None and assignment.active and assignment.telegram_admin_managed:
        return False, False, "", None

    allowed, reason = await can_assign_rank(
        session,
        group,
        actor_id,
        rank_code,
        target_id=target_id,
    )
    if not allowed:
        return True, False, reason, None
    if target_id == group.owner_telegram_id:
        return True, False, "Владельцу группы внутренний ранг не назначается.", None

    old_rank = assignment.rank_code if assignment is not None and assignment.active else None
    old_mode = assignment.access_mode if assignment is not None and assignment.active else None
    if assignment is None:
        assignment = RankAssignment(
            group_id=group.id,
            user_telegram_id=target_id,
            rank_code=rank_code,
            permissions={},
            active=True,
            assigned_by_telegram_id=actor_id,
            helper_for_telegram_id=None,
            access_mode=TELEGRAM_MODE,
            telegram_admin_managed=False,
        )
        session.add(assignment)
    else:
        assignment.rank_code = rank_code
        assignment.permissions = {}
        assignment.active = True
        assignment.assigned_by_telegram_id = actor_id
        assignment.helper_for_telegram_id = None
        assignment.access_mode = TELEGRAM_MODE
        assignment.telegram_admin_managed = False

    add_rank_event(
        session,
        group_id=group.id,
        actor_id=actor_id,
        target_id=target_id,
        action="assign" if old_rank is None else "change",
        old_rank=old_rank,
        new_rank=rank_code,
        details={
            "access_mode": TELEGRAM_MODE,
            "old_access_mode": old_mode,
            "telegram_rights_preserved": True,
        },
    )
    await session.commit()
    return True, True, "", assignment


@router.callback_query(F.data.regexp(r"^admin_access_apply:\d+:\d+:[a-z_]+:(telegram|bot_only)$"))
async def safe_admin_access_apply(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, raw_group, raw_user, rank_code, mode = callback.data.split(":")
    group = await _group(session, int(raw_group))
    if group is None:
        await callback.answer("Группа недоступна.", show_alert=True)
        return

    handled, ok, error, assignment = await _attach_existing_unmanaged_telegram_admin(
        bot,
        session,
        group,
        callback.from_user.id,
        int(raw_user),
        rank_code,
        mode,
    )
    if not handled:
        ok, error, assignment = await provision_assignment(
            bot,
            session,
            group,
            callback.from_user.id,
            int(raw_user),
            rank_code,
            mode,
            force_bot_only_demotion=True,
        )
    if not ok or assignment is None:
        if mode == BOT_ONLY_MODE and "снять Telegram-права" in error:
            error = (
                "Вы выбрали режим «Только Mimoru», но Telegram не разрешил боту снять текущую админку. "
                "Если человек должен остаться администратором Telegram, выберите «Telegram + Mimoru»."
            )
        await callback.answer(error, show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Доступ сохранён",
            f"Ранг: {_rank_label(assignment.rank_code)}\nРежим: {_mode_label(assignment.access_mode)}",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К администрации", callback_data=f"admin_access:{group.id}")]
        ]),
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data.regexp(r"^rank_change:\d+:\d+:[a-z_]+$"))
async def safe_rank_change(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, raw_group, raw_id, rank_code = callback.data.split(":")
    group = await _group(session, int(raw_group))
    assignment = await session.get(RankAssignment, int(raw_id))
    if group is None or assignment is None or assignment.group_id != group.id or not assignment.active:
        await callback.answer("Ранг не найден.", show_alert=True)
        return
    mode = TELEGRAM_MODE if rank_code in ADMIN_RANKS else BOT_ONLY_MODE
    ok, error, updated = await provision_assignment(
        bot,
        session,
        group,
        callback.from_user.id,
        assignment.user_telegram_id,
        rank_code,
        mode,
        force_bot_only_demotion=False,
    )
    if not ok or updated is None:
        await callback.answer(error, show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Ранг изменён",
            f"Теперь: {_rank_label(updated.rank_code)}\nРежим: {_mode_label(updated.access_mode)}",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К рангам", callback_data=f"roles:{group.id}")]
        ]),
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data.regexp(r"^rank_remove:\d+:\d+$"))
@router.callback_query(F.data.regexp(r"^role_remove:\d+:\d+$"))
async def safe_rank_remove(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, raw_group, raw_id = callback.data.split(":")
    group = await _group(session, int(raw_group))
    assignment = await session.get(RankAssignment, int(raw_id))
    if group is None or assignment is None or assignment.group_id != group.id or not assignment.active:
        await callback.answer("Ранг не найден.", show_alert=True)
        return
    # remove_assignment держит сетевой вызов к Telegram внутри транзакции с
    # FOR UPDATE на Group. Любое непредвиденное исключение раньше уходило в
    # middleware, и пользователь видел вечный спиннер без ответа. Ошибку
    # показываем явно: молчание хуже явной ошибки.
    try:
        ok, error = await remove_assignment(
            bot, session, group, callback.from_user.id, assignment
        )
    except Exception:
        await session.rollback()
        logger.exception("rank_remove_failed", group_id=group.id)
        await callback.answer(
            "Не удалось снять ранг. Попробуйте ещё раз.",
            show_alert=True,
        )
        return
    if not ok:
        await callback.answer(error, show_alert=True)
        return
    await callback.message.edit_text(
        panel_header("Ранг снят", f"Telegram ID: {assignment.user_telegram_id}"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К рангам", callback_data=f"roles:{group.id}")]
        ]),
    )
    await callback.answer("Ранг снят")
