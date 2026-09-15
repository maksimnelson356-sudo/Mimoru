import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.db.models import (
    AdOrder,
    AdPlacement,
    AutomationLog,
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
from app.services.audit import deliver_pending_logs
from app.services.deleted_accounts import remove_deleted_accounts, scan_known_members
from app.services.night_mode import is_night_window, parse_hhmm
from app.services.plans import feature_available
from app.services.safety import warning_expiry_cutoff
from app.services.scheduling import next_occurrence
from app.services.timezones import to_local
from app.services.ui import is_chat_unavailable

UNMUTED = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)


async def expire_punishments(bot: Bot) -> None:
    log = structlog.get_logger()
    async with SessionFactory() as session:
        now = datetime.now(UTC)
        rows = (
            await session.scalars(
                select(Punishment).where(
                    Punishment.active.is_(True),
                    Punishment.ends_at.is_not(None),
                    Punishment.ends_at <= now,
                )
            )
        ).all()
        for punishment in rows:
            group = await session.get(Group, punishment.group_id)
            if group is None or not group.is_active:
                punishment.active = False
                continue
            try:
                if punishment.kind == "mute":
                    await bot.restrict_chat_member(
                        group.telegram_chat_id,
                        punishment.user_telegram_id,
                        permissions=UNMUTED,
                    )
                elif punishment.kind == "ban":
                    await bot.unban_chat_member(
                        group.telegram_chat_id,
                        punishment.user_telegram_id,
                        only_if_banned=True,
                    )
                punishment.active = False
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                if is_chat_unavailable(error):
                    log.debug(
                        "punishment_expiry_failed",
                        punishment_id=punishment.id,
                        error=str(error),
                    )
                else:
                    log.warning(
                        "punishment_expiry_failed",
                        punishment_id=punishment.id,
                        error=str(error),
                    )
        await session.commit()


async def expire_captcha_sessions(bot: Bot, redis: Redis) -> None:
    """Kick users whose verification deadline has passed.

    Session data is kept in Redis as captcha:<chat_id>:<user_id> = deadline timestamp.
    """
    log = structlog.get_logger()
    async for key in redis.scan_iter(match="captcha:*", count=200):
        raw_deadline = await redis.get(key)
        if not raw_deadline:
            continue
        try:
            deadline = int(raw_deadline)
            _, raw_chat_id, raw_user_id = key.split(":", 2)
            chat_id, user_id = int(raw_chat_id), int(raw_user_id)
        except (TypeError, ValueError):
            await redis.delete(key)
            continue
        if deadline > int(datetime.now(UTC).timestamp()):
            continue
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            log.warning(
                "captcha_expiry_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(error),
            )
        finally:
            await redis.delete(key)


async def expire_warnings() -> None:
    async with SessionFactory() as session:
        groups = (
            await session.scalars(select(Group).where(Group.is_active.is_(True)))
        ).all()
        for group in groups:
            if not group.settings.automation_enabled:
                continue
            cutoff = warning_expiry_cutoff(group.settings.warning_expire_days)
            if cutoff is None:
                continue
            rows = (
                await session.scalars(
                    select(Warning).where(
                        Warning.group_id == group.id,
                        Warning.active.is_(True),
                        Warning.created_at < cutoff,
                    )
                )
            ).all()
            for row in rows:
                row.active = False
            if rows:
                session.add(
                    AutomationLog(
                        group_id=group.id,
                        rule_code="warning_expiry",
                        status="ok",
                        details={
                            "expired": len(rows),
                            "days": group.settings.warning_expire_days,
                        },
                    )
                )
        await session.commit()


async def send_daily_reports(bot: Bot) -> None:
    async with SessionFactory() as session:
        now = datetime.now(UTC)
        groups = (
            await session.scalars(select(Group).where(Group.is_active.is_(True)))
        ).all()
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
            day_start_utc = day_start_local.astimezone(UTC)
            day_end_utc = day_end_local.astimezone(UTC)

            messages = int(
                await session.scalar(
                    select(func.coalesce(func.sum(DailyStat.messages_count), 0)).where(
                        DailyStat.group_id == group.id, DailyStat.date == report_date_s
                    )
                )
                or 0
            )
            previous_messages = int(
                await session.scalar(
                    select(func.coalesce(func.sum(DailyStat.messages_count), 0)).where(
                        DailyStat.group_id == group.id,
                        DailyStat.date == previous_date_s,
                    )
                )
                or 0
            )
            active = int(
                await session.scalar(
                    select(func.count())
                    .select_from(DailyStat)
                    .where(
                        DailyStat.group_id == group.id,
                        DailyStat.date == report_date_s,
                        DailyStat.messages_count > 0,
                    )
                )
                or 0
            )
            deleted = int(
                await session.scalar(
                    select(func.coalesce(func.sum(DailyStat.deleted_count), 0)).where(
                        DailyStat.group_id == group.id, DailyStat.date == report_date_s
                    )
                )
                or 0
            )
            joined = int(
                await session.scalar(
                    select(func.count())
                    .select_from(NewMemberRecord)
                    .where(
                        NewMemberRecord.group_id == group.id,
                        NewMemberRecord.joined_at >= day_start_utc,
                        NewMemberRecord.joined_at < day_end_utc,
                    )
                )
                or 0
            )
            warnings = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Warning)
                    .where(
                        Warning.group_id == group.id,
                        Warning.created_at >= day_start_utc,
                        Warning.created_at < day_end_utc,
                    )
                )
                or 0
            )
            mutes = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Punishment)
                    .where(
                        Punishment.group_id == group.id,
                        Punishment.kind == "mute",
                        Punishment.created_at >= day_start_utc,
                        Punishment.created_at < day_end_utc,
                    )
                )
                or 0
            )
            deleted_accounts = int(
                await session.scalar(
                    select(func.count())
                    .select_from(GroupMember)
                    .where(
                        GroupMember.group_id == group.id,
                        GroupMember.is_present.is_(True),
                        GroupMember.is_deleted_account.is_(True),
                    )
                )
                or 0
            )
            bans = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Punishment)
                    .where(
                        Punishment.group_id == group.id,
                        Punishment.kind == "ban",
                        Punishment.created_at >= day_start_utc,
                        Punishment.created_at < day_end_utc,
                    )
                )
                or 0
            )

            if previous_messages == 0:
                trend = "без сравнения" if messages == 0 else "рост с нуля"
            else:
                delta = ((messages - previous_messages) / previous_messages) * 100
                trend = f"{delta:+.0f}% к предыдущему дню"

            try:
                await bot.send_message(
                    group.owner_telegram_id,
                    f"<b>📊 Mimoru · ежедневный отчёт</b>\n"
                    f"🏠 {group.title}\n"
                    f"📅 {report_date_s}\n\n"
                    f"💬 Сообщений: <b>{messages}</b> · {trend}\n"
                    f"👥 Активных участников: <b>{active}</b>\n"
                    f"🆕 Новых участников: <b>{joined}</b>\n"
                    f"🗑 Удалено сообщений: <b>{deleted}</b>\n"
                    f"🪦 Удалённых аккаунтов: <b>{deleted_accounts}</b>\n\n"
                    f"<b>🛡 Модерация</b>\n"
                    f"⚠️ Предупреждений: {warnings}\n"
                    f"🔇 Мутов: {mutes}\n"
                    f"⛔ Банов: {bans}",
                )
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                structlog.get_logger().warning(
                    "daily_report_delivery_failed",
                    group_id=group.id,
                    owner_id=group.owner_telegram_id,
                    error=str(error),
                )
            settings.last_report_date = local_today
        await session.commit()


async def complete_ad_orders(bot: Bot) -> None:
    """Finish published placements after their promised duration.

    Mimoru tries to remove the advertising post when the placement duration expires.
    If Telegram no longer has the message, the order is still completed so lifecycle state
    never remains stuck forever.
    """
    async with SessionFactory() as session:
        now = datetime.now(UTC)
        rows = (
            await session.scalars(
                select(AdOrder)
                .where(
                    AdOrder.status == "published",
                    AdOrder.published_at.is_not(None),
                )
                .limit(100)
            )
        ).all()
        for order in rows:
            placement = await session.get(AdPlacement, order.placement_id)
            group = await session.get(Group, placement.group_id) if placement else None
            if placement is None or order.published_at is None:
                continue
            expires_at = order.published_at + timedelta(
                hours=max(1, placement.duration_hours)
            )
            if expires_at > now:
                continue
            if group is not None and order.published_message_id is not None:
                try:
                    await bot.delete_message(
                        group.telegram_chat_id, order.published_message_id
                    )
                except (TelegramBadRequest, TelegramForbiddenError) as error:
                    structlog.get_logger().warning(
                        "ad_expiry_delete_failed", order_id=order.id, error=str(error)
                    )
            order.status = "completed"
            order.completed_at = now
        await session.commit()


async def send_subscription_notices(bot: Bot) -> None:
    """Notify group owners before a paid/trial subscription expires.

    GroupSubscriptionEvent is reused as a durable delivery ledger, so restarts do not
    create duplicate reminders for the same expiry timestamp.
    """
    async with SessionFactory() as session:
        now = datetime.now(UTC)
        rows = (
            await session.scalars(
                select(Group).where(
                    Group.is_active.is_(True),
                    Group.plan_code.in_(["trial", "standard", "pro"]),
                    Group.plan_expires_at.is_not(None),
                )
            )
        ).all()
        for group in rows:
            expires_at = group.plan_expires_at
            if expires_at is None:
                continue
            seconds = (expires_at - now).total_seconds()
            if seconds <= 0:
                event_type, label = "expiry_notice_0", "Подписка закончилась"
            elif seconds <= 86400:
                event_type, label = (
                    "expiry_notice_1",
                    "До окончания подписки меньше суток",
                )
            elif seconds <= 3 * 86400:
                event_type, label = (
                    "expiry_notice_3",
                    "До окончания подписки осталось 3 дня",
                )
            elif seconds <= 7 * 86400:
                event_type, label = (
                    "expiry_notice_7",
                    "До окончания подписки осталось 7 дней",
                )
            else:
                continue
            exists = await session.scalar(
                select(GroupSubscriptionEvent.id)
                .where(
                    GroupSubscriptionEvent.group_id == group.id,
                    GroupSubscriptionEvent.event_type == event_type,
                    GroupSubscriptionEvent.expires_at == expires_at,
                )
                .limit(1)
            )
            if exists:
                continue
            try:
                await bot.send_message(
                    group.owner_telegram_id,
                    f"<b>💎 Mimoru · подписка</b>\n\n🏠 {group.title}\n"
                    f"{label}.\nТариф: <b>{group.plan_code.upper()}</b>\n"
                    f"Дата окончания: <b>{expires_at:%d.%m.%Y %H:%M} UTC</b>\n\n"
                    "Продлить подписку можно в панели группы → 💎 Подписка.",
                )
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                structlog.get_logger().warning(
                    "subscription_notice_failed", group_id=group.id, error=str(error)
                )
                continue
            session.add(
                GroupSubscriptionEvent(
                    group_id=group.id,
                    actor_telegram_id=group.owner_telegram_id,
                    event_type=event_type,
                    plan_code=group.plan_code,
                    expires_at=expires_at,
                )
            )
        await session.commit()


async def expire_lockdowns(bot: Bot) -> None:
    async with SessionFactory() as session:
        now = datetime.now(UTC)
        groups = (
            await session.scalars(
                select(Group)
                .where(
                    Group.is_active.is_(True),
                    GroupSettings.lockdown_enabled.is_(True),
                    GroupSettings.lockdown_until.is_not(None),
                    GroupSettings.lockdown_until <= now,
                )
                .join(GroupSettings, Group.settings)
            )
        ).all()
        for group in groups:
            previous = group.settings.lockdown_previous_permissions
            permissions = ChatPermissions(**previous) if previous else UNMUTED
            try:
                await bot.set_chat_permissions(group.telegram_chat_id, permissions)
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                structlog.get_logger().warning(
                    "lockdown_restore_failed",
                    group_id=group.id,
                    chat_id=group.telegram_chat_id,
                    error=str(error),
                )
            group.settings.lockdown_enabled = False
            group.settings.lockdown_until = None
            group.settings.lockdown_previous_permissions = None
        await session.commit()


async def _deliver_scheduled_message(bot: Bot, row_id: int, now: datetime) -> None:
    async with SessionFactory() as session:
        group_id = await session.scalar(
            select(ScheduledMessage.group_id).where(ScheduledMessage.id == row_id)
        )
        if group_id is None:
            return

        group = await session.scalar(
            select(Group).where(Group.id == group_id).with_for_update()
        )
        row = await session.scalar(
            # fmt: off
            select(ScheduledMessage).where(ScheduledMessage.id == row_id).with_for_update()
            # fmt: on
        )
        if row is None or row.group_id != group_id:
            return
        if row.status != "pending" or row.send_at > now:
            return
        if group is None or not group.is_active:
            row.status = "failed"
            row.error_text = "Группа неактивна"
            await session.commit()
            return
        # fmt: off
        if row.creator_telegram_id != group.owner_telegram_id and not is_service_owner(row.creator_telegram_id):
        # fmt: on
            row.status = "cancelled"
            row.error_text = "Создатель больше не управляет группой"
            await session.commit()
            return

        try:
            sent = await bot.send_message(group.telegram_chat_id, row.text)
            row.sent_message_id = sent.message_id
            row.sent_at = now
            row.last_run_at = now
            row.error_text = None
            next_at = next_occurrence(row.send_at, row.recurrence)
            if next_at is None:
                row.status = "sent"
            else:
                row.send_at = next_at
                row.status = "pending"
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            row.status = "failed"
            row.error_text = str(error)[:1000]
        await session.commit()


async def send_scheduled_messages(bot: Bot) -> None:
    async with SessionFactory() as session:
        now = datetime.now(UTC)
        row_ids = list(
            (
                await session.scalars(
                    select(ScheduledMessage.id)
                    .where(
                        ScheduledMessage.status == "pending",
                        ScheduledMessage.send_at <= now,
                    )
                    .order_by(ScheduledMessage.send_at)
                    .limit(100)
                )
            ).all()
        )
    for row_id in row_ids:
        await _deliver_scheduled_message(bot, row_id, now)


async def apply_night_modes(bot: Bot) -> None:
    log = structlog.get_logger()
    async with SessionFactory() as session:
        now = datetime.now(UTC)
        groups = (
            await session.scalars(select(Group).where(Group.is_active.is_(True)))
        ).all()
        for group in groups:
            settings = group.settings
            if not settings.night_mode_enabled and not settings.night_mode_active:
                continue
            local_now = to_local(now, settings.timezone_name)
            should_lock = settings.night_mode_enabled and is_night_window(
                local_now.time().replace(tzinfo=None),
                parse_hhmm(settings.night_mode_start),
                parse_hhmm(settings.night_mode_end),
            )
            if (
                should_lock
                and not settings.night_mode_active
                and not settings.lockdown_enabled
            ):
                try:
                    chat = await bot.get_chat(group.telegram_chat_id)
                    settings.night_mode_previous_permissions = (
                        chat.permissions.model_dump(exclude_none=True)
                        if chat.permissions
                        else None
                    )
                    await bot.set_chat_permissions(
                        group.telegram_chat_id, ChatPermissions(can_send_messages=False)
                    )
                    settings.night_mode_active = True
                except (TelegramBadRequest, TelegramForbiddenError) as exc:
                    log.warning(
                        "night_mode_permission_failed",
                        group_id=group.id,
                        error=str(exc),
                    )
            elif not should_lock and settings.night_mode_active:
                if not settings.lockdown_enabled:
                    previous = settings.night_mode_previous_permissions
                    permissions = ChatPermissions(**previous) if previous else UNMUTED
                    try:
                        await bot.set_chat_permissions(
                            group.telegram_chat_id, permissions
                        )
                    except (TelegramBadRequest, TelegramForbiddenError) as exc:
                        log.warning(
                            "night_mode_permission_failed",
                            group_id=group.id,
                            error=str(exc),
                        )
                settings.night_mode_active = False
                settings.night_mode_previous_permissions = None
        await session.commit()


async def run_group_automation(bot: Bot) -> None:
    """Run safe scheduled maintenance rules configured by a group owner.

    Telegram does not expose a complete member list, so deleted-account cleanup only
    operates on members Mimoru already knows and revalidates them before removal.
    """
    log = structlog.get_logger()
    async with SessionFactory() as session:
        now = datetime.now(UTC)
        groups = (
            await session.scalars(select(Group).where(Group.is_active.is_(True)))
        ).all()
        for group in groups:
            settings = group.settings
            if not settings.automation_enabled:
                continue
            schedule = settings.deleted_cleanup_schedule
            if schedule not in {"weekly", "monthly"}:
                continue
            interval = timedelta(days=7 if schedule == "weekly" else 30)
            last_run = settings.deleted_cleanup_last_run_at
            if last_run is not None and now - last_run < interval:
                continue
            try:
                scan = await scan_known_members(bot, session, group)
                cleanup = await remove_deleted_accounts(bot, session, group)
                settings.deleted_cleanup_last_run_at = now
                session.add(
                    AutomationLog(
                        group_id=group.id,
                        rule_code="deleted_cleanup",
                        status="ok",
                        details={
                            "checked": scan.checked,
                            "found": scan.deleted,
                            "removed": cleanup.removed,
                            "failed": cleanup.failed,
                        },
                    )
                )
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                session.add(
                    AutomationLog(
                        group_id=group.id,
                        rule_code="deleted_cleanup",
                        status="telegram_error",
                        details={"error": str(error)[:500]},
                    )
                )
                log.warning(
                    "automation_deleted_cleanup_failed",
                    group_id=group.id,
                    error=str(error),
                )
            except Exception as error:
                session.add(
                    AutomationLog(
                        group_id=group.id,
                        rule_code="deleted_cleanup",
                        status="error",
                        details={"error": str(error)[:500]},
                    )
                )
                log.exception(
                    "automation_deleted_cleanup_unexpected", group_id=group.id
                )
        await session.commit()


async def background_loop(bot: Bot, redis: Redis, stop_event: asyncio.Event) -> None:
    log = structlog.get_logger()
    while not stop_event.is_set():
        try:
            await expire_punishments(bot)
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
