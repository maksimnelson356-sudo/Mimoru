from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.game_models import GameAction, GameGroupSettings, GamePlayer, GameResult, GameSession
from app.games.base import BaseGame, LeaveResult
from app.games.config import GameDefinition
from app.games.enums import GameSessionStatus
from app.games.stats import apply_game_result


TURN_TIMEOUT_SECONDS = 60
START_HP = 5
MAX_ROUNDS = 30


class ArenaPhase(StrEnum):
    TURN = "turn"
    FINISHED = "finished"


arena_definition = GameDefinition(
    code="arena",
    title="⚔️ Арена",
    min_players=2,
    max_players=8,
    exclusive_group_game=True,
    supports_rating=True,
    supports_spectators=False,
    uses_private_mapping=False,
    default_timeout_seconds=TURN_TIMEOUT_SECONDS,
)


class ArenaGame(BaseGame):
    definition = arena_definition

    async def _players(self, session: AsyncSession, game_id: int, *, for_update: bool = False) -> list[GamePlayer]:
        query = select(GamePlayer).where(GamePlayer.game_id == game_id).order_by(GamePlayer.id)
        if for_update:
            query = query.with_for_update()
        return list((await session.scalars(query)).all())

    async def start(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        players = [p for p in await self._players(session, game.id, for_update=True) if p.status == "joined"]
        if len(players) < 2:
            raise ValueError("not enough players for arena")
        order = [p.user_telegram_id for p in players]
        random.SystemRandom().shuffle(order)
        for player in players:
            player.status = "alive"
            player.score = 0
            player.afk_count = 0
            player.state_json = {"hp": START_HP, "guard": False, "result_applied": False}
        game.phase = ArenaPhase.TURN.value
        game.phase_seq += 1
        game.round_no = 1
        game.state_json = {"turn_order": order, "turn_index": 0, "turn_user_id": order[0], "last_action": None}
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        await session.commit()

    async def handle_action(self, session: AsyncSession, game: GameSession, *, actor_telegram_id: int, action: str, value: int | str | None = None) -> None:
        if action not in {"attack", "guard", "heal"}:
            raise ValueError("unsupported arena action")
        await self.act(session, game, actor_telegram_id=actor_telegram_id, action=action, target_id=int(value) if value else None)

    async def act(self, session: AsyncSession, game: GameSession, *, actor_telegram_id: int, action: str, target_id: int | None = None) -> tuple[str, bool]:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != ArenaPhase.TURN.value:
            raise ValueError("arena turn inactive")
        state = dict(game.state_json or {})
        if actor_telegram_id != int(state.get("turn_user_id") or 0):
            raise PermissionError("not your turn")
        existing = await session.scalar(select(GameAction).where(
            GameAction.game_id == game.id, GameAction.phase_seq == game.phase_seq,
            GameAction.actor_telegram_id == actor_telegram_id,
        ))
        if existing is not None:
            return "repeat", False
        players = await self._players(session, game.id, for_update=True)
        by_id = {p.user_telegram_id: p for p in players if p.status == "alive"}
        actor = by_id.get(actor_telegram_id)
        if actor is None:
            raise PermissionError("not alive")
        astate = dict(actor.state_json or {})
        result = action
        payload: dict = {"action": action}
        if action == "guard":
            astate["guard"] = True
            actor.state_json = astate
            result = "guard"
        elif action == "heal":
            if int(astate.get("hp") or 0) >= START_HP:
                raise ValueError("hp already full")
            astate["hp"] = min(START_HP, int(astate.get("hp") or 0) + 1)
            astate["guard"] = False
            actor.state_json = astate
            result = "heal"
        else:
            target = by_id.get(int(target_id or 0))
            if target is None or target.user_telegram_id == actor_telegram_id:
                raise ValueError("invalid target")
            tstate = dict(target.state_json or {})
            guarded = bool(tstate.get("guard"))
            damage = 0 if guarded else 1
            tstate["guard"] = False
            tstate["hp"] = max(0, int(tstate.get("hp") or 0) - damage)
            target.state_json = tstate
            astate["guard"] = False
            actor.state_json = astate
            actor.score += damage
            payload.update({"target_id": target.user_telegram_id, "damage": damage, "blocked": guarded})
            result = "blocked" if guarded else "hit"
            if tstate["hp"] <= 0:
                target.status = "eliminated"
                result = "knockout"
        state["last_action"] = {"user_id": actor_telegram_id, "result": result, **payload}
        session.add(GameAction(game_id=game.id, round_no=game.round_no, phase_seq=game.phase_seq,
                               actor_telegram_id=actor_telegram_id, action_type=f"arena_{action}", payload_json=payload))
        await session.flush()
        alive = [p for p in players if p.status == "alive"]
        if len(alive) <= 1 or game.round_no >= MAX_ROUNDS:
            winner = alive[0] if len(alive) == 1 else max(alive, key=lambda p: (int((p.state_json or {}).get("hp") or 0), p.score))
            game.state_json = state
            await self._finish(session, game, winner.user_telegram_id)
            return "winner", True
        order = [int(uid) for uid in list(state.get("turn_order") or [])]
        idx = int(state.get("turn_index") or 0)
        for _ in range(len(order)):
            idx = (idx + 1) % len(order)
            if order[idx] in {p.user_telegram_id for p in alive}:
                break
        state["turn_index"] = idx
        state["turn_user_id"] = order[idx]
        game.state_json = state
        game.phase_seq += 1
        game.round_no += 1
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        await session.commit()
        return result, True

    async def _finish(self, session: AsyncSession, game: GameSession, winner_id: int) -> None:
        players = await self._players(session, game.id, for_update=True)
        settings = await session.get(GameGroupSettings, game.group_id)
        rating_enabled = settings.rating_enabled if settings else True
        now = datetime.now(timezone.utc)
        game.status = GameSessionStatus.FINISHED.value
        game.phase = ArenaPhase.FINISHED.value
        game.phase_seq += 1
        game.deadline_at = None
        game.finished_at = now
        game.finish_reason = f"winner:{winner_id}"
        if await session.scalar(select(GameResult).where(GameResult.game_id == game.id)) is None:
            session.add(GameResult(game_id=game.id, group_id=game.group_id, game_type=game.game_type,
                winner_type="player", winner_json={"user_id": winner_id},
                summary_json={"rounds": game.round_no, "players": len(players)},
                duration_seconds=int((now-game.started_at).total_seconds()) if game.started_at else None))
        for player in players:
            pstate = dict(player.state_json or {})
            if not pstate.get("result_applied"):
                await apply_game_result(session, group_id=game.group_id, game_type=game.game_type,
                    user_telegram_id=player.user_telegram_id, won=player.user_telegram_id == winner_id,
                    rating_enabled=rating_enabled, commit=False)
                pstate["result_applied"] = True
                player.state_json = pstate
        await session.commit()

    async def handle_timeout(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != ArenaPhase.TURN.value:
            return
        state = dict(game.state_json or {})
        uid = int(state.get("turn_user_id") or 0)
        players = await self._players(session, game.id, for_update=True)
        actor = next(
            (player for player in players if player.user_telegram_id == uid and player.status == "alive"),
            None,
        )
        if actor is None:
            return

        existing = await session.scalar(select(GameAction).where(
            GameAction.game_id == game.id,
            GameAction.phase_seq == game.phase_seq,
            GameAction.actor_telegram_id == uid,
        ))
        if existing is not None:
            return

        actor.afk_count += 1
        actor_state = dict(actor.state_json or {})
        actor_state["guard"] = True
        actor.state_json = actor_state
        state["last_action"] = {
            "user_id": uid,
            "result": "guard",
            "action": "guard",
            "timeout": True,
        }
        session.add(GameAction(
            game_id=game.id,
            round_no=game.round_no,
            phase_seq=game.phase_seq,
            actor_telegram_id=uid,
            action_type="arena_timeout_guard",
            payload_json={"action": "guard", "timeout": True},
        ))
        await session.flush()

        alive = [player for player in players if player.status == "alive"]
        if len(alive) <= 1 or game.round_no >= MAX_ROUNDS:
            winner = alive[0] if len(alive) == 1 else max(
                alive,
                key=lambda player: (int((player.state_json or {}).get("hp") or 0), player.score),
            )
            game.state_json = state
            await self._finish(session, game, winner.user_telegram_id)
            return

        order = [int(user_id) for user_id in list(state.get("turn_order") or [])]
        index = int(state.get("turn_index") or 0)
        alive_ids = {player.user_telegram_id for player in alive}
        for _ in range(len(order)):
            index = (index + 1) % len(order)
            if order[index] in alive_ids:
                break
        state["turn_index"] = index
        state["turn_user_id"] = order[index]
        game.state_json = state
        game.phase_seq += 1
        game.round_no += 1
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        await session.commit()

    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        if game.phase == "recovering":
            game.status = GameSessionStatus.RUNNING.value
            game.phase = ArenaPhase.TURN.value
            game.phase_seq += 1
        if game.status == GameSessionStatus.RUNNING.value and game.deadline_at is None:
            game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        await session.commit()

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
            await self._finish(session, game, winner_id=alive[0].user_telegram_id)
        return LeaveResult.REMOVED

    async def sync_ui(self, bot, session: AsyncSession, game: GameSession) -> None:
        from app.games.arena.presentation import sync_arena_ui
        await sync_arena_ui(bot, session, game)
