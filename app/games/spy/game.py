from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import random

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.game_models import (
    GameAction,
    GameGroupSettings,
    GamePlayer,
    GameResult,
    GameSession,
)
from app.games.base import BaseGame, LeaveResult
from app.games.config import GameDefinition
from app.games.enums import GameSessionStatus
from app.games.stats import apply_game_result


log = structlog.get_logger()


class SpyPhase(StrEnum):
    DISCUSSION = "discussion"
    VOTING = "voting"
    SPY_GUESS = "spy_guess"
    FINISHED = "finished"


spy_definition = GameDefinition(
    code="spy",
    title="🕵️ Шпион",
    min_players=4,
    max_players=8,
    exclusive_group_game=True,
    supports_rating=True,
    supports_spectators=False,
    uses_private_mapping=True,
    default_timeout_seconds=60,
)


LOCATIONS = (
    "Аэропорт",
    "Банк",
    "Больница",
    "Военная база",
    "Зоопарк",
    "Казино",
    "Кинотеатр",
    "Космическая станция",
    "Отель",
    "Пиратский корабль",
    "Пляж",
    "Полицейский участок",
    "Ресторан",
    "Супермаркет",
    "Театр",
    "Университет",
)
LOCATION_OPTIONS_PER_GAME = 8


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


class SpyGame(BaseGame):
    definition = spy_definition

    async def _players(
        self,
        session: AsyncSession,
        game_id: int,
        *,
        for_update: bool = False,
    ) -> list[GamePlayer]:
        query = (
            select(GamePlayer)
            .where(GamePlayer.game_id == game_id)
            .order_by(GamePlayer.id)
        )
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
            raise ValueError("not enough players for spy")

        settings = await session.get(GameGroupSettings, game.group_id)
        all_settings = dict(settings.settings_json or {}) if settings is not None else {}
        spy_settings = dict(all_settings.get("spy") or {})
        discussion_seconds = _bounded_int(
            spy_settings.get("discussion_seconds"), 180, 30, 900
        )
        voting_seconds = _bounded_int(
            spy_settings.get("voting_seconds"), 60, 15, 300
        )
        guess_seconds = _bounded_int(spy_settings.get("guess_seconds"), 45, 15, 180)

        rng = random.SystemRandom()
        location = rng.choice(LOCATIONS)
        distractors = [candidate for candidate in LOCATIONS if candidate != location]
        options = [location, *rng.sample(distractors, LOCATION_OPTIONS_PER_GAME - 1)]
        rng.shuffle(options)
        spy_player = rng.choice(players)
        for player in players:
            is_spy = player.id == spy_player.id
            player.role = "spy" if is_spy else "local"
            player.team = "spy" if is_spy else "locals"
            player.status = "alive"
            player.afk_count = 0
            player.state_json = {"result_applied": False}

        game.phase = SpyPhase.DISCUSSION.value
        game.phase_seq += 1
        game.round_no = 1
        game.state_json = {
            "location": location,
            "location_options": options,
            "spy_user_id": spy_player.user_telegram_id,
            "last_vote": None,
            "timers": {
                "discussion": discussion_seconds,
                "voting": voting_seconds,
                "guess": guess_seconds,
            },
        }
        game.deadline_at = datetime.now(timezone.utc) + timedelta(
            seconds=discussion_seconds
        )
        await session.commit()
        log.info(
            "spy_started",
            game_id=game.id,
            group_id=game.group_id,
            players=len(players),
        )

    async def handle_action(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        action: str,
        value: int | str | None = None,
    ) -> None:
        raise ValueError(f"unsupported spy action: {action}")

    async def _finish(
        self,
        session: AsyncSession,
        game: GameSession,
        winning_team: str,
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
        game.phase = SpyPhase.FINISHED.value
        game.phase_seq += 1
        game.deadline_at = None
        game.finished_at = now
        game.finish_reason = f"winner:{winning_team}"
        duration = (
            int((now - game.started_at).total_seconds()) if game.started_at else None
        )
        existing_result = await session.scalar(
            select(GameResult).where(GameResult.game_id == game.id)
        )
        state = dict(game.state_json or {})
        if existing_result is None:
            session.add(
                GameResult(
                    game_id=game.id,
                    group_id=game.group_id,
                    game_type=game.game_type,
                    winner_type="team",
                    winner_json={"team": winning_team},
                    summary_json={
                        "rounds": game.round_no,
                        "players": len(players),
                        "location": state.get("location"),
                        "suspect_user_id": (state.get("last_vote") or {}).get(
                            "suspect_user_id"
                        ),
                    },
                    duration_seconds=duration,
                )
            )
        for player in players:
            player_state = dict(player.state_json or {})
            if player_state.get("result_applied"):
                continue
            await apply_game_result(
                session,
                group_id=game.group_id,
                game_type=game.game_type,
                user_telegram_id=player.user_telegram_id,
                won=player.team == winning_team,
                rating_enabled=rating_enabled,
                commit=False,
            )
            player_state["result_applied"] = True
            player.state_json = player_state
        await session.commit()
        log.info("spy_finished", game_id=game.id, winner=winning_team)

    async def handle_leave(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
    ) -> LeaveResult:
        """Обрабатывает выход игрока из шпиона.

        - Уход spy → CANCELLED (spy уникален, без него игра не работает).
        - Уход local → REMOVED; если locals < 2 — finish с победой spy.
        """
        player = await session.scalar(
            select(GamePlayer).where(
                GamePlayer.game_id == game.id,
                GamePlayer.user_telegram_id == actor_telegram_id,
            ).with_for_update()
        )
        if player is None or player.role is None:
            return LeaveResult.REMOVED

        # Помечаем left ДО проверки баланса.
        player.status = "left"
        player.left_at = datetime.now(timezone.utc)

        if player.role == "spy":
            return LeaveResult.CANCELLED

        alive = list((await session.scalars(
            select(GamePlayer).where(
                GamePlayer.game_id == game.id,
                GamePlayer.status == "alive",
            )
        )).all())
        locals_count = sum(1 for p in alive if p.team == "locals")
        if locals_count < 2:
            await self._finish(session, game, "spy")
        return LeaveResult.REMOVED

    async def _resolve_vote(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        expected_phase_seq: int,
    ) -> bool:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != SpyPhase.VOTING.value
            or game.phase_seq != expected_phase_seq
        ):
            return False
        actions = list(
            (
                await session.scalars(
                    select(GameAction).where(
                        GameAction.game_id == game.id,
                        GameAction.phase_seq == game.phase_seq,
                        GameAction.action_type == "spy_vote",
                    )
                )
            ).all()
        )
        counts = Counter(
            action.target_telegram_id
            for action in actions
            if action.target_telegram_id is not None
        )
        suspect_id = None
        tie = True
        if counts:
            highest = max(counts.values())
            leaders = [
                user_id for user_id, count in counts.items() if count == highest
            ]
            if len(leaders) == 1:
                suspect_id = leaders[0]
                tie = False
        state = dict(game.state_json or {})
        state["last_vote"] = {
            "suspect_user_id": suspect_id,
            "tie": tie,
            "votes": len(actions),
        }
        game.state_json = state
        spy_user_id = state.get("spy_user_id")
        if suspect_id is None or suspect_id != spy_user_id:
            await self._finish(session, game, "spy")
            return True
        game.phase = SpyPhase.SPY_GUESS.value
        game.phase_seq += 1
        timers = dict(state.get("timers") or {})
        game.deadline_at = datetime.now(timezone.utc) + timedelta(
            seconds=_bounded_int(timers.get("guess"), 45, 15, 180)
        )
        await session.commit()
        return True

    async def maybe_advance_if_ready(
        self,
        session: AsyncSession,
        game: GameSession,
    ) -> bool:
        game = await session.get(GameSession, game.id)
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != SpyPhase.VOTING.value
        ):
            return False
        total = len(await self._players(session, game.id))
        acted = len(
            list(
                (
                    await session.scalars(
                        select(GameAction.id).where(
                            GameAction.game_id == game.id,
                            GameAction.phase_seq == game.phase_seq,
                            GameAction.action_type == "spy_vote",
                        )
                    )
                ).all()
            )
        )
        if acted < total:
            return False
        return await self._resolve_vote(
            session,
            game,
            expected_phase_seq=game.phase_seq,
        )

    async def guess_location(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        number: int,
    ) -> tuple[str, bool]:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != SpyPhase.SPY_GUESS.value
        ):
            raise ValueError("guess phase is not active")
        state = dict(game.state_json or {})
        if actor_telegram_id != state.get("spy_user_id"):
            raise PermissionError("only spy may guess")
        existing = await session.scalar(
            select(GameAction).where(
                GameAction.game_id == game.id,
                GameAction.phase_seq == game.phase_seq,
                GameAction.actor_telegram_id == actor_telegram_id,
                GameAction.action_type == "spy_location_guess",
            )
        )
        if existing is not None:
            guessed = str((existing.payload_json or {}).get("location") or "")
            return guessed, False
        options = list(state.get("location_options") or [])
        if number < 1 or number > len(options):
            raise ValueError("invalid location number")
        guessed = str(options[number - 1])
        session.add(
            GameAction(
                game_id=game.id,
                round_no=game.round_no,
                phase_seq=game.phase_seq,
                actor_telegram_id=actor_telegram_id,
                action_type="spy_location_guess",
                target_telegram_id=None,
                payload_json={"number": number, "location": guessed},
            )
        )
        await session.flush()
        winning_team = "spy" if guessed == state.get("location") else "locals"
        await self._finish(session, game, winning_team)
        return guessed, True

    async def handle_timeout(
        self,
        session: AsyncSession,
        game: GameSession,
    ) -> None:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if game is None or game.status != GameSessionStatus.RUNNING.value:
            return
        if game.phase == SpyPhase.DISCUSSION.value:
            state = dict(game.state_json or {})
            timers = dict(state.get("timers") or {})
            game.phase = SpyPhase.VOTING.value
            game.phase_seq += 1
            game.deadline_at = datetime.now(timezone.utc) + timedelta(
                seconds=_bounded_int(timers.get("voting"), 60, 15, 300)
            )
            await session.commit()
            return
        if game.phase == SpyPhase.VOTING.value:
            await self._resolve_vote(
                session,
                game,
                expected_phase_seq=game.phase_seq,
            )
            return
        if game.phase == SpyPhase.SPY_GUESS.value:
            await self._finish(session, game, "locals")
            return
        raise ValueError(f"unsupported spy timeout phase: {game.phase}")

    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(
            select(GameSession).where(GameSession.id == game.id).with_for_update()
        )
        if game is None:
            return
        if game.phase == "recovering":
            players = await self._players(session, game.id)
            if not players:
                raise ValueError("spy game has no players")
            state = dict(game.state_json or {})
            has_roles = sum(player.role == "spy" for player in players) == 1
            has_state = bool(
                state.get("location")
                and state.get("spy_user_id")
                and state.get("location_options")
            )
            if not has_roles or not has_state:
                game.status = GameSessionStatus.RUNNING.value
                await session.commit()
                await self.start(session, game)
                return
            game.status = GameSessionStatus.RUNNING.value
            game.phase = SpyPhase.DISCUSSION.value
            game.phase_seq += 1
        if game.phase not in {
            SpyPhase.DISCUSSION.value,
            SpyPhase.VOTING.value,
            SpyPhase.SPY_GUESS.value,
            SpyPhase.FINISHED.value,
        }:
            raise ValueError(f"unknown spy phase: {game.phase}")
        if game.status == GameSessionStatus.RUNNING.value and game.deadline_at is None:
            state = dict(game.state_json or {})
            timers = dict(state.get("timers") or {})
            defaults = {
                SpyPhase.DISCUSSION.value: ("discussion", 180, 30, 900),
                SpyPhase.VOTING.value: ("voting", 60, 15, 300),
                SpyPhase.SPY_GUESS.value: ("guess", 45, 15, 180),
            }
            if game.phase in defaults:
                key, default, minimum, maximum = defaults[game.phase]
                game.deadline_at = datetime.now(timezone.utc) + timedelta(
                    seconds=_bounded_int(
                        timers.get(key),
                        default,
                        minimum,
                        maximum,
                    )
                )
        await session.commit()

    async def sync_ui(self, bot, session: AsyncSession, game: GameSession) -> None:
        from app.games.spy.presentation import sync_spy_ui

        await sync_spy_ui(bot, session, game)
