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
MAX_STRIKES = 3
MAX_ROUNDS = 40
OPTION_LIMIT = 6
WORD_POOL = (
    "арбуз", "замок", "кошка", "апельсин", "носорог", "гора", "автобус", "самолёт",
    "торт", "трава", "ананас", "собака", "акула", "альбом", "молоко", "окно",
    "облако", "огород", "дом", "машина", "аптека", "карандаш", "шар", "ракета",
    "арбузик", "книга", "авокадо", "океан", "небо", "остров", "волна", "апрель",
    "лимон", "ночь", "чай", "яблоко", "орех", "хомяк", "костёр", "река",
    "август", "телефон", "ножницы", "игра", "арбузный", "йогурт", "танк", "кран",
    "носок", "комета", "арбузовый", "йод", "дорога", "арбузина", "аккорд", "диван",
    "нос", "снег", "гитара", "аппарат", "театр", "робот", "тапок", "кепка",
    "арбузище", "ель", "лампа", "автор", "ручка", "арбузик", "кино", "обед",
    "дверь", "ёлка", "арбуз", "зебра", "арбузный", "йога", "арбузина", "актер",
)


class WordsPhase(StrEnum):
    TURN = "turn"
    FINISHED = "finished"


words_definition = GameDefinition(
    code="words",
    title="🔤 Слова",
    min_players=2,
    max_players=12,
    exclusive_group_game=True,
    supports_rating=True,
    supports_spectators=False,
    uses_private_mapping=False,
    default_timeout_seconds=TURN_TIMEOUT_SECONDS,
)


def last_letter(word: str) -> str:
    for char in reversed(word.casefold()):
        if char.isalpha() and char not in {"ь", "ъ", "ы", "й"}:
            return char
    return word.casefold()[-1]


def _options(required: str, used: set[str]) -> list[str]:
    candidates = sorted({word for word in WORD_POOL if word.casefold().startswith(required) and word not in used})
    random.SystemRandom().shuffle(candidates)
    return candidates[:OPTION_LIMIT]


class WordsGame(BaseGame):
    definition = words_definition

    async def _players(self, session: AsyncSession, game_id: int, *, for_update: bool = False) -> list[GamePlayer]:
        query = select(GamePlayer).where(GamePlayer.game_id == game_id).order_by(GamePlayer.id)
        if for_update:
            query = query.with_for_update()
        return list((await session.scalars(query)).all())

    @staticmethod
    def _alive(players: list[GamePlayer]) -> list[GamePlayer]:
        return [player for player in players if player.status == "alive"]

    @staticmethod
    def _winner(players: list[GamePlayer]) -> GamePlayer:
        ranked = sorted(
            players,
            key=lambda player: (-player.score, int((player.state_json or {}).get("strikes") or 0), player.id),
        )
        return ranked[0]

    @staticmethod
    def _next_alive_index(order: list[int], start_index: int, alive_ids: set[int]) -> int:
        index = start_index
        for _ in range(len(order)):
            index = (index + 1) % len(order)
            if order[index] in alive_ids:
                return index
        return start_index

    @staticmethod
    def _refresh_options(state: dict) -> None:
        used = set(state.get("used_words") or [])
        required = str(state.get("required_letter") or "а")
        options = _options(required, used)
        if not options:
            bridges = [word for word in sorted(set(WORD_POOL)) if word not in used]
            if bridges:
                bridge = random.SystemRandom().choice(bridges)
                used.add(bridge)
                state["used_words"] = list(used)
                state["current_word"] = bridge
                required = last_letter(bridge)
                state["required_letter"] = required
                options = _options(required, used)
        state["options"] = options

    async def start(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        players = [player for player in await self._players(session, game.id, for_update=True) if player.status == "joined"]
        if len(players) < self.definition.min_players:
            raise ValueError("not enough players for words")
        order = [player.user_telegram_id for player in players]
        random.SystemRandom().shuffle(order)
        seed = random.SystemRandom().choice(("арбуз", "кошка", "машина", "лимон", "гора", "телефон"))
        for player in players:
            player.status = "alive"
            player.score = 0
            player.afk_count = 0
            player.state_json = {"strikes": 0, "result_applied": False}
        state = {
            "turn_order": order,
            "turn_index": 0,
            "turn_user_id": order[0],
            "current_word": seed,
            "required_letter": last_letter(seed),
            "used_words": [seed],
            "options": [],
            "last_action": None,
        }
        self._refresh_options(state)
        game.phase = WordsPhase.TURN.value
        game.phase_seq += 1
        game.round_no = 1
        game.state_json = state
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        await session.commit()

    async def handle_action(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        action: str,
        value: int | str | None = None,
    ) -> None:
        if action == "pick":
            await self.pick_word(session, game, actor_telegram_id=actor_telegram_id, number=int(value or 0))
            return
        if action == "skip":
            await self.skip_turn(session, game, actor_telegram_id=actor_telegram_id)
            return
        raise ValueError("unsupported words action")

    async def _advance(self, session: AsyncSession, game: GameSession, state: dict, players: list[GamePlayer]) -> None:
        alive = self._alive(players)
        if len(alive) <= 1 or game.round_no >= MAX_ROUNDS:
            winner = alive[0] if len(alive) == 1 else self._winner(alive or players)
            game.state_json = state
            await self._finish(session, game, winner.user_telegram_id, players)
            return
        order = [int(value) for value in list(state.get("turn_order") or [])]
        alive_ids = {player.user_telegram_id for player in alive}
        next_index = self._next_alive_index(order, int(state.get("turn_index") or 0), alive_ids)
        state["turn_index"] = next_index
        state["turn_user_id"] = order[next_index]
        self._refresh_options(state)
        game.state_json = state
        game.phase_seq += 1
        game.round_no += 1
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        await session.commit()

    async def pick_word(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        number: int,
    ) -> tuple[str, bool]:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != WordsPhase.TURN.value:
            raise ValueError("words turn inactive")
        state = dict(game.state_json or {})
        if actor_telegram_id != int(state.get("turn_user_id") or 0):
            raise PermissionError("not your turn")
        existing = await session.scalar(select(GameAction).where(
            GameAction.game_id == game.id,
            GameAction.phase_seq == game.phase_seq,
            GameAction.actor_telegram_id == actor_telegram_id,
        ))
        if existing is not None:
            return "repeat", False
        options = list(state.get("options") or [])
        if number < 1 or number > len(options):
            raise ValueError("invalid option")
        word = str(options[number - 1])
        players = await self._players(session, game.id, for_update=True)
        actor = next((player for player in players if player.user_telegram_id == actor_telegram_id and player.status == "alive"), None)
        if actor is None:
            raise PermissionError("not alive")
        actor.score += 1
        used = list(state.get("used_words") or [])
        used.append(word)
        state["used_words"] = used
        state["current_word"] = word
        state["required_letter"] = last_letter(word)
        state["last_action"] = {"user_id": actor_telegram_id, "type": "pick", "word": word}
        session.add(GameAction(
            game_id=game.id,
            round_no=game.round_no,
            phase_seq=game.phase_seq,
            actor_telegram_id=actor_telegram_id,
            action_type="words_pick",
            payload_json={"word": word, "number": number},
        ))
        await session.flush()
        await self._advance(session, game, state, players)
        return word, True

    async def _apply_skip(
        self,
        session: AsyncSession,
        game: GameSession,
        state: dict,
        players: list[GamePlayer],
        actor: GamePlayer,
        *,
        timeout: bool,
    ) -> None:
        pstate = dict(actor.state_json or {})
        strikes = int(pstate.get("strikes") or 0) + 1
        pstate["strikes"] = strikes
        actor.state_json = pstate
        if timeout:
            actor.afk_count += 1
        if strikes >= MAX_STRIKES:
            actor.status = "eliminated"
        state["last_action"] = {
            "user_id": actor.user_telegram_id,
            "type": "timeout" if timeout else "skip",
            "strikes": strikes,
        }
        session.add(GameAction(
            game_id=game.id,
            round_no=game.round_no,
            phase_seq=game.phase_seq,
            actor_telegram_id=actor.user_telegram_id,
            action_type="words_timeout" if timeout else "words_skip",
            payload_json={"strikes": strikes},
        ))
        await session.flush()
        await self._advance(session, game, state, players)

    async def skip_turn(self, session: AsyncSession, game: GameSession, *, actor_telegram_id: int) -> tuple[str, bool]:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != WordsPhase.TURN.value:
            raise ValueError("words turn inactive")
        state = dict(game.state_json or {})
        if actor_telegram_id != int(state.get("turn_user_id") or 0):
            raise PermissionError("not your turn")
        existing = await session.scalar(select(GameAction).where(
            GameAction.game_id == game.id,
            GameAction.phase_seq == game.phase_seq,
            GameAction.actor_telegram_id == actor_telegram_id,
        ))
        if existing is not None:
            return "repeat", False
        players = await self._players(session, game.id, for_update=True)
        actor = next((player for player in players if player.user_telegram_id == actor_telegram_id and player.status == "alive"), None)
        if actor is None:
            raise PermissionError("not alive")
        await self._apply_skip(session, game, state, players, actor, timeout=False)
        return "skip", True

    async def _finish(
        self,
        session: AsyncSession,
        game: GameSession,
        winner_id: int,
        players: list[GamePlayer] | None = None,
    ) -> None:
        players = players or await self._players(session, game.id, for_update=True)
        participants = [player for player in players if player.status in {"alive", "eliminated"}]
        settings = await session.get(GameGroupSettings, game.group_id)
        rating_enabled = settings.rating_enabled if settings is not None else True
        now = datetime.now(timezone.utc)
        game.status = GameSessionStatus.FINISHED.value
        game.phase = WordsPhase.FINISHED.value
        game.phase_seq += 1
        game.deadline_at = None
        game.finished_at = now
        game.finish_reason = f"winner:{winner_id}"
        if await session.scalar(select(GameResult).where(GameResult.game_id == game.id)) is None:
            session.add(GameResult(
                game_id=game.id,
                group_id=game.group_id,
                game_type=game.game_type,
                winner_type="player",
                winner_json={"user_id": winner_id},
                summary_json={"rounds": game.round_no, "players": len(participants)},
                duration_seconds=int((now - game.started_at).total_seconds()) if game.started_at else None,
            ))
        for player in participants:
            pstate = dict(player.state_json or {})
            if pstate.get("result_applied"):
                continue
            await apply_game_result(
                session,
                group_id=game.group_id,
                game_type=game.game_type,
                user_telegram_id=player.user_telegram_id,
                won=player.user_telegram_id == winner_id,
                rating_enabled=rating_enabled,
                commit=False,
            )
            pstate["result_applied"] = True
            player.state_json = pstate
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

    async def handle_timeout(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != WordsPhase.TURN.value:
            return
        state = dict(game.state_json or {})
        actor_id = int(state.get("turn_user_id") or 0)
        players = await self._players(session, game.id, for_update=True)
        actor = next((player for player in players if player.user_telegram_id == actor_id and player.status == "alive"), None)
        if actor is None:
            return
        existing = await session.scalar(select(GameAction).where(
            GameAction.game_id == game.id,
            GameAction.phase_seq == game.phase_seq,
            GameAction.actor_telegram_id == actor_id,
        ))
        if existing is not None:
            return
        await self._apply_skip(session, game, state, players, actor, timeout=True)

    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        if game.phase == "recovering":
            state = dict(game.state_json or {})
            if not state.get("turn_order") or not state.get("current_word"):
                game.status = GameSessionStatus.RUNNING.value
                await session.commit()
                await self.start(session, game)
                return
            game.status = GameSessionStatus.RUNNING.value
            game.phase = WordsPhase.TURN.value
            game.phase_seq += 1
        if game.phase not in {WordsPhase.TURN.value, WordsPhase.FINISHED.value}:
            raise ValueError(f"unknown words phase: {game.phase}")
        if game.status == GameSessionStatus.RUNNING.value and game.deadline_at is None:
            game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        await session.commit()

    async def sync_ui(self, bot, session: AsyncSession, game: GameSession) -> None:
        from app.games.words.presentation import sync_words_ui

        await sync_words_ui(bot, session, game)
