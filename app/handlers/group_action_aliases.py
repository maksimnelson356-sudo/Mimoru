from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.fun_models import GameEvent, GroupMarriage
from app.db.models import (
    Complaint,
    DailyStat,
    Group,
    GroupMember,
    Punishment,
    User,
    Warning,
)
from app.db.rank_models import RankAssignment
from app.handlers.group_commands import group_complaint
from app.services.public_identity import public_user_token
from app.services.ranks import RANK_CODES, RANK_LABELS, get_actor_rank

router = Router(name=__name__)
GROUP_TYPES = {"group", "supergroup"}

ADMIN_ROSTER_ALIASES = {
    "кто тут главный",
    "кто за порядком",
    "кто за старшего",
    "кто у руля",
    "местная власть",
    "хранители порядка",
    "кто тут рулит",
    "кто тут смотрит",
    "хозяева группы",
    "закон и порядок",
}
SELF_PROFILE_ALIASES = {
    "моё досье",
    "мое досье",
    "личное дело",
    "моя статистика",
    "мой профиль",
    "моя история",
    "мои данные",
    "что за мной",
    "проверить себя",
    "что обо мне",
    "мой архив",
    "кто я",
    "моя стата",
    "моя инфа",
    "инфа обо мне",
    "информация обо мне",
}
LOOKUP_ALIASES = {
    "пробить гражданина",
    "открыть досье",
    "личное дело",
    "проверить участника",
    "проверить гражданина",
    "поднять дело",
    "карточка участника",
    "что за ним",
    "история участника",
    "кто ты",
    "ты кто",
    "инфа",
    "информация",
}
BOT_INFO_ALIASES = {
    "кто ты",
    "ты кто",
    "что ты умеешь",
    "что за бот",
    "что ты за бот",
    "кто такая мимору",
    "что такое мимору",
    "мимору кто ты",
}
GROUP_STATS_ALIASES = {
    "стата",
    "статистика",
    "стата сутки",
    "статистика сутки",
    "стата за сутки",
    "стата день",
    "стата неделя",
    "статистика неделя",
    "стата за неделю",
    "статистика за неделю",
    "статистика группы",
    "стата группы",
    "чат инфо",
    "инфа чата",
    "активность чата",
    "активность группы",
    "топ 10",
    "топ10",
    "топ 20",
    "топ20",
    "топ 30",
    "топ30",
}
ALL_BANS_ALIASES = {
    "все забаненные",
    "кто в бане",
    "все изгнанные",
    "кого наказали",
    "клуб изгнанных",
    "кого проводили",
    "все баны",
    "список банов",
    "забаненные",
    "кто забанен",
}
ALL_MUTES_ALIASES = {
    "кто молчит",
    "все замученные",
    "все муты",
    "режим тишины",
    "тихий час",
    "обет молчания",
    "рот на замке",
    "голос на паузе",
    "все молчуны",
    "отдыхают молча",
    "кого приглушили",
}
ALL_WARNINGS_ALIASES = {
    "кого предупредили",
    "все замечания",
    "на карандаше",
    "получили звоночек",
    "кого предупреждали",
    "шаг до бана",
    "список проказников",
    "особое внимание",
    "все предупреждения",
    "предупреждения",
}
MY_BANS_ALIASES = {
    "кого я забанил",
    "кого я выгнал",
    "мои отпускники",
    "кого я проводил",
    "наказанные мной",
    "мои изгнанники",
    "мои забаненные",
    "мои баны",
    "кого я посадил",
    "кого я отправил",
}
MY_MUTES_ALIASES = {
    "кого я замутил",
    "мои молчуны",
    "кого я приглушил",
    "мой тихий час",
    "мои молчальники",
    "кого я заткнул",
    "поставил на паузу",
    "кого я утихомирил",
    "мои муты",
    "отправил помолчать",
}
MY_WARNINGS_ALIASES = {
    "кого я предупредил",
    "мои замечания",
    "мои карточки",
    "мои кандидаты",
    "мои звоночки",
    "я предупреждал",
    "мои предупреждения",
    "мои проказники",
    "мои выговоры",
    "на моём карандаше",
    "на моем карандаше",
}
REPORT_ALIASES = {
    "сдать нарушителя",
    "настучать наверх",
    "доложить старшим",
    "позвать смотрящего",
    "вызвать наряд",
    "есть вопросики",
    "сигнал наверх",
    "зовите начальство",
    "передать старшим",
    "тут ситуация",
}

ALL_ALIASES = (
    ADMIN_ROSTER_ALIASES
    | SELF_PROFILE_ALIASES
    | LOOKUP_ALIASES
    | BOT_INFO_ALIASES
    | GROUP_STATS_ALIASES
    | ALL_BANS_ALIASES
    | ALL_MUTES_ALIASES
    | ALL_WARNINGS_ALIASES
    | MY_BANS_ALIASES
    | MY_MUTES_ALIASES
    | MY_WARNINGS_ALIASES
    | REPORT_ALIASES
)

ADMIN_INFO_RANKS = {
    "owner",
    "service_owner",
    "deputy_owner",
    "chief_admin",
    "chat_admin",
    "voice_admin",
}
PROFILE_CALLBACK_PREFIX = "member_profile"


def _norm(message: Message) -> str:
    return " ".join((message.text or "").casefold().strip().split())


async def _active_group(session: AsyncSession, chat_id: int) -> Group | None:
    return await session.scalar(
        select(Group).where(
            Group.telegram_chat_id == chat_id, Group.is_active.is_(True)
        )
    )


async def _user(session: AsyncSession, user_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == user_id))


async def _user_label(session: AsyncSession, user_id: int) -> str:
    return public_user_token(user_id)


async def _user_name(session: AsyncSession, user_id: int) -> str:
    return public_user_token(user_id)


async def _require_admin_info_access(
    message: Message, session: AsyncSession, group: Group
) -> bool:
    if message.from_user is None:
        return False
    actor = await get_actor_rank(session, group, message.from_user.id)
    if actor is not None and actor.code in ADMIN_INFO_RANKS:
        return True
    await message.reply(
        "Эта информация доступна только администрации Mimoru этой группы."
    )
    return False


async def _can_view_group_stats(
    message: Message, session: AsyncSession, group: Group
) -> bool:
    if await _require_admin_info_access(message, session, group):
        return True
    await message.reply(
        "Свою личную информацию можно посмотреть фразой «кто я» или «моя стата»."
    )
    return False


def _profile_keyboard(
    group_id: int, target_id: int, requester_id: int, active: str
) -> InlineKeyboardMarkup:
    def button(view: str, label: str) -> InlineKeyboardButton:
        prefix = "• " if view == active else ""
        return InlineKeyboardButton(
            text=f"{prefix}{label}",
            callback_data=f"{PROFILE_CALLBACK_PREFIX}:{group_id}:{target_id}:{requester_id}:{view}",
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("profile", "👤 Профиль"),
                button("history", "⚖️ История"),
                button("games", "🎮 Игры"),
            ]
        ]
    )


def _plural_messages(value: int) -> str:
    n = abs(value) % 100
    n1 = n % 10
    if 10 < n < 20:
        word = "сообщений"
    elif n1 == 1:
        word = "сообщение"
    elif 2 <= n1 <= 4:
        word = "сообщения"
    else:
        word = "сообщений"
    return f"{value} {word}"


def _ago_text(value: datetime | None) -> str:
    if value is None:
        return "неизвестно"
    now = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = now - value.astimezone(timezone.utc)
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return "только что"
    if seconds < 3600:
        minutes = max(1, seconds // 60)
        return f"{minutes} мин назад"
    if seconds < 86400:
        hours = max(1, seconds // 3600)
        return f"{hours} ч назад"
    days = seconds // 86400
    if days == 1:
        return "вчера"
    if days <= 7:
        return "на этой неделе"
    if days <= 31:
        return "в этом месяце"
    return value.astimezone(timezone.utc).strftime("%d.%m.%Y")


def _membership_age(value: datetime | None) -> str:
    if value is None:
        return "неизвестно"
    now = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    days = max(0, (now.date() - value.astimezone(timezone.utc).date()).days)
    years, remaining = divmod(days, 365)
    parts: list[str] = []
    if years:
        ending = (
            "год"
            if years % 10 == 1 and years % 100 != 11
            else (
                "года"
                if years % 10 in {2, 3, 4} and years % 100 not in {12, 13, 14}
                else "лет"
            )
        )
        parts.append(f"{years} {ending}")
    if remaining or not parts:
        parts.append(f"{remaining} дн")
    return " ".join(parts)


async def _marriage_line(session: AsyncSession, group_id: int, user_id: int) -> str:
    marriage = await session.scalar(
        select(GroupMarriage)
        .where(
            GroupMarriage.group_id == group_id,
            GroupMarriage.active.is_(True),
            or_(
                GroupMarriage.user1_telegram_id == user_id,
                GroupMarriage.user2_telegram_id == user_id,
            ),
        )
        .order_by(GroupMarriage.created_at.desc())
    )
    if marriage is None:
        return "💍 Брак: не состоит"
    partner_id = (
        marriage.user2_telegram_id
        if marriage.user1_telegram_id == user_id
        else marriage.user1_telegram_id
    )
    partner = await _user_label(session, partner_id)
    since = marriage.created_at.astimezone(timezone.utc).strftime("%d.%m.%Y")
    return f"💍 Брак: в браке с {partner} с {since}"


async def _message_periods(
    session: AsyncSession, group_id: int, user_id: int
) -> tuple[int, int, int, int, int]:
    today = datetime.now(timezone.utc).date()
    week = (today - timedelta(days=6)).isoformat()
    month = (today - timedelta(days=29)).isoformat()
    today_s = today.isoformat()
    rows = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(DailyStat.messages_count).filter(
                        DailyStat.date >= today_s
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(DailyStat.messages_count).filter(DailyStat.date >= week), 0
                ),
                func.coalesce(
                    func.sum(DailyStat.messages_count).filter(DailyStat.date >= month),
                    0,
                ),
                func.coalesce(func.sum(DailyStat.messages_count), 0),
                func.coalesce(func.sum(DailyStat.deleted_count), 0),
            ).where(
                DailyStat.group_id == group_id, DailyStat.user_telegram_id == user_id
            )
        )
    ).one()
    return tuple(int(value) for value in rows)  # type: ignore[return-value]


async def _active_punishment(
    session: AsyncSession, group_id: int, user_id: int, kind: str
) -> Punishment | None:
    return await session.scalar(
        select(Punishment)
        .where(
            Punishment.group_id == group_id,
            Punishment.user_telegram_id == user_id,
            Punishment.kind == kind,
            Punishment.active.is_(True),
        )
        .order_by(Punishment.created_at.desc())
    )


async def _profile_text(session: AsyncSession, group: Group, user_id: int) -> str:
    user_name = await _user_name(session, user_id)
    member = await session.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group.id, GroupMember.user_telegram_id == user_id
        )
    )
    assignment = await session.scalar(
        select(RankAssignment).where(
            RankAssignment.group_id == group.id,
            RankAssignment.user_telegram_id == user_id,
            RankAssignment.active.is_(True),
        )
    )
    if group.owner_telegram_id == user_id:
        role = "Владелец"
    else:
        role = (
            RANK_LABELS.get(assignment.rank_code, assignment.rank_code)
            if assignment
            else "Простой участник"
        )

    ban = await _active_punishment(session, group.id, user_id, "ban")
    if ban is not None:
        status = "🚫 Забанен в чате"
    elif member is not None and member.is_present:
        status = "🕵️ Состоит в чате"
    else:
        status = "🚪 Покинул чат"

    day_count, week_count, month_count, all_count, deleted = await _message_periods(
        session, group.id, user_id
    )
    marriage = await _marriage_line(session, group.id, user_id)
    last_active = _ago_text(member.last_seen_at if member is not None else None)
    joined_date = member.joined_at if member is not None else None
    joined = (
        "неизвестно"
        if joined_date is None
        else joined_date.astimezone(timezone.utc).strftime("%d.%m.%Y")
    )
    joined_age = _membership_age(joined_date)

    return (
        f"👤 Это пользователь {user_name}\n"
        f"{status}\n"
        f"🧠 Роль: {role}\n"
        f"{marriage}\n"
        f"⌛ Последний актив: {last_active}\n\n"
        f"💬 Сообщения: {all_count}\n"
        "день | неделя | месяц | всего\n"
        f"{day_count} | {week_count} | {month_count} | {all_count}\n\n"
        f"🗑 Удалено: {_plural_messages(deleted)}\n"
        f"📅 В группе с: {joined} ({joined_age})"
    )


async def _history_text(session: AsyncSession, group: Group, user_id: int) -> str:
    label = await _user_name(session, user_id)
    active_warnings = int(
        await session.scalar(
            select(func.count())
            .select_from(Warning)
            .where(
                Warning.group_id == group.id,
                Warning.user_telegram_id == user_id,
                Warning.active.is_(True),
            )
        )
        or 0
    )
    all_warnings = int(
        await session.scalar(
            select(func.count())
            .select_from(Warning)
            .where(Warning.group_id == group.id, Warning.user_telegram_id == user_id)
        )
        or 0
    )
    complaints = int(
        await session.scalar(
            select(func.count())
            .select_from(Complaint)
            .where(
                Complaint.group_id == group.id, Complaint.target_telegram_id == user_id
            )
        )
        or 0
    )
    mute = await _active_punishment(session, group.id, user_id, "mute")
    ban = await _active_punishment(session, group.id, user_id, "ban")

    def punishment_text(row: Punishment | None) -> str:
        if row is None:
            return "нет"
        if row.ends_at is None:
            return "да, без срока"
        return f"да, до {row.ends_at.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}"

    return (
        f"⚖️ ИСТОРИЯ — {label}\n\n"
        f"⚠️ Предупреждения: {active_warnings} активных / {all_warnings} всего\n"
        f"🔇 Мут: {punishment_text(mute)}\n"
        f"🚫 Бан: {punishment_text(ban)}\n"
        f"🚨 Жалобы: {complaints}\n\n"
        "Статистика относится только к этой группе."
    )


async def _games_text(session: AsyncSession, group: Group, user_id: int) -> str:
    label = await _user_name(session, user_id)
    made = int(
        await session.scalar(
            select(func.count(GameEvent.id)).where(
                GameEvent.group_id == group.id,
                GameEvent.event_type == "action",
                GameEvent.actor_telegram_id == user_id,
                GameEvent.outcome != "bot_wins",
            )
        )
        or 0
    )
    received = int(
        await session.scalar(
            select(func.count(GameEvent.id)).where(
                GameEvent.group_id == group.id,
                GameEvent.event_type == "action",
                GameEvent.target_telegram_id == user_id,
                GameEvent.outcome != "bot_wins",
            )
        )
        or 0
    )
    accepted_proposals = int(
        await session.scalar(
            select(func.count(GameEvent.id)).where(
                GameEvent.group_id == group.id,
                GameEvent.event_type == "proposal",
                GameEvent.target_telegram_id == user_id,
                GameEvent.outcome == "accepted",
            )
        )
        or 0
    )
    marriages = int(
        await session.scalar(
            select(func.count(GroupMarriage.id)).where(
                GroupMarriage.group_id == group.id,
                or_(
                    GroupMarriage.user1_telegram_id == user_id,
                    GroupMarriage.user2_telegram_id == user_id,
                ),
            )
        )
        or 0
    )
    bot_attacks = int(
        await session.scalar(
            select(func.count(GameEvent.id)).where(
                GameEvent.group_id == group.id,
                GameEvent.actor_telegram_id == user_id,
                or_(
                    GameEvent.event_type == "bot_attack",
                    GameEvent.outcome == "bot_wins",
                ),
            )
        )
        or 0
    )
    favorite_rows = (
        await session.execute(
            select(GameEvent.action, func.count(GameEvent.id))
            .where(
                GameEvent.group_id == group.id,
                GameEvent.event_type == "action",
                GameEvent.actor_telegram_id == user_id,
                GameEvent.outcome != "bot_wins",
            )
            .group_by(GameEvent.action)
            .order_by(func.count(GameEvent.id).desc(), GameEvent.action)
            .limit(5)
        )
    ).all()

    lines = [
        f"🎮 Игровая статистика — {label}",
        "",
        f"🎭 Действий совершено: {made}",
        f"🎯 Действий получено: {received}",
        f"💌 Принятых предложений: {accepted_proposals}",
        f"💍 Браков за всё время: {marriages}",
        f"🤖 Нападений на Mimoru: {bot_attacks}",
        "",
        "❤️ Любимые действия:",
    ]
    if favorite_rows:
        lines.extend(f"• {action} — {count}" for action, count in favorite_rows)
    else:
        lines.append("• пока нет")
    lines += ["", "Статистика относится только к этой группе."]
    return "\n".join(lines)


async def _profile_view_text(
    session: AsyncSession, group: Group, user_id: int, view: str
) -> str:
    if view == "history":
        return await _history_text(session, group, user_id)
    if view == "games":
        return await _games_text(session, group, user_id)
    return await _profile_text(session, group, user_id)


async def _send_profile(
    message: Message, session: AsyncSession, group: Group, target_id: int
) -> None:
    if message.from_user is None:
        return
    await message.reply(
        await _profile_text(session, group, target_id),
        reply_markup=_profile_keyboard(
            group.id, target_id, message.from_user.id, "profile"
        ),
    )


@router.callback_query(F.data.startswith(f"{PROFILE_CALLBACK_PREFIX}:"))
async def member_profile_tab(callback: CallbackQuery, session: AsyncSession) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer("Карточка устарела. Откройте её заново.", show_alert=True)
        return
    _, raw_group_id, raw_target_id, raw_requester_id, view = parts
    try:
        group_id = int(raw_group_id)
        target_id = int(raw_target_id)
        requester_id = int(raw_requester_id)
    except ValueError:
        await callback.answer("Карточка устарела. Откройте её заново.", show_alert=True)
        return
    if callback.from_user.id != requester_id:
        await callback.answer("Эта кнопка не для вас.", show_alert=True)
        return
    if view not in {"profile", "history", "games"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    group = await session.scalar(
        select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    )
    if group is None or callback.message.chat.id != group.telegram_chat_id:
        await callback.answer("Эта карточка больше недоступна.", show_alert=True)
        return
    text = await _profile_view_text(session, group, target_id, view)
    try:
        await callback.message.edit_text(
            text,
            reply_markup=_profile_keyboard(group.id, target_id, requester_id, view),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise
    await callback.answer()


async def _window_stats(
    session: AsyncSession, group_id: int, start_day: str | None
) -> tuple[int, int]:
    query = select(
        func.coalesce(func.sum(DailyStat.messages_count), 0),
        func.count(func.distinct(DailyStat.user_telegram_id)),
    ).where(DailyStat.group_id == group_id)
    if start_day is not None:
        query = query.where(DailyStat.date >= start_day)
    row = (await session.execute(query)).one()
    return int(row[0]), int(row[1])


async def _top_activity(
    session: AsyncSession,
    group_id: int,
    *,
    limit: int,
    start_day: str | None = None,
) -> list[tuple[int, int]]:
    total = func.sum(DailyStat.messages_count)
    query = select(DailyStat.user_telegram_id, total.label("total")).where(
        DailyStat.group_id == group_id
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
    return [(int(row[0]), int(row[1])) for row in rows]


async def _group_stats(
    message: Message, session: AsyncSession, group: Group, text: str
) -> None:
    now_day = datetime.now(timezone.utc).date()
    today = now_day.isoformat()
    week = (now_day - timedelta(days=6)).isoformat()

    if "топ" in text:
        limit = 30 if "30" in text else 20 if "20" in text else 10
        rows = await _top_activity(session, group.id, limit=limit)
        lines = [f"🏆 ТОП {limit} ПО АКТИВНОСТИ", "", f"🏠 {group.title}", ""]
        if not rows:
            lines.append("Пока нет учтённых сообщений.")
        else:
            for index, (user_id, count) in enumerate(rows, start=1):
                lines.append(
                    f"{index}. {await _user_label(session, user_id)} — {count} сообщений"
                )
        await message.reply("\n".join(lines))
        return

    if "сут" in text or "день" in text:
        total, active = await _window_stats(session, group.id, today)
        rows = await _top_activity(session, group.id, limit=10, start_day=today)
        lines = [
            "📊 СТАТИСТИКА ЗА СУТКИ",
            "",
            f"💬 Сообщений: {total}",
            f"👥 Писали сегодня: {active}",
        ]
        if rows:
            lines += ["", "🔥 Самые активные:"]
            for index, (user_id, count) in enumerate(rows, start=1):
                lines.append(
                    f"{index}. {await _user_label(session, user_id)} — {count}"
                )
        await message.reply("\n".join(lines))
        return

    if "недел" in text:
        total, active = await _window_stats(session, group.id, week)
        rows = await _top_activity(session, group.id, limit=10, start_day=week)
        lines = [
            "📊 СТАТИСТИКА ЗА 7 ДНЕЙ",
            "",
            f"💬 Сообщений: {total}",
            f"👥 Активных участников: {active}",
        ]
        if rows:
            lines += ["", "🔥 Самые активные:"]
            for index, (user_id, count) in enumerate(rows, start=1):
                lines.append(
                    f"{index}. {await _user_label(session, user_id)} — {count}"
                )
        await message.reply("\n".join(lines))
        return

    total_all, active_all = await _window_stats(session, group.id, None)
    total_today, active_today = await _window_stats(session, group.id, today)
    total_week, active_week = await _window_stats(session, group.id, week)
    present = int(
        await session.scalar(
            select(func.count())
            .select_from(GroupMember)
            .where(GroupMember.group_id == group.id, GroupMember.is_present.is_(True))
        )
        or 0
    )
    top = await _top_activity(session, group.id, limit=5)
    lines = [
        "📊 СТАТИСТИКА ГРУППЫ",
        "",
        f"🏠 {group.title}",
        f"👥 Участников известно сейчас: {present}",
        "",
        f"💬 Сообщений всего: {total_all}",
        f"☀️ За сутки: {total_today} · писали {active_today}",
        f"📅 За 7 дней: {total_week} · писали {active_week}",
        f"👤 Всего писали: {active_all}",
    ]
    if top:
        lines += ["", "🔥 ТОП-5 ПО СООБЩЕНИЯМ"]
        for index, (user_id, count) in enumerate(top, start=1):
            lines.append(f"{index}. {await _user_label(session, user_id)} — {count}")
    await message.reply("\n".join(lines))


async def _punishment_list(
    message: Message, session: AsyncSession, group: Group, *, kind: str, mine: bool
) -> None:
    query = select(Punishment).where(
        Punishment.group_id == group.id,
        Punishment.kind == kind,
        Punishment.active.is_(True),
    )
    if mine and message.from_user is not None:
        query = query.where(Punishment.moderator_telegram_id == message.from_user.id)
    rows = list(
        (
            await session.scalars(
                query.order_by(Punishment.created_at.desc()).limit(30)
            )
        ).all()
    )
    noun = "бане" if kind == "ban" else "муте"
    title = (
        ("🚫 Мои баны" if kind == "ban" else "🔇 Мои муты")
        if mine
        else ("🚫 Кто в бане" if kind == "ban" else "🔇 Кто в муте")
    )
    if not rows:
        await message.reply(f"{title}\n\nСейчас список пуст.")
        return
    lines = [title, ""]
    for index, row in enumerate(rows, start=1):
        target = await _user_label(session, row.user_telegram_id)
        moderator = await _user_label(session, row.moderator_telegram_id)
        until = (
            "без срока"
            if row.ends_at is None
            else row.ends_at.astimezone(timezone.utc).strftime("до %d.%m %H:%M UTC")
        )
        lines.append(f"{index}. {target} · {noun} · {until} · выдал {moderator}")
    await message.reply("\n".join(lines))


async def _warning_list(
    message: Message, session: AsyncSession, group: Group, *, mine: bool
) -> None:
    query = select(Warning).where(
        Warning.group_id == group.id, Warning.active.is_(True)
    )
    if mine and message.from_user is not None:
        query = query.where(Warning.moderator_telegram_id == message.from_user.id)
    rows = list(
        (
            await session.scalars(query.order_by(Warning.created_at.desc()).limit(30))
        ).all()
    )
    title = "⚠️ Мои предупреждения" if mine else "⚠️ Предупреждения группы"
    if not rows:
        await message.reply(f"{title}\n\nАктивных предупреждений нет.")
        return
    lines = [title, ""]
    for index, row in enumerate(rows, start=1):
        target = await _user_label(session, row.user_telegram_id)
        moderator = await _user_label(session, row.moderator_telegram_id)
        lines.append(f"{index}. {target} · {row.reason} · выдал {moderator}")
    await message.reply("\n".join(lines))


def _bot_info_text() -> str:
    return (
        "🤖 Я Mimoru — помощник этой группы.\n\n"
        "Помогаю с модерацией, жалобами, статистикой участников, ролями и развлечениями.\n"
        "Напиши «кто я» или «моя стата» — покажу твоё досье.\n"
        "Ответь «кто ты» или «инфа» на чужое сообщение — покажу карточку участника.\n"
        "Для полного списка возможностей используй /help или /commands."
    )


@router.message(F.chat.type.in_(GROUP_TYPES), F.text.casefold().in_(ALL_ALIASES))
async def readable_group_actions(
    message: Message, bot: Bot, session: AsyncSession
) -> None:
    text = _norm(message)
    group = await _active_group(session, message.chat.id)
    if group is None:
        return

    if text in LOOKUP_ALIASES and message.reply_to_message is not None:
        target = message.reply_to_message.from_user
        if target is None:
            await message.reply("Не удалось определить участника по этому сообщению.")
            return
        if target.id == bot.id:
            await message.reply(_bot_info_text())
            return
        await _send_profile(message, session, group, target.id)
        return

    if text in GROUP_STATS_ALIASES:
        if await _can_view_group_stats(message, session, group):
            await _group_stats(message, session, group, text)
        return

    if text in BOT_INFO_ALIASES:
        await message.reply(_bot_info_text())
        return

    if text in REPORT_ALIASES:
        if message.reply_to_message is None:
            await message.reply(
                "🚨 Пожаловаться\n\nОтветьте этой фразой на сообщение нарушителя. "
                "Жалоба будет передана администрации группы для проверки."
            )
            return
        await group_complaint(message, bot, session)
        return

    if text in SELF_PROFILE_ALIASES:
        if message.from_user is None:
            return
        await _send_profile(message, session, group, message.from_user.id)
        return

    if text in LOOKUP_ALIASES:
        await message.reply(
            "🔎 Проверить участника\n\nОтветьте этой фразой на сообщение нужного участника. "
            "Например: «кто ты», «ты кто», «инфа» или «проверить участника»."
        )
        return

    if text in ADMIN_ROSTER_ALIASES:
        rows = list(
            (
                await session.scalars(
                    select(RankAssignment).where(
                        RankAssignment.group_id == group.id,
                        RankAssignment.active.is_(True),
                    )
                )
            ).all()
        )
        lines = ["👑 Кто за порядком?"]
        if group.owner_telegram_id:
            lines += [
                "",
                "👑 Владелец",
                f"• {await _user_label(session, group.owner_telegram_id)}",
            ]
        by_rank: dict[str, list[RankAssignment]] = {code: [] for code in RANK_CODES}
        for row in rows:
            if row.rank_code in by_rank:
                by_rank[row.rank_code].append(row)
        for rank_code in RANK_CODES:
            assigned = by_rank[rank_code]
            if not assigned:
                continue
            lines += ["", RANK_LABELS.get(rank_code, rank_code)]
            for row in assigned:
                lines.append(f"• {await _user_label(session, row.user_telegram_id)}")
        await message.reply("\n".join(lines))
        return

    if not await _require_admin_info_access(message, session, group):
        return

    if text in ALL_BANS_ALIASES:
        await _punishment_list(message, session, group, kind="ban", mine=False)
    elif text in ALL_MUTES_ALIASES:
        await _punishment_list(message, session, group, kind="mute", mine=False)
    elif text in ALL_WARNINGS_ALIASES:
        await _warning_list(message, session, group, mine=False)
    elif text in MY_BANS_ALIASES:
        await _punishment_list(message, session, group, kind="ban", mine=True)
    elif text in MY_MUTES_ALIASES:
        await _punishment_list(message, session, group, kind="mute", mine=True)
    elif text in MY_WARNINGS_ALIASES:
        await _warning_list(message, session, group, mine=True)
