from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ad_market_models import DirectRequiredRule, GlobalPostDelivery, GlobalPostRequest
from app.db.models import Group, Punishment, RequiredChannel
from app.db.rank_models import RankAssignment
from app.db.session import SessionFactory
from app.services.ranks import ADMIN_RANKS, restore_telegram_rank


log = structlog.get_logger()
DELIVERY_CLAIM_STALE_AFTER = timedelta(minutes=10)
UNCERTAIN_DELIVERY_ERROR = "Доставка не подтверждена после прерывания worker; повтор отключён во избежание дубля"


def _creative_markup(button_text: str | None, button_url: str | None) -> InlineKeyboardMarkup | None:
    if not button_text or not button_url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=button_text, url=button_url),
    ]])


async def _recover_stale_delivery_claims(
    session: AsyncSession,
    request_id: int,
    now: datetime,
) -> None:
    cutoff = now - DELIVERY_CLAIM_STALE_AFTER

    # A stale `claimed` row is known to be pre-side-effect, so releasing it for a
    # fresh unique claim is safe. The conditional DELETE races safely with the
    # claimed -> processing transition: whichever row mutation wins determines
    # whether retry or quarantine owns recovery.
    await session.execute(
        delete(GlobalPostDelivery).where(
            GlobalPostDelivery.request_id == request_id,
            GlobalPostDelivery.status == "claimed",
            GlobalPostDelivery.delivered_at <= cutoff,
        )
    )

    stale_processing = list((await session.scalars(
        select(GlobalPostDelivery).where(
            GlobalPostDelivery.request_id == request_id,
            GlobalPostDelivery.status == "processing",
            GlobalPostDelivery.delivered_at <= cutoff,
        )
    )).all())
    for row in stale_processing:
        row.status = "failed"
        row.error_text = UNCERTAIN_DELIVERY_ERROR

    await session.commit()


async def _claim_delivery(
    session: AsyncSession,
    request_id: int,
    group_id: int,
) -> GlobalPostDelivery | None:
    """Claim one paid-ad delivery before any Telegram side effect.

    The database uniqueness constraint on (request_id, group_id) is the concurrency
    boundary. A duplicate claim is an idempotent no-op; unrelated integrity failures
    are re-raised after rollback.
    """
    claim = GlobalPostDelivery(
        request_id=request_id,
        group_id=group_id,
        message_id=None,
        status="claimed",
        error_text=None,
    )
    session.add(claim)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(GlobalPostDelivery.id).where(
            GlobalPostDelivery.request_id == request_id,
            GlobalPostDelivery.group_id == group_id,
        ).limit(1))
        if existing is not None:
            return None
        raise
    return claim


async def _mark_delivery_processing(claim_id: int) -> bool:
    """Durably enter the ambiguous Telegram-send window without releasing Group lock."""
    async with SessionFactory() as marker_session:
        result = await marker_session.execute(
            update(GlobalPostDelivery)
            .where(
                GlobalPostDelivery.id == claim_id,
                GlobalPostDelivery.status == "claimed",
            )
            .values(status="processing", delivered_at=func.now())
            .returning(GlobalPostDelivery.id)
        )
        transitioned = result.scalar_one_or_none() is not None
        await marker_session.commit()
        return transitioned


async def _finalize_request_if_complete(
    bot: Bot,
    session: AsyncSession,
    item: GlobalPostRequest,
    groups: list[Group],
) -> None:
    deliveries = list((await session.scalars(
        select(GlobalPostDelivery).where(GlobalPostDelivery.request_id == item.id)
    )).all())
    active_group_ids = {group.id for group in groups}
    attempted_group_ids = {row.group_id for row in deliveries}
    pending_group_ids = {
        row.group_id for row in deliveries
        if row.status in {"claimed", "processing"} and row.group_id in active_group_ids
    }
    if not active_group_ids.issubset(attempted_group_ids) or pending_group_ids:
        return

    completed_at = datetime.now(timezone.utc)
    result = await session.execute(
        update(GlobalPostRequest)
        .where(
            GlobalPostRequest.id == item.id,
            GlobalPostRequest.status == "paid",
        )
        .values(status="completed", completed_at=completed_at)
        .returning(GlobalPostRequest.id)
    )
    claimed_completion = result.scalar_one_or_none()
    await session.commit()
    if claimed_completion is None:
        return

    sent_count = sum(1 for row in deliveries if row.status == "sent")
    failed_count = sum(1 for row in deliveries if row.status == "failed")
    try:
        await bot.send_message(
            item.buyer_telegram_id,
            f"✅ Рекламный пост #{item.id} опубликован по сети Mimoru.\n"
            f"Успешно: {sent_count} групп.\n"
            f"Недоступно: {failed_count} групп.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Мои рекламные посты", callback_data="gpost:mine")],
                [InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")],
            ]),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def _publish_global_request(bot: Bot, request_id: int) -> None:
    """Deliver one paid global post to candidate groups.

    Session lifecycle: each phase uses its own session scope. The request data
    and stale-claim recovery happen in one short transaction. Then each group
    delivery uses its own claim-validate-send-commit cycle with a fresh session,
    so the connection is never held during Telegram API round-trips across groups.
    """
    # --- Phase 1: Load request and recover stale claims ---
    async with SessionFactory() as session:
        item = await session.get(GlobalPostRequest, request_id)
        if item is None or item.status != "paid":
            return

        now = datetime.now(timezone.utc)
        await _recover_stale_delivery_claims(session, item.id, now)

        candidate_group_ids = list((await session.scalars(
            select(Group.id).where(Group.is_active.is_(True)).order_by(Group.id)
        )).all())
        delivered_group_ids = set((await session.scalars(
            select(GlobalPostDelivery.group_id).where(GlobalPostDelivery.request_id == item.id)
        )).all())
        pending_group_ids = [
            group_id for group_id in candidate_group_ids
            if group_id not in delivered_group_ids
        ][:50]

    # Capture item data before releasing session
    item_id = item.id
    item_photo = item.photo_file_id
    item_text = item.text
    item_button_text = item.button_text
    item_button_url = item.button_url

    # --- Phase 2: Deliver to each group with per-group sessions ---
    for group_id in pending_group_ids:
        async with SessionFactory() as session:
            claim = await _claim_delivery(session, item_id, group_id)
            if claim is None:
                continue

            group = await session.scalar(
                select(Group).where(Group.id == group_id).with_for_update()
            )
            if group is None or not group.is_active:
                claim.status = "failed"
                claim.error_text = "Группа отключена до доставки"
                await session.commit()
                continue

            if not await _mark_delivery_processing(claim.id):
                await session.commit()
                continue

        # --- Telegram send with NO connection held ---
        chat_id = group.telegram_chat_id
        message_id: int | None = None
        status = "sent"
        error_text: str | None = None
        try:
            markup = _creative_markup(item_button_text, item_button_url)
            if item_photo:
                sent = await bot.send_photo(
                    chat_id,
                    item_photo,
                    caption=item_text or None,
                    reply_markup=markup,
                )
            else:
                sent = await bot.send_message(
                    chat_id,
                    item_text,
                    reply_markup=markup,
                )
            message_id = sent.message_id
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            status = "failed"
            error_text = str(error)[:1000]
            log.warning(
                "global_post_delivery_failed",
                request_id=item_id,
                group_id=group_id,
                error=str(error),
            )

        # --- Persist delivery result ---
        async with SessionFactory() as session:
            claim = await session.scalar(
                select(GlobalPostDelivery).where(GlobalPostDelivery.id == claim.id)
            )
            if claim is not None:
                claim.message_id = message_id
                claim.status = status
                claim.error_text = error_text
                await session.commit()

    # --- Phase 3: Finalize completion ---
    async with SessionFactory() as session:
        current_groups = list((await session.scalars(
            select(Group).where(Group.is_active.is_(True)).order_by(Group.id)
        )).all())
        refreshed_item = await session.get(GlobalPostRequest, item_id)
        if refreshed_item is not None:
            await _finalize_request_if_complete(bot, session, refreshed_item, current_groups)


async def distribute_global_posts(bot: Bot) -> None:
    async with SessionFactory() as session:
        ids = list((await session.scalars(
            select(GlobalPostRequest.id)
            .where(GlobalPostRequest.status == "paid")
            .order_by(GlobalPostRequest.paid_at.asc().nullsfirst(), GlobalPostRequest.id.asc())
            .limit(20)
        )).all())
    for request_id in ids:
        await _publish_global_request(bot, request_id)


async def expire_direct_required_rules() -> None:
    now = datetime.now(timezone.utc)
    async with SessionFactory() as session:
        candidates = list((await session.execute(
            select(DirectRequiredRule.id, DirectRequiredRule.group_id).where(
                DirectRequiredRule.active.is_(True),
                DirectRequiredRule.mode == "days",
                DirectRequiredRule.expires_at.is_not(None),
                DirectRequiredRule.expires_at <= now,
            )
        )).all())
        for rule_id, group_id in candidates:
            # Owner renewal takes the Group lock first. Match that boundary/order,
            # then reacquire and revalidate the rule so a stale candidate cannot
            # deactivate a rule that was renewed after the initial scan.
            await session.scalar(
                select(Group)
                .where(Group.id == group_id)
                .with_for_update()
            )
            rule = await session.scalar(
                select(DirectRequiredRule)
                .where(DirectRequiredRule.id == rule_id)
                .with_for_update()
            )
            if (
                rule is not None
                and rule.active
                and rule.mode == "days"
                and rule.expires_at is not None
                and rule.expires_at <= now
            ):
                rule.active = False
                channel = await session.scalar(select(RequiredChannel).where(
                    RequiredChannel.group_id == rule.group_id,
                    RequiredChannel.channel_username == rule.channel_username,
                ))
                if channel is not None:
                    channel.active = False
            # Release the Group/rule locks after each candidate instead of holding
            # unrelated groups for the whole expiry batch.
            await session.commit()


async def restore_ranked_admins_after_mute(bot: Bot) -> None:
    """Restore admin rights only after a locked, current no-mute decision."""
    async with SessionFactory() as session:
        candidate_ids = list((await session.scalars(
            select(RankAssignment.id).where(
                RankAssignment.active.is_(True),
                RankAssignment.restore_after_mute.is_(True),
                RankAssignment.rank_code.in_(ADMIN_RANKS),
            )
        )).all())

    for assignment_id in candidate_ids:
        async with SessionFactory() as session:
            group_id = await session.scalar(
                select(RankAssignment.group_id).where(RankAssignment.id == assignment_id)
            )
            if group_id is None:
                continue

            group = await session.scalar(
                select(Group).where(Group.id == group_id).with_for_update()
            )
            assignment = await session.scalar(
                select(RankAssignment)
                .where(RankAssignment.id == assignment_id)
                .with_for_update()
            )
            if (
                assignment is None
                or not assignment.active
                or not assignment.restore_after_mute
                or assignment.rank_code not in ADMIN_RANKS
            ):
                continue

            if group is None or not group.is_active:
                assignment.restore_after_mute = False
                await session.commit()
                continue

            still_muted = await session.scalar(
                select(exists().where(
                    Punishment.group_id == assignment.group_id,
                    Punishment.user_telegram_id == assignment.user_telegram_id,
                    Punishment.kind == "mute",
                    Punishment.active.is_(True),
                ))
            )
            if still_muted:
                continue

            if await restore_telegram_rank(bot, group, assignment):
                assignment.restore_after_mute = False
                await session.commit()
            else:
                log.warning(
                    "rank_admin_restore_failed",
                    group_id=assignment.group_id,
                    user_id=assignment.user_telegram_id,
                    rank=assignment.rank_code,
                )


async def ad_market_background_loop(bot: Bot, stop_event: asyncio.Event) -> None:
    # Интервалы разных задач:
    # - distribute_global_posts        — каждые 30 сек (важна скорость публикации)
    # - restore_ranked_admins_after_mute — каждые 30 сек (важна скорость снятия мьюта)
    # - expire_direct_required_rules   — каждые 300 сек (точность до 5 мин достаточна)
    EXPIRE_INTERVAL_SECONDS = 300
    last_expire = 0.0

    while not stop_event.is_set():
        try:
            now = time.monotonic()
            await distribute_global_posts(bot)
            await restore_ranked_admins_after_mute(bot)
            if now - last_expire >= EXPIRE_INTERVAL_SECONDS:
                await expire_direct_required_rules()
                last_expire = now
        except Exception:
            log.exception("ad_market_background_iteration_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except TimeoutError:
            continue
