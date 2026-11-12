"""External payment activation for global ad posts.

Idempotent, concurrency-safe application of a confirmed external payment to a
``GlobalPostRequest``. The unique partial index on ``payment_charge_id`` guards
against double-credit; duplicate charge ids are surfaced as ``duplicate``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.ad_market_models import GlobalPostRequest

log = logging.getLogger(__name__)


async def _charge_already_recorded(session, charge_id: str) -> bool:
    return (
        await session.scalar(
            select(GlobalPostRequest.id)
            .where(GlobalPostRequest.payment_charge_id == charge_id)
            .limit(1)
        )
    ) is not None


async def _commit_payment_once(session, *, charge_id: str, request_id: int) -> bool:
    try:
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        if not await _charge_already_recorded(session, charge_id):
            log.exception("global post commit integrity error", request_id=request_id, charge_id=charge_id)
            raise
        log.warning("global post already recorded", charge_id=charge_id)
        return False


async def activate_global_post_payment(
    session,
    *,
    request_id: int,
    charge_id: str,
    now: datetime | None = None,
) -> str:
    """Apply a paid global ad post.

    Returns outcome: ``done`` / ``duplicate`` / ``invalid`` / ``missing``.
    """
    now = now or datetime.now(UTC)
    item = await session.scalar(
        select(GlobalPostRequest).where(GlobalPostRequest.id == request_id).with_for_update()
    )
    if item is None:
        return "missing"
    if item.payment_charge_id == charge_id and item.status in {"paid", "completed"}:
        return "duplicate"
    if item.payment_charge_id is not None and item.payment_charge_id != charge_id:
        return "duplicate"
    if item.status != "approved":
        return "invalid"
    item.status = "paid"
    item.payment_charge_id = charge_id
    item.paid_at = now
    if not await _commit_payment_once(session, charge_id=charge_id, request_id=request_id):
        return "duplicate"
    return "done"
