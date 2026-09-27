import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select, update

from app.db.models import (
    DailyStat,
    Group,
    GroupMember,
    GroupSettings,
    GroupSubscriptionEvent,
    NewMemberRecord,
    Punishment,
    ScheduledMessage,
    Warning,
)
from app.db.session import SessionFactory
from app.services.access import is_service_owner
from app.services.owner_notifications import send_to_current_group_owner
from app.services.plans import feature_available
from app.services.punishment_expiry import expire_punishments
from app.services.scheduling import next_occurrence
from app.services.timezones import to_local
from app.tasks_ad_cleanup import complete_ad_orders
from app.tasks_captcha import expire_captcha_sessions
from app.tasks_deleted_cleanup import run_group_automation
from app.tasks_permission_modes import apply_night_modes, expire_lockdowns
from app.tasks_warning_expiry import expire_warnings
from app.services.audit import deliver_pending_logs


SCHEDULED_CLAIM_STALE_AFTER = timedelta(minutes=15)
UNCERTAIN_DELIVERY_ERROR = (
    "Предыдущая отправка была прервана после фиксации claim. "
    "Состояние доставки неизвестно; автоматический повтор отключён во избежание дубля."
)


async def _claim_daily_report(
    group_id: int,
    settings_id: int,
    local_today: str,
    expected_timezone: str,
) -> str | None:
    """Revalidate current report eligibility and persist the local-date claim."""
    async with SessionFactory() as session:
        locked_group = await session.scalar(
            select(Group).where(Group.id == group_id).with_for_update()
        )
        if (
            locked_group is None
            or not locked_group.is_active
            or locked_group.owner_telegram_id is None
        ):
            return None
        settings = locked_group.settings
        local_now = to_local(datetime.now(timezone.utc), settings.timezone_name)
        current_local_today = local_now.date().isoformat()
        if (
            settings.id != settings_id
            or settings.timezone_name != expected_timezone
            or current_local_today != local_today
            or not settings.reports_enabled
            or not feature_available(locked_group, "daily_reports")
            or settings.report_hour_utc != local_now.hour
            or settings.last_report_date == local_today
        ):
            return None
        claimed = await session.scalar(
            update(GroupSettings)
            .where(
                GroupSettings.id == settings.id,
                or_(
                    GroupSettings.last_report_date.is_(None),
                    GroupSettings.last_report_date != local_today,
                ),
            )
            .values(last_report_date=local_today)
            .returning(GroupSettings.id)
        )
        title = locked_group.title
        await session.commit()
        return title if claimed is not None else None


async def _release_daily_report_claim(group_id: int, local_today: str) -> None:
    """Release a pre-send claim so a transient Telegram failure can retry."""
    async with SessionFactory() as session:
        await session.execute(
            update(GroupSettings)
            .where(
                GroupSettings.group_id == group_id,
                GroupSettings.last_report_date == local_today,
            )
            .values(last_report_date=None)
        )
        await session.commit()


async def _release_subscription_notice_claim(
    group_id: int,
    event_type: str,
    expires_at: datetime,
) -> None:
    """Release a pre-send subscription notice claim after failed delivery."""
    async with SessionFactory() as session:
        await session.execute(
            delete(GroupSubscriptionEvent).where(
                GroupSubscriptionEvent.group_id == group_id,
                GroupSubscriptionEvent.event_type == event_type,
                GroupSubscriptionEvent.expires_at == expires_at,
            )
        )
        await session.commit()


async def send_daily_reports(bot: Bot) -> None:
    """Send each daily report at most once per group/local date.

    The existing last_report_date field becomes the durable claim and is committed before
    the external Telegram call. This preserves the prior no-retry-on-delivery-error
    behavior while also closing the crash-after-send-before-commit duplicate window.
    """
    async with SessionFactory() as session:
        now = datetime.now(timezone.utc)
        groups = (await session.scalars(select(Group).where(Group.is_active.is_(True)))).all()
        for group in groups:
            settings = group.settings
            local_now = to_local(now, settings.timezone_name)
            local_today = local_now.date().isoformat()
            if (
                not settings.reports_enabled
                or not feature_available(group, "daily_reports")
                or settings.report_hour_utc != local_now.hour
                or settings.last_report_date == local_today
            ):
                continue

            report_date = local_now.date() - timedelta(days=1)
            previous_date = report_date - timedelta(days=1)
            report_date_s = report_date.isoformat()
            previous_date_s = previous_date.isoformat()
            day_start_local = local_now.replace(
                year=report_date.year,
                month=report_date.month,
                day=report_date.day,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            day_end_local = day_start_local + timedelta(days=1)
            day_start_utc = day_start_local.astimezone(timezone.utc)
            day_end_utc = day_end_local.astimezone(timezone.utc)

            messages = int(await session.scalar(select(func.coalesce(func.sum(DailyStat.messages_count), 0)).where(
                DailyStat.group_id == group.id, DailyStat.date == report_date_s
            )) or 0)
            previous_messages = int(await session.scalar(select(func.coalesce(func.sum(DailyStat.messages_count), 0)).where(
                DailyStat.group_id == group.id, DailyStat.date == previous_date_s
            )) or 0)
            active = int(await session.scalar(select(func.count()).select_from(DailyStat).where(
                DailyStat.group_id == group.id, DailyStat.date == report_date_s, DailyStat.messages_count > 0
            )) or 0)
            deleted = int(await session.scalar(select(func.coalesce(func.sum(DailyStat.deleted_count), 0)).where(
                DailyStat.group_id == group.id, DailyStat.date == report_date_s
            )) or 0)
            joined = int(await session.scalar(select(func.count()).select_from(NewMemberRecord).where(
                NewMemberRecord.group_id == group.id,
                NewMemberRecord.joined_at >= day_start_utc,
                NewMemberRecord.joined_at < day_end_utc,
            )) or 0)
            warnings = int(await session.scalar(select(func.count()).select_from(Warning).where(
                Warning.group_id == group.id,
                Warning.created_at >= day_start_utc,
                Warning.created_at < day_end_utc,
            )) or 0)
            mutes = int(await session.scalar(select(func.count()).select_from(Punishment).where(
                Punishment.group_id == group.id,
                Punishment.kind == "mute",
                Punishment.created_at >= day_start_utc,
                Punishment.created_at < day_end_utc,
            )) or 0)
            deleted_accounts = int(await session.scalar(select(func.count()).select_from(GroupMember).where(
                GroupMember.group_id == group.id,
                GroupMember.is_present.is_(True),
                GroupMember.is_deleted_account.is_(True),
            )) or 0)
            bans = int(await session.scalar(select(func.count()).select_from(Punishment).where(
                Punishment.group_id == group.id,
                Punishment.kind == "ban",
                Punishment.created_at >= day_start_utc,
                Punishment.created_at < day_end_utc,
            )) or 0)

            if previous_messages == 0:
                trend = "без сравнения" if messages == 0 else "рост с нуля"
            else:
                delta = ((messages - previous_messages) / previous_messages) * 100
                trend = f"{delta:+.0f}% к предыдущему дню"

            title = await _claim_daily_report(
                group.id,
                settings.id,
                local_today,
                settings.timezone_name,
            )
            if title is None:
                continue
            report_text = (
                f"<b>📊 Mimoru · ежедневный отчёт</b>\n"
                f"🏠 {title}\n"
                f"📅 {report_date_s}\n\n"
                f"💬 Сообщений: <b>{messages}</b> · {trend}\n"
                f"👥 Активных участников: <b>{active}</b>\n"
                f"🆕 Новых участников: <b>{joined}</b>\n"
                f"🗑 Удалено сообщений: <b>{deleted}</b>\n"
                f"🪦 Удалённых аккаунтов: <b>{deleted_accounts}</b>\n\n"
                f"<b>🛡 Модерация</b>\n"
                f"⚠️ Предупреждений: {warnings}\n"
                f"🔇 Мутов: {mutes}\n"
                f"⛔ Банов: {bans}"
            )
            sent, owner_id, error = await send_to_current_group_owner(
                bot,
                group_id=group.id,
                text=report_text,
            )
            if not sent:
                await _release_daily_report_claim(group.id, local_today)
                if error not in {None, "group_unavailable"}:
                    structlog.get_logger().warning(
                        "daily_report_delivery_failed",
                        group_id=group.id,
                        owner_id=owner_id,
                        error=error,
                    )


async def _claim_subscription_notice(
    group_id: int,
    event_type: str,
    expected_expires_at: datetime,
) -> tuple[str, str, datetime] | None:
    """Revalidate and durably claim a current subscription notice before sending it."""
    async with SessionFactory() as session:
        locked_group = await session.scalar(
            select(Group).where(Group.id == group_id).with_for_update()
        )
        if (
            locked_group is None
            or not locked_group.is_active
            or locked_group.owner_telegram_id is None
            or locked_group.plan_code not in {"trial", "standard", "pro"}
            or locked_group.plan_expires_at is None
            or locked_group.plan_expires_at != expected_expires_at
        ):
            return None
        exists = await session.scalar(select(GroupSubscriptionEvent.id).where(
            GroupSubscriptionEvent.group_id == group_id,
            GroupSubscriptionEvent.event_type == event_type,
            GroupSubscriptionEvent.expires_at == expected_expires_at,
        ).limit(1))
        if exists:
            return None
        session.add(GroupSubscriptionEvent(
            group_id=group_id,
            actor_telegram_id=locked_group.owner_telegram_id,
            event_type=event_type,
            plan_code=locked_group.plan_code,
            expires_at=expected_expires_at,
        ))
        snapshot = (
            locked_group.title,
            locked_group.plan_code,
            locked_group.plan_expires_at,
        )
        await session.commit()
        return snapshot


async def send_subscription_notices(bot: Bot) -> None:
    """Notify current owners once per expiry threshold using a durable pre-send ledger claim."""
    async with SessionFactory() as session:
        now = datetime.now(timezone.utc)
        rows = (await session.scalars(select(Group).where(
            Group.is_active.is_(True),
            Group.plan_code.in_(["trial", "standard", "pro"]),
            Group.plan_expires_at.is_not(None),
        ))).all()
        for group in rows:
            expires_at = group.plan_expires_at
            if expires_at is None:
                continue
            seconds = (expires_at - now).total_seconds()
            if seconds <= 0:
                event_type, label = "expiry_notice_0", "Подписка закончилась"
            elif seconds <= 86400:
                event_type, label = "expiry_notice_1", "До окончания подписки меньше суток"
            elif seconds <= 3 * 86400:
                event_type, label = "expiry_notice_3", "До окончания подписки осталось 3 дня"
            elif seconds <= 7 * 86400:
                event_type, label = "expiry_notice_7", "До окончания подписки осталось 7 дней"
            else:
                continue
            claim = await _claim_subscription_notice(group.id, event_type, expires_at)
            if claim is None:
                continue
            title, plan_code, claimed_expires_at = claim
            notice_text = (
                f"<b>💎 Mimoru · подписка</b>\n\n🏠 {title}\n"
                f"{label}.\nТариф: <b>{plan_code.upper()}</b>\n"
                f"Дата окончания: <b>{claimed_expires_at:%d.%m.%Y %H:%M} UTC</b>\n\n"
                "Продлить подписку можно в панели группы → 💎 Подписка."
            )
            sent, owner_id, error = await send_to_current_group_owner(
                bot,
                group_id=group.id,
                text=notice_text,
            )
            if not sent:
                await _release_subscription_notice_claim(
                    group.id,
                    event_type,
                    claimed_expires_at,
                )
                if error not in {None, "group_unavailable"}:
                    structlog.get_logger().warning(
                        "subscription_notice_delivery_failed_after_claim",
                        group_id=group.id,
                        owner_id=owner_id,
                        event_type=event_type,
                        error=error,
                    )


async def recover_interrupted_scheduled_messages() -> None:
    """Retry definitely pre-send claims and quarantine ambiguous in-flight sends."""
    async with SessionFactory() as session:
        cutoff = datetime.now(timezone.utc) - SCHEDULED_CLAIM_STALE_AFTER
        await session.execute(
            update(ScheduledMessage)
            .where(
                ScheduledMessage.status == "claimed",
                ScheduledMessage.last_run_at.is_not(None),
                ScheduledMessage.last_run_at <= cutoff,
            )
            .values(status="pending", last_run_at=None, error_text=None)
        )
        await session.execute(
            update(ScheduledMessage)
            .where(
                ScheduledMessage.status == "processing",
                ScheduledMessage.last_run_at.is_not(None),
                ScheduledMessage.last_run_at <= cutoff,
            )
            .values(status="failed", error_text=UNCERTAIN_DELIVERY_ERROR)
        )
        await session.commit()


async def _claim_scheduled_message(message_id: int, now: datetime) -> ScheduledMessage | None:
    """Atomically reserve one due occurrence before any external side effect."""
    async with SessionFactory() as session:
        claimed_id = await session.scalar(
            update(ScheduledMessage)
            .where(
                ScheduledMessage.id == message_id,
                ScheduledMessage.status == "pending",
                ScheduledMessage.send_at <= now,
            )
            .values(status="claimed", last_run_at=now, error_text=None)
            .returning(ScheduledMessage.id)
        )
        await session.commit()
        if claimed_id is None:
            return None
    async with SessionFactory() as session:
        return await session.get(ScheduledMessage, claimed_id)


async def _mark_scheduled_message_processing(message_id: int) -> bool:
    """Durably enter the ambiguous Telegram-send window while Group stays locked."""
    async with SessionFactory() as marker_session:
        transitioned_id = await marker_session.scalar(
            update(ScheduledMessage)
            .where(
                ScheduledMessage.id == message_id,
                ScheduledMessage.status == "claimed",
            )
            .values(status="processing", last_run_at=datetime.now(timezone.utc))
            .returning(ScheduledMessage.id)
        )
        await marker_session.commit()
        return transitioned_id is not None


async def send_scheduled_messages(bot: Bot) -> None:
    """Send due scheduled messages only after a durable claim and live authority check."""
    now = datetime.now(timezone.utc)
    async with SessionFactory() as session:
        due_ids = list((await session.scalars(select(ScheduledMessage.id).where(
            ScheduledMessage.status == "pending",
            ScheduledMessage.send_at <= now,
        ).order_by(ScheduledMessage.send_at).limit(100))).all())

    for message_id in due_ids:
        row = await _claim_scheduled_message(message_id, now)
        if row is None:
            continue

        # Hold the group row lock through the authorization check and Telegram
        # side effect. Ownership transfer uses the same lock, so it cannot race
        # between the check and the send.
        async with SessionFactory() as session:
            group = await session.scalar(
                select(Group).where(Group.id == row.group_id).with_for_update()
            )
            stored = await session.get(ScheduledMessage, row.id)
            if stored is None or stored.status != "claimed":
                continue
            if group is None or not group.is_active:
                stored.status = "failed"
                stored.error_text = "Группа неактивна"
                await session.commit()
                continue
            if (
                stored.creator_telegram_id != group.owner_telegram_id
                and not is_service_owner(stored.creator_telegram_id)
            ):
                stored.status = "cancelled"
                stored.error_text = "Создатель публикации больше не управляет группой"
                await session.commit()
                continue

            # Enter the ambiguous side-effect window durably in a separate short
            # transaction. The main session keeps Group FOR UPDATE through this
            # transition and the Telegram call, preserving ownership serialization.
            if not await _mark_scheduled_message_processing(stored.id):
                continue

            try:
                sent = await bot.send_message(group.telegram_chat_id, stored.text)
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                stored.status = "failed"
                stored.error_text = str(error)[:1000]
                await session.commit()
                continue

            stored.sent_message_id = sent.message_id
            stored.sent_at = now
            next_at = next_occurrence(stored.send_at, stored.recurrence)
            if next_at is None:
                stored.status = "sent"
            else:
                stored.send_at = next_at
                stored.status = "pending"
            await session.commit()


async def background_loop(bot: Bot, redis: Redis, stop_event: asyncio.Event) -> None:
    """Core scheduler with durable claims around externally visible message delivery."""
    log = structlog.get_logger()
    await recover_interrupted_scheduled_messages()
    while not stop_event.is_set():
        try:
            await expire_punishments(bot, redis)
            await expire_captcha_sessions(bot, redis)
            await expire_warnings()
            await send_daily_reports(bot)
            await expire_lockdowns(bot)
            await apply_night_modes(bot)
            await send_scheduled_messages(bot)
            await complete_ad_orders(bot)
            await send_subscription_notices(bot)
            await deliver_pending_logs(bot)
            await run_group_automation(bot)
        except Exception:
            log.exception("background_iteration_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5)
        except TimeoutError:
            continue