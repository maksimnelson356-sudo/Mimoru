from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyStat, Group, ModerationLog, User
from app.keyboards.home import automation_menu, moderation_menu, protection_menu
from app.services.access import is_service_owner
from app.services.public_identity import public_user_token
from app.services.ui import clean_ui_text, panel_header

router = Router(name=__name__)


async def _owned_group(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    *,
    for_update: bool = False,
) -> Group | None:
    query = select(Group).where(Group.id == group_id, Group.is_active.is_(True))
    if not is_service_owner(user_id):
        query = query.where(Group.owner_telegram_id == user_id)
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


def _warning_limit_menu(group_id: int, current: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if current == value else ''}{value}",
                callback_data=f"automation_warning_limit_set:{group_id}:{value}",
            )
        ]
        for value in (1, 2, 3, 4, 5)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="◀️ Назад к автоматизации", callback_data=f"automation:{group_id}"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _default_mute_menu(group_id: int, current: int) -> InlineKeyboardMarkup:
    variants = [
        (300, "5 мин"),
        (900, "15 мин"),
        (3600, "1 час"),
        (21600, "6 часов"),
        (86400, "24 часа"),
    ]
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if current == seconds else ''}{label}",
                callback_data=f"setting_set:{group_id}:defaultmute:{seconds}",
            )
        ]
        for seconds, label in variants
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="◀️ Назад к модерации",
                callback_data=f"group_section:{group_id}:moderation",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _antiflood_menu(
    group_id: int, current_limit: int, current_window: int
) -> InlineKeyboardMarkup:
    variants = [
        (4, 5, "Строго · 4 за 5с"),
        (6, 10, "Обычно · 6 за 10с"),
        (8, 15, "Мягко · 8 за 15с"),
    ]
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if (current_limit, current_window) == (limit, window) else ''}{label}",
                callback_data=f"setting_flood:{group_id}:{limit}:{window}",
            )
        ]
        for limit, window, label in variants
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="◀️ Назад к защите",
                callback_data=f"group_section:{group_id}:protection",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _setup_profile_menu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Сообщество",
                    callback_data=f"setup:{group_id}:type:community",
                ),
                InlineKeyboardButton(
                    text="🎮 Игры", callback_data=f"setup:{group_id}:type:gaming"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🪙 Крипта", callback_data=f"setup:{group_id}:type:crypto"
                ),
                InlineKeyboardButton(
                    text="🛍 Продажи", callback_data=f"setup:{group_id}:type:sales"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📰 Новости", callback_data=f"setup:{group_id}:type:news"
                ),
                InlineKeyboardButton(
                    text="🎓 Обучение", callback_data=f"setup:{group_id}:type:education"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✖️ Отмена", callback_data=f"group_section:{group_id}:settings"
                )
            ],
        ]
    )


def _back(callback_data: str, text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=callback_data)]]
    )


@router.callback_query(F.data.regexp(r"^setup:\d+:start$"))
async def setup_start_with_contextual_cancel(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await _owned_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer(
            "Настройка доступна только владельцу группы.", show_alert=True
        )
        return
    await callback.message.edit_text(
        panel_header("Мастер настройки", f"Группа: {clean_ui_text(group.title)}")
        + "\n\nMimoru применит безопасный стартовый профиль. После мастера любую настройку можно изменить отдельно."
        + "\n\nШаг 1 из 6 · Какой это тип сообщества?",
        reply_markup=_setup_profile_menu(group.id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^automation_warning_limit:\d+$"))
async def warning_limit(callback: CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await _owned_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        panel_header(
            "Лимит предупреждений",
            "После достижения этого количества Mimoru применяет настроенное автоматическое ограничение.",
        ),
        reply_markup=_warning_limit_menu(group.id, group.settings.warnings_limit),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^automation_warning_limit_set:\d+:[1-5]$"))
async def warning_limit_set(callback: CallbackQuery, session: AsyncSession) -> None:
    _, raw_group_id, raw_value = callback.data.split(":")
    group = await _owned_group(
        session, int(raw_group_id), callback.from_user.id, for_update=True
    )
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    group.settings.warnings_limit = int(raw_value)
    await session.commit()
    await callback.message.edit_text(
        panel_header("Автоматизация", "Лимит предупреждений сохранён."),
        reply_markup=automation_menu(group),
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data.regexp(r"^setting_num:\d+:(defaultmute|antiflood)$"))
async def contextual_setting_num(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    _, raw_group_id, field = callback.data.split(":")
    group = await _owned_group(session, int(raw_group_id), callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if field == "defaultmute":
        title = "Мут по умолчанию"
        markup = _default_mute_menu(group.id, group.settings.default_mute_seconds)
    else:
        title = "Профиль антифлуда"
        markup = _antiflood_menu(
            group.id,
            group.settings.antiflood_limit,
            group.settings.antiflood_window_seconds,
        )
    await callback.message.edit_text(
        panel_header(title, "Выберите значение."), reply_markup=markup
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^setting_set:\d+:defaultmute:\d+$"))
async def contextual_default_mute_set(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    _, raw_group_id, _, raw_value = callback.data.split(":")
    group = await _owned_group(
        session, int(raw_group_id), callback.from_user.id, for_update=True
    )
    value = int(raw_value)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if value not in {300, 900, 3600, 21600, 86400}:
        await callback.answer("Недопустимое значение.", show_alert=True)
        return
    group.settings.default_mute_seconds = value
    await session.commit()
    await callback.message.edit_text(
        panel_header("Модерация", "Мут по умолчанию сохранён."),
        reply_markup=moderation_menu(group.id),
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data.regexp(r"^setting_flood:\d+:(4|6|8):(5|10|15)$"))
async def contextual_antiflood_set(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    _, raw_group_id, raw_limit, raw_window = callback.data.split(":")
    group = await _owned_group(
        session, int(raw_group_id), callback.from_user.id, for_update=True
    )
    limit, window = int(raw_limit), int(raw_window)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if (limit, window) not in {(4, 5), (6, 10), (8, 15)}:
        await callback.answer("Недопустимый профиль.", show_alert=True)
        return
    group.settings.antiflood_limit = limit
    group.settings.antiflood_window_seconds = window
    await session.commit()
    await callback.message.edit_text(
        panel_header("Защита от спама", "Профиль антифлуда сохранён."),
        reply_markup=protection_menu(group),
    )
    await callback.answer("Сохранено")


_ACTION_ICONS = {
    "ban": "🔨",
    "unban": "✅",
    "mute": "🔇",
    "unmute": "🔊",
    "warn": "⚠️",
    "unwarn": "✅",
    "kick": "🚪",
    "auto_mute": "🤖",
    "automut": "🤖",
    "filter_repeat": "🛡",
    "antiflood_mute": "🌊",
    "complaint_resolved": "📩",
    "complaint_rejected": "📩",
    "deleted_accounts_cleanup": "🧹",
    "unwarn_all": "✅",
    "remove_by_ban": "🎖",
    "remove_by_kick": "🎖",
}

_ACTION_VERBS = {
    "ban": "забанен",
    "unban": "разбанен",
    "mute": "замучен",
    "unmute": "размучен",
    "warn": "получил предупреждение",
    "unwarn": "предупреждение снято",
    "kick": "исключён",
    "auto_mute": "автомут",
    "automut": "автомут",
    "filter_repeat": "фильтр: повтор",
    "antiflood_mute": "антифлуд: мут",
    "complaint_resolved": "жалоба обработана",
    "complaint_rejected": "жалоба отклонена",
    "deleted_accounts_cleanup": "очистка удалённых",
    "unwarn_all": "сняты все предупреждения",
    "remove_by_ban": "снятие ранга (бан)",
    "remove_by_kick": "снятие ранга (кик)",
}

PAGE_SIZE = 10


@router.callback_query(F.data.regexp(r"^logs:\d+$"))
@router.callback_query(F.data.regexp(r"^logs:\d+:page:\d+$"))
async def moderation_logs_with_contextual_back(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[1])
    page = int(parts[3]) if len(parts) > 3 and parts[2] == "page" else 0

    group = await _owned_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    offset = page * PAGE_SIZE

    logs = list(
        (
            await session.scalars(
                select(ModerationLog)
                .where(ModerationLog.group_id == group.id)
                .order_by(ModerationLog.created_at.desc())
                .offset(offset)
                .limit(PAGE_SIZE)
            )
        ).all()
    )

    total = (
        await session.scalar(
            select(func.count())
            .select_from(ModerationLog)
            .where(ModerationLog.group_id == group.id)
        )
        or 0
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    lines = []
    for item in logs:
        target = (
            public_user_token(item.target_telegram_id)
            if item.target_telegram_id
            else "—"
        )
        actor = (
            public_user_token(item.actor_telegram_id) if item.actor_telegram_id else "—"
        )
        icon = _ACTION_ICONS.get(item.action, "•")
        verb = _ACTION_VERBS.get(item.action, item.action)
        time_str = item.created_at.strftime("%d.%m %H:%M")
        reason_part = f" · {item.reason}" if item.reason else ""
        lines.append(
            f"{icon} {time_str}\n" f"{target} {verb}\n" f"👮 {actor}{reason_part}"
        )

    text = panel_header("Журнал модерации") + "\n\n"
    text += "\n\n".join(lines) if lines else "Журнал пока пуст."

    # Pagination buttons
    rows = []
    if page > 0 or page + 1 < total_pages:
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    text="◀ Назад",
                    callback_data=f"logs:{group.id}:page:{page - 1}",
                )
            )
        if total_pages > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text=f"{page + 1}/{total_pages}",
                    callback_data="noop",
                )
            )
        if page + 1 < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text="Вперёд ▶",
                    callback_data=f"logs:{group.id}:page:{page + 1}",
                )
            )
        rows.append(nav_row)
    rows.append(
        [
            InlineKeyboardButton(
                text="◀ К модерации",
                callback_data=f"group_section:{group.id}:moderation",
            )
        ]
    )

    reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)

    await callback.message.edit_text(text, reply_markup=reply_markup)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^members_stats:\d+$"))
async def member_activity_with_contextual_back(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    group_id = int(callback.data.split(":")[-1])
    group = await _owned_group(session, group_id, callback.from_user.id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    start = (datetime.now(UTC).date() - timedelta(days=29)).isoformat()
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
    lines: list[str] = []
    for uid, username, first_name, count, deleted in rows:
        label = (
            f"@{clean_ui_text(username)}"
            if username
            else clean_ui_text(first_name or f"ID {uid}")
        )
        lines.append(f"• {label}: {int(count)} сообщ., удалено {int(deleted)}")
    text = (
        panel_header("Активность участников", "Последние 30 дней")
        + "\n\n"
        + ("\n".join(lines) if lines else "Данных пока нет.")
    )
    text += "\n\nДля конкретного участника ответьте на его сообщение командой «статистика участника»."
    await callback.message.edit_text(
        text,
        reply_markup=_back(
            f"group_section:{group.id}:members", "◀️ Назад к участникам"
        ),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^member_history:\d+:-?\d+$"))
async def member_history_with_contextual_back(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    _, raw_group_id, raw_user_id = callback.data.split(":")
    group = await _owned_group(session, int(raw_group_id), callback.from_user.id)
    user_id = int(raw_user_id)
    if group is None:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    rows = list(
        (
            await session.scalars(
                select(ModerationLog)
                .where(
                    ModerationLog.group_id == group.id,
                    ModerationLog.target_telegram_id == user_id,
                )
                .order_by(ModerationLog.created_at.desc())
                .limit(20)
            )
        ).all()
    )
    lines = [
        f"• {row.created_at:%d.%m %H:%M} · {clean_ui_text(row.action)}"
        + (f" · {clean_ui_text(row.reason)}" if row.reason else "")
        for row in rows
    ]
    await callback.message.edit_text(
        panel_header("История участника", f"ID {user_id}")
        + "\n\n"
        + ("\n".join(lines) if lines else "История пуста."),
        reply_markup=_back(f"member_card:{group.id}:{user_id}", "◀️ Назад к карточке"),
    )
    await callback.answer()
