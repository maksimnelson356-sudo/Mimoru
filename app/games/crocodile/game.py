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
ROUND_TIMEOUT_SECONDS = 90

WORDS = (
    "самолёт", "телескоп", "пингвин", "будильник", "чемодан", "пожарный",
    "дельфин", "робот", "фотограф", "пылесос", "скейтборд", "дирижёр",
    "водопад", "динозавр", "космонавт", "хамелеон", "пианино", "детектив",
    "подводная лодка", "воздушный шар", "снежная буря", "американские горки",
)


class CrocodilePhase(StrEnum):
    ROUND = "round"
    FINISHED = "finished"


crocodile_definition = GameDefinition(
    code="crocodile",
    title="🐊 Крокодил",
    min_players=3,
    max_players=20,
    exclusive_group_game=True,
    supports_rating=True,
    supports_spectators=False,
    uses_private_mapping=True,
    default_timeout_seconds=ROUND_TIMEOUT_SECONDS,
)


class CrocodileGame(BaseGame):
    definition = crocodile_definition

    async def _players(
        self,
        session: AsyncSession,
        game_id: int,
        *,
        statuses: tuple[str, ...] | None = None,
        for_update: bool = False,
    ) -> list[GamePlayer]:
        query = select(GamePlayer).where(GamePlayer.game_id == game_id)
        if statuses:
            query = query.where(GamePlayer.status.in_(statuses))
        query = query.order_by(GamePlayer.id)
        if for_update:
            query = query.with_for_update()
        return list((await session.scalars(query)).all())

    async def start(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if game is None:
            return
        players = await self._players(
            session,
            game.id,
            statuses=("joined",),
            for_update=True,
        )
        if len(players) < self.definition.min_players:
            raise ValueError("not enough players for crocodile")

        rng = random.SystemRandom()
        order = [player.user_telegram_id for player in players]
        rng.shuffle(order)
        words = rng.sample(list(WORDS), min(len(order), len(WORDS)))
        for player in players:
            player.status = "alive"
            player.score = 0
            player.afk_count = 0
            player.state_json = {"result_applied": False}

        game.phase = CrocodilePhase.ROUND.value
        game.phase_seq += 1
        game.round_no = 1
        game.state_json = {
            "host_order": order,
            "round_index": 0,
            "host_user_id": order[0],
            "words": words,
            "current_word": words[0],
            "last_round": None,
            "round_timeout_seconds": ROUND_TIMEOUT_SECONDS,
        }
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=ROUND_TIMEOUT_SECONDS)
        await session.commit()
        log.info("crocodile_started", game_id=game.id, group_id=game.group_id, players=len(players))

    async def handle_action(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        action: str,
        value: int | str | None = None,
    ) -> None:
        if action == "guessed":
            await self.mark_guessed(
                session,
                game,
                actor_telegram_id=actor_telegram_id,
                guesser_telegram_id=int(value or 0),
            )
            return
        if action == "skip":
            await self.skip_round(session, game, actor_telegram_id=actor_telegram_id)
            return
        raise ValueError(f"unsupported crocodile action: {action}")

    async def _advance_round(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        expected_phase_seq: int,
        result: dict,
    ) -> bool:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != CrocodilePhase.ROUND.value
            or game.phase_seq != expected_phase_seq
        ):
            return False

        state = dict(game.state_json or {})
        state["last_round"] = result
        next_index = int(state.get("round_index") or 0) + 1
        order = [int(value) for value in list(state.get("host_order") or [])]
        words = [str(value) for value in list(state.get("words") or [])]
        if next_index >= len(order):
            game.state_json = state
            await self._finish(session, game)
            return True

        state["round_index"] = next_index
        state["host_user_id"] = order[next_index]
        state["current_word"] = words[next_index % len(words)]
        game.state_json = state
        game.round_no += 1
        game.phase_seq += 1
        seconds = int(state.get("round_timeout_seconds") or ROUND_TIMEOUT_SECONDS)
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await session.commit()
        return True

    async def mark_guessed(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        guesser_telegram_id: int,
    ) -> bool:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != CrocodilePhase.ROUND.value
        ):
            raise ValueError("crocodile round is not active")
        state = dict(game.state_json or {})
        if actor_telegram_id != int(state.get("host_user_id") or 0):
            raise PermissionError("only current host can confirm guess")
        if guesser_telegram_id == actor_telegram_id:
            raise ValueError("host cannot guess own word")

        guesser = await session.scalar(
            select(GamePlayer).where(
                GamePlayer.game_id == game.id,
                GamePlayer.user_telegram_id == guesser_telegram_id,
                GamePlayer.status == "alive",
            ).with_for_update()
        )
        host = await session.scalar(
            select(GamePlayer).where(
                GamePlayer.game_id == game.id,
                GamePlayer.user_telegram_id == actor_telegram_id,
                GamePlayer.status == "alive",
            ).with_for_update()
        )
        if guesser is None or host is None:
            raise PermissionError("player is not active")

        existing = await session.scalar(
            select(GameAction).where(
                GameAction.game_id == game.id,
                GameAction.phase_seq == game.phase_seq,
                GameAction.actor_telegram_id == actor_telegram_id,
                GameAction.action_type == "crocodile_guessed",
            )
        )
        if existing is not None:
            return False

        session.add(
            GameAction(
                game_id=game.id,
                round_no=game.round_no,
                phase_seq=game.phase_seq,
                actor_telegram_id=actor_telegram_id,
                action_type="crocodile_guessed",
                target_telegram_id=guesser_telegram_id,
                payload_json={"word": str(state.get("current_word") or "")},
            )
        )
        host.score += 1
        guesser.score += 1
        await session.flush()
        return await self._advance_round(
            session,
            game,
            expected_phase_seq=game.phase_seq,
            result={
                "host_user_id": actor_telegram_id,
                "guesser_user_id": guesser_telegram_id,
                "word": str(state.get("current_word") or ""),
                "result": "guessed",
            },
        )

    async def skip_round(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
    ) -> bool:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != CrocodilePhase.ROUND.value
        ):
            raise ValueError("crocodile round is not active")
        state = dict(game.state_json or {})
        if actor_telegram_id != int(state.get("host_user_id") or 0):
            raise PermissionError("only current host can skip")
        return await self._advance_round(
            session,
            game,
            expected_phase_seq=game.phase_seq,
            result={
                "host_user_id": actor_telegram_id,
                "word": str(state.get("current_word") or ""),
                "result": "skipped",
            },
        )

    async def _finish(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if game is None or game.status == GameSessionStatus.FINISHED.value:
            return
        players = await self._players(
            session,
            game.id,
            statuses=("alive",),
            for_update=True,
        )
        top_score = max((player.score for player in players), default=0)
        winners = [player.user_telegram_id for player in players if player.score == top_score]
        settings = await session.get(GameGroupSettings, game.group_id)
        rating_enabled = settings.rating_enabled if settings is not None else True
        now = datetime.now(timezone.utc)
        game.status = GameSessionStatus.FINISHED.value
        game.phase = CrocodilePhase.FINISHED.value
        game.phase_seq += 1
        game.deadline_at = None
        game.finished_at = now
        game.finish_reason = "winner:players"
        duration = int((now - game.started_at).total_seconds()) if game.started_at else None
        if await session.scalar(select(GameResult).where(GameResult.game_id == game.id)) is None:
            session.add(
                GameResult(
                    game_id=game.id,
                    group_id=game.group_id,
                    game_type=game.game_type,
                    winner_type="players",
                    winner_json={"user_ids": winners, "score": top_score},
                    summary_json={"rounds": game.round_no, "players": len(players), "top_score": top_score},
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
                won=player.user_telegram_id in winners,
                rating_enabled=rating_enabled,
                commit=False,
            )
            state["result_applied"] = True
            player.state_json = state
        await session.commit()
        log.info("crocodile_finished", game_id=game.id, winners=winners, top_score=top_score)

    async def handle_leave(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
    ) -> LeaveResult:
        """Убирает игрока. Если остался 1 живой — завершает игру через _finish."""
        players = list((await session.scalars(
            select(GamePlayer).where(GamePlayer.game_id == game.id)
        )).all())
        alive = [p for p in players if p.status == "alive" and p.user_telegram_id != actor_telegram_id]
        if len(alive) <= 1:
            await self._finish(session, game)
        return LeaveResult.REMOVED

    async def handle_timeout(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if game is None or game.status != GameSessionStatus.RUNNING.value:
            return
        if game.phase != CrocodilePhase.ROUND.value:
            raise ValueError(f"unsupported crocodile timeout phase: {game.phase}")
        state = dict(game.state_json or {})
        host_id = int(state.get("host_user_id") or 0)
        host = await session.scalar(
            select(GamePlayer).where(
                GamePlayer.game_id == game.id,
                GamePlayer.user_telegram_id == host_id,
                GamePlayer.status == "alive",
            ).with_for_update()
        )
        if host is not None:
            host.afk_count += 1
        await session.flush()
        await self._advance_round(
            session,
            game,
            expected_phase_seq=game.phase_seq,
            result={
                "host_user_id": host_id,
                "word": str(state.get("current_word") or ""),
                "result": "timeout",
            },
        )

    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if game is None:
            return
        if game.phase == "recovering":
            state = dict(game.state_json or {})
            players = await self._players(session, game.id, statuses=("alive", "joined"))
            if not players:
                raise ValueError("crocodile game has no players")
            if not state.get("host_order") or not state.get("words"):
                game.status = GameSessionStatus.RUNNING.value
                await session.commit()
                await self.start(session, game)
                return
            game.status = GameSessionStatus.RUNNING.value
            game.phase = CrocodilePhase.ROUND.value
            game.phase_seq += 1
        if game.phase not in {CrocodilePhase.ROUND.value, CrocodilePhase.FINISHED.value}:
            raise ValueError(f"unknown crocodile phase: {game.phase}")
        if game.status == GameSessionStatus.RUNNING.value and game.deadline_at is None:
            state = dict(game.state_json or {})
            seconds = int(state.get("round_timeout_seconds") or ROUND_TIMEOUT_SECONDS)
            game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await session.commit()

    async def sync_ui(self, bot, session: AsyncSession, game: GameSession) -> None:
        from app.games.crocodile.presentation import sync_crocodile_ui

        await sync_crocodile_ui(bot, session, game)
