from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.db.rank_models import GroupRankPolicy
from app.services.ranks import (
    DEFAULT_ROLE_PERMISSIONS,
    PERMISSION_LABELS,
    RANK_CODES,
    RANK_LABELS,
    ROLE_CEILINGS,
    is_service_owner,
)
from app.services.ui import panel_header


router = Router(name=__name__)
REMOVED_PERMISSIONS = {"kick"}


async def _effective_policy(session: AsyncSession, group_id: int, rank_code: str) -> dict[str, bool]:
    row = await session.scalar(select(GroupRankPolicy).where(
        GroupRankPolicy.group_id == group_id,
        GroupRankPolicy.rank_code == rank_code,
    ))
    result = dict(DEFAULT_ROLE_PERMISSIONS.get(rank_code, {}))
    if row is not None:
        for name, value in (row.permissions or {}).items():
            if name in ROLE_CEILINGS.get(rank_code, set()):
                result[name] = bool(value)
    return result


async def _render(callback: CallbackQuery, session: AsyncSession, group: Group, rank_code: str) -> None:
    permissions = await _effective_policy(session, group.id, rank_code)
    visible_permissions = ROLE_CEILINGS.get(rank_code, set()) - REMOVED_PERMISSIONS
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if permissions.get(name, False) else '❌'} {PERMISSION_LABELS.get(name, name)}",
            callback_data=f"rank_policy_perm:{group.id}:{rank_code}:{name}",
        )]
        for name in sorted(visible_permissions, key=lambda key: PERMISSION_LABELS.get(key, key))
    ]
    rows.append([InlineKeyboardButton(text="◀️ К рангам", callback_data=f"rank_policies:{group.id}")])
    await callback.message.edit_text(
        panel_header(
            RANK_LABELS.get(rank_code, rank_code),
            "Включите только те права, которые должны автоматически получать участники этого ранга в данной группе.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# Kept as startswith rather than duplicating the exact legacy regexp. This
# router is registered before telegram_roles and owns this transition.
@router.callback_query(F.data.startswith("rank_policy_perm:"))
async def rank_policy_permission_fixed(callback: CallbackQuery, session: AsyncSession) -> None:
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Некорректная настройка права.", show_alert=True)
        return
    _, raw_group, rank_code, permission = parts
    try:
        group_id = int(raw_group)
    except ValueError:
        await callback.answer("Некорректная группа.", show_alert=True)
        return
    group = await session.scalar(
        select(Group)
        .where(Group.id == group_id, Group.is_active.is_(True))
        .with_for_update()
    )
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    from app.services.ranks import DEPUTY_OWNER, get_assignment

    assignment = await get_assignment(session, group.id, callback.from_user.id)
    has_access = (
        is_service_owner(callback.from_user.id)
        or group.owner_telegram_id == callback.from_user.id
        or (assignment is not None and assignment.active and assignment.rank_code == DEPUTY_OWNER)
    )
    if not has_access:
        await callback.answer("Настраивать права рангов может только владелец группы.", show_alert=True)
        return
    if (
        rank_code not in RANK_CODES
        or permission in REMOVED_PERMISSIONS
        or permission not in ROLE_CEILINGS.get(rank_code, set())
    ):
        await callback.answer("Это право недоступно данному рангу.", show_alert=True)
        return

    row = await session.scalar(select(GroupRankPolicy).where(
        GroupRankPolicy.group_id == group.id,
        GroupRankPolicy.rank_code == rank_code,
    ))
    current = await _effective_policy(session, group.id, rank_code)
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
    await _render(callback, session, group, rank_code)
    await callback.answer("Право изменено")
