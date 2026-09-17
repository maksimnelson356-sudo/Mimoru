from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, Payment
from app.services.access import accessible_group, is_service_owner, owned_group
from app.services.plans import (
    effective_plan,
    paid_plan,
    remaining_days,
    subscription_state,
)
from app.services.ui import clean_ui_text, panel_header


router = Router(name=__name__)


def _catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆓 FREE", callback_data="plans_catalog:free")],
        [InlineKeyboardButton(text="⭐ STANDARD · 250 Stars / 30 дней", callback_data="plans_catalog:standard")],
        [InlineKeyboardButton(text="💎 PRO · 500 Stars / 30 дней", callback_data="plans_catalog:pro")],
        [InlineKeyboardButton(text="📊 Сравнить тарифы", callback_data="plans_catalog:compare")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="panel:home")],
    ])


def _plan_description(plan_code: str) -> str:
    if plan_code == "free":
        return panel_header(
            "FREE",
            "Бесплатный тариф для базового управления группой.\n\n"
            "Что доступно:\n"
            "• базовая модерация участников\n"
            "• базовая защита группы\n"
            "• базовая статистика\n"
            "• до 10 запрещённых слов или фраз\n"
            "• до 5 обязательных подписок\n"
            "• до 5 собственных причин наказаний\n"
            "• количество администраторов и модераторов Mimoru не ограничивается тарифом\n\n"
            "Не входят расширенная аналитика, ежедневные отчёты и рекламный маркетплейс.",
        )
    if plan_code == "standard":
        return panel_header(
            "STANDARD · 250 Stars / 30 дней",
            "Для владельцев групп, которым нужны расширенная защита, аналитика и рекламные инструменты.\n\n"
            "Всё из FREE, а также:\n"
            "• расширенная защита и дополнительные сценарии антиспама\n"
            "• расширенная аналитика группы\n"
            "• ежедневные отчёты владельцу\n"
            "• доступ к рекламному маркетплейсу Mimoru\n"
            "• до 100 запрещённых слов или фраз\n"
            "• до 5 обязательных подписок\n"
            "• до 30 собственных причин наказаний\n"
            "• количество администраторов и модераторов Mimoru не ограничивается тарифом\n\n"
            "Подходит для большинства активно развивающихся групп.",
        )
    return panel_header(
        "PRO · 500 Stars / 30 дней",
        "Максимальный тариф для крупных групп и активного коммерческого использования Mimoru.\n\n"
        "Всё из STANDARD, а также:\n"
        "• максимальные лимиты контента и управления\n"
        "• до 1000 запрещённых слов или фраз\n"
        "• до 5 обязательных подписок\n"
        "• до 100 собственных причин наказаний\n"
        "• приоритетная поддержка\n"
        "• полный набор коммерческих возможностей Mimoru\n"
        "• количество администраторов и модераторов Mimoru не ограничивается тарифом\n\n"
        "Подходит крупным сообществам и владельцам, которые используют рекламу и расширенную автоматизацию.",
    )


def _detail_keyboard(plan_code: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if plan_code in {"standard", "pro"}:
        rows.append([InlineKeyboardButton(text="🏠 Выбрать группу для этого тарифа", callback_data=f"plans_choose_group:{plan_code}")])
    rows.extend([
        [InlineKeyboardButton(text="📊 Сравнить тарифы", callback_data="plans_catalog:compare")],
        [InlineKeyboardButton(text="◀️ К тарифам", callback_data="panel:plans")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _comparison_text() -> str:
    return panel_header(
        "Сравнение тарифов",
        "FREE\n"
        "Базовая модерация, защита и статистика. 10 запрещённых слов, до 5 обязательных подписок, 5 причин наказаний.\n\n"
        "STANDARD · 250 Stars / 30 дней\n"
        "Расширенная защита и аналитика, ежедневные отчёты, рекламный маркетплейс. 100 слов, до 5 обязательных подписок, 30 причин.\n\n"
        "PRO · 500 Stars / 30 дней\n"
        "Всё из STANDARD, максимальные лимиты, приоритетная поддержка и полный коммерческий набор. 1000 слов, до 5 обязательных подписок, 100 причин.\n\n"
        "Администраторы и модераторы\n"
        "Их количество не ограничивается тарифом: владелец может назначить столько администраторов Telegram и ролей Mimoru, сколько ему нужно.",
    )


def _group_choice_keyboard(groups: list[Group], plan_code: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🏠 {group.title[:42]}", callback_data=f"plans_apply:{plan_code}:{group.id}:catalog")]
        for group in groups
    ]
    rows.append([InlineKeyboardButton(text="◀️ К описанию тарифа", callback_data=f"plans_catalog:{plan_code}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _group_plan_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ STANDARD · 250 Stars", callback_data=f"plans_apply:standard:{group_id}:group")],
        [InlineKeyboardButton(text="💎 PRO · 500 Stars", callback_data=f"plans_apply:pro:{group_id}:group")],
        [InlineKeyboardButton(text="📜 История платежей", callback_data=f"plans_history:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group_id}")],
    ])


def _purchase_keyboard(group_id: int, plan_code: str, source: str) -> InlineKeyboardMarkup:
    plan = paid_plan(plan_code)
    back = f"plans_catalog:{plan_code}" if source == "catalog" else f"plan:{group_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Оформить за {plan['stars']} Stars", callback_data=f"plan_checkout:{group_id}:{plan_code}:{source}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back)],
    ])





@router.callback_query(F.data == "panel:plans")
async def catalog(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        panel_header(
            "Тарифы и подписка",
            "Сначала выберите тариф и посмотрите, что именно он даёт. После этого Mimoru предложит выбрать группу, для которой нужно оформить подписку.",
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
async def catalog_compare(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        _comparison_text(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ STANDARD", callback_data="plans_catalog:standard"), InlineKeyboardButton(text="💎 PRO", callback_data="plans_catalog:pro")],
            [InlineKeyboardButton(text="◀️ К тарифам", callback_data="panel:plans")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plans_choose_group:(standard|pro)$"))
async def choose_group(callback: CallbackQuery, session: AsyncSession) -> None:
    plan_code = callback.data.rsplit(":", 1)[1]
    groups = list((
        await session.scalars(
            select(Group)
            .where(Group.is_active.is_(True))
            .order_by(Group.title)
        )
    ).all())
    text = panel_header(
        "Выберите группу",
        f"Тариф: {plan_code.upper()}\n\nВыберите группу, для которой хотите оформить подписку."
        if groups else "У вас пока нет активных групп. Сначала добавьте Mimoru администратором в группу.",
    )
    await callback.message.edit_text(text, reply_markup=_group_choice_keyboard(groups, plan_code))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plan:\d+$"))
async def group_plan(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return
    state = subscription_state(group)
    state_text = {"free": "FREE", "trial": "TRIAL", "active": "активна", "expired": "истекла"}[state]
    days = remaining_days(group)
    left = f"\nОсталось дней: {days}" if days is not None else ""
    await callback.message.edit_text(
        panel_header(
            "Тариф группы",
            f"{group.title}\n\nТекущий тариф: {effective_plan(group).upper()}\nСтатус: {state_text}{left}\n\nВыберите тариф, чтобы посмотреть подробное описание перед оплатой.",
        ),
        reply_markup=_group_plan_keyboard(group.id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plans_apply:(standard|pro):\d+:(catalog|group)$"))
async def plan_for_group(callback: CallbackQuery, session: AsyncSession) -> None:
    _, plan_code, raw_group_id, source = callback.data.split(":")
    group = await accessible_group(session, int(raw_group_id), callback.from_user.id)
    if group is None:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return
    text = _plan_description(plan_code) + f"\n\nГруппа для подключения: {clean_ui_text(group.title)}"
    await callback.message.edit_text(text, reply_markup=_purchase_keyboard(group.id, plan_code, source))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^plan_checkout:\d+:(standard|pro):(catalog|group)$"))
async def checkout(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group_id, plan_code, source = callback.data.split(":")
    group = await owned_group(
        session, int(raw_group_id), callback.from_user.id, for_update=True
    )
    if group is None:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return
    plan = paid_plan(plan_code)
    payment = Payment(
        user_telegram_id=callback.from_user.id,
        group_id=group.id,
        amount=plan["stars"],
        plan_code=plan_code,
        duration_days=plan["days"],
    )
    session.add(payment)
    await session.flush()
    payload = f"payment:{payment.id}:{group.id}:{plan_code}"
    await session.commit()
    invoice_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Оплатить {plan['stars']} Stars", pay=True)],
        [InlineKeyboardButton(text="◀️ Назад к описанию тарифа", callback_data=f"plan_invoice_back:{group.id}:{plan_code}:{source}")],
    ])
    await callback.message.answer_invoice(
        title=f"{plan['title']} на 30 дней",
        description=f"Подписка Mimoru для группы «{clean_ui_text(group.title)}»",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=f"{plan['title']} · 30 дней", amount=plan["stars"])],
        provider_token="",
        reply_markup=invoice_keyboard,
    )
    await callback.answer("Счёт создан")


@router.callback_query(F.data.regexp(r"^plan_invoice_back:\d+:(standard|pro):(catalog|group)$"))
async def invoice_back(callback: CallbackQuery) -> None:
    # Telegram invoices are separate service messages and cannot reliably be
    # converted back into normal text with editMessageText. The description
    # screen is still directly above the invoice, so remove the invoice itself.
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        await callback.answer("Не удалось закрыть счёт. Описание тарифа находится сообщением выше.", show_alert=True)
        return
    await callback.answer("Вернулись к описанию тарифа")


@router.callback_query(F.data.regexp(r"^plans_history:\d+$"))
async def history(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await accessible_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Группа не найдена или нет доступа.", show_alert=True)
        return
    payments = list((await session.scalars(
        select(Payment).where(Payment.group_id == group.id).order_by(Payment.created_at.desc()).limit(15)
    )).all())
    lines = []
    for payment in payments:
        mark = {"paid": "✅", "pending": "⏳", "failed": "❌"}.get(payment.status, "•")
        when = payment.paid_at or payment.created_at
        date = when.strftime("%d.%m.%Y") if when else "—"
        lines.append(f"{mark} {date} · {payment.plan_code.upper()} · {payment.amount} Stars")
    await callback.message.edit_text(
        panel_header("История платежей", group.title) + "\n\n" + ("\n".join(lines) if lines else "Платежей пока нет."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад к тарифу группы", callback_data=f"plan:{group.id}")]
        ]),
    )
    await callback.answer()
