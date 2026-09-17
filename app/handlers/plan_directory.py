from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group
from app.services.access import accessible_group
from app.services.group_refs import group_reference_label
from app.services.plans import effective_plan, paid_plan, remaining_days, subscription_state
from app.services.ui import panel_header

router = Router(name=__name__)


def _catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆓 FREE", callback_data="plans_catalog:free")],
        [InlineKeyboardButton(text="⭐ STANDARD · 250 Stars / 30 дней", callback_data="plans_catalog:standard")],
        [InlineKeyboardButton(text="💎 PRO · 500 Stars / 30 дней", callback_data="plans_catalog:pro")],
        [InlineKeyboardButton(text="📊 Сравнить тарифы", callback_data="plans_catalog:compare")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="panel:home")],
    ])


def _detail_keyboard(plan_code: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if plan_code in {"standard", "pro"}:
        rows.append([InlineKeyboardButton(
            text="🏠 Выбрать группу для этого тарифа",
            callback_data=f"plans_choose_group:{plan_code}",
        )])
    rows.append([InlineKeyboardButton(text="📊 Сравнить тарифы", callback_data="plans_catalog:compare")])
    rows.append([InlineKeyboardButton(text="◀️ К тарифам", callback_data="panel:plans")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _plan_description(plan_code: str) -> str:
    common = (
        "\n\nОдинаково во всех тарифах:\n"
        "• до 5 обязательных подписок / каналов\n"
        "• количество администраторов Telegram и внутренних ролей Mimoru не ограничивается тарифом"
    )
    if plan_code == "free":
        return panel_header(
            "FREE",
            "Для базового управления группой без подписки.\n\n"
            "Доступно:\n"
            "• базовая модерация участников\n"
            "• базовая защита группы\n"
            "• базовая статистика\n"
            "• до 10 запрещённых слов или фраз\n"
            "• до 5 собственных причин наказаний\n\n"
            "Не входят расширенная аналитика, ежедневные отчёты и платные коммерческие возможности."
            + common,
        )
    if plan_code == "standard":
        return panel_header(
            "STANDARD · 250 Stars / 30 дней",
            "Для активно развивающихся групп.\n\n"
            "Всё из FREE, а также:\n"
            "• расширенная защита от спама и флуда\n"
            "• расширенная аналитика\n"
            "• ежедневные отчёты владельцу\n"
            "• доступ к рекламным инструментам Mimoru\n"
            "• до 100 запрещённых слов или фраз\n"
            "• до 30 собственных причин наказаний"
            + common,
        )
    return panel_header(
        "PRO · 500 Stars / 30 дней",
        "Для крупных групп и активного коммерческого использования.\n\n"
        "Всё из STANDARD, а также:\n"
        "• максимальные лимиты контента и управления\n"
        "• до 1000 запрещённых слов или фраз\n"
        "• до 100 собственных причин наказаний\n"
        "• приоритетная поддержка\n"
        "• полный коммерческий набор Mimoru"
        + common,
    )


def _purchase_keyboard(group_id: int, plan_code: str, source: str) -> InlineKeyboardMarkup:
    plan = paid_plan(plan_code)
    back = f"plans_catalog:{plan_code}" if source == "catalog" else f"plan:{group_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"⭐ Оформить за {plan['stars']} Stars",
            callback_data=f"plan_checkout:{group_id}:{plan_code}:{source}",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back)],
    ])


@router.callback_query(F.data == "panel:plans")


@router.callback_query(F.data == "panel:plans")
async def catalog(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        panel_header(
            "Тарифы и подписка",
            "Сначала выберите тариф и посмотрите реальные отличия. Затем выберите группу, для которой хотите его оформить.",
        ),
        reply_markup=_catalog_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plans_catalog:(free|standard|pro)$"))
async def catalog_detail(callback: CallbackQuery) -> None:
    plan_code = callback.data.rsplit(":", 1)[1]
    await callback.message.edit_text(_plan_description(plan_code), reply_markup=_detail_keyboard(plan_code))
    await callback.answer()


@router.callback_query(F.data == "plans_catalog:compare")
async def compare(callback: CallbackQuery) -> None:
    text = panel_header(
        "Сравнение тарифов",
        "FREE\n"
        "Базовая модерация, защита и статистика. 10 запрещённых слов, 5 причин наказаний.\n\n"
        "STANDARD · 250 Stars / 30 дней\n"
        "Расширенная защита и аналитика, ежедневные отчёты, рекламные инструменты. 100 слов, 30 причин.\n\n"
        "PRO · 500 Stars / 30 дней\n"
        "Всё из STANDARD, максимальные лимиты, приоритетная поддержка и полный коммерческий набор. 1000 слов, 100 причин.\n\n"
        "Во всех тарифах одинаково\n"
        "До 5 обязательных подписок / каналов. Количество администраторов Telegram и ролей Mimoru тарифом не ограничивается.",
    )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⭐ STANDARD", callback_data="plans_catalog:standard"),
                InlineKeyboardButton(text="💎 PRO", callback_data="plans_catalog:pro"),
            ],
            [InlineKeyboardButton(text="◀️ К тарифам", callback_data="panel:plans")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plans_choose_group:(standard|pro)$"))
async def choose_group_by_reference(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    plan_code = callback.data.rsplit(":", 1)[1]
    groups = list((await session.scalars(
        select(Group).where(
            Group.owner_telegram_id == callback.from_user.id,
            Group.is_active.is_(True),
        ).order_by(Group.created_at.desc())
    )).all())
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups:
        label = await group_reference_label(bot, group)
        rows.append([InlineKeyboardButton(
            text=label[:58],
            callback_data=f"plans_apply:{plan_code}:{group.id}:catalog",
        )])
    rows.append([InlineKeyboardButton(
        text="🔎 Найти по ID / @username",
        callback_data=f"group_lookup:plan:{plan_code}",
    )])
    rows.append([InlineKeyboardButton(text="◀️ К описанию тарифа", callback_data=f"plans_catalog:{plan_code}")])
    await callback.message.edit_text(
        panel_header(
            "Выберите группу",
            f"Тариф: {plan_code.upper()}\n\nВыберите группу по Telegram ID / @username или воспользуйтесь поиском."
            if groups else "Подключённых групп пока нет. Сначала добавьте Mimoru администратором в группу.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plans_apply:(standard|pro):\d+:(catalog|group)$"))
async def plan_for_group(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    _, plan_code, raw_group_id, source = callback.data.split(":")
    group = await accessible_group(session, int(raw_group_id), callback.from_user.id)
    if group is None:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return
    identity = await group_reference_label(bot, group)
    await callback.message.edit_text(
        _plan_description(plan_code) + f"\n\nГруппа для подключения: {identity}",
        reply_markup=_purchase_keyboard(group.id, plan_code, source),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plan:\d+$"))
async def group_plan(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return
    identity = await group_reference_label(bot, group)
    state = subscription_state(group)
    state_text = {"free": "FREE", "trial": "TRIAL", "active": "активна", "expired": "истекла"}[state]
    days = remaining_days(group)
    left = f"\nОсталось дней: {days}" if days is not None else ""
    await callback.message.edit_text(
        panel_header(
            "Тариф группы",
            f"{identity}\n\nТекущий тариф: {effective_plan(group).upper()}\nСтатус: {state_text}{left}\n\nВыберите тариф, чтобы посмотреть подробное описание перед оплатой.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ STANDARD · 250 Stars", callback_data=f"plans_apply:standard:{group.id}:group")],
            [InlineKeyboardButton(text="💎 PRO · 500 Stars", callback_data=f"plans_apply:pro:{group.id}:group")],
            [InlineKeyboardButton(text="📜 История платежей", callback_data=f"plans_history:{group.id}")],
            [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group.id}")],
        ]),
    )
    await callback.answer()
