from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.fun_models import GameEvent
from app.db.models import Complaint, DailyStat, Group, GroupMember, Punishment, User, Warning
from app.services.rank_access import get_actor_rank_with_access


router = Router(name=__name__)
GROUP_TYPES = {"group", "supergroup"}
CALLBACK_PREFIX = "group_stats_v2"
ADMIN_RANKS = {"owner", "service_owner", "deputy_owner", "chief_admin", "chat_admin", "voice_admin"}

ALIASES = {
    "стата", "статистика", "статистика группы", "стата группы", "чат инфо", "инфа чата",
    "активность чата", "активность группы",
    "стата сутки", "статистика сутки", "стата за сутки", "стата день",
    "стата неделя", "статистика неделя", "стата за неделю", "статистика за неделю",
    "стата месяц", "статистика месяц", "стата за месяц", "статистика за месяц",
    "топ 10", "топ10", "топ 20", "топ20", "топ 30", "топ30",
}

PERIOD_LABELS = {
    "day": "за сутки",
    "week": "за 7 дней",
    "month": "за 30 дней",
    "all": "за всё время",
}


def _norm(text: str | None) -> str:
    return " ".join((text or "").casefold().strip().split())


def _period_start(period: str) -> tuple[str | None, datetime | None]:
    now = datetime.now(timezone.utc)
    today = now.date()
    if period == "day":
        return today.isoformat(), datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    if period == "week":
        day = today - timedelta(days=6)
        return day.isoformat(), datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    if period == "month":
        day = today - timedelta(days=29)
        return day.isoformat(), datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    return None, None


async def _group(session: AsyncSession, chat_id: int) -> Group | None:
    return await session.scalar(
        select(Group).where(Group.telegram_chat_id == chat_id, Group.is_active.is_(True))
    )


async def _allowed(
    bot: Bot,
    session: AsyncSession,
    group: Group,
    user_id: int,
) -> bool:
    actor = await get_actor_rank_with_access(bot, session, group, user_id)
    return bool(actor is not None and actor.code in ADMIN_RANKS)


async def _user_label(session: AsyncSession, user_id: int) -> str:
    user = await session.scalar(select(User).where(User.telegram_id == user_id))
    if user is None:
        return f"ID {user_id}"
    if user.username:
        return f"@{user.username}"
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return name or f"ID {user_id}"


def _keyboard(
    group_id: int,
    requester_id: int,
    *,
    period: str,
    mode: str = "summary",
    limit: int = 10,
    private: bool = False,
) -> InlineKeyboardMarkup:
    def period_button(code: str, label: str) -> InlineKeyboardButton:
        mark = "• " if mode == "summary" and period == code else ""
        return InlineKeyboardButton(
            text=f"{mark}{label}",
            callback_data=f"{CALLBACK_PREFIX}:{group_id}:{requester_id}:summary:{code}:10",
        )

    def top_button(value: int) -> InlineKeyboardButton:
        mark = "• " if mode == "top" and limit == value else ""
        return InlineKeyboardButton(
            text=f"{mark}Топ {value}",
            callback_data=f"{CALLBACK_PREFIX}:{group_id}:{requester_id}:top:{period}:{value}",
        )

    rows = [
        [period_button("day", "☀️ Сутки"), period_button("week", "📅 Неделя")],
        [period_button("month", "🗓 Месяц"), period_button("all", "♾ Всё время")],
        [top_button(10)],
        [top_button(20), top_button(30)],
    ]
    if private:
        rows.append([InlineKeyboardButton(text="◀️ Назад к группе", callback_data=f"group:{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _daily_totals(session: AsyncSession, group_id: int, period: str) -> tuple[int, int, int]:
    start_day, _ = _period_start(period)
    query = select(
        func.coalesce(func.sum(DailyStat.messages_count), 0),
        func.coalesce(func.sum(DailyStat.deleted_count), 0),
        func.count(func.distinct(DailyStat.user_telegram_id)).filter(DailyStat.messages_count > 0),
    ).where(DailyStat.group_id == group_id)
    if start_day is not None:
        query = query.where(DailyStat.date >= start_day)
    row = (await session.execute(query)).one()
    return int(row[0]), int(row[1]), int(row[2])


async def _count_period(session: AsyncSession, model, group_id: int, period: str, *conditions) -> int:
    _, start_dt = _period_start(period)
    query = select(func.count()).select_from(model).where(model.group_id == group_id, *conditions)
    if start_dt is not None:
        query = query.where(model.created_at >= start_dt)
    return int(await session.scalar(query) or 0)


async def _summary_text(session: AsyncSession, group: Group, period: str) -> str:
    messages, deleted, active = await _daily_totals(session, group.id, period)
    known_present = int(
        await session.scalar(
            select(func.count()).select_from(GroupMember).where(
                GroupMember.group_id == group.id,
                GroupMember.is_present.is_(True),
            )
        )
        or 0
    )
    warnings = await _count_period(session, Warning, group.id, period)
    mutes = await _count_period(session, Punishment, group.id, period, Punishment.kind == "mute")
    bans = await _count_period(session, Punishment, group.id, period, Punishment.kind == "ban")
    complaints = await _count_period(session, Complaint, group.id, period)
    games = await _count_period(session, GameEvent, group.id, period)

    top = await _top_rows(session, group.id, period, 5)
    lines = [
        "📊 СТАТИСТИКА ГРУППЫ",
        f"🏠 {group.title}",
        f"🕒 Период: {PERIOD_LABELS[period]}",
        "",
        f"👥 Сейчас известно в группе: {known_present}",
        f"✍️ Активных авторов: {active}",
        f"💬 Сообщений: {messages}",
        f"🗑 Удалено сообщений: {deleted}",
        "",
        "⚖️ МОДЕРАЦИЯ",
        f"⚠️ Предупреждений: {warnings}",
        f"🔇 Мутов: {mutes}",
        f"🚫 Банов: {bans}",
        f"🚨 Жалоб: {complaints}",
        "",
        f"🎮 Игровых событий: {games}",
    ]
    if top:
        lines += ["", "🔥 Самые активные:"]
        for index, (user_id, count) in enumerate(top, start=1):
            lines.append(f"{index}. {await _user_label(session, user_id)} — {count}")
    lines += ["", "Статистика относится только к этой группе."]
    return "\n".join(lines)


async def _top_rows(session: AsyncSession, group_id: int, period: str, limit: int) -> list[tuple[int, int]]:
    start_day, _ = _period_start(period)
    total = func.sum(DailyStat.messages_count)
    query = select(DailyStat.user_telegram_id, total.label("total")).where(
        DailyStat.group_id == group_id,
        DailyStat.messages_count > 0,
    )
    if start_day is not None:
        query = query.where(DailyStat.date >= start_day)
    rows = (
        await session.execute(
            query.group_by(DailyStat.user_telegram_id)
            .order_by(total.desc(), DailyStat.user_telegram_id)
            .limit(limit)
        )
    ).all()
    return [(int(user_id), int(count)) for user_id, count in rows]


async def _top_text(session: AsyncSession, group: Group, period: str, limit: int) -> str:
    rows = await _top_rows(session, group.id, period, limit)
    lines = [
        f"🏆 ТОП {limit} ПО АКТИВНОСТИ",
        f"🏠 {group.title}",
        f"🕒 Период: {PERIOD_LABELS[period]}",
        "",
    ]
    if not rows:
        lines.append("Пока нет учтённых сообщений за этот период.")
    else:
        for index, (user_id, count) in enumerate(rows, start=1):
            lines.append(f"{index}. {await _user_label(session, user_id)} — {count}")
    lines += ["", "В рейтинге учитываются люди и боты, которые писали в группе."]
    return "\n".join(lines)


async def _render(session: AsyncSession, group: Group, *, mode: str, period: str, limit: int) -> str:
    if mode == "top":
        return await _top_text(session, group, period, limit)
    return await _summary_text(session, group, period)


def _initial_view(text: str) -> tuple[str, str, int]:
    if "топ" in text:
        limit = 30 if "30" in text else 20 if "20" in text else 10
        return "top", "all", limit
    if "сут" in text or "день" in text:
        return "summary", "day", 10
    if "недел" in text:
        return "summary", "week", 10
    if "месяц" in text:
        return "summary", "month", 10
    return "summary", "all", 10


@router.message(F.chat.type.in_(GROUP_TYPES), F.text.casefold().in_(ALIASES))
async def group_stats(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    group = await _group(session, message.chat.id)
    if group is None:
        return
    if not await _allowed(bot, session, group, message.from_user.id):
        await message.reply(
            "📊 Общая статистика группы доступна только администрации Mimoru.\n"
            "Свою карточку можно посмотреть фразой «кто я» или «моя стата»."
        )
        return
    mode, period, limit = _initial_view(_norm(message.text))
    await message.reply(
        await _render(session, group, mode=mode, period=period, limit=limit),
        reply_markup=_keyboard(group.id, message.from_user.id, period=period, mode=mode, limit=limit),
    )


@router.callback_query(F.data.regexp(r"^group_section:\d+:analytics$"))
async def private_group_stats(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    if callback.message is None or callback.message.chat.type != "private":
        return
    group_id = int(callback.data.split(":")[1])
    group = await session.scalar(select(Group).where(Group.id == group_id, Group.is_active.is_(True)))
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    if not await _allowed(bot, session, group, callback.from_user.id):
        await callback.answer("У вас нет доступа к статистике этой группы.", show_alert=True)
        return
    await callback.message.edit_text(
        await _render(session, group, mode="summary", period="all", limit=10),
        reply_markup=_keyboard(
            group.id,
            callback.from_user.id,
            period="all",
            mode="summary",
            limit=10,
            private=True,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("group_stats_v2:"))
async def group_stats_callback(
    callback: CallbackQuery, bot: Bot, session: AsyncSession
) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 6:
        await callback.answer("Статистика устарела. Откройте её заново.", show_alert=True)
        return
    _, raw_group_id, raw_requester_id, mode, period, raw_limit = parts
    try:
        group_id = int(raw_group_id)
        requester_id = int(raw_requester_id)
        limit = int(raw_limit)
    except ValueError:
        await callback.answer("Статистика устарела. Откройте её заново.", show_alert=True)
        return
    if callback.from_user.id != requester_id:
        await callback.answer(
            "Не для тебя мать кнопки прислала, отдыхай! Выпей лучше валерьянки и узбогойся...",
            show_alert=True,
        )
        return
    if mode not in {"summary", "top"} or period not in PERIOD_LABELS or limit not in {10, 20, 30}:
        await callback.answer("Неизвестный раздел статистики.", show_alert=True)
        return
    group = await session.scalar(select(Group).where(Group.id == group_id, Group.is_active.is_(True)))
    if group is None:
        await callback.answer("Эта статистика больше недоступна.", show_alert=True)
        return
    is_private = callback.message.chat.type == "private"
    if not is_private and callback.message.chat.id != group.telegram_chat_id:
        await callback.answer("Эта статистика больше недоступна.", show_alert=True)
        return
    if not await _allowed(bot, session, group, callback.from_user.id):
        await callback.answer("У вас больше нет доступа к статистике группы.", show_alert=True)
        return
    text = await _render(session, group, mode=mode, period=period, limit=limit)
    try:
        await callback.message.edit_text(
            text,
            reply_markup=_keyboard(
                group.id,
                requester_id,
                period=period,
                mode=mode,
                limit=limit,
                private=is_private,
            ),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise
    await callback.answer()
