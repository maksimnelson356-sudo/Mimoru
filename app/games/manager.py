from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.game_models import GameGroupSettings, GamePlayer, GameSession
from app.db.models import Group
from app.games.enums import ACTIVE_SESSION_STATUSES, GameSessionStatus
from app.games.group_limits import effective_max_players, lobby_max_players
from app.games.registry import GameRegistry, game_registry
from app.games.base import LeaveResult


class GameConflictError(RuntimeError):
    pass


class GameNotFoundError(RuntimeError):
    pass


class GamePlayerError(RuntimeError):
    pass


class GameManager:
    def __init__(self, registry: GameRegistry | None = None) -> None:
        self.registry = registry or game_registry

    async def get_active_game(
        self,
        session: AsyncSession,
        *,
        group_id: int,
        for_update: bool = False,
    ) -> GameSession | None:
        query = (
            select(GameSession)
            .where(
                GameSession.group_id == group_id,
                GameSession.status.in_(ACTIVE_SESSION_STATUSES),
            )
            .order_by(GameSession.id.desc())
            .limit(1)
        )
        if for_update:
            query = query.with_for_update()
        return await session.scalar(query)

    async def get_game(
        self,
        session: AsyncSession,
        *,
        game_id: int,
        for_update: bool = False,
    ) -> GameSession | None:
        query = select(GameSession).where(GameSession.id == game_id)
        if for_update:
            query = query.with_for_update()
        return await session.scalar(query)

    async def create_lobby(
        self,
        session: AsyncSession,
        *,
        telegram_chat_id: int,
        game_type: str,
        creator_telegram_id: int,
        creator_display_name: str,
    ) -> GameSession:
        definition = self.registry.require(game_type)
        group = await session.scalar(
            select(Group)
            .where(
                Group.telegram_chat_id == telegram_chat_id,
                Group.is_active.is_(True),
            )
            .with_for_update()
        )
        if group is None:
            raise GameNotFoundError("active group not found")

        current = await self.get_active_game(session, group_id=group.id, for_update=True)
        if current is not None:
            raise GameConflictError(current.game_type)

        settings = await session.get(GameGroupSettings, group.id)
        max_players = effective_max_players(definition, settings)
        game = GameSession(
            group_id=group.id,
            game_type=definition.code,
            status=GameSessionStatus.LOBBY.value,
            phase="lobby",
            phase_seq=0,
            round_no=0,
            creator_telegram_id=creator_telegram_id,
            exclusive_group_game=definition.exclusive_group_game,
            state_json={"lobby_max_players": max_players},
        )
        session.add(game)
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise GameConflictError("active game already exists") from error

        session.add(
            GamePlayer(
                game_id=game.id,
                user_telegram_id=creator_telegram_id,
                display_name=creator_display_name,
                status="joined",
                state_json={},
            )
        )
        await session.commit()
        await session.refresh(game)
        return game

    async def list_players(
        self,
        session: AsyncSession,
        *,
        game_id: int,
        for_update: bool = False,
    ) -> list[GamePlayer]:
        query = (
            select(GamePlayer)
            .where(
                GamePlayer.game_id == game_id,
                GamePlayer.status == "joined",
            )
            .order_by(GamePlayer.id)
        )
        if for_update:
            query = query.with_for_update()
        return list((await session.scalars(query)).all())

    async def join_lobby(
        self,
        session: AsyncSession,
        *,
        game_id: int,
        user_telegram_id: int,
        display_name: str,
    ) -> GamePlayer:
        game = await self.get_game(session, game_id=game_id, for_update=True)
        if game is None:
            raise GameNotFoundError("game not found")
        if game.status != GameSessionStatus.LOBBY.value:
            raise GamePlayerError("lobby is closed")

        definition = self.registry.require(game.game_type)
        max_players = lobby_max_players(game, definition)
        all_players = list(
            (
                await session.scalars(
                    select(GamePlayer)
                    .where(GamePlayer.game_id == game.id)
                    .order_by(GamePlayer.id)
                    .with_for_update()
                )
            ).all()
        )
        joined = [player for player in all_players if player.status == "joined"]
        existing = next(
            (player for player in all_players if player.user_telegram_id == user_telegram_id),
            None,
        )
        if existing is not None and existing.status == "joined":
            return existing
        if len(joined) >= max_players:
            raise GamePlayerError("lobby is full")
        if existing is not None:
            existing.status = "joined"
            existing.display_name = display_name
            existing.left_at = None
            await session.commit()
            await session.refresh(existing)
            return existing

        player = GamePlayer(
            game_id=game.id,
            user_telegram_id=user_telegram_id,
            display_name=display_name,
            status="joined",
            state_json={},
        )
        session.add(player)
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            existing = await session.scalar(
                select(GamePlayer).where(
                    GamePlayer.game_id == game.id,
                    GamePlayer.user_telegram_id == user_telegram_id,
                )
            )
            if existing is not None and existing.status == "joined":
                return existing
            raise GamePlayerError("failed to join lobby") from error
        await session.refresh(player)
        return player

    async def leave_lobby(
        self,
        session: AsyncSession,
        *,
        game_id: int,
        user_telegram_id: int,
    ) -> None:
        game = await self.get_game(session, game_id=game_id, for_update=True)
        if game is None:
            raise GameNotFoundError("game not found")
        if game.status != GameSessionStatus.LOBBY.value:
            raise GamePlayerError("lobby is closed")
        if game.creator_telegram_id == user_telegram_id:
            raise GamePlayerError("lobby creator cannot leave; cancel the lobby")

        player = await session.scalar(
            select(GamePlayer)
            .where(
                GamePlayer.game_id == game.id,
                GamePlayer.user_telegram_id == user_telegram_id,
                GamePlayer.status == "joined",
            )
            .with_for_update()
        )
        if player is None:
            return
        player.status = "left"
        player.left_at = datetime.now(timezone.utc)
        await session.commit()

    async def leave_running(
        self,
        session: AsyncSession,
        *,
        game_id: int,
        user_telegram_id: int,
    ) -> LeaveResult:
        game = await self.get_game(session, game_id=game_id, for_update=True)
        if game is None:
            raise GameNotFoundError("game not found")
        if game.status not in {
            GameSessionStatus.RUNNING.value,
            GameSessionStatus.RECOVERING.value,
        }:
            raise GamePlayerError("game is not running")
        if game.creator_telegram_id == user_telegram_id:
            await self.cancel_game(session, game_id=game.id, reason="creator_left")
            return LeaveResult.CANCELLED
        player = await session.scalar(
            select(GamePlayer)
            .where(
                GamePlayer.game_id == game.id,
                GamePlayer.user_telegram_id == user_telegram_id,
                GamePlayer.status == "joined",
            )
            .with_for_update()
        )
        if player is None:
            raise GamePlayerError("not a player")
        engine = self.registry.engine(game.game_type)
        result = await engine.handle_leave(
            session, game, actor_telegram_id=user_telegram_id
        )
        if result == LeaveResult.REMOVED:
            player.status = "left"
            player.left_at = datetime.now(timezone.utc)
        await session.commit()
        return result

    async def start_lobby(self, session: AsyncSession, *, game_id: int) -> GameSession:
        game = await self.get_game(session, game_id=game_id, for_update=True)
        if game is None:
            raise GameNotFoundError("game not found")
        if game.status != GameSessionStatus.LOBBY.value:
            raise GamePlayerError("lobby is closed")

        definition = self.registry.require(game.game_type)
        players = await self.list_players(session, game_id=game.id, for_update=True)
        if len(players) < definition.min_players:
            raise GamePlayerError("not enough players")

        await session.execute(
            delete(GamePlayer).where(
                GamePlayer.game_id == game.id,
                GamePlayer.status == "left",
            )
        )

        game.status = GameSessionStatus.RUNNING.value
        game.phase = "starting"
        game.phase_seq += 1
        game.round_no = 0
        game.started_at = datetime.now(timezone.utc)
        game.deadline_at = None
        await session.commit()
        await session.refresh(game)
        return game

    async def cancel_game(
        self,
        session: AsyncSession,
        *,
        game_id: int,
        reason: str = "cancelled",
    ) -> GameSession:
        game = await self.get_game(session, game_id=game_id, for_update=True)
        if game is None:
            raise GameNotFoundError("game not found")
        if game.status in {GameSessionStatus.FINISHED.value, GameSessionStatus.CANCELLED.value}:
            return game
        game.status = GameSessionStatus.CANCELLED.value
        game.phase = "cancelled"
        game.phase_seq += 1
        game.deadline_at = None
        game.finished_at = datetime.now(timezone.utc)
        game.finish_reason = reason[:64]
        await session.commit()
        await session.refresh(game)
        return game

    async def player_count(self, session: AsyncSession, *, game_id: int) -> int:
        return len(await self.list_players(session, game_id=game_id))