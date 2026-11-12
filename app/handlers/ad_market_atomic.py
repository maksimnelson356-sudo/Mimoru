from __future__ import annotations

from datetime import UTC, datetime

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.ad_market_models import (
    GlobalPostRequest,
    RequiredAdDealRequest,
    RequiredAdListing,
)
from app.db.models import Group
from app.handlers.ad_market_v3 import RequiredDealForm, RequiredListingForm
from app.handlers.required_direct import (
    ActivationError,
    activate_deal_subscription,
)
from app.services.crocopay import (
    CrocoPayError,
    create_invoice as croco_create_invoice,
    get_invoice as croco_get_invoice,
)
from app.services.global_post_payments import activate_global_post_payment
from app.services.xrocket import (
    XRocketError,
    create_invoice as xr_create_invoice,
    get_invoice as xr_get_invoice,
    is_paid_status as xr_is_paid_status,
)
from app.services.required_resources import (
    normalize_public_telegram_resource,
    validate_invite_link,
)
from app.services.ui import clean_ui_text, panel_header

router = Router(name=__name__)
settings = get_settings()
log = structlog.get_logger(__name__)


def _contact_url(user_id: int) -> str:
    return f"tg://user?id={user_id}"


def _unit_label(unit: str) -> str:
    return "за сутки" if unit == "day" else "за одного подписчика"


async def _member_count(bot: Bot, group: Group) -> int | None:
    try:
        return max(0, int(await bot.get_chat_member_count(group.telegram_chat_id)))
    except (TelegramBadRequest, TelegramForbiddenError):
        return None


async def _offer_global_payment_choice(bot: Bot, item: GlobalPostRequest) -> bool:
    """Send the buyer a payment method chooser."""
    amount = settings.ad_price_rub
    await bot.send_message(
        item.buyer_telegram_id,
        f"💳 Счёт за рекламный пост #{item.id}: {amount} ₽.\n"
        "Выберите способ оплаты:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💳 Оплатить картой (CrocoPay)",
                        callback_data=f"gpost:pay:{item.id}:crocopay",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🚀 Оплатить через XRocket",
                        callback_data=f"gpost:pay:{item.id}:xrocket",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="◀️ Мои рекламные посты", callback_data="gpost:mine"
                    )
                ],
            ]
        ),
    )
    return True


@router.callback_query(F.data.regexp(r"^gpost:(approve|reject):\d+$"))
async def atomic_global_review(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    if callback.from_user.id not in settings.service_owner_ids:
        await callback.answer(
            "Это действие доступно только создателю Mimoru.", show_alert=True
        )
        return
    _, decision, raw_id = callback.data.split(":")
    item = await session.scalar(
        select(GlobalPostRequest)
        .where(GlobalPostRequest.id == int(raw_id))
        .with_for_update()
    )
    if item is None or item.status != "pending_review":
        await callback.answer("Заявка уже обработана или недоступна.", show_alert=True)
        return

    item.reviewed_by_telegram_id = callback.from_user.id
    item.reviewed_at = datetime.now(UTC)
    item.status = "approved" if decision == "approve" else "rejected"
    await session.commit()

    if decision == "approve":
        status = "✅ Рекламный пост одобрен. Покупателю доступен счёт."
        try:
            await bot.send_message(
                item.buyer_telegram_id,
                f"✅ Рекламный пост #{item.id} одобрен создателем Mimoru. Оплатите счёт ниже — после успешной оплаты публикация начнётся автоматически во всех активных группах Mimoru.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="📢 Мои рекламные посты",
                                callback_data="gpost:mine",
                            )
                        ]
                    ]
                ),
            )
            await _offer_global_payment_choice(bot, item)
        except (TelegramBadRequest, TelegramForbiddenError):
            await callback.answer(
                "Заявка одобрена, но покупателю не удалось отправить сообщение. Он сможет получить счёт из «Моих рекламных постов».",
                show_alert=True,
            )
    else:
        status = "❌ Рекламный пост отклонён."
        try:
            await bot.send_message(
                item.buyer_telegram_id,
                f"❌ Рекламный пост #{item.id} не прошёл проверку создателем Mimoru.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="📢 Мои рекламные посты",
                                callback_data="gpost:mine",
                            )
                        ]
                    ]
                ),
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning(
                "ad_rejection_notify_failed",
                buyer_id=item.buyer_telegram_id,
                item_id=item.id,
                error=str(exc),
            )

    await callback.message.edit_text(panel_header("Проверка завершена", status))
    await callback.answer("Решение сохранено")


@router.callback_query(F.data.regexp(r"^gpost:pay:\d+:(crocopay|xrocket)$"))
async def atomic_global_choose_provider(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    _, _, raw_id, provider = callback.data.split(":")
    item = await session.get(GlobalPostRequest, int(raw_id))
    if item is None or item.buyer_telegram_id != callback.from_user.id:
        await callback.answer("Заявка недоступна.", show_alert=True)
        return
    if item.status != "approved":
        await callback.answer("Счёт для этой заявки недоступен.", show_alert=True)
        return
    base = settings.public_base_url.rstrip("/")
    if not base:
        await callback.answer("Оплата временно недоступна.", show_alert=True)
        return
    amount = settings.ad_price_rub
    invoice_id = item.payment_charge_id or ""
    pay_button = None
    try:
        if provider == "crocopay":
            callback_url = f"{base}{settings.croco_webhook_path}?kind=g&id={item.id}"
            if not invoice_id:
                invoice = await croco_create_invoice(amount=amount, callback_url=callback_url)
                invoice_id = str(invoice.get("id") or "")
                item.payment_charge_id = invoice_id
                display = str(invoice.get("card") or "")
            else:
                info = await croco_get_invoice(invoice_id)
                display = str(info.get("card") or "")
            is_link = display.startswith(("http://", "https://"))
            if is_link:
                pay_button = InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=display)
            text = f"💳 Счёт за рекламный пост #{item.id}: {amount} ₽.\n"
            if display and not is_link:
                text += f"Переведите на карту: <code>{display}</code>\n"
            text += "После перевода нажмите «Я оплатил(а)»."
        else:
            ref = f"g{item.id}"
            callback_url = f"{base}{settings.xrocket_webhook_path}"
            if not invoice_id:
                invoice = await xr_create_invoice(
                    amount=amount,
                    client_invoice_id=ref,
                    description=f"Рекламный пост Mimoru #{item.id}",
                    callback_url=callback_url,
                    callback_payload={"clientInvoiceId": ref},
                )
                invoice_id = str(invoice.get("id") or "")
                item.payment_charge_id = invoice_id
                pay_link = str((invoice.get("links") or {}).get("telegramBotLink") or "")
            else:
                info = await xr_get_invoice(ref)
                pay_link = str((info.get("links") or {}).get("telegramBotLink") or "")
            if pay_link:
                pay_button = InlineKeyboardButton(text="🚀 Оплатить через XRocket", url=pay_link)
            text = f"🚀 Счёт за рекламный пост #{item.id}: {amount} ₽.\nОткройте счёт в Telegram."
    except (CrocoPayError, XRocketError) as exc:
        log.warning("ad_provider_invoice_failed", provider=provider, request_id=item.id, error=str(exc))
        await callback.answer("Не удалось создать счёт, попробуйте ещё раз.", show_alert=True)
        return
    await session.commit()
    keyboard = []
    if pay_button is not None:
        keyboard.append([pay_button])
    keyboard.append(
        [
            InlineKeyboardButton(
                text="✅ Я оплатил(а)", callback_data=f"gpost:check:{item.id}:{provider}"
            )
        ]
    )
    keyboard.append(
        [InlineKeyboardButton(text="◀️ Мои рекламные посты", callback_data="gpost:mine")]
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^gpost:check:\d+:(crocopay|xrocket)$"))
async def atomic_global_check_payment(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    _, _, raw_id, provider = callback.data.split(":")
    item = await session.get(GlobalPostRequest, int(raw_id))
    if item is None or item.buyer_telegram_id != callback.from_user.id:
        await callback.answer("Заявка недоступна.", show_alert=True)
        return
    if item.status in {"paid", "completed"}:
        await callback.answer("Оплата подтверждена, публикация идёт.", show_alert=True)
        return
    invoice_id = item.payment_charge_id or ""
    try:
        if provider == "crocopay":
            if not invoice_id:
                await callback.answer("Счёт не найден.", show_alert=True)
                return
            info = await croco_get_invoice(invoice_id)
            paid = str(info.get("status") or "").lower() == "success"
        else:
            info = await xr_get_invoice(f"g{item.id}")
            paid = xr_is_paid_status(str(info.get("status") or "").lower())
    except (CrocoPayError, XRocketError):
        await callback.answer("Не удалось проверить оплату, попробуйте ещё раз.", show_alert=True)
        return
    if not paid:
        await callback.answer("Оплата ещё не завершена.", show_alert=True)
        return
    outcome = await activate_global_post_payment(
        session, request_id=item.id, charge_id=invoice_id
    )
    if outcome in {"done", "duplicate"}:
        await callback.message.edit_text(
            panel_header(
                "Оплата получена",
                f"✅ Рекламный пост #{item.id} оплачен. Публикация во всех активных группах начнётся автоматически.",
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📢 Мои рекламные посты", callback_data="gpost:mine"
                        )
                    ]
                ]
            ),
        )
        await callback.answer()
    else:
        await callback.answer("Оплата пока не подтверждена.", show_alert=True)


@router.message(RequiredListingForm.price, F.chat.type == "private")
async def atomic_required_listing_price(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    group_id = int(data.get("group_id", 0))
    price = clean_ui_text((message.text or "").strip())[:64]
    if not price:
        await message.answer("Цена не может быть пустой.")
        return
    if not data.get("min_days") or not data.get("price_unit"):
        await state.clear()
        await message.answer("Форма устарела. Начните заново.")
        return

    group_snapshot = await session.get(Group, group_id)
    if group_snapshot is None:
        await state.clear()
        await message.answer("Группа недоступна.")
        return
    current_count = await _member_count(bot, group_snapshot)

    group = await session.scalar(
        select(Group)
        .where(
            Group.id == group_id,
            Group.owner_telegram_id == message.from_user.id,
            Group.is_active.is_(True),
        )
        .with_for_update()
    )
    if group is None:
        await state.clear()
        await message.answer("Группа недоступна.")
        return

    listing = await session.scalar(
        select(RequiredAdListing)
        .where(RequiredAdListing.seller_group_id == group.id)
        .with_for_update()
    )
    count = (
        current_count
        if current_count is not None
        else (listing.member_count_snapshot if listing is not None else 0)
    )
    if listing is None:
        listing = RequiredAdListing(
            seller_group_id=group.id,
            seller_owner_telegram_id=message.from_user.id,
            member_count_snapshot=count,
            min_days=int(data["min_days"]),
            price_unit=str(data["price_unit"]),
            price_text=price,
            active=True,
        )
        session.add(listing)
    else:
        listing.seller_owner_telegram_id = message.from_user.id
        listing.member_count_snapshot = count
        listing.min_days = int(data["min_days"])
        listing.price_unit = str(data["price_unit"])
        listing.price_text = price
        listing.active = True
    await session.commit()
    await state.clear()
    await message.answer(
        panel_header(
            "Объявление опубликовано",
            f"Группа: {clean_ui_text(group.title)}\nУчастников: {count:,}\nМинимальный срок: {listing.min_days} дн.\nЦена: {price} {_unit_label(listing.price_unit)}",
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="◀️ К объявлению",
                        callback_data=f"reqlist:group:{group.id}",
                    )
                ],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]
        ),
    )


@router.callback_query(F.data.regexp(r"^reqlist:toggle:\d+$"))
async def atomic_required_listing_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    listing_id = int(callback.data.split(":")[-1])
    group_id = await session.scalar(
        select(RequiredAdListing.seller_group_id).where(
            RequiredAdListing.id == listing_id
        )
    )
    if group_id is None:
        await callback.answer("Объявление недоступно.", show_alert=True)
        return

    group = await session.scalar(
        select(Group)
        .where(
            Group.id == group_id,
            Group.owner_telegram_id == callback.from_user.id,
            Group.is_active.is_(True),
        )
        .with_for_update()
    )
    if group is None:
        await callback.answer("Объявление недоступно.", show_alert=True)
        return

    listing = await session.scalar(
        select(RequiredAdListing)
        .where(
            RequiredAdListing.id == listing_id,
            RequiredAdListing.seller_group_id == group.id,
        )
        .with_for_update()
    )
    if listing is None or listing.seller_owner_telegram_id != callback.from_user.id:
        await callback.answer("Объявление недоступно.", show_alert=True)
        return

    listing.active = not listing.active
    await session.commit()
    status = "✅ показывается покупателям" if listing.active else "⏸ скрыто"
    await callback.message.edit_text(
        panel_header(
            "Объявление ОП",
            f"Группа: {clean_ui_text(group.title)}\n"
            f"Участников: {listing.member_count_snapshot:,}\n"
            f"Минимальный срок: {listing.min_days} дн.\n"
            f"Цена: {clean_ui_text(listing.price_text)} {_unit_label(listing.price_unit)}\n"
            f"Статус: {status}",
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ Изменить условия",
                        callback_data=f"reqlist:start:{group.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⏸ Скрыть" if listing.active else "▶️ Опубликовать",
                        callback_data=f"reqlist:toggle:{listing.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📥 Входящие заявки", callback_data="reqdeal:seller"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="◀️ К моим группам", callback_data="ads:sell:required"
                    )
                ],
            ]
        ),
    )
    await callback.answer("Статус объявления изменён")


@router.message(RequiredDealForm.target, F.chat.type == "private")
async def atomic_required_deal_target(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    listing = await session.scalar(
        select(RequiredAdListing)
        .where(RequiredAdListing.id == int(data.get("listing_id", 0)))
        .with_for_update()
    )
    target = normalize_public_telegram_resource(message.text or "")
    if target is None:
        target = validate_invite_link(message.text or "")
    if listing is None or not listing.active:
        await state.clear()
        await message.answer("Объявление больше недоступно.")
        return
    if listing.seller_owner_telegram_id == message.from_user.id:
        await state.clear()
        await message.answer("Нельзя отправить запрос по собственному объявлению.")
        return
    if target is None:
        await message.answer(
            "Отправьте публичный @username, ссылку t.me/username или invite-ссылку t.me/+..."
        )
        return

    duplicate = await session.scalar(
        select(RequiredAdDealRequest.id).where(
            RequiredAdDealRequest.listing_id == listing.id,
            RequiredAdDealRequest.buyer_telegram_id == message.from_user.id,
            RequiredAdDealRequest.status == "pending",
        )
    )
    if duplicate:
        await session.rollback()
        await state.clear()
        await message.answer("У вас уже есть ожидающий запрос по этому объявлению.")
        return

    deal = RequiredAdDealRequest(
        listing_id=listing.id,
        buyer_telegram_id=message.from_user.id,
        seller_telegram_id=listing.seller_owner_telegram_id,
        target_resource=target,
        status="pending",
    )
    session.add(deal)
    await session.commit()

    group = await session.get(Group, listing.seller_group_id)
    current_count = await _member_count(bot, group) if group is not None else None
    count = (
        current_count if current_count is not None else listing.member_count_snapshot
    )
    if (
        group is not None
        and current_count is not None
        and current_count != listing.member_count_snapshot
    ):
        listing.member_count_snapshot = current_count
        await session.commit()
    group_title = (
        clean_ui_text(group.title) if group is not None else "Группа недоступна"
    )
    seller_text = panel_header(
        "Новый запрос на ОП",
        f"Ваша группа: {group_title}\n"
        f"Участников: {count:,}\n"
        f"Условия объявления: от {listing.min_days} дн., {clean_ui_text(listing.price_text)} {_unit_label(listing.price_unit)}\n\n"
        f"Покупатель хочет подключить: {target}\n\n"
        "Вы можете сначала связаться с покупателем напрямую, а затем принять или отклонить запрос.",
    )
    try:
        await bot.send_message(
            listing.seller_owner_telegram_id,
            seller_text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💬 Связаться с покупателем",
                            url=_contact_url(message.from_user.id),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="✅ Принять", callback_data=f"reqdeal:accept:{deal.id}"
                        ),
                        InlineKeyboardButton(
                            text="❌ Отклонить",
                            callback_data=f"reqdeal:reject:{deal.id}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="📥 Входящие заявки", callback_data="reqdeal:seller"
                        )
                    ],
                ]
            ),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning(
            "ad_trade_notify_failed",
            seller_id=listing.seller_owner_telegram_id,
            deal_id=deal.id,
            error=str(exc),
        )

    await state.clear()
    await message.answer(
        panel_header(
            "Запрос отправлен",
            f"Запрос #{deal.id} отправлен владельцу группы «{group_title}». Результат придёт обоим участникам сделки.",
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Связаться с продавцом",
                        url=_contact_url(listing.seller_owner_telegram_id),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📨 Мои запросы", callback_data="reqdeal:buyer"
                    )
                ],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]
        ),
    )


@router.callback_query(F.data.regexp(r"^reqdeal:(accept|reject):\d+$"))
async def atomic_required_deal_decision(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    redis: Redis,
) -> None:
    _, decision, raw_id = callback.data.split(":")
    deal_id = int(raw_id)

    snapshot = await session.execute(
        select(
            RequiredAdDealRequest.listing_id,
            RequiredAdListing.seller_group_id,
        )
        .join(
            RequiredAdListing, RequiredAdListing.id == RequiredAdDealRequest.listing_id
        )
        .where(RequiredAdDealRequest.id == deal_id)
    )
    ids = snapshot.one_or_none()
    if ids is None:
        await callback.answer("Запрос недоступен.", show_alert=True)
        return
    listing_id, group_id = ids

    group = await session.scalar(
        select(Group)
        .where(
            Group.id == group_id,
            Group.owner_telegram_id == callback.from_user.id,
            Group.is_active.is_(True),
        )
        .with_for_update()
    )
    if group is None:
        await callback.answer("Запрос недоступен.", show_alert=True)
        return

    listing = await session.scalar(
        select(RequiredAdListing)
        .where(
            RequiredAdListing.id == listing_id,
            RequiredAdListing.seller_group_id == group.id,
        )
        .with_for_update()
    )
    if listing is None or listing.seller_owner_telegram_id != callback.from_user.id:
        await callback.answer("Запрос недоступен.", show_alert=True)
        return

    deal = await session.scalar(
        select(RequiredAdDealRequest)
        .where(
            RequiredAdDealRequest.id == deal_id,
            RequiredAdDealRequest.listing_id == listing.id,
        )
        .with_for_update()
    )
    if deal is None or deal.seller_telegram_id != callback.from_user.id:
        await callback.answer("Запрос недоступен.", show_alert=True)
        return
    if deal.status != "pending":
        await callback.answer("Запрос уже обработан.", show_alert=True)
        return

    group_title = clean_ui_text(group.title)
    accepted = decision == "accept"
    activation_ok = False
    activation_error = ""

    if accepted:
        try:
            await activate_deal_subscription(
                session,
                group_id=group.id,
                channel=deal.target_resource,
                min_days=listing.min_days,
                created_by=deal.seller_telegram_id,
                dedupe_key=f"ad:{deal.id}",
            )
            await session.commit()
            activation_ok = True
        except ActivationError as exc:
            activation_error = str(exc)

    deal.status = "accepted" if accepted else "rejected"
    deal.decided_at = datetime.now(UTC)
    await session.commit()

    result = "✅ Запрос принят" if accepted else "❌ Запрос отклонён"
    if accepted and activation_ok:
        seller_extra = (
            f"\n\n✅ Обязательная подписка на {deal.target_resource} "
            f"автоматически включена на {listing.min_days} дн."
        )
    elif accepted and activation_error:
        seller_extra = (
            f"\n\n⚠️ Автоматическая активация не удалась: {activation_error}\n"
            f"Включите ОП вручную командой в группе:\n"
            f"подключить {deal.target_resource} {listing.min_days} дней"
        )
    else:
        seller_extra = ""

    await callback.message.edit_text(
        panel_header(
            result,
            f"Группа: {group_title}\nРесурс покупателя: {clean_ui_text(deal.target_resource)}\n\n"
            f"Результат отправлен покупателю.{seller_extra}",
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Связаться с покупателем",
                        url=_contact_url(deal.buyer_telegram_id),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📥 Входящие заявки", callback_data="reqdeal:seller"
                    )
                ],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]
        ),
    )
    try:
        if accepted and activation_ok:
            buyer_notice = (
                f"Продавец группы «{group_title}» принял ваш запрос #{deal.id}.\n"
                f"Ресурс: {clean_ui_text(deal.target_resource)}\n\n"
                f"✅ Обязательная подписка уже активна. Новые участники группы "
                f"обязаны подписаться на {deal.target_resource} в течение {listing.min_days} дн."
            )
        elif accepted:
            buyer_notice = (
                f"Продавец группы «{group_title}» принял ваш запрос #{deal.id}.\n"
                f"Ресурс: {clean_ui_text(deal.target_resource)}\n\n"
                f"Продавец должен включить ОП вручную. Свяжитесь с ним напрямую."
            )
        else:
            buyer_notice = (
                f"Продавец группы «{group_title}» отклонил ваш запрос #{deal.id}.\n"
                f"Ресурс: {clean_ui_text(deal.target_resource)}"
            )
        await bot.send_message(
            deal.buyer_telegram_id,
            panel_header(result, buyer_notice),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💬 Связаться с продавцом",
                            url=_contact_url(deal.seller_telegram_id),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📨 Мои запросы", callback_data="reqdeal:buyer"
                        )
                    ],
                    [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
                ]
            ),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning(
            "ad_broadcast_failed",
            buyer_id=deal.buyer_telegram_id,
            deal_id=deal.id,
            error=str(exc),
        )
    await callback.answer("Решение сохранено")
