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
CHAMBERS = 6
TURN_TIMEOUT_SECONDS = 45


class RoulettePhase(StrEnum):
    TURN = "turn"
    FINISHED = "finished"


roulette_definition = GameDefinition(
    code="roulette",
    title="💣 Рулетка",
    min_players=2,
    max_players=12,
    exclusive_group_game=True,
    supports_rating=True,
    supports_spectators=False,
    uses_private_mapping=False,
    default_timeout_seconds=TURN_TIMEOUT_SECONDS,
)


def _reload_state(rng: random.Random) -> dict[str, int]:
    return {"bullet": rng.randrange(CHAMBERS), "chamber": 0}


class RouletteGame(BaseGame):
    definition = roulette_definition

    async def _players(
        self,
        session: AsyncSession,
        game_id: int,
        *,
        for_update: bool = False,
    ) -> list[GamePlayer]:
        query = select(GamePlayer).where(GamePlayer.game_id == game_id).order_by(GamePlayer.id)
        if for_update:
            query = query.with_for_update()
        return list((await session.scalars(query)).all())

    async def start(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if game is None:
            return
        players = await self._players(session, game.id, for_update=True)
        if len(players) < self.definition.min_players:
            raise ValueError("not enough players for roulette")

        rng = random.SystemRandom()
        order = [player.user_telegram_id for player in players]
        rng.shuffle(order)
        for player in players:
            player.status = "alive"
            player.score = 0
            player.afk_count = 0
            player.state_json = {"result_applied": False}

        game.phase = RoulettePhase.TURN.value
        game.phase_seq += 1
        game.round_no = 1
        game.state_json = {
            "order": order,
            "turn_user_id": order[0],
            "alive_user_ids": list(order),
            "drum": _reload_state(rng),
            "last_turn": None,
            "turn_timeout_seconds": TURN_TIMEOUT_SECONDS,
        }
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        await session.commit()
        log.info("roulette_started", game_id=game.id, group_id=game.group_id, players=len(players))

    async def handle_action(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        action: str,
        value: int | str | None = None,
    ) -> None:
        if action != "trigger":
            raise ValueError(f"unsupported roulette action: {action}")
        await self.trigger(session, game, actor_telegram_id=actor_telegram_id)

    async def _finish(
        self,
        session: AsyncSession,
        game: GameSession,
        winner_user_id: int,
    ) -> None:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if game is None or game.status == GameSessionStatus.FINISHED.value:
            return
        players = await self._players(session, game.id, for_update=True)
        settings = await session.get(GameGroupSettings, game.group_id)
        rating_enabled = settings.rating_enabled if settings is not None else True
        now = datetime.now(timezone.utc)
        game.status = GameSessionStatus.FINISHED.value
        game.phase = RoulettePhase.FINISHED.value
        game.phase_seq += 1
        game.deadline_at = None
        game.finished_at = now
        game.finish_reason = f"winner:{winner_user_id}"
        duration = int((now - game.started_at).total_seconds()) if game.started_at else None
        if await session.scalar(select(GameResult).where(GameResult.game_id == game.id)) is None:
            session.add(
                GameResult(
                    game_id=game.id,
                    group_id=game.group_id,
                    game_type=game.game_type,
                    winner_type="player",
                    winner_json={"user_id": winner_user_id},
                    summary_json={"rounds": game.round_no, "players": len(players)},
                    duration_seconds=duration,
                )
            )
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
        log.info("roulette_finished", game_id=game.id, winner_user_id=winner_user_id)

    async def handle_leave(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
    ) -> LeaveResult:
        """Убирает игрока. Если остался 1 живой — завершает игру его победой."""
        players = list((await session.scalars(
            select(GamePlayer).where(GamePlayer.game_id == game.id)
        )).all())
        alive = [p for p in players if p.status == "alive" and p.user_telegram_id != actor_telegram_id]
        if len(alive) == 1:
            await self._finish(session, game, winner_user_id=alive[0].user_telegram_id)
        return LeaveResult.REMOVED

    @staticmethod
    def _next_alive(order: list[int], alive: list[int], current: int) -> int:
        start = order.index(current) if current in order else -1
        for offset in range(1, len(order) + 1):
            candidate = order[(start + offset) % len(order)]
            if candidate in alive:
                return candidate
        raise ValueError("roulette has no next alive player")

    async def trigger(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
    ) -> tuple[str, bool]:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != RoulettePhase.TURN.value
        ):
            raise ValueError("roulette turn is not active")
        state = dict(game.state_json or {})
        if actor_telegram_id != state.get("turn_user_id"):
            raise PermissionError("not your turn")
        player = await session.scalar(
            select(GamePlayer).where(
                GamePlayer.game_id == game.id,
                GamePlayer.user_telegram_id == actor_telegram_id,
                GamePlayer.status == "alive",
            ).with_for_update()
        )
        if player is None:
            raise PermissionError("not an alive roulette player")
        existing = await session.scalar(
            select(GameAction).where(
                GameAction.game_id == game.id,
                GameAction.phase_seq == game.phase_seq,
                GameAction.actor_telegram_id == actor_telegram_id,
                GameAction.action_type == "roulette_trigger",
            )
        )
        if existing is not None:
            return str((existing.payload_json or {}).get("result") or "repeat"), False

        drum = dict(state.get("drum") or {})
        bullet = int(drum.get("bullet", -1))
        chamber = int(drum.get("chamber", 0))
        if bullet < 0 or bullet >= CHAMBERS or chamber < 0 or chamber >= CHAMBERS:
            raise ValueError("invalid roulette drum state")
        fired = chamber == bullet
        result = "fired" if fired else "safe"
        session.add(
            GameAction(
                game_id=game.id,
                round_no=game.round_no,
                phase_seq=game.phase_seq,
                actor_telegram_id=actor_telegram_id,
                action_type="roulette_trigger",
                target_telegram_id=actor_telegram_id,
                payload_json={"result": result},
            )
        )

        alive = [int(user_id) for user_id in list(state.get("alive_user_ids") or [])]
        order = [int(user_id) for user_id in list(state.get("order") or [])]
        if fired:
            player.status = "eliminated"
            player.left_at = datetime.now(timezone.utc)
            alive = [user_id for user_id in alive if user_id != actor_telegram_id]
            state["alive_user_ids"] = alive
            state["last_turn"] = {"actor_user_id": actor_telegram_id, "result": "fired"}
            if len(alive) == 1:
                state["turn_user_id"] = alive[0]
                game.state_json = state
                await session.flush()
                await self._finish(session, game, alive[0])
                return result, True
            state["drum"] = _reload_state(random.SystemRandom())
        else:
            drum["chamber"] = chamber + 1
            state["drum"] = drum
            state["last_turn"] = {"actor_user_id": actor_telegram_id, "result": "safe"}

        state["turn_user_id"] = self._next_alive(order, alive, actor_telegram_id)
        game.state_json = state
        game.phase_seq += 1
        game.round_no += 1
        game.deadline_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(state.get("turn_timeout_seconds") or TURN_TIMEOUT_SECONDS)
        )
        await session.commit()
        return result, True

    async def handle_timeout(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if game is None or game.status != GameSessionStatus.RUNNING.value:
            return
        if game.phase != RoulettePhase.TURN.value:
            raise ValueError(f"unsupported roulette timeout phase: {game.phase}")
        state = dict(game.state_json or {})
        current = int(state.get("turn_user_id") or 0)
        player = await session.scalar(
            select(GamePlayer).where(
                GamePlayer.game_id == game.id,
                GamePlayer.user_telegram_id == current,
                GamePlayer.status == "alive",
            ).with_for_update()
        )
        if player is not None:
            player.afk_count += 1
        await session.flush()
        await self.trigger(session, game, actor_telegram_id=current)

    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if game is None:
            return
        if game.phase == "recovering":
            state = dict(game.state_json or {})
            players = await self._players(session, game.id)
            if not players:
                raise ValueError("roulette game has no players")
            if not state.get("order") or not state.get("alive_user_ids") or not state.get("drum"):
                game.status = GameSessionStatus.RUNNING.value
                await session.commit()
                await self.start(session, game)
                return
            game.status = GameSessionStatus.RUNNING.value
            game.phase = RoulettePhase.TURN.value
            game.phase_seq += 1
        if game.phase not in {RoulettePhase.TURN.value, RoulettePhase.FINISHED.value}:
            raise ValueError(f"unknown roulette phase: {game.phase}")
        if game.status == GameSessionStatus.RUNNING.value and game.deadline_at is None:
            state = dict(game.state_json or {})
            game.deadline_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(state.get("turn_timeout_seconds") or TURN_TIMEOUT_SECONDS)
            )
        await session.commit()

    async def sync_ui(self, bot, session: AsyncSession, game: GameSession) -> None:
        from app.games.roulette.presentation import sync_roulette_ui

        await sync_roulette_ui(bot, session, game)
