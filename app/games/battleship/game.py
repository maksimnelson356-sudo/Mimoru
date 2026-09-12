from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
import random

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.game_models import GameAction, GameGroupSettings, GamePlayer, GameResult, GameSession
from app.games.base import BaseGame, LeaveResult
from app.games.config import GameDefinition
from app.games.enums import GameSessionStatus
from app.games.stats import apply_game_result


log = structlog.get_logger()
BOARD_SIZE = 5
SHIP_CELLS = 5
TURN_TIMEOUT_SECONDS = 60


class BattleshipPhase(StrEnum):
    TURN = "turn"
    FINISHED = "finished"


battleship_definition = GameDefinition(
    code="battleship",
    title="🚢 Морской бой",
    min_players=2,
    max_players=2,
    exclusive_group_game=False,
    supports_rating=True,
    supports_spectators=False,
    uses_private_mapping=False,
    default_timeout_seconds=TURN_TIMEOUT_SECONDS,
)


def _coord(number: int) -> str:
    row, col = divmod(number, BOARD_SIZE)
    return f"{chr(65 + row)}{col + 1}"


def _new_board(rng: random.Random) -> list[int]:
    cells = list(range(BOARD_SIZE * BOARD_SIZE))
    return sorted(rng.sample(cells, SHIP_CELLS))


class BattleshipGame(BaseGame):
    definition = battleship_definition

    async def _players(self, session: AsyncSession, game_id: int, *, for_update: bool = False) -> list[GamePlayer]:
        query = select(GamePlayer).where(GamePlayer.game_id == game_id).order_by(GamePlayer.id)
        if for_update:
            query = query.with_for_update()
        return list((await session.scalars(query)).all())

    async def start(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        players = await self._players(session, game.id, for_update=True)
        if len(players) != 2:
            raise ValueError("battleship requires exactly two players")
        rng = random.SystemRandom()
        turn_order = [player.user_telegram_id for player in players]
        rng.shuffle(turn_order)
        boards: dict[str, dict[str, list[int]]] = {}
        for player in players:
            player.status = "alive"
            player.team = None
            player.role = None
            player.score = 0
            player.afk_count = 0
            player.state_json = {"result_applied": False}
            boards[str(player.user_telegram_id)] = {"ships": _new_board(rng), "hits": [], "misses": []}
        game.phase = BattleshipPhase.TURN.value
        game.phase_seq += 1
        game.round_no = 1
        game.state_json = {
            "boards": boards,
            "turn_order": turn_order,
            "turn_user_id": turn_order[0],
            "last_shot": None,
            "turn_timeout_seconds": TURN_TIMEOUT_SECONDS,
        }
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        await session.commit()
        log.info("battleship_started", game_id=game.id, group_id=game.group_id)

    async def handle_action(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        action: str,
        value: int | str | None = None,
    ) -> None:
        if action != "fire" or not isinstance(value, int):
            raise ValueError("unsupported battleship action")
        await self.fire(session, game, actor_telegram_id=actor_telegram_id, cell=value)

    async def _finish(self, session: AsyncSession, game: GameSession, winner_user_id: int) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status == GameSessionStatus.FINISHED.value:
            return
        players = await self._players(session, game.id, for_update=True)
        settings = await session.get(GameGroupSettings, game.group_id)
        rating_enabled = settings.rating_enabled if settings is not None else True
        now = datetime.now(timezone.utc)
        game.status = GameSessionStatus.FINISHED.value
        game.phase = BattleshipPhase.FINISHED.value
        game.phase_seq += 1
        game.deadline_at = None
        game.finished_at = now
        game.finish_reason = f"winner:{winner_user_id}"
        duration = int((now - game.started_at).total_seconds()) if game.started_at else None
        if await session.scalar(select(GameResult).where(GameResult.game_id == game.id)) is None:
            session.add(GameResult(
                game_id=game.id,
                group_id=game.group_id,
                game_type=game.game_type,
                winner_type="player",
                winner_json={"user_id": winner_user_id},
                summary_json={"rounds": game.round_no, "players": len(players)},
                duration_seconds=duration,
            ))
        for player in players:
            state = dict(player.state_json or {})
            if state.get("result_applied"):
                continue
            await apply_game_result(
                session,
                group_id=game.group_id,
                game_type=game.game_type,
                user_telegram_id=player.user_telegram_id,
                won=player.user_telegram_id == winner_user_id,
                rating_enabled=rating_enabled,
                commit=False,
            )
            state["result_applied"] = True
            player.state_json = state
        await session.commit()
        log.info("battleship_finished", game_id=game.id, winner_user_id=winner_user_id)

    async def handle_leave(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
    ) -> LeaveResult:
        """Убирает игрока. Если остался 1 активный — завершает игру его победой."""
        players = list((await session.scalars(
            select(GamePlayer).where(GamePlayer.game_id == game.id)
        )).all())
        active = [p for p in players if p.status != "left" and p.user_telegram_id != actor_telegram_id]
        if len(active) == 1:
            await self._finish(session, game, winner_user_id=active[0].user_telegram_id)
        return LeaveResult.REMOVED

    async def fire(self, session: AsyncSession, game: GameSession, *, actor_telegram_id: int, cell: int) -> tuple[str, bool]:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != BattleshipPhase.TURN.value:
            raise ValueError("turn is not active")
        if cell < 0 or cell >= BOARD_SIZE * BOARD_SIZE:
            raise ValueError("invalid cell")
        state = dict(game.state_json or {})
        if actor_telegram_id != state.get("turn_user_id"):
            raise PermissionError("not your turn")
        players = await self._players(session, game.id)
        if actor_telegram_id not in {p.user_telegram_id for p in players}:
            raise PermissionError("not a player")
        existing = await session.scalar(select(GameAction).where(
            GameAction.game_id == game.id,
            GameAction.phase_seq == game.phase_seq,
            GameAction.actor_telegram_id == actor_telegram_id,
            GameAction.action_type == "battleship_fire",
        ))
        if existing is not None:
            return str((existing.payload_json or {}).get("result") or "repeat"), False
        opponent = next(p for p in players if p.user_telegram_id != actor_telegram_id)
        boards = dict(state.get("boards") or {})
        board = dict(boards.get(str(opponent.user_telegram_id)) or {})
        hits = list(board.get("hits") or [])
        misses = list(board.get("misses") or [])
        if cell in hits or cell in misses:
            raise ValueError("cell already fired")
        ships = list(board.get("ships") or [])
        hit = cell in ships
        (hits if hit else misses).append(cell)
        board["hits"] = hits
        board["misses"] = misses
        boards[str(opponent.user_telegram_id)] = board
        result = "hit" if hit else "miss"
        session.add(GameAction(
            game_id=game.id,
            round_no=game.round_no,
            phase_seq=game.phase_seq,
            actor_telegram_id=actor_telegram_id,
            action_type="battleship_fire",
            target_telegram_id=opponent.user_telegram_id,
            payload_json={"cell": cell, "coord": _coord(cell), "result": result},
        ))
        state["boards"] = boards
        state["last_shot"] = {"actor_user_id": actor_telegram_id, "cell": cell, "coord": _coord(cell), "result": result}
        game.state_json = state
        if len(hits) >= len(ships):
            await session.flush()
            await self._finish(session, game, actor_telegram_id)
            return result, True
        order = list(state.get("turn_order") or [])
        next_user = opponent.user_telegram_id
        state["turn_user_id"] = next_user
        game.state_json = state
        game.phase_seq += 1
        game.round_no += 1
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=int(state.get("turn_timeout_seconds") or TURN_TIMEOUT_SECONDS))
        await session.commit()
        return result, True

    async def handle_timeout(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value:
            return
        if game.phase != BattleshipPhase.TURN.value:
            raise ValueError(f"unsupported battleship timeout phase: {game.phase}")
        state = dict(game.state_json or {})
        current = state.get("turn_user_id")
        order = list(state.get("turn_order") or [])
        if current not in order or len(order) != 2:
            raise ValueError("invalid battleship turn order")
        current_player = await session.scalar(select(GamePlayer).where(
            GamePlayer.game_id == game.id,
            GamePlayer.user_telegram_id == current,
        ).with_for_update())
        if current_player is not None:
            current_player.afk_count += 1
        state["turn_user_id"] = order[1] if current == order[0] else order[0]
        state["last_shot"] = {"actor_user_id": current, "result": "timeout"}
        game.state_json = state
        game.phase_seq += 1
        game.round_no += 1
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=int(state.get("turn_timeout_seconds") or TURN_TIMEOUT_SECONDS))
        await session.commit()

    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        if game.phase == "recovering":
            players = await self._players(session, game.id)
            state = dict(game.state_json or {})
            if len(players) != 2 or not state.get("boards") or not state.get("turn_order"):
                game.status = GameSessionStatus.RUNNING.value
                await session.commit()
                await self.start(session, game)
                return
            game.status = GameSessionStatus.RUNNING.value
            game.phase = BattleshipPhase.TURN.value
            game.phase_seq += 1
        if game.phase not in {BattleshipPhase.TURN.value, BattleshipPhase.FINISHED.value}:
            raise ValueError(f"unknown battleship phase: {game.phase}")
        if game.status == GameSessionStatus.RUNNING.value and game.deadline_at is None:
            state = dict(game.state_json or {})
            game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=int(state.get("turn_timeout_seconds") or TURN_TIMEOUT_SECONDS))
        await session.commit()

    async def sync_ui(self, bot, session: AsyncSession, game: GameSession) -> None:
        from app.games.battleship.presentation import sync_battleship_ui
        await sync_battleship_ui(bot, session, game)
