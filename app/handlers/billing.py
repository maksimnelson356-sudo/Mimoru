from datetime import datetime, timedelta, timezone

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, PreCheckoutQuery
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ad_market_models import GlobalPostRequest
from app.db.models import Group, GroupSubscriptionEvent, Payment
from app.services.global_post_refunds import record_and_attempt_duplicate_refund
from app.services.subscription_duplicate_refunds import record_and_attempt_subscription_duplicate_refund
from app.services.ui import clean_ui_text


router = Router(name=__name__)


async def _current_owned_group(session: AsyncSession, group_id: int, owner_id: int) -> Group | None:
    return await session.scalar(
        select(Group).where(
            Group.id == group_id,
            Group.owner_telegram_id == owner_id,
            Group.is_active.is_(True),
        )
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, session: AsyncSession) -> None:
    parts = query.invoice_payload.split(":")

    if len(parts) == 2 and parts[0] == "globalpost":
        try:
            request_id = int(parts[1])
        except ValueError:
            await query.answer(ok=False, error_message="Некорректная заявка рекламного поста.")
            return
        item = await session.get(GlobalPostRequest, request_id)
        if (
            item is None
            or item.status != "approved"
            or item.buyer_telegram_id != query.from_user.id
            or query.currency != "XTR"
            or query.total_amount != item.price_stars
        ):
            await query.answer(
                ok=False,
                error_message="Заявка не одобрена, уже оплачена или её параметры изменились.",
            )
            return
        await query.answer(ok=True)
        return

    if len(parts) == 4 and parts[0] == "payment":
        try:
            payment_id = int(parts[1])
            group_id = int(parts[2])
        except ValueError:
            await query.answer(ok=False, error_message="Некорректный платёж.")
            return
        payment = await session.get(Payment, payment_id)
        if (
            payment is None
            or payment.status != "pending"
            or payment.user_telegram_id != query.from_user.id
            or payment.group_id != group_id
            or payment.plan_code != parts[3]
            or query.currency != payment.currency
            or query.total_amount != payment.amount
        ):
            await query.answer(
                ok=False,
                error_message="Платёж уже обработан или его параметры изменились.",
            )
            return
        group = await _current_owned_group(session, group_id, query.from_user.id)
        if group is None:
            await query.answer(
                ok=False,
                error_message="Группа больше не активна или уже не принадлежит вам. Создайте новый счёт из текущей карточки группы.",
            )
            return
        await query.answer(ok=True)
        return

    await query.answer(ok=False, error_message="Неизвестный тип платежа.")


async def _locked_global_post(session: AsyncSession, request_id: int) -> GlobalPostRequest | None:
    return await session.scalar(
        select(GlobalPostRequest)
        .where(GlobalPostRequest.id == request_id)
        .with_for_update()
    )


async def _locked_payment(session: AsyncSession, payment_id: int) -> Payment | None:
    return await session.scalar(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
    )


async def _locked_group(session: AsyncSession, group_id: int) -> Group | None:
    return await session.scalar(
        select(Group)
        .where(Group.id == group_id)
        .with_for_update()
    )


async def _charge_already_recorded(session: AsyncSession, *, charge_id: str, kind: str) -> bool:
    if kind == "globalpost":
        existing = await session.scalar(
            select(GlobalPostRequest.id).where(GlobalPostRequest.payment_charge_id == charge_id)
        )
    else:
        existing = await session.scalar(
            select(Payment.id).where(Payment.provider_payment_id == charge_id)
        )
    return existing is not None


async def _commit_payment_once(
    session: AsyncSession,
    *,
    charge_id: str,
    kind: str,
    record_id: int,
) -> bool:
    """Commit a claimed payment and suppress only a confirmed duplicate charge race."""
    try:
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        if not await _charge_already_recorded(session, charge_id=charge_id, kind=kind):
            raise
        structlog.get_logger().warning(
            "duplicate_payment_charge_ignored",
            kind=kind,
            record_id=record_id,
            charge_id=charge_id,
        )
        return False


def _already_refunded(error: TelegramBadRequest) -> bool:
    text = str(error).casefold()
    return "refund" in text and "already" in text


async def _finish_subscription_refund(
    message: Message,
    session: AsyncSession,
    payment: Payment,
    *,
    charge_id: str,
) -> bool:
    try:
        await message.bot.refund_star_payment(
            user_id=payment.user_telegram_id,
            telegram_payment_charge_id=charge_id,
        )
    except TelegramBadRequest as error:
        if not _already_refunded(error):
            structlog.get_logger().warning(
                "subscription_refund_failed",
                payment_id=payment.id,
                user_id=payment.user_telegram_id,
                charge_id=charge_id,
                error=str(error),
            )
            return False
    payment.status = "refunded"
    await session.commit()
    return True


async def _refund_stale_subscription_payment(
    message: Message,
    session: AsyncSession,
    payment: Payment,
    *,
    charge_id: str,
    now: datetime,
) -> None:
    payment.status = "refund_pending"
    payment.provider_payment_id = charge_id
    payment.paid_at = now
    if not await _commit_payment_once(
        session,
        charge_id=charge_id,
        kind="subscription",
        record_id=payment.id,
    ):
        return
    refunded = await _finish_subscription_refund(
        message,
        session,
        payment,
        charge_id=charge_id,
    )
    if refunded:
        await message.answer(
            "↩️ Оплата возвращена: группа больше не активна или уже не принадлежит вам. "
            "Создайте новый счёт из актуальной карточки группы."
        )
    else:
        await message.answer(
            "⚠️ Оплата получена, но группа больше не принадлежит вам. Автоматический возврат не завершился; "
            "обратитесь в поддержку и укажите номер платежа."
        )


@router.message(F.successful_payment)
async def successful_payment(message: Message, session: AsyncSession) -> None:
    successful = message.successful_payment
    parts = successful.invoice_payload.split(":")

    if len(parts) == 2 and parts[0] == "globalpost":
        try:
            request_id = int(parts[1])
        except ValueError:
            return
        item = await _locked_global_post(session, request_id)
        if item is None:
            return
        charge_id = successful.telegram_payment_charge_id
        if (
            item.buyer_telegram_id != message.from_user.id
            or successful.currency != "XTR"
            or successful.total_amount != item.price_stars
        ):
            return
        if item.status in {"paid", "completed"}:
            if item.payment_charge_id == charge_id:
                return
            refunded = await record_and_attempt_duplicate_refund(
                message.bot,
                session,
                request_id=item.id,
                buyer_telegram_id=message.from_user.id,
                charge_id=charge_id,
            )
            if refunded:
                await message.answer(
                    f"↩️ Повторная оплата рекламного поста #{item.id} возвращена. "
                    "Исходная оплата и публикация остаются без изменений."
                )
            else:
                await message.answer(
                    f"⚠️ Обнаружена повторная оплата рекламного поста #{item.id}. Возврат сохранён и будет повторён автоматически."
                )
            return
        if item.status != "approved":
            return
        item.status = "paid"
        item.payment_charge_id = charge_id
        item.paid_at = datetime.now(timezone.utc)
        if not await _commit_payment_once(
            session,
            charge_id=charge_id,
            kind="globalpost",
            record_id=item.id,
        ):
            return
        await message.answer(
            f"✅ Рекламный пост #{item.id} оплачен. Mimoru начинает автоматическую публикацию во всех активных группах.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Мои рекламные посты", callback_data="gpost:mine")],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]),
        )
        return

    if len(parts) == 4 and parts[0] == "payment":
        try:
            payment_id = int(parts[1])
            group_id = int(parts[2])
        except ValueError:
            return
        # Lock order: Group first, then Payment (prevents deadlocks)
        group = await _locked_group(session, group_id)
        if group is None:
            return
        payment = await _locked_payment(session, payment_id)
        if payment is None:
            return
        charge_id = successful.telegram_payment_charge_id
        if payment.status in {"paid", "refunded"}:
            if not payment.provider_payment_id or payment.provider_payment_id == charge_id:
                return
            if (
                payment.user_telegram_id != message.from_user.id
                or payment.group_id != group_id
                or payment.plan_code != parts[3]
                or successful.currency != payment.currency
                or successful.total_amount != payment.amount
            ):
                return
            refunded = await record_and_attempt_subscription_duplicate_refund(
                message.bot,
                session,
                payment_id=payment.id,
                buyer_telegram_id=payment.user_telegram_id,
                charge_id=charge_id,
            )
            if refunded:
                await message.answer(
                    "↩️ Повторная оплата тарифа возвращена. Исходная оплата и срок подписки остаются без изменений."
                )
            else:
                await message.answer(
                    "⚠️ Обнаружена повторная оплата тарифа. Возврат сохранён и будет повторён автоматически."
                )
            return
        if payment.status == "refund_pending":
            if payment.provider_payment_id == charge_id:
                refunded = await _finish_subscription_refund(
                    message,
                    session,
                    payment,
                    charge_id=charge_id,
                )
                if refunded:
                    await message.answer("↩️ Возврат Stars по этому счёту завершён.")
                return
            if (
                payment.user_telegram_id != message.from_user.id
                or payment.group_id != group_id
                or payment.plan_code != parts[3]
                or successful.currency != payment.currency
                or successful.total_amount != payment.amount
            ):
                return
            refunded = await record_and_attempt_subscription_duplicate_refund(
                message.bot,
                session,
                payment_id=payment.id,
                buyer_telegram_id=payment.user_telegram_id,
                charge_id=charge_id,
            )
            if refunded:
                await message.answer(
                    "↩️ Дополнительная оплата тарифа возвращена. Исходный возврат по счёту продолжается отдельно."
                )
            else:
                await message.answer(
                    "⚠️ Обнаружена дополнительная оплата тарифа. Возврат сохранён и будет повторён автоматически."
                )
            return
        if (
            payment.status != "pending"
            or payment.user_telegram_id != message.from_user.id
            or payment.group_id != group_id
            or payment.plan_code != parts[3]
            or successful.currency != payment.currency
            or successful.total_amount != payment.amount
        ):
            return
        # Group already locked at the start (lock order: Group -> Payment)
        if group.owner_telegram_id != message.from_user.id or not group.is_active:
            await _refund_stale_subscription_payment(
                message,
                session,
                payment,
                charge_id=charge_id,
                now=now,
            )
            return
        start = group.plan_expires_at if group.plan_expires_at and group.plan_expires_at > now else now
        group.plan_code = payment.plan_code
        group.plan_expires_at = start + timedelta(days=payment.duration_days)
        payment.status = "paid"
        payment.provider_payment_id = charge_id
        payment.paid_at = now
        session.add(GroupSubscriptionEvent(
            group_id=group.id,
            actor_telegram_id=message.from_user.id,
            event_type="payment",
            plan_code=payment.plan_code,
            expires_at=group.plan_expires_at,
        ))
        if not await _commit_payment_once(
            session,
            charge_id=charge_id,
            kind="subscription",
            record_id=payment.id,
        ):
            return
        await message.answer(
            f"✅ Оплата получена. Тариф {payment.plan_code.upper()} активирован для группы «{clean_ui_text(group.title)}» до {group.plan_expires_at:%d.%m.%Y}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К тарифу группы", callback_data=f"plan:{group.id}")],
                [InlineKeyboardButton(text="◀️ К группе", callback_data=f"group:{group.id}")],
            ]),
        )