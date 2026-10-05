from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, GroupSubscriptionEvent, PromoCode, PromoCodeUse
from app.services.promos import extend_plan, normalize_promo_code, promo_is_available


class PromoRedemptionError(ValueError):
    pass


@dataclass(frozen=True)
class PromoRedemptionResult:
    code: str
    group_id: int
    plan_code: str
    bonus_days: int
    expires_at: datetime


async def _redeem_locked_promo(
    session: AsyncSession,
    *,
    promo: PromoCode,
    group: Group,
    user_telegram_id: int,
    now: datetime,
) -> PromoRedemptionResult:
    if not promo_is_available(
        active=promo.active,
        expires_at=promo.expires_at,
        current_uses=promo.current_uses,
        max_uses=promo.max_uses,
        now=now,
    ):
        raise PromoRedemptionError("Промокод недоступен или закончился.")

    existing_use = await session.scalar(
        select(PromoCodeUse.id).where(
            PromoCodeUse.promo_code_id == promo.id,
            PromoCodeUse.user_telegram_id == user_telegram_id,
        ).limit(1)
    )
    if existing_use is not None:
        raise PromoRedemptionError("Вы уже использовали этот промокод.")

    # Group is already locked by caller (lock order: Group -> PromoCode)
    promo.current_uses += 1
    group.plan_code = promo.plan_code
    group.plan_expires_at = extend_plan(group.plan_expires_at, promo.bonus_days, now=now)
    session.add(PromoCodeUse(
        promo_code_id=promo.id,
        user_telegram_id=user_telegram_id,
        group_id=group.id,
    ))
    session.add(GroupSubscriptionEvent(
        group_id=group.id,
        actor_telegram_id=user_telegram_id,
        event_type="promo_redeem",
        plan_code=promo.plan_code,
        expires_at=group.plan_expires_at,
    ))
    await session.flush()
    return PromoRedemptionResult(
        code=promo.code,
        group_id=group.id,
        plan_code=promo.plan_code,
        bonus_days=promo.bonus_days,
        expires_at=group.plan_expires_at,
    )


async def redeem_promo_code(
    session: AsyncSession,
    *,
    user_telegram_id: int,
    group_id: int,
    raw_code: str,
    now: datetime | None = None,
) -> PromoRedemptionResult:
    """Atomically redeem one normalized promo code for one owned active group."""
    now = now or datetime.now(timezone.utc)
    code = normalize_promo_code(raw_code)
    if not code:
        raise PromoRedemptionError("Промокод пуст.")
    # Lock order: Group first, then PromoCode (prevents deadlocks)
    group = await session.scalar(
        select(Group).where(
            Group.id == group_id,
            Group.owner_telegram_id == user_telegram_id,
            Group.is_active.is_(True),
        ).with_for_update()
    )
    if group is None:
        raise PromoRedemptionError("Группа не найдена или не принадлежит вам.")
    promo = await session.scalar(
        select(PromoCode).where(PromoCode.code == code).with_for_update()
    )
    if promo is None:
        raise PromoRedemptionError("Промокод не найден.")
    return await _redeem_locked_promo(
        session,
        promo=promo,
        group=group,
        user_telegram_id=user_telegram_id,
        now=now,
    )


async def redeem_promo_id(
    session: AsyncSession,
    *,
    user_telegram_id: int,
    group_id: int,
    promo_id: int,
    now: datetime | None = None,
) -> PromoRedemptionResult:
    """Redeem a promo selected by a short callback-safe database id."""
    now = now or datetime.now(timezone.utc)
    # Lock order: Group first, then PromoCode (prevents deadlocks)
    group = await session.scalar(
        select(Group).where(
            Group.id == group_id,
            Group.owner_telegram_id == user_telegram_id,
            Group.is_active.is_(True),
        ).with_for_update()
    )
    if group is None:
        raise PromoRedemptionError("Группа не найдена или не принадлежит вам.")
    promo = await session.scalar(
        select(PromoCode).where(PromoCode.id == promo_id).with_for_update()
    )
    if promo is None:
        raise PromoRedemptionError("Промокод не найден.")
    return await _redeem_locked_promo(
        session,
        promo=promo,
        group=group,
        user_telegram_id=user_telegram_id,
        now=now,
    )
