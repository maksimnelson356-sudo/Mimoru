from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.game_models import (
    GamePanel,
    GamePlayerGameStats,
    GamePlayerStats,
    GameSession,
)
from app.db.models import Group, User
from app.games.enums import ACTIVE_SESSION_STATUSES, GameSessionStatus
from app.games.locks import advisory_xact_lock
from app.games.registry import GameRegistry, game_registry
from app.services.ui import is_chat_unavailable

log = structlog.get_logger()
_GAME_PANEL_LOCK_NAMESPACE = 4_676_944


def panel_markup(*, active_game: GameSession | None) -> InlineKeyboardMarkup:
    if active_game is not None:
        rows = [
            [
                InlineKeyboardButton(
                    text="👀 Открыть игру", callback_data=f"gm:open:{active_game.id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📖 Правила", callback_data=f"gm:rules:{active_game.game_type}"
                )
            ],
        ]
        if active_game.status in {
            GameSessionStatus.RUNNING.value,
            GameSessionStatus.RECOVERING.value,
        }:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="➖ Выйти из игры",
                        callback_data=f"gm:leave:{active_game.id}",
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎮 Начать игру", callback_data="gm:list")],
            [
                InlineKeyboardButton(text="🏆 Рейтинг", callback_data="gm:rating"),
                InlineKeyboardButton(text="👤 Мой профиль", callback_data="gm:profile"),
            ],
            [
                InlineKeyboardButton(text="ℹ️ Правила", callback_data="gm:rules:all"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="gm:settings"),
            ],
        ]
    )


def panel_text(
    *, active_game: GameSession | None, registry: GameRegistry | None = None
) -> str:
    registry = registry or game_registry
    if active_game is not None:
        definition = registry.get(active_game.game_type)
        title = definition.title if definition is not None else active_game.game_type
        return (
            "🎮 ИГРОВОЙ ЦЕНТР\n\n"
            "🔴 Сейчас идёт игра\n\n"
            f"{title}\n"
            f"🔄 Раунд: {active_game.round_no}\n\n"
            "Обычная переписка группы продолжает работать независимо от игры."
        )
    return (
        "🎮 ИГРОВОЙ ЦЕНТР\n\n"
        "🟢 Статус: свободно\n\n"
        "Здесь запускаются полноценные групповые игры Mimoru.\n"
        "Игровые действия выполняются кнопками и не мешают обычной переписке."
    )


async def active_game_for_group(
    session: AsyncSession, group_id: int
) -> GameSession | None:
    return await session.scalar(
        select(GameSession)
        .where(
            GameSession.group_id == group_id,
            GameSession.status.in_(ACTIVE_SESSION_STATUSES),
        )
        .order_by(GameSession.id.desc())
        .limit(1)
    )


async def ensure_game_panel(
    bot: Bot,
    session: AsyncSession,
    *,
    group: Group,
    pin: bool = True,
) -> GamePanel | None:
    await advisory_xact_lock(
        session,
        namespace=_GAME_PANEL_LOCK_NAMESPACE,
        key=group.id,
    )
    try:
        active_game = await active_game_for_group(session, group.id)
        text_value = panel_text(active_game=active_game)
        markup = panel_markup(active_game=active_game)
        panel = await session.get(GamePanel, group.id)

        if panel is not None:
            try:
                await bot.edit_message_text(
                    chat_id=group.telegram_chat_id,
                    message_id=panel.message_id,
                    text=text_value,
                    reply_markup=markup,
                )
                await session.commit()
                return panel
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).casefold():
                    await session.commit()
                    return panel
                log.info(
                    "game_panel_recreate",
                    group_id=group.id,
                    message_id=panel.message_id,
                    error=str(error),
                )
            except TelegramForbiddenError as error:
                if is_chat_unavailable(error):
                    log.debug(
                        "game_panel_edit_forbidden", group_id=group.id, error=str(error)
                    )
                else:
                    log.warning(
                        "game_panel_edit_forbidden", group_id=group.id, error=str(error)
                    )
                await session.commit()
                return None

        try:
            message = await bot.send_message(
                group.telegram_chat_id, text_value, reply_markup=markup
            )
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            if is_chat_unavailable(error):
                log.debug("game_panel_send_failed", group_id=group.id, error=str(error))
            else:
                log.warning(
                    "game_panel_send_failed", group_id=group.id, error=str(error)
                )
            await session.commit()
            return None

        if panel is None:
            panel = GamePanel(
                group_id=group.id, message_id=message.message_id, pinned=False
            )
            session.add(panel)
        else:
            panel.message_id = message.message_id
            panel.pinned = False

        if pin:
            try:
                await bot.pin_chat_message(
                    group.telegram_chat_id,
                    message.message_id,
                    disable_notification=True,
                )
                panel.pinned = True
            except (TelegramBadRequest, TelegramForbiddenError) as error:
                log.info("game_panel_pin_skipped", group_id=group.id, error=str(error))

        await session.commit()
        return panel
    except Exception:
        await session.rollback()
        raise


async def render_profile(
    session: AsyncSession, *, group_id: int, user_id: int, name: str
) -> str:
    stats = await session.scalar(
        select(GamePlayerStats).where(
            GamePlayerStats.group_id == group_id,
            GamePlayerStats.user_telegram_id == user_id,
        )
    )
    if stats is None:
        return (
            f"👤 {name}\n\n🎮 Игр: 0\n🏆 Побед: 0\n⭐ Рейтинг: 1000\n🔥 Серия побед: 0"
        )
    per_game = list(
        (
            await session.scalars(
                select(GamePlayerGameStats)
                .where(
                    GamePlayerGameStats.group_id == group_id,
                    GamePlayerGameStats.user_telegram_id == user_id,
                )
                .order_by(GamePlayerGameStats.games_played.desc())
                .limit(3)
            )
        ).all()
    )
    lines = [
        f"👤 {name}",
        "",
        f"🎮 Игр: {stats.games_played}",
        f"🏆 Побед: {stats.wins}",
        f"⭐ Рейтинг: {stats.rating}",
        f"🔥 Серия побед: {stats.win_streak}",
    ]
    for row in per_game:
        title = game_registry.get(row.game_type)
        label = title.title if title is not None else row.game_type
        lines.append(f"{label}: {row.wins} побед / {row.games_played} игр")
    return "\n".join(lines)[:200]


def _user_name(user: User | None, telegram_id: int) -> str:
    if user is None:
        return f"Игрок {telegram_id}"
    full = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    if full:
        return full
    if user.username:
        return f"@{user.username}"
    return f"Игрок {telegram_id}"


async def render_rating(session: AsyncSession, *, group_id: int) -> str:
    rows = list(
        (
            await session.scalars(
                select(GamePlayerStats)
                .where(GamePlayerStats.group_id == group_id)
                .order_by(
                    GamePlayerStats.rating.desc(), GamePlayerStats.games_played.desc()
                )
                .limit(10)
            )
        ).all()
    )
    if not rows:
        return "🏆 РЕЙТИНГ ИГРОКОВ\n\nРейтинговых игр в этой группе ещё не было."
    ids = [row.user_telegram_id for row in rows]
    users = list(
        (await session.scalars(select(User).where(User.telegram_id.in_(ids)))).all()
    )
    by_id = {user.telegram_id: user for user in users}
    lines = ["🏆 РЕЙТИНГ ИГРОКОВ", ""]
    medals = ("🥇", "🥈", "🥉")
    for index, stats in enumerate(rows, start=1):
        prefix = medals[index - 1] if index <= 3 else f"{index}."
        lines.append(
            f"{prefix} {_user_name(by_id.get(stats.user_telegram_id), stats.user_telegram_id)} — {stats.rating}"
        )
    return "\n".join(lines)
