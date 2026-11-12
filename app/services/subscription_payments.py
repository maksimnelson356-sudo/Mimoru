"""External payment activation for subscriptions.

Both external providers (CrocoPay, XRocket) confirm a payment through their
signed webhook or through a status poll. This module applies the payment to a
``Payment`` row and a ``Group`` atomically and idempotently.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Group, GroupSubscriptionEvent, Payment

log = logging.getLogger(__name__)


async def _locked_group(session, group_id: int) -> Group | None:
    return await session.scalar(select(Group).where(Group.id == group_id).with_for_update())


async def _locked_payment(session, payment_id: int) -> Payment | None:
    return await session.scalar(select(Payment).where(Payment.id == payment_id).with_for_update())


async def _charge_already_recorded(session, charge_id: str) -> bool:
    return (
        await session.scalar(
            select(Payment.id).where(Payment.provider_payment_id == charge_id).limit(1)
        )
    ) is not None


async def _commit_payment_once(session, *, charge_id: str, payment_id: int) -> bool:
    try:
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        if not await _charge_already_recorded(session, charge_id):
            log.exception("payment commit integrity error", payment_id=payment_id, charge_id=charge_id)
            raise
        log.warning("payment already recorded", charge_id=charge_id)
        return False


async def activate_subscription_payment(
    session,
    *,
    payment_id: int,
    charge_id: str,
    provider: str,
    now: datetime | None = None,
) -> str:
    """Apply a paid subscription for an external payment.

    Returns outcome: ``done`` / ``duplicate`` / ``stale`` / ``invalid`` /
    ``missing``. The expected amount/currency are read from the ``Payment``
    row; the provider's own signed webhook is the source of truth.
    """
    now = now or datetime.now(UTC)
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.group_id is None:
        return "missing"
    group = await _locked_group(session, payment.group_id)
    if group is None:
        return "invalid"
    payment = await _locked_payment(session, payment_id)
    if payment is None:
        return "missing"
    if payment.provider != provider:
        return "invalid"
    if payment.status == "paid":
        return "duplicate"
    if payment.status in {"refunded", "refund_pending"}:
        return "duplicate"
    if payment.status != "pending":
        return "invalid"
    if not group.is_active or group.owner_telegram_id != payment.user_telegram_id:
        payment.status = "refund_pending"
        payment.provider_payment_id = charge_id
        payment.paid_at = now
        await _commit_payment_once(session, charge_id=charge_id, payment_id=payment_id)
        return "stale"
    start = group.plan_expires_at if group.plan_expires_at is not None and group.plan_expires_at > now else now
    group.plan_code = payment.plan_code
    group.plan_expires_at = start + timedelta(days=payment.duration_days)
    payment.status = "paid"
    payment.provider_payment_id = charge_id
    payment.paid_at = now
    session.add(
        GroupSubscriptionEvent(
            group_id=group.id,
            actor_telegram_id=payment.user_telegram_id,
            event_type="payment",
            plan_code=payment.plan_code,
            expires_at=group.plan_expires_at,
        )
    )
    if not await _commit_payment_once(session, charge_id=charge_id, payment_id=payment_id):
        return "duplicate"
    return "done"
