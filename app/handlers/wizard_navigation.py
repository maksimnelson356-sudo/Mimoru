from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.services.access import is_service_owner
from app.services.plans import feature_available
from app.services.setup_mutations import mutate_setup_group
from app.services.setup_profiles import LEVEL_LABELS, PROFILE_LABELS, apply_setup_profile
from app.services.ui import clean_ui_text, panel_header

router = Router(name=__name__)


async def _owned_group(session: AsyncSession, group_id: int, user_id: int) -> Group | None:
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not is_service_owner(user_id):
        query = query.where(Group.owner_telegram_id == user_id)
    return await session.scalar(query)


def _profile_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Сообщество", callback_data=f"setupnav:{group_id}:type:community"),
            InlineKeyboardButton(text="🎮 Игры", callback_data=f"setupnav:{group_id}:type:gaming"),
        ],
        [
            InlineKeyboardButton(text="🪙 Крипта", callback_data=f"setupnav:{group_id}:type:crypto"),
            InlineKeyboardButton(text="🛍 Продажи", callback_data=f"setupnav:{group_id}:type:sales"),
        ],
        [
            InlineKeyboardButton(text="📰 Новости", callback_data=f"setupnav:{group_id}:type:news"),
            InlineKeyboardButton(text="🎓 Обучение", callback_data=f"setupnav:{group_id}:type:education"),
        ],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data=f"group_section:{group_id}:settings")],
    ])


def _level_menu(group_id: int, profile: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Минимальный", callback_data=f"setupnav:{group_id}:level:{profile}:minimal")],
        [InlineKeyboardButton(text="🟡 Стандартный", callback_data=f"setupnav:{group_id}:level:{profile}:standard")],
        [InlineKeyboardButton(text="🔴 Максимальный", callback_data=f"setupnav:{group_id}:level:{profile}:maximum")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"setupnav:{group_id}:step1")],
        [InlineKeyboardButton(text="✖️ Завершить позже", callback_data=f"group_section:{group_id}:settings")],
    ])


def _yes_no_menu(group_id: int, profile: str, level: str, step: int, field: str) -> InlineKeyboardMarkup:
    previous = (
        f"setupnav:{group_id}:step{step - 1}:{profile}:{level}"
        if step > 3
        else f"setupnav:{group_id}:step2:{profile}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data=f"setupnav:{group_id}:{field}:{profile}:{level}:on"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"setupnav:{group_id}:{field}:{profile}:{level}:off"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=previous)],
        [InlineKeyboardButton(text="✖️ Завершить позже", callback_data=f"group_section:{group_id}:settings")],
    ])


def _wizard_text(group: Group, step: int, body: str) -> str:
    return panel_header("Мастер настройки", f"Группа: {clean_ui_text(group.title)}") + f"\n\nШаг {step} из 6 · {body}"


async def _render_step1(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await _owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Настройка доступна только владельцу группы.", show_alert=True)
        return
    await callback.message.edit_text(
        _wizard_text(group, 1, "Какой это тип сообщества?"),
        reply_markup=_profile_menu(group.id),
    )
    await callback.answer()


async def _render_step3(callback: CallbackQuery, session: AsyncSession, group_id: int, profile: str, level: str) -> None:
    group = await _owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        _wizard_text(group, 3, "Проверять новых участников капчей?"),
        reply_markup=_yes_no_menu(group.id, profile, level, 3, "captcha"),
    )
    await callback.answer()


async def _render_step4(callback: CallbackQuery, session: AsyncSession, group_id: int, profile: str, level: str) -> None:
    group = await _owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        _wizard_text(group, 4, "Отправлять приветствие новым участникам?"),
        reply_markup=_yes_no_menu(group.id, profile, level, 4, "welcome"),
    )
    await callback.answer()


async def _render_step5(callback: CallbackQuery, session: AsyncSession, group_id: int, profile: str, level: str) -> None:
    group = await _owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        _wizard_text(group, 5, "Включить карантин новичков?"),
        reply_markup=_yes_no_menu(group.id, profile, level, 5, "quarantine"),
    )
    await callback.answer()


async def _render_step6(callback: CallbackQuery, session: AsyncSession, group_id: int, profile: str, level: str) -> None:
    group = await _owned_group(session, group_id, callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        _wizard_text(group, 6, "Включить ежедневную сводку?"),
        reply_markup=_yes_no_menu(group.id, profile, level, 6, "reports"),
    )
    await callback.answer()


# Compatibility: old messages and existing keyboards may still contain
# setup:<group_id>:start. This router is registered before navigation_fixes, so
# old buttons enter the same reversible flow instead of the legacy wizard.
@router.callback_query(F.data.regexp(r"^setup:\d+:start$"))
async def legacy_start(callback: CallbackQuery, session: AsyncSession) -> None:
    await _render_step1(callback, session)


@router.callback_query(F.data.regexp(r"^setupnav:\d+:step1$"))
async def step1(callback: CallbackQuery, session: AsyncSession) -> None:
    await _render_step1(callback, session)


@router.callback_query(F.data.regexp(r"^setupnav:\d+:type:(community|gaming|crypto|sales|news|education)$"))
async def choose_type(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile = callback.data.split(":")
    group = await _owned_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        _wizard_text(group, 2, f"Тип: {clean_ui_text(PROFILE_LABELS[profile])}. Выберите уровень защиты."),
        reply_markup=_level_menu(group.id, profile),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setupnav:\d+:step2:(community|gaming|crypto|sales|news|education)$"))
async def step2(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile = callback.data.split(":")
    group = await _owned_group(session, int(raw_group), callback.from_user.id)
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        _wizard_text(group, 2, f"Тип: {clean_ui_text(PROFILE_LABELS[profile])}. Выберите уровень защиты."),
        reply_markup=_level_menu(group.id, profile),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setupnav:\d+:level:(community|gaming|crypto|sales|news|education):(minimal|standard|maximum)$"))
async def choose_level(callback: CallbackQuery, session: AsyncSession) -> None:
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
        reply_markup=_yes_no_menu(group.id, profile, level, 3, "captcha"),
    )
    await callback.answer("Профиль применён")


@router.callback_query(F.data.regexp(r"^setupnav:\d+:step3:(community|gaming|crypto|sales|news|education):(minimal|standard|maximum)$"))
async def step3(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile, level = callback.data.split(":")
    await _render_step3(callback, session, int(raw_group), profile, level)


@router.callback_query(F.data.regexp(r"^setupnav:\d+:step4:(community|gaming|crypto|sales|news|education):(minimal|standard|maximum)$"))
async def step4(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile, level = callback.data.split(":")
    await _render_step4(callback, session, int(raw_group), profile, level)


@router.callback_query(F.data.regexp(r"^setupnav:\d+:step5:(community|gaming|crypto|sales|news|education):(minimal|standard|maximum)$"))
async def step5(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile, level = callback.data.split(":")
    await _render_step5(callback, session, int(raw_group), profile, level)


@router.callback_query(F.data.regexp(r"^setupnav:\d+:step6:(community|gaming|crypto|sales|news|education):(minimal|standard|maximum)$"))
async def step6(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile, level = callback.data.split(":")
    await _render_step6(callback, session, int(raw_group), profile, level)


@router.callback_query(F.data.regexp(r"^setupnav:\d+:captcha:(community|gaming|crypto|sales|news|education):(minimal|standard|maximum):(on|off)$"))
async def choose_captcha(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile, level, value = callback.data.split(":")

    def mutate(group: Group) -> None:
        group.settings.captcha_enabled = value == "on"

    group, _ = await mutate_setup_group(
        session,
        group_id=int(raw_group),
        actor_id=callback.from_user.id,
        mutation=mutate,
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        _wizard_text(group, 4, "Отправлять приветствие новым участникам?"),
        reply_markup=_yes_no_menu(group.id, profile, level, 4, "welcome"),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setupnav:\d+:welcome:(community|gaming|crypto|sales|news|education):(minimal|standard|maximum):(on|off)$"))
async def choose_welcome(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile, level, value = callback.data.split(":")

    def mutate(group: Group) -> None:
        group.settings.welcome_enabled = value == "on"

    group, _ = await mutate_setup_group(
        session,
        group_id=int(raw_group),
        actor_id=callback.from_user.id,
        mutation=mutate,
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        _wizard_text(group, 5, "Включить карантин новичков?"),
        reply_markup=_yes_no_menu(group.id, profile, level, 5, "quarantine"),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setupnav:\d+:quarantine:(community|gaming|crypto|sales|news|education):(minimal|standard|maximum):(on|off)$"))
async def choose_quarantine(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, profile, level, value = callback.data.split(":")

    def mutate(group: Group) -> None:
        group.settings.newcomer_quarantine_enabled = value == "on"

    group, _ = await mutate_setup_group(
        session,
        group_id=int(raw_group),
        actor_id=callback.from_user.id,
        mutation=mutate,
    )
    if not group:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        _wizard_text(group, 6, "Включить ежедневную сводку?"),
        reply_markup=_yes_no_menu(group.id, profile, level, 6, "reports"),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setupnav:\d+:reports:(community|gaming|crypto|sales|news|education):(minimal|standard|maximum):(on|off)$"))
async def choose_reports(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group, _, _profile, _level, value = callback.data.split(":")
    wants_reports = value == "on"

    def mutate(group: Group) -> bool:
        available = feature_available(group, "daily_reports")
        group.settings.reports_enabled = wants_reports and available
        return available

    group, available = await mutate_setup_group(
        session,
        group_id=int(raw_group),
        actor_id=callback.from_user.id,
        mutation=mutate,
    )
    if not group or available is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    enabled = [
        name for enabled_value, name in [
            (group.settings.antiflood_enabled, "Антифлуд"),
            (group.settings.anti_raid_enabled, "Anti-Raid"),
            (group.settings.edit_protection_enabled, "Защита редактирования"),
            (group.settings.captcha_enabled, "Капча"),
            (group.settings.newcomer_quarantine_enabled, "Карантин новичков"),
            (group.settings.welcome_enabled, "Приветствие"),
            (group.settings.reports_enabled, "Ежедневный отчёт"),
        ] if enabled_value
    ]
    text = panel_header("Настройка завершена", f"Группа: {clean_ui_text(group.title)}")
    text += "\n\nВключено:\n" + ("\n".join(f"✅ {clean_ui_text(item)}" for item in enabled) if enabled else "Ничего")
    text += "\n\nТеперь проверьте состояние группы — Mimoru покажет права бота и рекомендации."
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Проверить состояние", callback_data=f"health:{group.id}")],
        [InlineKeyboardButton(text="⚙️ К настройкам", callback_data=f"group_section:{group.id}:settings")],
        [InlineKeyboardButton(text="🏠 К группе", callback_data=f"group:{group.id}")],
    ])
    await callback.message.edit_text(text, reply_markup=markup)
    if wants_reports and not available:
        await callback.answer("Ежедневные отчёты доступны на TRIAL, STANDARD и PRO.", show_alert=True)
    else:
        await callback.answer("Готово")
