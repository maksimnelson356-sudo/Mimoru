from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AutomationLog, Group
from app.keyboards.panel import (
    automation_cleanup_menu,
    automation_menu,
    automation_newcomer_menu,
    automation_warning_menu,
)
from app.services.access import accessible_group, is_service_owner
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


@router.callback_query(F.data.regexp(r"^automation:\d+$"))
async def automation_home(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    s = group.settings
    state = "работает" if s.automation_enabled else "остановлена"
    text = panel_header(
        "Автоматизация",
        f"Группа: {clean_ui_text(group.title)}\n\n"
        f"Состояние: {state}\n"
        "Здесь Mimoru выполняет только заранее понятные правила — без ИИ.\n\n"
        "• обслуживание удалённых аккаунтов\n"
        "• срок жизни предупреждений\n"
        "• готовый сценарий для новичков\n"
        "• журнал автоматических запусков",
    )
    await callback.message.edit_text(text, reply_markup=automation_menu(group))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^automation_toggle:\d+$"))
async def automation_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id, for_update=True)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group.settings.automation_enabled = not group.settings.automation_enabled
    session.add(
        AutomationLog(
            group_id=group.id,
            rule_code="automation_master",
            status="changed",
            details={
                "enabled": group.settings.automation_enabled,
                "actor": callback.from_user.id,
            },
        )
    )
    await session.commit()
    await callback.message.edit_reply_markup(reply_markup=automation_menu(group))
    await callback.answer(
        "Автоматизация включена."
        if group.settings.automation_enabled
        else "Автоматизация остановлена."
    )


@router.callback_query(F.data.regexp(r"^automation_cleanup:\d+$"))
async def cleanup_screen(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Очистка удалённых аккаунтов",
            "Mimoru проверяет только известных ей участников и перед удалением повторно сверяет профиль через Telegram.",
        ),
        reply_markup=automation_cleanup_menu(
            group.id, group.settings.deleted_cleanup_schedule
        ),
    )
    await callback.answer()


@router.callback_query(
    F.data.regexp(r"^automation_cleanup_set:\d+:(off|weekly|monthly)$")
)
async def cleanup_set(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group_id, schedule = callback.data.split(":")
    group = await owned_group(
        session, int(raw_group_id), callback.from_user.id, for_update=True
    )
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group.settings.deleted_cleanup_schedule = schedule
    if schedule == "off":
        group.settings.deleted_cleanup_last_run_at = None
    else:
        group.settings.deleted_cleanup_last_run_at = datetime.now(UTC)
    session.add(
        AutomationLog(
            group_id=group.id,
            rule_code="deleted_cleanup_schedule",
            status="changed",
            details={"schedule": schedule, "actor": callback.from_user.id},
        )
    )
    await session.commit()
    await callback.message.edit_reply_markup(
        reply_markup=automation_cleanup_menu(group.id, schedule)
    )
    await callback.answer("Расписание сохранено.")


@router.callback_query(F.data.regexp(r"^automation_warnings:\d+$"))
async def warnings_screen(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Срок предупреждений",
            "По истечении выбранного срока старое активное предупреждение автоматически перестаёт учитываться.",
        ),
        reply_markup=automation_warning_menu(
            group.id, group.settings.warning_expire_days
        ),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^automation_warning_set:\d+:(0|7|30|90)$"))
async def warnings_set(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group_id, raw_days = callback.data.split(":")
    group = await owned_group(
        session, int(raw_group_id), callback.from_user.id, for_update=True
    )
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    days = int(raw_days)
    group.settings.warning_expire_days = days
    session.add(
        AutomationLog(
            group_id=group.id,
            rule_code="warning_expiry",
            status="changed",
            details={"days": days, "actor": callback.from_user.id},
        )
    )
    await session.commit()
    await callback.message.edit_reply_markup(
        reply_markup=automation_warning_menu(group.id, days)
    )
    await callback.answer("Срок сохранён.")


@router.callback_query(F.data.regexp(r"^automation_newcomer:\d+$"))
async def newcomer_screen(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Сценарий новичка",
            "Профиль меняет только настройки входа. Его всегда можно затем подправить вручную.",
        ),
        reply_markup=automation_newcomer_menu(group),
    )
    await callback.answer()


@router.callback_query(
    F.data.regexp(r"^automation_newcomer_set:\d+:(basic|standard|strict)$")
)
async def newcomer_set(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group_id, profile = callback.data.split(":")
    group = await owned_group(
        session, int(raw_group_id), callback.from_user.id, for_update=True
    )
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    s = group.settings
    if profile == "basic":
        s.welcome_enabled, s.captcha_enabled, s.newcomer_quarantine_enabled = (
            True,
            False,
            False,
        )
    elif profile == "standard":
        s.welcome_enabled, s.captcha_enabled, s.newcomer_quarantine_enabled = (
            True,
            True,
            False,
        )
    else:
        s.welcome_enabled, s.captcha_enabled, s.newcomer_quarantine_enabled = (
            True,
            True,
            True,
        )
        s.newcomer_quarantine_seconds = max(s.newcomer_quarantine_seconds, 86400)
    session.add(
        AutomationLog(
            group_id=group.id,
            rule_code="newcomer_profile",
            status="changed",
            details={"profile": profile, "actor": callback.from_user.id},
        )
    )
    await session.commit()
    await callback.message.edit_reply_markup(
        reply_markup=automation_newcomer_menu(group)
    )
    await callback.answer("Профиль применён.")


@router.callback_query(F.data.regexp(r"^automation_logs:\d+$"))
async def automation_logs(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    rows = (
        await session.scalars(
            select(AutomationLog)
            .where(AutomationLog.group_id == group.id)
            .order_by(AutomationLog.created_at.desc())
            .limit(20)
        )
    ).all()
    labels = {
        "automation_master": "Общий переключатель",
        "deleted_cleanup_schedule": "Расписание очистки",
        "deleted_cleanup": "Очистка удалённых аккаунтов",
        "warning_expiry": "Срок предупреждений",
        "newcomer_profile": "Сценарий новичка",
    }
    lines = []
    for row in rows:
        when = row.created_at.strftime("%d.%m %H:%M") if row.created_at else "—"
        lines.append(
            f"• {when} · {clean_ui_text(labels.get(row.rule_code, row.rule_code))} · {clean_ui_text(row.status)}"
        )
    text = (
        panel_header("Журнал автоматизации", "Последние 20 событий")
        + "\n\n"
        + ("\n".join(lines) if lines else "Запусков пока не было.")
    )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="◀️ Автоматизация", callback_data=f"automation:{group.id}"
                    )
                ]
            ]
        ),
    )
    await callback.answer()
