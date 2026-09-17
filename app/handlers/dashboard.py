from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    AdOrder,
    AdPlacement,
    DailyStat,
    Group,
    GroupMember,
    GroupSubscriptionEvent,
    NewMemberRecord,
    Payment,
    Punishment,
    SupportTicket,
    User,
    Warning,
)
from app.keyboards.panel import (
    analytics_back,
    back_to_group,
    main_menu,
    report_settings_menu,
    service_menu,
    service_tickets_menu,
    stats_periods,
    support_menu,
    service_plan_group_menu,
)
from app.services.access import is_service_owner as access_service_owner
from app.services.analytics import compact_period_label, trend_text
from app.services.ui import panel_header
from app.services.plans import effective_plan, feature_available

router = Router(name=__name__)
settings = get_settings()


def is_service_owner(user_id: int) -> bool:
    return user_id in settings.service_owner_ids


async def owned_group(session: AsyncSession, group_id: int, user_id: int) -> Group | None:
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not access_service_owner(user_id):
        query = query.where(Group.owner_telegram_id == user_id)
    return await session.scalar(query)


def user_label(user_id: int, username: str | None, first_name: str | None) -> str:
    if username:
        return f"@{escape(username)}"
    if first_name:
        return escape(first_name)
    return f"<code>{user_id}</code>"


@router.callback_query(F.data == "panel:my_stats")
async def my_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    groups = (
        await session.scalars(
            select(Group).where(
                Group.owner_telegram_id == callback.from_user.id,
                Group.is_active.is_(True),
            )
        )
    ).all()
    group_ids = [group.id for group in groups]
    total_messages = 0
    active_users = 0
    if group_ids:
        total_messages = int(
            await session.scalar(
                select(func.coalesce(func.sum(DailyStat.messages_count), 0)).where(
                    DailyStat.group_id.in_(group_ids)
                )
            )
            or 0
        )
        active_users = int(
            await session.scalar(
                select(func.count(func.distinct(DailyStat.user_telegram_id))).where(
                    DailyStat.group_id.in_(group_ids), DailyStat.messages_count > 0
                )
            )
            or 0
        )
    deleted_accounts = 0
    if group_ids:
        deleted_accounts = int(
            await session.scalar(
                select(func.count()).select_from(GroupMember).where(
                    GroupMember.group_id.in_(group_ids),
                    GroupMember.is_present.is_(True),
                    GroupMember.is_deleted_account.is_(True),
                )
            )
            or 0
        )
    orders_bought = int(
        await session.scalar(
            select(func.count()).select_from(AdOrder).where(
                AdOrder.buyer_telegram_id == callback.from_user.id
            )
        )
        or 0
    )
    orders_sold = int(
        await session.scalar(
            select(func.count()).select_from(AdOrder).where(
                AdOrder.seller_telegram_id == callback.from_user.id
            )
        )
        or 0
    )
    text = (
        panel_header("Общая аналитика")
        + f"\n\n🏠 Подключённых групп: {len(groups)}\n"
        f"💬 Сообщений собрано: {total_messages}\n"
        f"👥 Активных участников: {active_users}\n"
        f"🪦 Удалённых аккаунтов: {deleted_accounts}\n"
        f"🛒 Рекламных заказов куплено: {orders_bought}\n"
        f"💼 Рекламных заказов получено: {orders_sold}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=main_menu(is_service_owner(callback.from_user.id)),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^stats:\d+:(1|7|30)$"))
async def group_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group_id, raw_days = callback.data.split(":")
    group = await owned_group(session, int(raw_group_id), callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    days = int(raw_days)
    now = datetime.now(timezone.utc)
    start_date = (now.date() - timedelta(days=days - 1)).isoformat()
    start_dt = now - timedelta(days=days)
    previous_start_date = (now.date() - timedelta(days=(days * 2) - 1)).isoformat()
    previous_end_date = (now.date() - timedelta(days=days)).isoformat()

    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(DailyStat.messages_count), 0),
                func.coalesce(func.sum(DailyStat.deleted_count), 0),
                func.count(func.distinct(DailyStat.user_telegram_id)),
            ).where(DailyStat.group_id == group.id, DailyStat.date >= start_date)
        )
    ).one()

    previous_totals = await _period_totals(session, group.id, previous_start_date, previous_end_date)

    joined = int(
        await session.scalar(
            select(func.count())
            .select_from(NewMemberRecord)
            .where(
                NewMemberRecord.group_id == group.id,
                NewMemberRecord.joined_at >= start_dt,
            )
        )
        or 0
    )
    warnings = int(
        await session.scalar(
            select(func.count())
            .select_from(Warning)
            .where(Warning.group_id == group.id, Warning.created_at >= start_dt)
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
                Punishment.created_at >= start_dt,
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
                Punishment.created_at >= start_dt,
            )
        )
        or 0
    )

    deleted_accounts = int(
        await session.scalar(
            select(func.count()).select_from(GroupMember).where(
                GroupMember.group_id == group.id,
                GroupMember.is_present.is_(True),
                GroupMember.is_deleted_account.is_(True),
            )
        )
        or 0
    )

    top = (
        await session.execute(
            select(
                DailyStat.user_telegram_id,
                User.username,
                User.first_name,
                func.sum(DailyStat.messages_count).label("cnt"),
            )
            .outerjoin(User, User.telegram_id == DailyStat.user_telegram_id)
            .where(
                DailyStat.group_id == group.id,
                DailyStat.date >= start_date,
                DailyStat.messages_count > 0,
            )
            .group_by(DailyStat.user_telegram_id, User.username, User.first_name)
            .order_by(func.sum(DailyStat.messages_count).desc())
            .limit(10)
        )
    ).all()
    lines = [
        f"{index}. {user_label(uid, username, first_name)} — {int(count)}"
        for index, (uid, username, first_name, count) in enumerate(top, 1)
    ]

    text = (
        panel_header("Аналитика группы", group.title)
        + f"\n\nПериод: {days} дн.\n\n"
        f"💬 Сообщений: {int(totals[0])} · {trend_text(int(totals[0]), previous_totals[0])}\n"
        f"👥 Активных участников: {int(totals[2])} · {trend_text(int(totals[2]), previous_totals[2])}\n"
        f"🆕 Новых участников: {joined}\n"
        f"🗑 Удалено сообщений: {int(totals[1])}\n"
        f"🪦 Удалённых аккаунтов: {deleted_accounts}\n"
        f"⚠️ Предупреждений: {warnings}\n"
        f"🔇 Мутов: {mutes}\n"
        f"⛔ Банов: {bans}\n\n"
        "<b>🏆 Топ участников</b>\n"
        + ("\n".join(lines) if lines else "Данных пока нет.")
    )
    await callback.message.edit_text(text, reply_markup=stats_periods(group.id))
    await callback.answer()


async def _period_totals(session: AsyncSession, group_id: int, start_date: str, end_date: str | None = None) -> tuple[int, int, int]:
    filters = [DailyStat.group_id == group_id, DailyStat.date >= start_date]
    if end_date is not None:
        filters.append(DailyStat.date <= end_date)
    row = (await session.execute(select(
        func.coalesce(func.sum(DailyStat.messages_count), 0),
        func.coalesce(func.sum(DailyStat.deleted_count), 0),
        func.count(func.distinct(DailyStat.user_telegram_id)),
    ).where(*filters))).one()
    return int(row[0]), int(row[1]), int(row[2])


@router.callback_query(F.data.regexp(r"^analytics:\d+:(activity|moderation|growth|reports)$"))
async def analytics_section(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group_id, section = callback.data.split(":")
    group = await owned_group(session, int(raw_group_id), callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    now = datetime.now(timezone.utc)
    start_30 = (now.date() - timedelta(days=29)).isoformat()

    if section == "activity":
        rows = (await session.execute(
            select(
                DailyStat.user_telegram_id, User.username, User.first_name,
                func.sum(DailyStat.messages_count).label("cnt"),
                func.sum(DailyStat.deleted_count).label("deleted"),
            )
            .outerjoin(User, User.telegram_id == DailyStat.user_telegram_id)
            .where(DailyStat.group_id == group.id, DailyStat.date >= start_30, DailyStat.messages_count > 0)
            .group_by(DailyStat.user_telegram_id, User.username, User.first_name)
            .order_by(func.sum(DailyStat.messages_count).desc())
            .limit(20)
        )).all()
        lines = [
            f"{i}. {user_label(uid, username, first_name)} — {int(cnt)} сообщ. · удалено {int(deleted)}"
            for i, (uid, username, first_name, cnt, deleted) in enumerate(rows, 1)
        ]
        text = panel_header("Активность участников", "Последние 30 дней") + "\n\n" + ("\n".join(lines) if lines else "Данных пока нет.")
        markup = analytics_back(group.id)

    elif section == "moderation":
        start_dt = now - timedelta(days=30)
        warnings = int(await session.scalar(select(func.count()).select_from(Warning).where(Warning.group_id == group.id, Warning.created_at >= start_dt)) or 0)
        punishments = (await session.execute(
            select(Punishment.kind, func.count()).where(Punishment.group_id == group.id, Punishment.created_at >= start_dt).group_by(Punishment.kind)
        )).all()
        by_kind = {str(kind): int(count) for kind, count in punishments}
        active_warnings = int(await session.scalar(select(func.count()).select_from(Warning).where(Warning.group_id == group.id, Warning.active.is_(True))) or 0)
        active_mutes = int(await session.scalar(select(func.count()).select_from(Punishment).where(Punishment.group_id == group.id, Punishment.kind == "mute", Punishment.active.is_(True))) or 0)
        active_bans = int(await session.scalar(select(func.count()).select_from(Punishment).where(Punishment.group_id == group.id, Punishment.kind == "ban", Punishment.active.is_(True))) or 0)
        text = (panel_header("Модерация", "Последние 30 дней") +
                f"\n\n⚠️ Предупреждений выдано: {warnings}" +
                f"\n🔇 Мутов: {by_kind.get('mute', 0)}" +
                f"\n🚪 Исключений: {by_kind.get('kick', 0)}" +
                f"\n⛔ Блокировок: {by_kind.get('ban', 0)}" +
                "\n\n<b>Сейчас активно</b>" +
                f"\n⚠️ Предупреждений: {active_warnings}" +
                f"\n🔇 Мутов: {active_mutes}" +
                f"\n⛔ Блокировок: {active_bans}")
        markup = analytics_back(group.id)

    elif section == "growth":
        today = now.date()
        current_start = (today - timedelta(days=6)).isoformat()
        previous_start = (today - timedelta(days=13)).isoformat()
        previous_end = (today - timedelta(days=7)).isoformat()
        current_messages, _, current_active = await _period_totals(session, group.id, current_start)
        previous_messages, _, previous_active = await _period_totals(session, group.id, previous_start, previous_end)
        joined_current = int(await session.scalar(select(func.count()).select_from(NewMemberRecord).where(NewMemberRecord.group_id == group.id, NewMemberRecord.joined_at >= now - timedelta(days=7))) or 0)
        joined_previous = int(await session.scalar(select(func.count()).select_from(NewMemberRecord).where(NewMemberRecord.group_id == group.id, NewMemberRecord.joined_at >= now - timedelta(days=14), NewMemberRecord.joined_at < now - timedelta(days=7))) or 0)
        text = (panel_header("Динамика группы", "Текущие 7 дней против предыдущих 7") +
                f"\n\n💬 Сообщения: {current_messages} · {trend_text(current_messages, previous_messages)}" +
                f"\n👥 Активные: {current_active} · {trend_text(current_active, previous_active)}" +
                f"\n🆕 Новые участники: {joined_current} · {trend_text(joined_current, joined_previous)}" +
                f"\n\nПредыдущий период: {previous_messages} сообщ., {previous_active} активных, {joined_previous} новых.")
        markup = analytics_back(group.id)

    else:
        status = "включён" if group.settings.reports_enabled else "выключен"
        text = (panel_header("Автоматические отчёты", group.title) +
                f"\n\n📬 Ежедневный отчёт: <b>{status}</b>" +
                f"\n🕒 Время: <b>{group.settings.report_hour_utc:02d}:00</b>" +
                f"\n🌍 Часовой пояс: <code>{escape(group.settings.timezone_name)}</code>" +
                "\n\nОтчёт приходит владельцу группы в личные сообщения и содержит активность, рост и модерацию за предыдущий день.")
        markup = report_settings_menu(group)

    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^analytics_report_toggle:\d+$"))
async def analytics_report_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if not feature_available(group, "daily_reports"):
        await callback.answer("Ежедневные отчёты доступны в STANDARD и PRO.", show_alert=True)
        return
    group.settings.reports_enabled = not group.settings.reports_enabled
    group.settings.last_report_date = None
    await session.commit()
    await callback.message.edit_reply_markup(reply_markup=report_settings_menu(group))
    await callback.answer("Ежедневный отчёт включён." if group.settings.reports_enabled else "Ежедневный отчёт выключен.")


@router.callback_query(F.data.regexp(r"^analytics_report_hour:\d+:(6|8|12|18|21)$"))
async def analytics_report_hour(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group_id, raw_hour = callback.data.split(":")
    group = await owned_group(session, int(raw_group_id), callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if not feature_available(group, "daily_reports"):
        await callback.answer("Настройка отчётов доступна в STANDARD и PRO.", show_alert=True)
        return
    group.settings.report_hour_utc = int(raw_hour)
    group.settings.last_report_date = None
    await session.commit()
    await callback.message.edit_reply_markup(reply_markup=report_settings_menu(group))
    await callback.answer(f"Время отчёта: {int(raw_hour):02d}:00")


@router.callback_query(F.data.regexp(r"^members_stats:\d+$"))
async def members_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await owned_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    start = (datetime.now(timezone.utc).date() - timedelta(days=29)).isoformat()
    rows = (
        await session.execute(
            select(
                DailyStat.user_telegram_id,
                User.username,
                User.first_name,
                func.sum(DailyStat.messages_count).label("cnt"),
                func.sum(DailyStat.deleted_count).label("deleted"),
            )
            .outerjoin(User, User.telegram_id == DailyStat.user_telegram_id)
            .where(DailyStat.group_id == group.id, DailyStat.date >= start)
            .group_by(DailyStat.user_telegram_id, User.username, User.first_name)
            .order_by(func.sum(DailyStat.messages_count).desc())
            .limit(30)
        )
    ).all()
    lines = [
        f"• {user_label(uid, username, first_name)}: {int(count)} сообщ., удалено {int(deleted)}"
        for uid, username, first_name, count, deleted in rows
    ]
    text = panel_header("Активность участников", "Последние 30 дней") + "\n\n"
    text += "\n".join(lines) if lines else "Данных пока нет."
    text += (
        "\n\nДля конкретного участника ответьте на его сообщение командой "
        "<code>статистика участника</code>."
    )
    await callback.message.edit_text(text, reply_markup=back_to_group(group.id))
    await callback.answer()


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.casefold() == "статистика участника",
)
async def specific_member_stats(message: Message, session: AsyncSession) -> None:
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Ответьте этой командой на сообщение нужного участника.")
        return
    group = await session.scalar(
        select(Group).where(
            Group.telegram_chat_id == message.chat.id,
            Group.is_active.is_(True),
        )
    )
    if group is None:
        return
    target = message.reply_to_message.from_user
    rows = (
        await session.execute(
            select(
                func.coalesce(func.sum(DailyStat.messages_count), 0),
                func.coalesce(func.sum(DailyStat.deleted_count), 0),
            ).where(
                DailyStat.group_id == group.id,
                DailyStat.user_telegram_id == target.id,
            )
        )
    ).one()
    warnings = int(
        await session.scalar(
            select(func.count())
            .select_from(Warning)
            .where(
                Warning.group_id == group.id,
                Warning.user_telegram_id == target.id,
                Warning.active.is_(True),
            )
        )
        or 0
    )
    await message.reply(
        f"<b>📊 Статистика {escape(target.full_name)}</b>\n\n"
        f"🆔 ID: <code>{target.id}</code>\n"
        f"💬 Сообщений: {int(rows[0])}\n"
        f"🗑 Удалено: {int(rows[1])}\n"
        f"⚠️ Активных предупреждений: {warnings}"
    )


@router.callback_query(F.data == "service:home")
async def service_home(callback: CallbackQuery) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header("Управление сервисом"),
        reply_markup=service_menu(),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^service:(clients|groups|billing|ads|tickets|stats)$"))
async def service_sections(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    section = callback.data.split(":")[1]
    if section == "clients":
        total = int(await session.scalar(select(func.count()).select_from(User)) or 0)
        blocked = int(
            await session.scalar(
                select(func.count()).select_from(User).where(User.service_blocked.is_(True))
            )
            or 0
        )
        text = (
            panel_header("Клиенты")
            + f"\n\nВсего: {total}\nЗаблокировано: {blocked}\n\n"
            "Команды: <code>клиенты</code>, <code>заблокировать клиента ID</code>."
        )
    elif section == "groups":
        total = int(await session.scalar(select(func.count()).select_from(Group)) or 0)
        active = int(
            await session.scalar(
                select(func.count()).select_from(Group).where(Group.is_active.is_(True))
            )
            or 0
        )
        paid = int(
            await session.scalar(
                select(func.count())
                .select_from(Group)
                .where(Group.plan_code.in_(["standard", "pro"]))
            )
            or 0
        )
        latest = (
            await session.scalars(select(Group).order_by(Group.created_at.desc()).limit(10))
        ).all()
        listing = "\n".join(
            f"• #{group.id} {escape(group.title)} — {'активна' if group.is_active else 'отключена'}"
            for group in latest
        )
        text = (
            panel_header("Группы")
            + f"\n\nВсего: {total}\nАктивных: {active}\nПлатных: {paid}\n\n"
            "<b>Последние группы</b>\n"
            + (listing or "Нет групп.")
        )
    elif section == "billing":
        paid_count = int(
            await session.scalar(
                select(func.count()).select_from(Payment).where(Payment.status == "paid")
            )
            or 0
        )
        plan_stars = int(
            await session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.status == "paid"
                )
            )
            or 0
        )
        ad_stars = int(
            await session.scalar(
                select(func.coalesce(func.sum(AdOrder.price_stars), 0)).where(
                    AdOrder.status.in_(["paid", "published"])
                )
            )
            or 0
        )
        text = (
            panel_header("Платежи")
            + f"\n\nУспешных оплат тарифов: {paid_count}\n"
            f"Stars за тарифы: {plan_stars}\n"
            f"Stars за рекламу: {ad_stars}"
        )
    elif section == "ads":
        placements = int(
            await session.scalar(
                select(func.count())
                .select_from(AdPlacement)
                .where(AdPlacement.active.is_(True))
            )
            or 0
        )
        orders = int(await session.scalar(select(func.count()).select_from(AdOrder)) or 0)
        pending = int(
            await session.scalar(
                select(func.count())
                .select_from(AdOrder)
                .where(AdOrder.status == "pending")
            )
            or 0
        )
        paid = int(
            await session.scalar(
                select(func.count())
                .select_from(AdOrder)
                .where(AdOrder.status.in_(["paid", "published"]))
            )
            or 0
        )
        text = (
            panel_header("Реклама")
            + f"\n\nАктивных площадок: {placements}\n"
            f"Всего заказов: {orders}\n"
            f"Ожидают решения: {pending}\n"
            f"Оплачено: {paid}"
        )
    elif section == "tickets":
        rows = (await session.scalars(
            select(SupportTicket).order_by(SupportTicket.created_at.desc()).limit(30)
        )).all()
        new = sum(1 for ticket in rows if ticket.status == "new")
        text = panel_header("Поддержка", f"Новых обращений: {new}. Выберите обращение.")
        await callback.message.edit_text(text, reply_markup=await service_tickets_menu(rows))
        await callback.answer()
        return
    else:
        users = int(await session.scalar(select(func.count()).select_from(User)) or 0)
        groups = int(
            await session.scalar(
                select(func.count()).select_from(Group).where(Group.is_active.is_(True))
            )
            or 0
        )
        messages = int(
            await session.scalar(select(func.coalesce(func.sum(DailyStat.messages_count), 0)))
            or 0
        )
        text = (
            panel_header("Статистика сервиса")
            + f"\n\nПользователей: {users}\n"
            f"Активных групп: {groups}\n"
            f"Обработано сообщений: {messages}"
        )
    await callback.message.edit_text(text, reply_markup=service_menu())
    await callback.answer()


@router.callback_query(F.data == "service:subscriptions")
async def service_subscriptions(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    now = datetime.now(timezone.utc)
    in_7 = now + timedelta(days=7)
    active = int(await session.scalar(select(func.count()).select_from(Group).where(
        Group.is_active.is_(True), Group.plan_code.in_(["standard", "pro", "trial"]), Group.plan_expires_at > now
    )) or 0)
    expiring = int(await session.scalar(select(func.count()).select_from(Group).where(
        Group.is_active.is_(True), Group.plan_expires_at > now, Group.plan_expires_at <= in_7
    )) or 0)
    revenue = int(await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid", Payment.currency == "XTR")) or 0)
    rows = (await session.scalars(select(Group).where(Group.is_active.is_(True)).order_by(Group.plan_expires_at.asc().nullslast()).limit(12))).all()
    kb_rows = [[InlineKeyboardButton(text=f"{g.title[:28]} · {effective_plan(g).upper()}", callback_data=f"service_plan:{g.id}")] for g in rows]
    kb_rows.append([InlineKeyboardButton(text="◀️ Панель Mimoru", callback_data="service:home")])
    text = panel_header("Управление тарифами", "Быстрый контроль подписок клиентов") + f"\n\n✅ Активных подписок: {active}\n⌛ Истекают ≤7 дней: {expiring}\n⭐ Получено за тарифы: {revenue}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)); await callback.answer()


@router.callback_query(F.data.regexp(r"^service_plan:\d+$"))
async def service_plan_group(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    group = await session.get(Group, int(callback.data.split(":")[1]))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True); return
    expires = group.plan_expires_at.strftime("%d.%m.%Y %H:%M UTC") if group.plan_expires_at else "без срока"
    text = panel_header("Тариф группы", group.title) + f"\n\nID: <code>{group.id}</code>\nВладелец: <code>{group.owner_telegram_id}</code>\nТариф: <b>{effective_plan(group).upper()}</b>\nСрок: {expires}"
    await callback.message.edit_text(text, reply_markup=service_plan_group_menu(group.id)); await callback.answer()


@router.callback_query(F.data.regexp(r"^service_plan_grant:\d+:(free|trial|standard|pro):(0|7|30)$"))
async def service_plan_grant(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_service_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    _, raw_gid, plan_code, raw_days = callback.data.split(":")
    group = await session.get(Group, int(raw_gid))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True); return
    days = int(raw_days); now = datetime.now(timezone.utc)
    if plan_code == "free":
        group.plan_code = "free"; group.plan_expires_at = None
    else:
        start = group.plan_expires_at if group.plan_expires_at and group.plan_expires_at > now else now
        group.plan_code = plan_code; group.plan_expires_at = start + timedelta(days=days)
    session.add(GroupSubscriptionEvent(group_id=group.id, actor_telegram_id=callback.from_user.id, event_type="admin_grant", plan_code=plan_code, expires_at=group.plan_expires_at))
    await session.commit()
    await service_plan_group(callback, session)


@router.callback_query(F.data == "panel:plans")
async def plans_screen(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        panel_header("Тарифы")
        + "\n\n<b>FREE</b> — базовая модерация.\n\n"
        "<b>STANDARD · 250 ⭐ / 30 дней</b>\n"
        "Расширенная защита, аналитика, реклама и увеличенные лимиты.\n\n"
        "<b>PRO · 500 ⭐ / 30 дней</b>\n"
        "Максимальные лимиты и все коммерческие функции.\n\n"
        "Для покупки выберите группу и отправьте в личном чате:\n"
        "<code>купить standard ID_ГРУППЫ</code> или <code>купить pro ID_ГРУППЫ</code>.",
        reply_markup=main_menu(is_service_owner(callback.from_user.id)),
    )
    await callback.answer()


@router.callback_query(F.data == "panel:support")
async def support_screen(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        panel_header("Поддержка", "Создайте обращение или посмотрите предыдущие."),
        reply_markup=support_menu(),
    )
    await callback.answer()
