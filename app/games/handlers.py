from __future__ import annotations

import structlog
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.game_models import GameGroupSettings, GamePlayer, GameSession
from app.db.models import Group
from app.games.enums import GameSessionStatus
from app.games.game_center import send_game_center_snapshot
from app.games.lobby import close_lobby_message, ensure_lobby_message
from app.games.messages import retire_active_messages
from app.games.manager import GameConflictError, GameManager, GameNotFoundError, GamePlayerError
from app.games.panels import ensure_game_panel, render_profile, render_rating
from app.games.registry import game_registry
from app.services.access import can_manage_group


router = Router(name=__name__)
GROUP_TYPES = {"group", "supergroup"}
manager = GameManager()
log = structlog.get_logger()


async def _active_group(session: AsyncSession, chat_id: int) -> Group | None:
    return await session.scalar(select(Group).where(Group.telegram_chat_id == chat_id, Group.is_active.is_(True)))


async def _callback_game_group(callback: CallbackQuery, session: AsyncSession, *, game_id: int) -> tuple[GameSession, Group] | None:
    if callback.message is None:
        await callback.answer("Игровое сообщение больше недоступно.", show_alert=True); return None
    game = await manager.get_game(session, game_id=game_id)
    if game is None:
        await callback.answer("❌ Эта кнопка больше не активна.", show_alert=True); return None
    group = await session.get(Group, game.group_id)
    if group is None or not group.is_active or callback.message.chat.id != group.telegram_chat_id:
        await callback.answer("❌ Эта кнопка относится к другой игровой сессии.", show_alert=True); return None
    return game, group


async def _is_joined_player(session: AsyncSession, *, game_id: int, user_id: int) -> bool:
    player = await session.scalar(select(GamePlayer.id).where(GamePlayer.game_id == game_id, GamePlayer.user_telegram_id == user_id, GamePlayer.status == "joined"))
    return player is not None


async def _can_start_lobby(bot: Bot, session: AsyncSession, *, group: Group, game: GameSession, user_id: int) -> bool:
    if user_id == game.creator_telegram_id or await can_manage_group(bot, group, user_id, session): return True
    settings = await session.get(GameGroupSettings, group.id)
    policy = settings.creator_policy if settings is not None else "lobby_creator"
    return policy == "any_at_min" and await _is_joined_player(session, game_id=game.id, user_id=user_id)


def _games_markup() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=d.title, callback_data=f"gm:new:{d.code}") for d in game_registry.all()]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="◀️ В игровой центр", callback_data="gm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _games_text() -> str:
    definitions = game_registry.all()
    if not definitions:
        return "🎮 ВЫБОР ИГРЫ\n\nИгровое ядро готово. Первая полноценная игра пока не подключена.\nСтарые развлекательные команды сюда больше не относятся."
    lines = ["🎮 ВЫБОР ИГРЫ", ""]
    for definition in definitions: lines.append(f"{definition.title} · {definition.min_players}–{definition.max_players} игроков")
    lines.append("\nВыберите игру кнопкой ниже.")
    return "\n".join(lines)


@router.message(Command("games"), F.chat.type.in_(GROUP_TYPES))
async def games_command(message: Message, bot: Bot, session: AsyncSession) -> None:
    group = await _active_group(session, message.chat.id)
    if group is not None: await send_game_center_snapshot(bot, session, group=group, reply_to_message_id=message.message_id)


@router.callback_query(F.data == "gm:home")
async def game_home(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if callback.message is None:
        await callback.answer("Игровая панель недоступна.", show_alert=True); return
    group = await _active_group(session, callback.message.chat.id)
    if group is None:
        await callback.answer("Группа больше не подключена к Mimoru.", show_alert=True); return
    await ensure_game_panel(bot, session, group=group, pin=False); await callback.answer()


@router.callback_query(F.data == "gm:list")
async def game_list(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer("Игровая панель недоступна.", show_alert=True); return
    await callback.message.edit_text(_games_text(), reply_markup=_games_markup()); await callback.answer()


@router.callback_query(F.data == "gm:profile")
async def game_profile(callback: CallbackQuery, session: AsyncSession) -> None:
    if callback.message is None:
        await callback.answer("Игровая панель недоступна.", show_alert=True); return
    group = await _active_group(session, callback.message.chat.id)
    if group is None:
        await callback.answer("Группа больше не подключена к Mimoru.", show_alert=True); return
    name = (callback.from_user.full_name or callback.from_user.username or "Игрок").strip()
    await callback.answer(await render_profile(session, group_id=group.id, user_id=callback.from_user.id, name=name), show_alert=True)


@router.callback_query(F.data == "gm:rating")
async def game_rating(callback: CallbackQuery, session: AsyncSession) -> None:
    if callback.message is None:
        await callback.answer("Игровая панель недоступна.", show_alert=True); return
    group = await _active_group(session, callback.message.chat.id)
    if group is None:
        await callback.answer("Группа больше не подключена к Mimoru.", show_alert=True); return
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В игровой центр", callback_data="gm:home")]])
    await callback.message.edit_text(await render_rating(session, group_id=group.id), reply_markup=markup); await callback.answer()


@router.callback_query(F.data == "gm:rules:all")
async def game_rules(callback: CallbackQuery) -> None:
    await callback.answer("Игры идут внутри группы кнопками. Обычная переписка не является игровым вводом. В группе одновременно работает одна групповая игра.", show_alert=True)


@router.callback_query(F.data.regexp(r"^gm:rules:[a-z0-9_]{1,32}$"))
async def game_specific_rules(callback: CallbackQuery) -> None:
    code = (callback.data or "").rsplit(":", 1)[-1]; definition = game_registry.get(code)
    if definition is None:
        await callback.answer("Эта игра больше не доступна.", show_alert=True); return
    await callback.answer(f"{definition.title}: правила доступны после выбора игры.", show_alert=True)


@router.callback_query(F.data.regexp(r"^gm:new:[a-z0-9_]{1,32}$"))
async def game_create_lobby(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if callback.message is None:
        await callback.answer("Игровая панель недоступна.", show_alert=True); return
    code = (callback.data or "").rsplit(":", 1)[-1]; definition = game_registry.get(code)
    if definition is None:
        await callback.answer("Эта игра больше не доступна.", show_alert=True); return
    group = await _active_group(session, callback.message.chat.id)
    if group is None:
        await callback.answer("Группа больше не подключена к Mimoru.", show_alert=True); return
    settings = await session.get(GameGroupSettings, group.id)
    if settings is not None and not settings.enabled:
        await callback.answer("🎮 Игры в этой группе отключены администратором.", show_alert=True); return
    if settings is not None and settings.allowed_games and code not in settings.allowed_games:
        await callback.answer("Эта игра отключена в настройках группы.", show_alert=True); return
    name = (callback.from_user.full_name or callback.from_user.username or "Игрок").strip()
    try:
        game = await manager.create_lobby(session, telegram_chat_id=group.telegram_chat_id, game_type=code, creator_telegram_id=callback.from_user.id, creator_display_name=name)
    except GameConflictError:
        active = await manager.get_active_game(session, group_id=group.id)
        title = game_registry.get(active.game_type).title if active and game_registry.get(active.game_type) else "другая игра"
        await callback.answer(f"🎮 В этой группе уже идёт {title}. Дождитесь её завершения.", show_alert=True); return
    except (GameNotFoundError, KeyError):
        await callback.answer("Не удалось создать игровое лобби.", show_alert=True); return
    await ensure_lobby_message(bot, session, group=group, game=game, manager=manager)
    await ensure_game_panel(bot, session, group=group, pin=False)
    log.info("game_lobby_created", game_id=game.id, group_id=group.id, game_type=code); await callback.answer("Лобби создано")


@router.callback_query(F.data.regexp(r"^gm:j:\d+$"))
async def game_join_lobby(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    game_id = int((callback.data or "").rsplit(":", 1)[-1]); resolved = await _callback_game_group(callback, session, game_id=game_id)
    if resolved is None: return
    game, group = resolved
    if game.status != GameSessionStatus.LOBBY.value:
        await callback.answer("❌ Это лобби уже закрыто.", show_alert=True); return
    name = (callback.from_user.full_name or callback.from_user.username or "Игрок").strip()
    try: await manager.join_lobby(session, game_id=game.id, user_telegram_id=callback.from_user.id, display_name=name)
    except GamePlayerError as error:
        await callback.answer("Лобби уже заполнено." if "full" in str(error) else "Присоединиться уже нельзя.", show_alert=True); return
    await ensure_lobby_message(bot, session, group=group, game=game, manager=manager); await callback.answer("✅ Вы в игре")


@router.callback_query(F.data.regexp(r"^gm:l:\d+$"))
async def game_leave_lobby(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    game_id = int((callback.data or "").rsplit(":", 1)[-1]); resolved = await _callback_game_group(callback, session, game_id=game_id)
    if resolved is None: return
    game, group = resolved
    if game.status != GameSessionStatus.LOBBY.value:
        await callback.answer("❌ Это лобби уже закрыто.", show_alert=True); return
    try: await manager.leave_lobby(session, game_id=game.id, user_telegram_id=callback.from_user.id)
    except GamePlayerError as error:
        await callback.answer("Создатель лобби может только отменить игру." if "creator" in str(error) else "Выйти из этого лобби уже нельзя.", show_alert=True); return
    await ensure_lobby_message(bot, session, group=group, game=game, manager=manager); await callback.answer("Вы вышли из лобби")


@router.callback_query(F.data.regexp(r"^gm:s:\d+$"))
async def game_start_lobby(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    game_id = int((callback.data or "").rsplit(":", 1)[-1]); resolved = await _callback_game_group(callback, session, game_id=game_id)
    if resolved is None: return
    game, group = resolved
    if game.status != GameSessionStatus.LOBBY.value:
        await callback.answer("❌ Эта кнопка больше не активна.", show_alert=True); return
    if not await _can_start_lobby(bot, session, group=group, game=game, user_id=callback.from_user.id):
        await callback.answer("❌ У вас нет права запускать это лобби.", show_alert=True); return
    try: game = await manager.start_lobby(session, game_id=game.id)
    except GamePlayerError as error:
        if "not enough" in str(error):
            await callback.answer(f"Нужно минимум {game_registry.require(game.game_type).min_players} игроков.", show_alert=True)
        else: await callback.answer("Лобби уже закрыто.", show_alert=True)
        return
    engine = game_registry.engine(game.game_type)
    try: await engine.start(session, game)
    except Exception:
        locked = await manager.get_game(session, game_id=game.id, for_update=True)
        if locked is not None and locked.status == GameSessionStatus.RUNNING.value:
            locked.status = GameSessionStatus.RECOVERING.value; locked.phase = "recovering"; await session.commit()
        log.exception("game_start_failed", game_id=game.id, game_type=game.game_type)
        await callback.answer("Игра сохранена для восстановления после ошибки запуска.", show_alert=True); return
    await close_lobby_message(bot, session, group=group, game=game, text=f"▶️ {game_registry.require(game.game_type).title} началась.")
    await ensure_game_panel(bot, session, group=group, pin=False)
    log.info("game_started", game_id=game.id, group_id=group.id, game_type=game.game_type); await callback.answer("▶️ Игра началась")


@router.callback_query(F.data.regexp(r"^gm:c:\d+$"))
async def game_cancel(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    game_id = int((callback.data or "").rsplit(":", 1)[-1]); resolved = await _callback_game_group(callback, session, game_id=game_id)
    if resolved is None: return
    game, group = resolved
    if game.status not in {GameSessionStatus.LOBBY.value, GameSessionStatus.RUNNING.value, GameSessionStatus.RECOVERING.value}:
        await callback.answer("❌ Эта кнопка больше не активна.", show_alert=True); return
    can_cancel = callback.from_user.id == game.creator_telegram_id or await can_manage_group(bot, group, callback.from_user.id, session)
    if not can_cancel:
        await callback.answer("❌ Отменить игру может создатель или администратор.", show_alert=True); return
        game = await manager.cancel_game(session, game_id=game.id, reason="cancelled_by_user")
    # #9a: снимаем активные игровые сообщения, чтобы старые кнопки не работали
    await retire_active_messages(
        bot,
        session,
        game_id=game.id,
        replacement_text=f"❌ {game_registry.require(game.game_type).title} отменена.",
    )
    await close_lobby_message(bot, session, group=group, game=game, text=f"❌ {game_registry.require(game.game_type).title} отменена.")
    await ensure_game_panel(bot, session, group=group, pin=False)
    log.info("game_cancelled", game_id=game.id, group_id=group.id, actor_id=callback.from_user.id); await callback.answer("Игра отменена")


@router.callback_query(F.data.regexp(r"^gm:open:\d+$"))
async def game_open(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    game_id = int((callback.data or "").rsplit(":", 1)[-1]); resolved = await _callback_game_group(callback, session, game_id=game_id)
    if resolved is None: return
    game, group = resolved
    if game.status == GameSessionStatus.LOBBY.value:
        await ensure_lobby_message(bot, session, group=group, game=game, manager=manager); await callback.answer("Лобби обновлено"); return
    if game.status not in {GameSessionStatus.RUNNING.value, GameSessionStatus.RECOVERING.value}:
        await callback.answer("❌ Эта игровая сессия уже завершена.", show_alert=True); return
    entry = game_registry.get_entry(game.game_type)
    sync_ui = getattr(entry.engine, "sync_ui", None) if entry is not None else None
    if sync_ui is not None:
        try: await sync_ui(bot, session, game)
        except Exception:
            log.exception("game_open_sync_failed", game_id=game.id, game_type=game.game_type)
            await callback.answer("Не удалось открыть карточку игры. Попробуйте ещё раз.", show_alert=True); return
        await callback.answer("👀 Актуальная карточка игры обновлена"); return
    await callback.answer(f"{game_registry.require(game.game_type).title}: фаза {game.phase}, раунд {game.round_no}.", show_alert=True)
