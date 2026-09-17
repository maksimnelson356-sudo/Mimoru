from __future__ import annotations

from datetime import UTC

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, GroupMember, User
from app.keyboards.panel import deleted_accounts_confirm_menu, deleted_accounts_menu
from app.services.access import accessible_group, is_service_owner
from app.services.deleted_accounts import (
    deleted_accounts_count,
    remove_deleted_accounts,
    scan_known_members,
)
from app.services.moderation import log_action
from app.services.ui import clean_ui_text, panel_header

router = Router(name=__name__)


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


async def _screen(session: AsyncSession, group: Group) -> tuple[str, object]:
    known = int(await session.scalar(select(func.count()).select_from(GroupMember).where(
        GroupMember.group_id == group.id,
    )) or 0)
    present = int(await session.scalar(select(func.count()).select_from(GroupMember).where(
        GroupMember.group_id == group.id,
        GroupMember.is_present.is_(True),
    )) or 0)
    deleted = await deleted_accounts_count(session, group.id)
    last_checked = await session.scalar(select(func.max(GroupMember.last_checked_at)).where(GroupMember.group_id == group.id))
    rows = (await session.execute(
        select(GroupMember.user_telegram_id, User.username, User.first_name)
        .outerjoin(User, User.telegram_id == GroupMember.user_telegram_id)
        .where(
            GroupMember.group_id == group.id,
            GroupMember.is_present.is_(True),
            GroupMember.is_deleted_account.is_(True),
        )
        .order_by(GroupMember.user_telegram_id)
        .limit(30)
    )).all()
    lines = []
    for uid, username, first_name in rows:
        label = f"@{clean_ui_text(username)}" if username else clean_ui_text(first_name or "Deleted Account")
        lines.append(f"• {label} · {uid}")
    checked_label = "ещё не выполнялась"
    if last_checked is not None:
        checked_label = last_checked.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")
    text = (
        panel_header("Удалённые аккаунты", group.title)
        + f"\n\n🪦 Найдено: {deleted}"
        + f"\n👥 Известно Mimoru участников: {known}"
        + f"\n✅ Сейчас отмечены в группе: {present}"
        + f"\n🕒 Последняя проверка: {checked_label}"
        + "\n\nНайденные аккаунты\n"
        + ("\n".join(lines) if lines else "Удалённых аккаунтов среди известных участников не найдено.")
        + "\n\nВажно: Telegram Bot API не позволяет боту получить полный список всех участников группы. "
          "Mimoru проверяет аккаунты, которые уже видела в сообщениях, входах и событиях участников. "
          "Кнопка «Проверить сейчас» обновляет их состояние через Telegram."
    )
    return text, deleted_accounts_menu(group.id, deleted > 0)


@router.callback_query(F.data.regexp(r"^deleted_accounts:\d+$"))
async def deleted_accounts_screen(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    text, markup = await _screen(session, group)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^deleted_accounts_scan:\d+$"))
async def deleted_accounts_scan(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(
        session,
        group_id,
        callback.from_user.id,
        for_update=True,
    )
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer("Проверяю известных участников…")
    await callback.message.edit_text(panel_header("Удалённые аккаунты", "Проверяю известных Mimoru участников через Telegram…"))
    result = await scan_known_members(bot, session, group)
    await session.commit()
    text, markup = await _screen(session, group)
    text += (
        f"\n\nРезультат проверки\n"
        f"Проверено: {result.checked}\n"
        f"В группе: {result.present}\n"
        f"Удалённых аккаунтов: {result.deleted}"
    )
    if result.inaccessible:
        text += f"\nНе удалось проверить: {result.inaccessible}"
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^deleted_accounts_remove_confirm:\d+$"))
async def deleted_accounts_remove_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    count = await deleted_accounts_count(session, group.id)
    if count == 0:
        await callback.answer("Удалённых аккаунтов не найдено.", show_alert=True)
        return
    text = (
        panel_header("Подтверждение очистки", group.title)
        + f"\n\nБудет удалено найденных аккаунтов: {count}."
        + "\n\nПеред удалением Mimoru ещё раз обновит список. Реальные пользователи не удаляются: "
          "действие выполняется только для профилей, которые Telegram возвращает как «Deleted Account»."
    )
    await callback.message.edit_text(text, reply_markup=deleted_accounts_confirm_menu(group.id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^deleted_accounts_remove:\d+$"))
async def deleted_accounts_remove(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(
        session,
        group_id,
        callback.from_user.id,
        for_update=True,
    )
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer("Обновляю список и очищаю…")
    await callback.message.edit_text(panel_header("Очистка удалённых аккаунтов", "Проверяю список перед удалением…"))
    scan = await scan_known_members(bot, session, group)
    cleanup = await remove_deleted_accounts(bot, session, group)
    log_action(
        session,
        group.id,
        callback.from_user.id,
        0,
        "deleted_accounts_cleanup",
        "Массовая очистка удалённых Telegram-аккаунтов",
        {"checked": scan.checked, "found": cleanup.found, "removed": cleanup.removed, "failed": cleanup.failed},
    )
    await session.commit()
    text, markup = await _screen(session, group)
    text += (
        f"\n\nОчистка завершена\n"
        f"Найдено перед удалением: {cleanup.found}\n"
        f"✅ Удалено: {cleanup.removed}\n"
        f"⚠️ Не удалось удалить: {cleanup.failed}"
    )
    await callback.message.edit_text(text, reply_markup=markup)
