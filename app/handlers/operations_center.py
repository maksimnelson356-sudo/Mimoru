from __future__ import annotations

import structlog
from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AutomationLog,
    Group,
    GroupConfigSnapshot,
    ModerationLog,
    OperationEvent,
)
from app.keyboards.panel import operations_menu, snapshot_targets_menu
from app.services.access import accessible_group, is_service_owner, owned_group
from app.services.operations_center import (
    apply_snapshot_authorized,
    create_snapshot,
    diagnose_group,
    diagnostics_score,
    record_operation,
)
from app.services.ui import clean_ui_text, panel_header

router = Router(name=__name__)


async def authorized_snapshot(
    session: AsyncSession,
    snapshot_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> tuple[GroupConfigSnapshot | None, Group | None]:
    snap = await session.get(GroupConfigSnapshot, snapshot_id)
    if snap is None:
        return None, None

    if for_update:
        source = await owned_group(
            session, snap.group_id, user_id, for_update=True
        )
    else:
        source = await accessible_group(session, snap.group_id, user_id)

    if source is None:
        return None, None
    return snap, source


@router.callback_query(F.data.regexp(r"^ops:\d+$"))
async def ops_home(callback: CallbackQuery, session: AsyncSession) -> None:
    gid = int(callback.data.split(":")[1])
    group = await accessible_group(session, gid, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Operations Center",
            f"Группа: {clean_ui_text(group.title)}\n\nПроверка прав, журнал событий, резервные копии и безопасное управление.",
        ),
        reply_markup=operations_menu(group.id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ops_diag:\d+$"))
async def ops_diag(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    gid = int(callback.data.split(":")[1])
    group = await accessible_group(session, gid, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    d = await diagnose_group(bot, group)
    score = diagnostics_score(d)
    lines = [
        f"{'✅' if d['reachable'] else '❌'} Группа доступна",
        f"{'✅' if d['is_admin'] else '❌'} Mimoru — администратор",
        f"{'✅' if d['delete'] else '❌'} Удаление сообщений",
        f"{'✅' if d['restrict'] else '❌'} Мут / бан / кик",
        f"{'✅' if d['invite'] else '❌'} Пригласительные ссылки",
        f"{'✅' if d['manage_chat'] else '❌'} Управление группой",
    ]
    warnings = []
    s = group.settings
    if (
        s.antiflood_enabled
        or not s.links_enabled
        or s.caps_enabled
        or s.edit_protection_enabled
    ) and not d["delete"]:
        warnings.append(
            "⚠️ Защита сообщений включена, но удаление сообщений недоступно."
        )
    if (s.antiflood_enabled or s.newcomer_quarantine_enabled) and not d["restrict"]:
        warnings.append(
            "⚠️ Ограничения включены, но Mimoru не может ограничивать участников."
        )
    if s.join_requests_enabled and not d["invite"]:
        warnings.append("⚠️ Работа со ссылками/заявками ограничена правами Telegram.")
    status = "🟢" if score >= 90 else ("🟡" if score >= 60 else "🔴")
    body = (
        f"{clean_ui_text(group.title)}\n\n{status} Готовность: {score}/100\n\n"
        + "\n".join(lines)
    )
    if warnings:
        body += "\n\n" + "\n".join(warnings)
    if d.get("error"):
        body += f"\n\nОшибка Telegram: {clean_ui_text(str(d['error']))}"
    await record_operation(
        session,
        group.id,
        "diagnostics",
        status="ok" if score >= 60 else "warning",
        actor=callback.from_user.id,
        details={"score": score, **{k: v for k, v in d.items() if k != "title"}},
    )
    await session.commit()
    await callback.message.edit_text(
        panel_header("Диагностика", body),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Проверить снова", callback_data=f"ops_diag:{gid}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="◀️ Operations Center", callback_data=f"ops:{gid}"
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ops_logs:\d+$"))
async def ops_logs(callback: CallbackQuery, session: AsyncSession) -> None:
    gid = int(callback.data.split(":")[1])
    group = await accessible_group(session, gid, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    ops = (
        await session.scalars(
            select(OperationEvent)
            .where(OperationEvent.group_id == gid)
            .order_by(OperationEvent.created_at.desc())
            .limit(12)
        )
    ).all()
    mods = (
        await session.scalars(
            select(ModerationLog)
            .where(ModerationLog.group_id == gid)
            .order_by(ModerationLog.created_at.desc())
            .limit(12)
        )
    ).all()
    autos = (
        await session.scalars(
            select(AutomationLog)
            .where(AutomationLog.group_id == gid)
            .order_by(AutomationLog.created_at.desc())
            .limit(12)
        )
    ).all()
    rows = []
    for x in ops:
        rows.append((x.created_at, "🧰", x.event_type, x.status, x.message or ""))
    for x in mods:
        rows.append(
            (
                x.created_at,
                "👮",
                x.action,
                "ошибка" if x.delivery_error else "ok",
                x.reason or "",
            )
        )
    for x in autos:
        rows.append((x.created_at, "⚙️", x.rule_code, x.status, ""))
    rows.sort(key=lambda x: x[0], reverse=True)
    lines = []
    for dt, icon, event, status, msg in rows[:25]:
        lines.append(
            f"• {dt:%d.%m %H:%M} {icon} {clean_ui_text(str(event))} · {clean_ui_text(str(status))}"
            + (f" · {clean_ui_text(msg[:70])}" if msg else "")
        )
    await callback.message.edit_text(
        panel_header("Единый журнал", "Последние события группы")
        + "\n\n"
        + ("\n".join(lines) if lines else "Событий пока нет."),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="◀️ Operations Center", callback_data=f"ops:{gid}"
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ops_backup:\d+$"))
async def backup_home(callback: CallbackQuery, session: AsyncSession) -> None:
    gid = int(callback.data.split(":")[1])
    group = await accessible_group(session, gid, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    snaps = (
        await session.scalars(
            select(GroupConfigSnapshot)
            .where(GroupConfigSnapshot.group_id == gid)
            .order_by(GroupConfigSnapshot.created_at.desc())
            .limit(5)
        )
    ).all()
    rows = [
        [
            InlineKeyboardButton(
                text="➕ Создать резервную копию",
                callback_data=f"ops_backup_create:{gid}",
            )
        ]
    ]
    for s in snaps:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📦 {s.created_at:%d.%m %H:%M}",
                    callback_data=f"ops_snapshot:{s.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="◀️ Operations Center", callback_data=f"ops:{gid}")]
    )
    await callback.message.edit_text(
        panel_header(
            "Резервные копии",
            f"Группа: {clean_ui_text(group.title)}\n\nСохраняются настройки защиты, контент-фильтры, обязательные каналы, причины и автоматизация. Роли модераторов не переносятся автоматически.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ops_backup_create:\d+$"))
async def backup_create(callback: CallbackQuery, session: AsyncSession) -> None:
    gid = int(callback.data.split(":")[1])
    group = await owned_group(
        session, gid, callback.from_user.id, for_update=True
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    snap = await create_snapshot(session, group, callback.from_user.id)
    await session.commit()
    await callback.answer(f"Копия #{snap.id} создана.", show_alert=True)
    callback.data = f"ops_backup:{gid}"
    await backup_home(callback, session)


@router.callback_query(F.data.regexp(r"^ops_snapshot:\d+$"))
async def snapshot_open(callback: CallbackQuery, session: AsyncSession) -> None:
    sid = int(callback.data.split(":")[1])
    snap, source = await authorized_snapshot(session, sid, callback.from_user.id)
    if not snap or not source:
        await callback.answer("Нет доступа или копия не найдена.", show_alert=True)
        return
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
    if is_service_owner(callback.from_user.id):
        groups = (
            await session.scalars(
                select(Group)
                .where(Group.is_active.is_(True))
                .order_by(Group.title)
                .limit(50)
            )
        ).all()
    await callback.message.edit_text(
        panel_header(
            "Резервная копия",
            f"#{snap.id} · {snap.created_at:%d.%m.%Y %H:%M} UTC\nИсточник: {clean_ui_text(source.title)}\n\nВыберите группу, в которую применить настройки. Это заменит фильтры, причины и часть настроек выбранной группы.",
        ),
        reply_markup=snapshot_targets_menu(snap.id, groups, source.id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ops_snapshot_confirm:\d+:\d+$"))
async def snapshot_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    _, sid, gid = callback.data.split(":")
    snap, _ = await authorized_snapshot(
        session, int(sid), callback.from_user.id, for_update=True
    )
    target = await owned_group(session, int(gid), callback.from_user.id, for_update=True)
    if not snap or not target:
        await callback.answer("Нет доступа или копия не найдена.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ Применить к «{clean_ui_text(target.title[:28])}»",
                    callback_data=f"ops_snapshot_apply:{sid}:{gid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data=f"ops_snapshot:{sid}"
                )
            ],
        ]
    )
    await callback.message.edit_text(
        panel_header(
            "Подтверждение",
            f"Настройки группы «{clean_ui_text(target.title)}» будут заменены данными резервной копии #{sid}. Участники, статистика, наказания, подписка и платежи не затрагиваются.",
        ),
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ops_snapshot_apply:\d+:\d+$"))
async def snapshot_apply(callback: CallbackQuery, session: AsyncSession) -> None:
    _, sid, gid = callback.data.split(":")
    target = await apply_snapshot_authorized(
        session,
        snapshot_id=int(sid),
        target_group_id=int(gid),
        actor_id=callback.from_user.id,
    )
    if target is None:
        await callback.answer(
            "Нет доступа, группа изменилась или копия не найдена.", show_alert=True
        )
        return
    await callback.message.edit_text(
        panel_header(
            "Готово",
            f"Резервная копия #{sid} применена к группе «{clean_ui_text(target.title)}».",
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🧰 Operations Center", callback_data=f"ops:{target.id}"
                    )
                ]
            ]
        ),
    )
    await callback.answer("Настройки восстановлены.")


@router.my_chat_member()
async def bot_membership_changed(
    event: ChatMemberUpdated, bot: Bot, session: AsyncSession
) -> None:
    group = await session.scalar(
        select(Group).where(Group.telegram_chat_id == event.chat.id)
    )
    if not group:
        return
    old = getattr(
        event.old_chat_member.status, "value", str(event.old_chat_member.status)
    )
    new = getattr(
        event.new_chat_member.status, "value", str(event.new_chat_member.status)
    )
    await record_operation(
        session,
        group.id,
        "bot_membership_changed",
        status="warning" if new not in {"administrator", "creator"} else "ok",
        actor=event.from_user.id,
        details={"old": old, "new": new},
    )
    await session.commit()
    if group.owner_telegram_id and new not in {"administrator", "creator"}:
        try:
            await bot.send_message(
                group.owner_telegram_id,
                f"⚠️ Mimoru · права изменились\n\n🏠 {clean_ui_text(group.title)}\nСтатус Mimoru теперь: {clean_ui_text(new)}.\n\nЧасть защиты и модерации может перестать работать. Откройте Operations Center → Диагностика.",
            )
        except Exception as exc:
            structlog.get_logger().warning(
                "owner_rights_notification_failed",
                group_id=group.id,
                owner_id=group.owner_telegram_id,
                error=str(exc),
            )
