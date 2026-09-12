from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.game_models import GameSession
from app.games.config import GameDefinition

from enum import Enum


class LeaveResult(Enum):
    REMOVED = "removed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class BaseGame(ABC):
    definition: GameDefinition

    @abstractmethod
    async def start(self, session: AsyncSession, game: GameSession) -> None:
        return None

    @abstractmethod
    async def handle_action(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        action: str,
        value: int | str | None = None,
    ) -> None:
        return None

    @abstractmethod
    async def handle_timeout(self, session: AsyncSession, game: GameSession) -> None:
        return None

    @abstractmethod
    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        return None

    async def handle_leave(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
    ) -> LeaveResult:
        """По умолчанию — просто убрать игрока. Mafia/Spy override'ят."""
        return LeaveResult.REMOVED
