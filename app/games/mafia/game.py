from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
import random

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.game_models import GameAction, GameGroupSettings, GamePlayer, GameSession
from app.games.base import BaseGame, LeaveResult
from app.games.config import GameDefinition
from app.games.enums import GameSessionStatus
from app.games.mafia.resolution import finish_game, resolve_day_vote, resolve_night, winner


log = structlog.get_logger()


class MafiaPhase(StrEnum):
    ROLE_ASSIGNMENT = "role_assignment"
    DAY_START = "day_start"
    DISCUSSION = "discussion"
    DAY_VOTING = "day_voting"
    VOTING_RESULT = "voting_result"
    NIGHT_START = "night_start"
    NIGHT_ACTIONS = "night_actions"
    NIGHT_RESULT = "night_result"
    FINISHED = "finished"


RECOVERABLE_MAFIA_PHASES = frozenset({
    MafiaPhase.DAY_START.value,
    MafiaPhase.DISCUSSION.value,
    MafiaPhase.DAY_VOTING.value,
    MafiaPhase.VOTING_RESULT.value,
    MafiaPhase.NIGHT_START.value,
    MafiaPhase.NIGHT_ACTIONS.value,
    MafiaPhase.NIGHT_RESULT.value,
})


mafia_definition = GameDefinition(
    code="mafia",
    title="🐺 Мафия",
    min_players=4,
    max_players=15,
    exclusive_group_game=True,
    supports_rating=True,
    supports_spectators=False,
    uses_private_mapping=True,
    default_timeout_seconds=60,
)


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _remember_finish_context(game: GameSession, phase: str) -> None:
    state = dict(game.state_json or {})
    state["finish_context"] = {"phase": phase}
    game.state_json = state


class MafiaGame(BaseGame):
    definition = mafia_definition

    async def _players(self, session: AsyncSession, game: GameSession) -> list[GamePlayer]:
        return list((await session.scalars(
            select(GamePlayer)
            .where(GamePlayer.game_id == game.id, GamePlayer.status.in_(("joined", "alive")))
            .order_by(GamePlayer.id)
            .with_for_update()
        )).all())

    @staticmethod
    def _role_deck(count: int) -> list[str]:
        mafia_count = max(1, count // 4)
        roles = ["mafia"] * mafia_count
        roles.extend(["doctor", "commissioner"])
        roles.extend(["civilian"] * (count - len(roles)))
        random.SystemRandom().shuffle(roles)
        return roles

    @staticmethod
    def _seconds(game: GameSession, key: str, default: int) -> int:
        state = dict(game.state_json or {})
        timers = dict(state.get("timers") or {})
        return _bounded_int(timers.get(key), default, 3, 600)

    async def start(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        players = await self._players(session, game)
        if len(players) < self.definition.min_players:
            raise ValueError("not enough players for mafia")
        settings = await session.get(GameGroupSettings, game.group_id)
        all_settings = dict(settings.settings_json or {}) if settings is not None else {}
        mafia_settings = dict(all_settings.get("mafia") or {})
        timers = {
            "day_start": _bounded_int(mafia_settings.get("day_start_seconds"), 15, 3, 120),
            "discussion": _bounded_int(mafia_settings.get("discussion_seconds"), 90, 15, 600),
            "voting": _bounded_int(mafia_settings.get("voting_seconds"), 60, 15, 300),
            "result": _bounded_int(mafia_settings.get("result_seconds"), 10, 3, 60),
            "night_start": _bounded_int(mafia_settings.get("night_start_seconds"), 10, 3, 60),
            "night_actions": _bounded_int(mafia_settings.get("night_seconds"), 60, 15, 300),
        }
        roles = self._role_deck(len(players))
        for player, role in zip(players, roles, strict=True):
            player.role = role
            player.team = "mafia" if role == "mafia" else "town"
            player.status = "alive"
            player.afk_count = 0
            player.state_json = {"role_revealed": False, "result_applied": False}
        game.phase = MafiaPhase.DAY_START.value
        game.phase_seq += 1
        game.round_no = 1
        game.state_json = {
            "day": 1,
            "doctor_can_self_heal": bool(mafia_settings.get("doctor_can_self_heal", True)),
            "doctor_can_heal_same_player_twice": bool(mafia_settings.get("doctor_can_heal_same_player_twice", False)),
            "afk_strikes_to_remove": _bounded_int(mafia_settings.get("afk_strikes_to_remove"), 2, 1, 5),
            "last_healed_user_id": None,
            "last_day_result": None,
            "last_night_result": None,
            "last_afk_removed": [],
            "finish_context": None,
            "timers": timers,
        }
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=timers["day_start"])
        await session.commit()
        log.info("mafia_started", game_id=game.id, group_id=game.group_id, players=len(players))

    async def handle_action(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        action: str,
        value: int | str | None = None,
    ) -> None:
        raise ValueError(f"unsupported mafia action: {action}")

    async def _penalize_missing_actions(self, session: AsyncSession, game: GameSession, phase: str) -> None:
        players = list((await session.scalars(
            select(GamePlayer)
            .where(GamePlayer.game_id == game.id, GamePlayer.status == "alive")
            .with_for_update()
        )).all())
        if phase == MafiaPhase.DAY_VOTING.value:
            required = {player.user_telegram_id for player in players}
            action_types = ("day_vote",)
        elif phase == MafiaPhase.NIGHT_ACTIONS.value:
            required = {
                player.user_telegram_id
                for player in players
                if player.role in {"mafia", "doctor", "commissioner"}
            }
            action_types = ("mafia_kill", "doctor_heal", "commissioner_check")
        else:
            return
        acted = set((await session.scalars(
            select(GameAction.actor_telegram_id).where(
                GameAction.game_id == game.id,
                GameAction.phase_seq == game.phase_seq,
                GameAction.action_type.in_(action_types),
            )
        )).all())
        removed: list[str] = []
        threshold = _bounded_int((game.state_json or {}).get("afk_strikes_to_remove"), 2, 1, 5)
        for player in players:
            if player.user_telegram_id not in required or player.user_telegram_id in acted:
                continue
            player.afk_count += 1
            if player.afk_count >= threshold:
                player.status = "dead"
                removed.append(player.display_name)
        state = dict(game.state_json or {})
        state["last_afk_removed"] = removed
        game.state_json = state
        if removed:
            log.info("mafia_afk_removed", game_id=game.id, phase=phase, count=len(removed))

    async def _advance_phase(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        expected_phase_seq: int,
    ) -> bool:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value:
            return False
        if game.phase_seq != expected_phase_seq:
            log.info(
                "mafia_phase_advance_stale",
                game_id=game.id,
                expected_phase_seq=expected_phase_seq,
                current_phase_seq=game.phase_seq,
                phase=game.phase,
            )
            return False
        now = datetime.now(timezone.utc)
        current = game.phase
        if current == MafiaPhase.DAY_START.value:
            game.phase = MafiaPhase.DISCUSSION.value
            game.phase_seq += 1
            game.deadline_at = now + timedelta(seconds=self._seconds(game, "discussion", 90))
        elif current == MafiaPhase.DISCUSSION.value:
            game.phase = MafiaPhase.DAY_VOTING.value
            game.phase_seq += 1
            game.deadline_at = now + timedelta(seconds=self._seconds(game, "voting", 60))
        elif current == MafiaPhase.DAY_VOTING.value:
            await resolve_day_vote(session, game)
            await self._penalize_missing_actions(session, game, current)
            winning_team = await winner(session, game.id)
            if winning_team is not None:
                _remember_finish_context(game, current)
                await finish_game(session, game, winning_team)
                log.info("mafia_finished", game_id=game.id, winner=winning_team, phase=current)
                return True
            game.phase = MafiaPhase.VOTING_RESULT.value
            game.phase_seq += 1
            game.deadline_at = now + timedelta(seconds=self._seconds(game, "result", 10))
        elif current == MafiaPhase.VOTING_RESULT.value:
            game.phase = MafiaPhase.NIGHT_START.value
            game.phase_seq += 1
            game.deadline_at = now + timedelta(seconds=self._seconds(game, "night_start", 10))
        elif current == MafiaPhase.NIGHT_START.value:
            game.phase = MafiaPhase.NIGHT_ACTIONS.value
            game.phase_seq += 1
            game.deadline_at = now + timedelta(seconds=self._seconds(game, "night_actions", 60))
        elif current == MafiaPhase.NIGHT_ACTIONS.value:
            await resolve_night(session, game)
            await self._penalize_missing_actions(session, game, current)
            winning_team = await winner(session, game.id)
            if winning_team is not None:
                _remember_finish_context(game, current)
                await finish_game(session, game, winning_team)
                log.info("mafia_finished", game_id=game.id, winner=winning_team, phase=current)
                return True
            game.phase = MafiaPhase.NIGHT_RESULT.value
            game.phase_seq += 1
            game.deadline_at = now + timedelta(seconds=self._seconds(game, "result", 10))
        elif current == MafiaPhase.NIGHT_RESULT.value:
            game.round_no += 1
            state = dict(game.state_json or {})
            state["day"] = int(state.get("day", 1)) + 1
            state["last_afk_removed"] = []
            game.state_json = state
            game.phase = MafiaPhase.DAY_START.value
            game.phase_seq += 1
            game.deadline_at = now + timedelta(seconds=self._seconds(game, "day_start", 15))
        else:
            return False
        await session.commit()
        log.info(
            "mafia_phase_changed",
            game_id=game.id,
            from_phase=current,
            to_phase=game.phase,
            phase_seq=game.phase_seq,
            round_no=game.round_no,
        )
        return True

    async def handle_timeout(self, session: AsyncSession, game: GameSession) -> None:
        await self._advance_phase(session, game, expected_phase_seq=game.phase_seq)

    async def handle_leave(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
    ) -> LeaveResult:
        """Обрабатывает выход игрока из мафии.

        - Уход mafia → CANCELLED (баланс нарушен).
        - Уход civilian/doctor/commissioner → REMOVED.
          После удаления проверяется winner: если он определён — игра завершается.
        """
        from app.games.mafia.resolution import finish_game, winner

        player = await session.scalar(
            select(GamePlayer).where(
                GamePlayer.game_id == game.id,
                GamePlayer.user_telegram_id == actor_telegram_id,
            ).with_for_update()
        )
        if player is None or player.role is None:
            return LeaveResult.REMOVED

        # Помечаем left ДО winner() — чтобы игрок не считался живым.
        player.status = "left"
        player.left_at = datetime.now(timezone.utc)

        if player.role == "mafia":
            return LeaveResult.CANCELLED

        winning_team = await winner(session, game.id)
        if winning_team is not None:
            await finish_game(session, game, winning_team)
        return LeaveResult.REMOVED

    async def maybe_advance_if_ready(self, session: AsyncSession, game: GameSession) -> bool:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value:
            return False
        expected_phase_seq = game.phase_seq
        alive = list((await session.scalars(
            select(GamePlayer).where(GamePlayer.game_id == game.id, GamePlayer.status == "alive")
        )).all())
        if game.phase == MafiaPhase.DAY_VOTING.value:
            actors = {player.user_telegram_id for player in alive}
            voted = set((await session.scalars(
                select(GameAction.actor_telegram_id).where(
                    GameAction.game_id == game.id,
                    GameAction.phase_seq == game.phase_seq,
                    GameAction.action_type == "day_vote",
                )
            )).all())
            if actors and actors <= voted:
                return await self._advance_phase(
                    session,
                    game,
                    expected_phase_seq=expected_phase_seq,
                )
        elif game.phase == MafiaPhase.NIGHT_ACTIONS.value:
            required = {
                player.user_telegram_id
                for player in alive
                if player.role in {"mafia", "doctor", "commissioner"}
            }
            acted = set((await session.scalars(
                select(GameAction.actor_telegram_id).where(
                    GameAction.game_id == game.id,
                    GameAction.phase_seq == game.phase_seq,
                    GameAction.action_type.in_(("mafia_kill", "doctor_heal", "commissioner_check")),
                )
            )).all())
            if required and required <= acted:
                return await self._advance_phase(
                    session,
                    game,
                    expected_phase_seq=expected_phase_seq,
                )
        return False

    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status not in {
            GameSessionStatus.RUNNING.value,
            GameSessionStatus.RECOVERING.value,
        }:
            return

        if game.phase in {"starting", "recovering"}:
            game.status = GameSessionStatus.RUNNING.value
            await self.start(session, game)
            log.info(
                "mafia_start_recovered",
                game_id=game.id,
                phase=game.phase,
                phase_seq=game.phase_seq,
            )
            return

        if game.phase not in RECOVERABLE_MAFIA_PHASES:
            raise ValueError(f"invalid mafia recovery phase: {game.phase}")

        game.status = GameSessionStatus.RUNNING.value
        if game.deadline_at is None:
            game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=self.definition.default_timeout_seconds)
        await session.commit()
        log.info("mafia_restored", game_id=game.id, phase=game.phase, phase_seq=game.phase_seq)

    async def sync_ui(self, bot, session: AsyncSession, game: GameSession) -> None:
        from app.games.mafia.presentation import sync_mafia_ui

        await sync_mafia_ui(bot, session, game)
