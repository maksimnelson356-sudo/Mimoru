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


class QuizPhase(StrEnum):
    QUESTION = "question"
    ROUND_RESULT = "round_result"
    FINISHED = "finished"


quiz_definition = GameDefinition(
    code="quiz",
    title="🧠 Квиз",
    min_players=2,
    max_players=20,
    exclusive_group_game=True,
    supports_rating=True,
    supports_spectators=False,
    uses_private_mapping=False,
    default_timeout_seconds=30,
)


QUESTION_BANK = (
    {"q": "Какая планета ближе всего к Солнцу?", "a": ("Венера", "Меркурий", "Марс", "Земля"), "correct": "Меркурий"},
    {"q": "Сколько сторон у правильного шестиугольника?", "a": ("5", "6", "7", "8"), "correct": "6"},
    {"q": "Столица Японии?", "a": ("Киото", "Осака", "Токио", "Нагоя"), "correct": "Токио"},
    {"q": "Какой океан самый большой?", "a": ("Атлантический", "Индийский", "Северный Ледовитый", "Тихий"), "correct": "Тихий"},
    {"q": "Сколько минут в двух часах?", "a": ("100", "110", "120", "140"), "correct": "120"},
    {"q": "Какой газ преобладает в атмосфере Земли?", "a": ("Кислород", "Азот", "Углекислый газ", "Водород"), "correct": "Азот"},
    {"q": "Кто написал «Войну и мир»?", "a": ("Пушкин", "Достоевский", "Толстой", "Чехов"), "correct": "Толстой"},
    {"q": "Сколько континентов обычно выделяют в школьной географии?", "a": ("5", "6", "7", "8"), "correct": "6"},
    {"q": "Какой металл обозначается символом Fe?", "a": ("Серебро", "Железо", "Медь", "Олово"), "correct": "Железо"},
    {"q": "Чему равен квадрат числа 12?", "a": ("124", "132", "144", "156"), "correct": "144"},
)


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


class QuizGame(BaseGame):
    definition = quiz_definition

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
        if len(players) < self.definition.min_players:
            raise ValueError("not enough players for quiz")

        settings = await session.get(GameGroupSettings, game.group_id)
        all_settings = dict(settings.settings_json or {}) if settings is not None else {}
        quiz_settings = dict(all_settings.get("quiz") or {})
        question_seconds = _bounded_int(quiz_settings.get("question_seconds"), 30, 10, 120)
        result_seconds = _bounded_int(quiz_settings.get("result_seconds"), 6, 3, 30)
        rounds_count = _bounded_int(quiz_settings.get("rounds"), 5, 3, min(10, len(QUESTION_BANK)))

        rng = random.SystemRandom()
        selected = rng.sample(list(QUESTION_BANK), rounds_count)
        rounds: list[dict] = []
        for item in selected:
            options = list(item["a"])
            rng.shuffle(options)
            rounds.append({
                "question": item["q"],
                "options": options,
                "correct_index": options.index(item["correct"]) + 1,
            })

        for player in players:
            player.status = "alive"
            player.score = 0
            player.afk_count = 0
            player.state_json = {"result_applied": False}

        game.phase = QuizPhase.QUESTION.value
        game.phase_seq += 1
        game.round_no = 1
        game.state_json = {
            "rounds": rounds,
            "round_index": 0,
            "last_round": None,
            "timers": {"question": question_seconds, "result": result_seconds},
        }
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=question_seconds)
        await session.commit()
        log.info("quiz_started", game_id=game.id, group_id=game.group_id, players=len(players), rounds=rounds_count)

    async def handle_action(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        action: str,
        value: int | str | None = None,
    ) -> None:
        if action != "answer":
            raise ValueError(f"unsupported quiz action: {action}")
        await self.answer(session, game, actor_telegram_id=actor_telegram_id, number=int(value or 0))

    async def answer(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        number: int,
    ) -> tuple[bool, bool]:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != QuizPhase.QUESTION.value:
            raise ValueError("question phase is not active")
        player = await session.scalar(
            select(GamePlayer).where(
                GamePlayer.game_id == game.id,
                GamePlayer.user_telegram_id == actor_telegram_id,
                GamePlayer.status == "alive",
            ).with_for_update()
        )
        if player is None:
            raise PermissionError("not a quiz player")
        existing = await session.scalar(
            select(GameAction).where(
                GameAction.game_id == game.id,
                GameAction.phase_seq == game.phase_seq,
                GameAction.actor_telegram_id == actor_telegram_id,
                GameAction.action_type == "quiz_answer",
            )
        )
        if existing is not None:
            return bool((existing.payload_json or {}).get("correct")), False

        state = dict(game.state_json or {})
        rounds = list(state.get("rounds") or [])
        index = int(state.get("round_index") or 0)
        if index < 0 or index >= len(rounds):
            raise ValueError("quiz round is missing")
        current = dict(rounds[index])
        options = list(current.get("options") or [])
        if number < 1 or number > len(options):
            raise ValueError("invalid answer number")
        correct = number == int(current.get("correct_index") or 0)
        session.add(GameAction(
            game_id=game.id,
            round_no=game.round_no,
            phase_seq=game.phase_seq,
            actor_telegram_id=actor_telegram_id,
            action_type="quiz_answer",
            target_telegram_id=None,
            payload_json={"number": number, "correct": correct},
        ))
        if correct:
            player.score += 1
        await session.commit()
        return correct, True

    async def maybe_advance_if_ready(self, session: AsyncSession, game: GameSession) -> bool:
        game = await session.get(GameSession, game.id)
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != QuizPhase.QUESTION.value:
            return False
        total = len(await self._players(session, game.id))
        answered = len(list((await session.scalars(
            select(GameAction.id).where(
                GameAction.game_id == game.id,
                GameAction.phase_seq == game.phase_seq,
                GameAction.action_type == "quiz_answer",
            )
        )).all()))
        if answered < total:
            return False
        return await self._close_question(session, game, expected_phase_seq=game.phase_seq)

    async def _close_question(self, session: AsyncSession, game: GameSession, *, expected_phase_seq: int) -> bool:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != QuizPhase.QUESTION.value
            or game.phase_seq != expected_phase_seq
        ):
            return False
        state = dict(game.state_json or {})
        rounds = list(state.get("rounds") or [])
        index = int(state.get("round_index") or 0)
        current = dict(rounds[index])
        answered = list((await session.scalars(
            select(GameAction).where(
                GameAction.game_id == game.id,
                GameAction.phase_seq == game.phase_seq,
                GameAction.action_type == "quiz_answer",
            )
        )).all())
        state["last_round"] = {
            "round_no": game.round_no,
            "correct_index": current.get("correct_index"),
            "correct_answer": (list(current.get("options") or [])[int(current.get("correct_index") or 1) - 1]),
            "answered": len(answered),
            "correct_count": sum(1 for action in answered if bool((action.payload_json or {}).get("correct"))),
        }
        game.state_json = state
        game.phase = QuizPhase.ROUND_RESULT.value
        game.phase_seq += 1
        timers = dict(state.get("timers") or {})
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=_bounded_int(timers.get("result"), 6, 3, 30))
        await session.commit()
        return True

    async def _next_round_or_finish(self, session: AsyncSession, game: GameSession, *, expected_phase_seq: int) -> bool:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != QuizPhase.ROUND_RESULT.value
            or game.phase_seq != expected_phase_seq
        ):
            return False
        state = dict(game.state_json or {})
        rounds = list(state.get("rounds") or [])
        next_index = int(state.get("round_index") or 0) + 1
        if next_index >= len(rounds):
            await self._finish(session, game)
            return True
        state["round_index"] = next_index
        game.state_json = state
        game.round_no += 1
        game.phase = QuizPhase.QUESTION.value
        game.phase_seq += 1
        timers = dict(state.get("timers") or {})
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=_bounded_int(timers.get("question"), 30, 10, 120))
        await session.commit()
        return True

    async def _finish(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status == GameSessionStatus.FINISHED.value:
            return
        players = await self._players(session, game.id, for_update=True)
        top_score = max((player.score for player in players), default=0)
        winners = [player.user_telegram_id for player in players if player.score == top_score]
        settings = await session.get(GameGroupSettings, game.group_id)
        rating_enabled = settings.rating_enabled if settings is not None else True
        now = datetime.now(timezone.utc)
        game.status = GameSessionStatus.FINISHED.value
        game.phase = QuizPhase.FINISHED.value
        game.phase_seq += 1
        game.deadline_at = None
        game.finished_at = now
        game.finish_reason = "winner:players"
        duration = int((now - game.started_at).total_seconds()) if game.started_at else None
        existing_result = await session.scalar(select(GameResult).where(GameResult.game_id == game.id))
        if existing_result is None:
            session.add(GameResult(
                game_id=game.id,
                group_id=game.group_id,
                game_type=game.game_type,
                winner_type="players",
                winner_json={"user_ids": winners, "score": top_score},
                summary_json={"rounds": game.round_no, "players": len(players), "top_score": top_score},
                duration_seconds=duration,
            ))
        for player in players:
            player_state = dict(player.state_json or {})
            if player_state.get("result_applied"):
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
            player_state["result_applied"] = True
            player.state_json = player_state
        await session.commit()
        log.info("quiz_finished", game_id=game.id, winners=winners, top_score=top_score)

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
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value:
            return
        if game.phase == QuizPhase.QUESTION.value:
            await self._close_question(session, game, expected_phase_seq=game.phase_seq)
            return
        if game.phase == QuizPhase.ROUND_RESULT.value:
            await self._next_round_or_finish(session, game, expected_phase_seq=game.phase_seq)
            return
        raise ValueError(f"unsupported quiz timeout phase: {game.phase}")

    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        if game.phase == "recovering":
            state = dict(game.state_json or {})
            players = await self._players(session, game.id)
            if not players:
                raise ValueError("quiz game has no players")
            if not state.get("rounds"):
                game.status = GameSessionStatus.RUNNING.value
                await session.commit()
                await self.start(session, game)
                return
            game.status = GameSessionStatus.RUNNING.value
            game.phase = QuizPhase.QUESTION.value
            game.phase_seq += 1
        if game.phase not in {QuizPhase.QUESTION.value, QuizPhase.ROUND_RESULT.value, QuizPhase.FINISHED.value}:
            raise ValueError(f"unknown quiz phase: {game.phase}")
        if game.status == GameSessionStatus.RUNNING.value and game.deadline_at is None:
            state = dict(game.state_json or {})
            timers = dict(state.get("timers") or {})
            if game.phase == QuizPhase.QUESTION.value:
                seconds = _bounded_int(timers.get("question"), 30, 10, 120)
            else:
                seconds = _bounded_int(timers.get("result"), 6, 3, 30)
            game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await session.commit()

    async def sync_ui(self, bot, session: AsyncSession, game: GameSession) -> None:
        from app.games.quiz.presentation import sync_quiz_ui
        await sync_quiz_ui(bot, session, game)
