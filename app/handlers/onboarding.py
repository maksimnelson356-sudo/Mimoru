from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.keyboards.panel import (
    group_health_menu,
    setup_captcha_menu,
    setup_finish_menu,
    setup_level_menu,
    setup_profile_menu,
    setup_quarantine_menu,
    setup_reports_menu,
    setup_welcome_menu,
)
from app.services.access import is_service_owner
from app.services.group_health import calculate_group_health
from app.services.plans import feature_available
from app.services.setup_mutations import mutate_setup_group
from app.services.setup_profiles import LEVEL_LABELS, PROFILE_LABELS, apply_setup_profile
from app.services.ui import clean_ui_text, panel_header

router = Router(name=__name__)


async def managed_group(session: AsyncSession, group_id: int, user_id: int) -> Group | None:
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not is_service_owner(user_id):
        query = query.where(Group.owner_telegram_id == user_id)
    return await session.scalar(query)


def _wizard_intro(group: Group) -> str:
    return (
        panel_header("Мастер настройки", f"Группа: {clean_ui_text(group.title)}")
        + "\n\nMimoru применит безопасный стартовый профиль. После мастера любую настройку можно изменить отдельно."
        + "\n\nШаг 1 из 6 · Какой это тип сообщества?"
    )


@router.callback_query(F.data.regexp(r"^setup:\d+:start$"))
async def setup_start(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await managed_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Настройка доступна только владельцу группы.", show_alert=True)
        return
    await callback.message.edit_text(_wizard_intro(group), reply_markup=setup_profile_menu(group.id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setup:\d+:type:(community|gaming|crypto|sales|news|education)$"))
async def setup_type(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile = callback.data.split(":")
    group = await managed_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header("Мастер настройки", f"Группа: {clean_ui_text(group.title)}")
        + f"\n\nТип: {clean_ui_text(PROFILE_LABELS[profile])}"
        + "\n\nШаг 2 из 6 · Выберите уровень защиты.",
        reply_markup=setup_level_menu(group.id, profile),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setup:\d+:level:(community|gaming|crypto|sales|news|education):(minimal|standard|maximum)$"))
async def setup_level(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile, level = callback.data.split(":")

    def apply_profile(group: Group) -> None:
        apply_setup_profile(group.settings, profile, level)

    group, _ = await mutate_setup_group(
        session,
        group_id=int(raw_group),
        actor_id=callback.from_user.id,
        mutation=apply_profile,
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header("Мастер настройки", f"{clean_ui_text(PROFILE_LABELS[profile])} · {clean_ui_text(LEVEL_LABELS[level])}")
        + "\n\nШаг 3 из 6 · Проверять новых участников капчей?",
        reply_markup=setup_captcha_menu(group.id),
    )
    await callback.answer("Профиль применён")


@router.callback_query(F.data.regexp(r"^setup:\d+:captcha:(on|off)$"))
async def setup_captcha(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, value = callback.data.split(":")

    def set_captcha(group: Group) -> None:
        group.settings.captcha_enabled = value == "on"

    group, _ = await mutate_setup_group(
        session,
        group_id=int(raw_group),
        actor_id=callback.from_user.id,
        mutation=set_captcha,
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header("Мастер настройки", "Параметры входа")
        + "\n\nШаг 4 из 6 · Отправлять приветствие новым участникам?",
        reply_markup=setup_welcome_menu(group.id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setup:\d+:welcome:(on|off)$"))
async def setup_welcome(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, value = callback.data.split(":")

    def set_welcome(group: Group) -> None:
        group.settings.welcome_enabled = value == "on"

    group, _ = await mutate_setup_group(
        session,
        group_id=int(raw_group),
        actor_id=callback.from_user.id,
        mutation=set_welcome,
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header("Мастер настройки", "Параметры новичков")
        + "\n\nШаг 5 из 6 · Включить карантин новичков?",
        reply_markup=setup_quarantine_menu(group.id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setup:\d+:quarantine:(on|off)$"))
async def setup_quarantine(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, value = callback.data.split(":")

    def set_quarantine(group: Group) -> None:
        group.settings.newcomer_quarantine_enabled = value == "on"

    group, _ = await mutate_setup_group(
        session,
        group_id=int(raw_group),
        actor_id=callback.from_user.id,
        mutation=set_quarantine,
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header("Мастер настройки", "Отчёты владельцу")
        + "\n\nШаг 6 из 6 · Включить ежедневную сводку?",
        reply_markup=setup_reports_menu(group.id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setup:\d+:reports:(on|off)$"))
async def setup_reports(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, value = callback.data.split(":")
    wants_reports = value == "on"

    def set_reports(group: Group) -> bool:
        reports_available = feature_available(group, "daily_reports")
        group.settings.reports_enabled = wants_reports and reports_available
        return reports_available

    group, reports_available = await mutate_setup_group(
        session,
        group_id=int(raw_group),
        actor_id=callback.from_user.id,
        mutation=set_reports,
    )
    if not group or reports_available is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    s = group.settings
    enabled = [
        name for enabled_value, name in [
            (s.antiflood_enabled, "Антифлуд"),
            (s.anti_raid_enabled, "Anti-Raid"),
            (s.edit_protection_enabled, "Защита редактирования"),
            (s.captcha_enabled, "Капча"),
            (s.newcomer_quarantine_enabled, "Карантин новичков"),
            (s.welcome_enabled, "Приветствие"),
            (s.reports_enabled, "Ежедневный отчёт"),
        ] if enabled_value
    ]
    text = panel_header("Настройка завершена", f"Группа: {clean_ui_text(group.title)}")
    text += "\n\nВключено:\n" + ("\n".join(f"✅ {clean_ui_text(x)}" for x in enabled) if enabled else "Ничего")
    text += "\n\nТеперь проверьте состояние группы — Mimoru покажет права бота и рекомендации."
    await callback.message.edit_text(text, reply_markup=setup_finish_menu(group.id))
    if wants_reports and not reports_available:
        await callback.answer("Ежедневные отчёты доступны на TRIAL, STANDARD и PRO.", show_alert=True)
    else:
        await callback.answer("Готово")


@router.callback_query(F.data.regexp(r"^health:\d+$"))
async def group_health(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await managed_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    health = await calculate_group_health(bot, session, group)
    perms = [
        f"{'✅' if health.bot_is_admin else '❌'} Администратор",
        f"{'✅' if health.can_delete_messages else '❌'} Удаление сообщений",
        f"{'✅' if health.can_restrict_members else '❌'} Ограничение участников",
        f"{'✅' if health.can_invite_users else '⚪'} Управление приглашениями",
    ]
    text = panel_header("Состояние группы", f"{clean_ui_text(group.title)} · {health.score}/100 · {clean_ui_text(health.level)}")
    text += (
        "\n\nПрава Mimoru\n" + "\n".join(perms)
        + "\n\nОценка"
        + f"\n🛡 Права: {health.permission_score}/35"
        + f"\n🔐 Защита: {health.protection_score}/28"
        + f"\n👋 Новички: {health.newcomer_score}/12"
        + f"\n📬 Отчёты: {health.reporting_score}/5"
        + f"\n🧹 Чистота: {health.hygiene_score}/20"
        + f"\n\n👥 Известно участников: {health.known_members}"
        + f"\n🪦 Удалённых аккаунтов: {health.deleted_accounts}"
    )
    if health.recommendations:
        text += "\n\nЧто можно улучшить\n" + "\n".join(f"• {clean_ui_text(item)}" for item in health.recommendations)
    else:
        text += "\n\n✅ Критичных рекомендаций сейчас нет."
    await callback.message.edit_text(text, reply_markup=group_health_menu(group.id))
    await callback.answer("Проверено")
