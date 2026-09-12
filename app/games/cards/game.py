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
TURN_TIMEOUT_SECONDS = 75
HAND_SIZE = 5
COLORS = ("R", "G", "B", "Y")
COLOR_LABELS = {"R": "🔴", "G": "🟢", "B": "🔵", "Y": "🟡"}


class CardsPhase(StrEnum):
    TURN = "turn"
    FINISHED = "finished"


cards_definition = GameDefinition(
    code="cards",
    title="🃏 Карты",
    min_players=2,
    max_players=8,
    exclusive_group_game=True,
    supports_rating=True,
    supports_spectators=False,
    uses_private_mapping=False,
    default_timeout_seconds=TURN_TIMEOUT_SECONDS,
)


def _card(color: str, value: str) -> str:
    return f"{color}:{value}"


def card_label(card: str) -> str:
    color, value = card.split(":", 1)
    names = {"S": "⛔", "R": "🔄", "D2": "+2"}
    return f"{COLOR_LABELS.get(color, color)} {names.get(value, value)}"


def _deck() -> list[str]:
    cards: list[str] = []
    for color in COLORS:
        cards.extend(_card(color, str(number)) for number in range(1, 10))
        cards.extend((_card(color, "S"), _card(color, "R"), _card(color, "D2")))
    return cards


def _playable(card: str, top: str) -> bool:
    color, value = card.split(":", 1)
    top_color, top_value = top.split(":", 1)
    return color == top_color or value == top_value


class CardsGame(BaseGame):
    definition = cards_definition

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
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        players = await self._players(session, game.id, statuses=("joined",), for_update=True)
        if len(players) < self.definition.min_players:
            raise ValueError("not enough players for cards")
        rng = random.SystemRandom()
        deck = _deck()
        rng.shuffle(deck)
        order = [player.user_telegram_id for player in players]
        rng.shuffle(order)
        hands: dict[str, list[str]] = {}
        for player in players:
            hand = [deck.pop() for _ in range(HAND_SIZE)]
            hands[str(player.user_telegram_id)] = hand
            player.status = "alive"
            player.score = 0
            player.afk_count = 0
            player.state_json = {"result_applied": False}
        top_card = deck.pop()
        while top_card.split(":", 1)[1] in {"S", "R", "D2"}:
            deck.insert(0, top_card)
            top_card = deck.pop()
        game.phase = CardsPhase.TURN.value
        game.phase_seq += 1
        game.round_no = 1
        game.state_json = {
            "deck": deck,
            "discard": [top_card],
            "hands": hands,
            "turn_order": order,
            "turn_index": 0,
            "turn_user_id": order[0],
            "direction": 1,
            "last_action": None,
            "turn_timeout_seconds": TURN_TIMEOUT_SECONDS,
        }
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=TURN_TIMEOUT_SECONDS)
        await session.commit()
        log.info("cards_started", game_id=game.id, group_id=game.group_id, players=len(players))

    async def handle_action(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        action: str,
        value: int | str | None = None,
    ) -> None:
        if action == "play":
            await self.play_card(session, game, actor_telegram_id=actor_telegram_id, card_index=int(value or -1))
            return
        if action == "draw":
            await self.draw_card(session, game, actor_telegram_id=actor_telegram_id)
            return
        raise ValueError(f"unsupported cards action: {action}")

    @staticmethod
    def _next_index(index: int, count: int, direction: int, steps: int = 1) -> int:
        return (index + direction * steps) % count

    @staticmethod
    def _draw_one(state: dict) -> str | None:
        deck = list(state.get("deck") or [])
        discard = list(state.get("discard") or [])
        if not deck and len(discard) > 1:
            top = discard[-1]
            deck = discard[:-1]
            random.SystemRandom().shuffle(deck)
            discard = [top]
        if not deck:
            return None
        card = deck.pop()
        state["deck"] = deck
        state["discard"] = discard
        return card

    async def _advance(self, session: AsyncSession, game: GameSession, state: dict, *, steps: int = 1) -> None:
        order = [int(value) for value in list(state.get("turn_order") or [])]
        direction = int(state.get("direction") or 1)
        index = int(state.get("turn_index") or 0)
        index = self._next_index(index, len(order), direction, steps)
        state["turn_index"] = index
        state["turn_user_id"] = order[index]
        game.state_json = state
        game.phase_seq += 1
        game.round_no += 1
        seconds = int(state.get("turn_timeout_seconds") or TURN_TIMEOUT_SECONDS)
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await session.commit()

    async def play_card(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        card_index: int,
    ) -> tuple[str, bool]:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != CardsPhase.TURN.value:
            raise ValueError("cards turn is not active")
        state = dict(game.state_json or {})
        if actor_telegram_id != int(state.get("turn_user_id") or 0):
            raise PermissionError("not your turn")
        hands = {str(key): list(value) for key, value in dict(state.get("hands") or {}).items()}
        hand = list(hands.get(str(actor_telegram_id)) or [])
        if card_index < 0 or card_index >= len(hand):
            raise ValueError("invalid card")
        existing = await session.scalar(select(GameAction).where(
            GameAction.game_id == game.id,
            GameAction.phase_seq == game.phase_seq,
            GameAction.actor_telegram_id == actor_telegram_id,
            GameAction.action_type.in_(("cards_play", "cards_draw")),
        ))
        if existing is not None:
            return str((existing.payload_json or {}).get("result") or "repeat"), False
        card = hand[card_index]
        discard = list(state.get("discard") or [])
        top = discard[-1]
        if not _playable(card, top):
            raise ValueError("card is not playable")
        hand.pop(card_index)
        hands[str(actor_telegram_id)] = hand
        discard.append(card)
        state["hands"] = hands
        state["discard"] = discard
        state["last_action"] = {"user_id": actor_telegram_id, "type": "play", "card": card}
        session.add(GameAction(
            game_id=game.id,
            round_no=game.round_no,
            phase_seq=game.phase_seq,
            actor_telegram_id=actor_telegram_id,
            action_type="cards_play",
            payload_json={"result": "played", "card": card},
        ))
        player = await session.scalar(select(GamePlayer).where(
            GamePlayer.game_id == game.id,
            GamePlayer.user_telegram_id == actor_telegram_id,
        ).with_for_update())
        if player is None or player.status != "alive":
            raise PermissionError("not a player")
        player.score += 1
        if not hand:
            game.state_json = state
            await session.flush()
            await self._finish(session, game, actor_telegram_id)
            return "winner", True
        value = card.split(":", 1)[1]
        steps = 1
        if value == "R":
            state["direction"] = -int(state.get("direction") or 1)
            if len(list(state.get("turn_order") or [])) == 2:
                steps = 2
        elif value == "S":
            steps = 2
        elif value == "D2":
            order = [int(item) for item in list(state.get("turn_order") or [])]
            current = int(state.get("turn_index") or 0)
            direction = int(state.get("direction") or 1)
            victim_index = self._next_index(current, len(order), direction)
            victim_id = order[victim_index]
            victim_hand = list(hands.get(str(victim_id)) or [])
            for _ in range(2):
                drawn = self._draw_one(state)
                if drawn is not None:
                    victim_hand.append(drawn)
            hands[str(victim_id)] = victim_hand
            state["hands"] = hands
            steps = 2
        await session.flush()
        await self._advance(session, game, state, steps=steps)
        return "played", True

    async def draw_card(self, session: AsyncSession, game: GameSession, *, actor_telegram_id: int) -> tuple[str, bool]:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != CardsPhase.TURN.value:
            raise ValueError("cards turn is not active")
        state = dict(game.state_json or {})
        if actor_telegram_id != int(state.get("turn_user_id") or 0):
            raise PermissionError("not your turn")
        existing = await session.scalar(select(GameAction).where(
            GameAction.game_id == game.id,
            GameAction.phase_seq == game.phase_seq,
            GameAction.actor_telegram_id == actor_telegram_id,
            GameAction.action_type.in_(("cards_play", "cards_draw")),
        ))
        if existing is not None:
            return "repeat", False
        hands = {str(key): list(value) for key, value in dict(state.get("hands") or {}).items()}
        hand = list(hands.get(str(actor_telegram_id)) or [])
        drawn = self._draw_one(state)
        if drawn is not None:
            hand.append(drawn)
        hands[str(actor_telegram_id)] = hand
        state["hands"] = hands
        state["last_action"] = {"user_id": actor_telegram_id, "type": "draw"}
        session.add(GameAction(
            game_id=game.id,
            round_no=game.round_no,
            phase_seq=game.phase_seq,
            actor_telegram_id=actor_telegram_id,
            action_type="cards_draw",
            payload_json={"result": "drawn" if drawn else "empty"},
        ))
        await session.flush()
        await self._advance(session, game, state)
        return "drawn" if drawn else "empty", True

    async def _finish(self, session: AsyncSession, game: GameSession, winner_user_id: int) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status == GameSessionStatus.FINISHED.value:
            return
        players = await self._players(session, game.id, statuses=("alive",), for_update=True)
        settings = await session.get(GameGroupSettings, game.group_id)
        rating_enabled = settings.rating_enabled if settings is not None else True
        now = datetime.now(timezone.utc)
        game.status = GameSessionStatus.FINISHED.value
        game.phase = CardsPhase.FINISHED.value
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
            pstate = dict(player.state_json or {})
            if pstate.get("result_applied"):
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
            pstate["result_applied"] = True
            player.state_json = pstate
        await session.commit()
        log.info("cards_finished", game_id=game.id, winner_user_id=winner_user_id)

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

    async def handle_timeout(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != CardsPhase.TURN.value:
            return
        state = dict(game.state_json or {})
        user_id = int(state.get("turn_user_id") or 0)
        player = await session.scalar(select(GamePlayer).where(
            GamePlayer.game_id == game.id,
            GamePlayer.user_telegram_id == user_id,
            GamePlayer.status == "alive",
        ).with_for_update())
        if player is not None:
            player.afk_count += 1
        hands = {str(key): list(value) for key, value in dict(state.get("hands") or {}).items()}
        hand = list(hands.get(str(user_id)) or [])
        drawn = self._draw_one(state)
        if drawn is not None:
            hand.append(drawn)
        hands[str(user_id)] = hand
        state["hands"] = hands
        state["last_action"] = {"user_id": user_id, "type": "timeout"}
        await session.flush()
        await self._advance(session, game, state)

    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        if game.phase == "recovering":
            state = dict(game.state_json or {})
            if not state.get("turn_order") or not state.get("hands") or not state.get("discard"):
                game.status = GameSessionStatus.RUNNING.value
                await session.commit()
                await self.start(session, game)
                return
            game.status = GameSessionStatus.RUNNING.value
            game.phase = CardsPhase.TURN.value
            game.phase_seq += 1
        if game.phase not in {CardsPhase.TURN.value, CardsPhase.FINISHED.value}:
            raise ValueError(f"unknown cards phase: {game.phase}")
        if game.status == GameSessionStatus.RUNNING.value and game.deadline_at is None:
            state = dict(game.state_json or {})
            seconds = int(state.get("turn_timeout_seconds") or TURN_TIMEOUT_SECONDS)
            game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await session.commit()

    async def sync_ui(self, bot, session: AsyncSession, game: GameSession) -> None:
        from app.games.cards.presentation import sync_cards_ui

        await sync_cards_ui(bot, session, game)
