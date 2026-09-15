from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
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
    restrict_existing_unsubscribed_members,
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


async def _send_global_invoice(bot: Bot, item: GlobalPostRequest) -> None:
    await bot.send_invoice(
        chat_id=item.buyer_telegram_id,
        title=f"Рекламный пост Mimoru #{item.id}",
        description="Одобренный рекламный пост для публикации во всех активных группах Mimoru.",
        payload=f"globalpost:{item.id}",
        currency="XTR",
        prices=[
            LabeledPrice(label="Глобальный рекламный пост", amount=item.price_stars)
        ],
        provider_token="",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"⭐ Оплатить {item.price_stars} Stars", pay=True
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
            await _send_global_invoice(bot, item)
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
            )
            await session.commit()
            activation_ok = True
            asyncio.create_task(
                restrict_existing_unsubscribed_members(
                    bot,
                    redis,
                    group_id=group.id,
                    telegram_chat_id=group.telegram_chat_id,
                    channels=[deal.target_resource],
                )
            )
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
