from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Group, Payment
from app.services.access import accessible_group, owned_group
from app.services.crocopay import (
    CrocoPayError,
    create_invoice as croco_create_invoice,
    get_invoice as croco_get_invoice,
)
from app.services.plans import (
    effective_plan,
    paid_plan,
    remaining_days,
    subscription_state,
)
from app.services.subscription_payments import activate_subscription_payment
from app.services.ui import clean_ui_text, panel_header
from app.services.xrocket import (
    XRocketError,
    create_invoice as xr_create_invoice,
    get_invoice as xr_get_invoice,
    is_paid_status as xr_is_paid_status,
)


router = Router(name=__name__)


def _catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆓 FREE", callback_data="plans_catalog:free")],
        [InlineKeyboardButton(text="⭐ STANDARD · 199 ₽ / 30 дней", callback_data="plans_catalog:standard")],
        [InlineKeyboardButton(text="💎 PRO · 499 ₽ / 30 дней", callback_data="plans_catalog:pro")],
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
            "STANDARD · 199 ₽ / 30 дней",
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
        "PRO · 499 ₽ / 30 дней",
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
        "STANDARD · 199 ₽ / 30 дней\n"
        "Расширенная защита и аналитика, ежедневные отчёты, рекламный маркетплейс. 100 слов, до 5 обязательных подписок, 30 причин.\n\n"
        "PRO · 499 ₽ / 30 дней\n"
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
        [InlineKeyboardButton(text="⭐ STANDARD · 199 ₽", callback_data=f"plans_apply:standard:{group_id}:group")],
        [InlineKeyboardButton(text="💎 PRO · 499 ₽", callback_data=f"plans_apply:pro:{group_id}:group")],
        [InlineKeyboardButton(text="📜 История платежей", callback_data=f"plans_history:{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group_id}")],
    ])


def _purchase_keyboard(group_id: int, plan_code: str, source: str) -> InlineKeyboardMarkup:
    plan = paid_plan(plan_code)
    back = f"plans_catalog:{plan_code}" if source == "catalog" else f"plan:{group_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить картой · {plan['ruble']} ₽", callback_data=f"plan_checkout:{group_id}:{plan_code}:{source}")],
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
    settings = get_settings()
    currency = settings.croco_currency or settings.xrocket_currency
    payment = Payment(
        user_telegram_id=callback.from_user.id,
        group_id=group.id,
        currency=currency,
        amount=plan["ruble"],
        plan_code=plan_code,
        duration_days=plan["days"],
    )
    session.add(payment)
    await session.flush()
    await session.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить картой (CrocoPay)", callback_data=f"plan_choose_provider:{payment.id}:crocopay")],
        [InlineKeyboardButton(text="🚀 Оплатить через XRocket", callback_data=f"plan_choose_provider:{payment.id}:xrocket")],
        [InlineKeyboardButton(text="◀️ Отменить", callback_data=f"plan_invoice_back:{group.id}:{plan_code}:{source}")],
    ])
    await callback.message.edit_text(
        panel_header(
            f"Оплата · {plan['title']} на 30 дней",
            f"Группа: «{clean_ui_text(group.title)}»\n"
            f"Сумма: {plan['ruble']} ₽\n\n"
            "Выберите способ оплаты.",
        ),
        reply_markup=kb,
    )
    await callback.answer("Выберите способ оплаты")


@router.callback_query(F.data.regexp(r"^plan_choose_provider:\d+:(crocopay|xrocket)$"))
async def choose_provider(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_id, provider = callback.data.split(":")
    payment_id = int(raw_id)
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.user_telegram_id != callback.from_user.id:
        await callback.answer("Платёж не найден.", show_alert=True)
        return
    if payment.provider not in ("", "telegram_stars"):
        await callback.answer("Платёж уже обработан.", show_alert=True)
        return
    plan = paid_plan(payment.plan_code)
    settings = get_settings()
    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        await callback.answer("Вебхук-база не настроена.", show_alert=True)
        return

    if provider == "crocopay":
        callback_url = f"{base}{settings.croco_webhook_path}?kind=s&id={payment_id}"
        try:
            invoice = await croco_create_invoice(amount=payment.amount, callback_url=callback_url)
        except CrocoPayError as error:
            structlog.get_logger().warning("crocopay_checkout_create_failed", error=str(error))
            await callback.answer("Не удалось создать счёт в CrocoPay.", show_alert=True)
            return
        payment.provider = "crocopay"
        if invoice.get("id"):
            payment.provider_payment_id = str(invoice["id"])
        await session.commit()
        card = str(invoice.get("card") or "")
        buttons = []
        if card.startswith(("http://", "https://")):
            buttons.append([InlineKeyboardButton(text=f"💳 Оплатить {payment.amount} ₽", url=card)])
        buttons.append([InlineKeyboardButton(text="✅ Я оплатил(а)", callback_data=f"plan_check_payment:{payment.id}")])
        buttons.append([InlineKeyboardButton(text="◀️ Отменить", callback_data=f"plan_invoice_back:{payment.group_id}:{payment.plan_code}:group")])
        if card and not card.startswith(("http://", "https://")):
            detail = f"Карта: <code>{card}</code>"
        elif card.startswith(("http://", "https://")):
            detail = "Оплатите по ссылке выше."
        else:
            detail = "Запросите реквизиты заново."
        await callback.message.edit_text(
            panel_header(
                "Счёт · CrocoPay",
                f"Сумма: {payment.amount} ₽\n{detail}\n\nПосле оплаты нажмите «Я оплатил(а)».",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    else:  # xrocket
        callback_url = f"{base}{settings.xrocket_webhook_path}"
        ref = f"s{payment_id}"
        try:
            invoice = await xr_create_invoice(
                amount=payment.amount,
                client_invoice_id=ref,
                description=f"{plan['title']} на 30 дней · Mimoru",
                callback_url=callback_url,
                callback_payload={"clientInvoiceId": ref},
            )
        except XRocketError as error:
            structlog.get_logger().warning("xrocket_checkout_create_failed", error=str(error))
            await callback.answer("Не удалось создать счёт в XRocket.", show_alert=True)
            return
        payment.provider = "xrocket"
        if invoice.get("id"):
            payment.provider_payment_id = str(invoice["id"])
        await session.commit()
        pay_link = ((invoice.get("links") or {}).get("telegramBotLink")) or ""
        if not pay_link:
            await callback.answer("XRocket не вернул ссылку на оплату.", show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🚀 Оплатить {payment.amount} ₽", url=pay_link)],
            [InlineKeyboardButton(text="✅ Я оплатил(а)", callback_data=f"plan_check_payment:{payment.id}")],
            [InlineKeyboardButton(text="◀️ Отменить", callback_data=f"plan_invoice_back:{payment.group_id}:{payment.plan_code}:group")],
        ])
        await callback.message.edit_text(
            panel_header(
                "Счёт · XRocket",
                f"Сумма: {payment.amount} ₽\nОплатите внутри Telegram, затем нажмите «Я оплатил(а)».",
            ),
            reply_markup=kb,
        )
    await callback.answer("Счёт создан")


@router.callback_query(F.data.regexp(r"^plan_invoice_back:\d+:(standard|pro):(catalog|group)$"))
async def invoice_back(callback: CallbackQuery) -> None:
    _, raw_group_id, plan_code, source = callback.data.split(":")
    if source == "group":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К тарифу группы", callback_data=f"plan:{raw_group_id}")]
        ])
    else:
        keyboard = _detail_keyboard(plan_code)
    await callback.message.edit_text(
        _plan_description(plan_code),
        reply_markup=keyboard,
    )
    await callback.answer("Счёт отменён")


@router.callback_query(F.data.regexp(r"^plan_check_payment:\d+$"))
async def check_payment(callback: CallbackQuery, session: AsyncSession) -> None:
    payment_id = int(callback.data.split(":")[-1])
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.user_telegram_id != callback.from_user.id:
        await callback.answer("Платёж не найден.", show_alert=True)
        return

    if payment.status == "paid":
        group = await session.get(Group, payment.group_id)
        title = clean_ui_text(group.title) if group else payment.group_id
        paid_on = f"{payment.paid_at:%d.%m.%Y}" if payment.paid_at else "—"
        await callback.message.edit_text(
            panel_header(
                "Тариф активирован",
                f"{payment.plan_code.upper()} для «{title}» активен до {paid_on}.",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К тарифу группы", callback_data=f"plan:{payment.group_id}")]
            ]),
        )
        await callback.answer()
        return

    provider = payment.provider
    if provider == "crocopay":
        invoice_id = payment.provider_payment_id
        if not invoice_id:
            await callback.answer("Счёт не найден.", show_alert=True)
            return
        try:
            info = await croco_get_invoice(invoice_id)
        except CrocoPayError as error:
            await callback.answer(f"⚠️ Не удалось проверить оплату: {error}", show_alert=True)
            return
        if str(info.get("status") or "").lower() != "success":
            await callback.answer("⏳ Оплата ещё не завершена.", show_alert=True)
            return
        outcome = await activate_subscription_payment(
            session, payment_id=payment.id, charge_id=invoice_id, provider="crocopay"
        )
    elif provider == "xrocket":
        ref = f"s{payment.id}"
        try:
            info = await xr_get_invoice(ref)
        except XRocketError as error:
            await callback.answer(f"⚠️ Не удалось проверить оплату: {error}", show_alert=True)
            return
        if not xr_is_paid_status(str(info.get("status") or "").lower()):
            await callback.answer("⏳ Оплата ещё не завершена.", show_alert=True)
            return
        charge_id = str(info.get("id") or payment.provider_payment_id or ref)
        outcome = await activate_subscription_payment(
            session, payment_id=payment.id, charge_id=charge_id, provider="xrocket"
        )
    else:
        await callback.answer("Способ оплаты неизвестен.", show_alert=True)
        return

    if outcome == "done":
        await callback.message.edit_text(
            panel_header(
                "✅ Оплата получена",
                f"Тариф {payment.plan_code.upper()} активирован.",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К тарифу группы", callback_data=f"plan:{payment.group_id}")]
            ]),
        )
    elif outcome == "duplicate":
        await callback.answer("Этот платёж уже обработан.", show_alert=True)
    else:
        await callback.answer(
            "Платёж обработан в статусе «ожидает проверки». При необходимости обратитесь в поддержку.",
            show_alert=True,
        )
    await callback.answer()


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
        amount_text = f"{payment.amount} Stars" if payment.currency == "XTR" else f"{payment.amount} ₽"
        lines.append(f"{mark} {date} · {payment.plan_code.upper()} · {amount_text}")
    await callback.message.edit_text(
        panel_header("История платежей", group.title) + "\n\n" + ("\n".join(lines) if lines else "Платежей пока нет."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад к тарифу группы", callback_data=f"plan:{group.id}")]
        ]),
    )
    await callback.answer()
