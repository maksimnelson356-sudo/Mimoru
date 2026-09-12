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


CASE_SECONDS = 60
RESULT_SECONDS = 8
ROUNDS = 4


class DetectivePhase(StrEnum):
    INVESTIGATION = "investigation"
    ROUND_RESULT = "round_result"
    FINISHED = "finished"


detective_definition = GameDefinition(
    code="detective",
    title="🕵️ Детектив",
    min_players=2,
    max_players=20,
    exclusive_group_game=True,
    supports_rating=True,
    supports_spectators=False,
    uses_private_mapping=False,
    default_timeout_seconds=CASE_SECONDS,
)


CASE_BANK = (
    {
        "title": "Исчезнувшая картина",
        "intro": "Из закрытой галереи ночью исчезла картина. Сигнализация не сработала.",
        "clues": (
            "На раме нет следов взлома, но замок витрины открыт штатным ключом.",
            "Камера у служебной двери была отключена из технического шкафа.",
            "В журнале доступа отмечен поздний вход сотрудника охраны.",
        ),
        "suspects": ("Охранник", "Куратор", "Уборщик", "Посетитель"),
        "guilty": "Охранник",
        "explanation": "Охранник имел ключ, доступ к техническому шкафу и вошёл после закрытия.",
    },
    {
        "title": "Отравленный десерт",
        "intro": "На закрытом ужине один из гостей отравился после десерта. Остальные ели то же блюдо.",
        "clues": (
            "Яд обнаружен только в декоративном сиропе на одной порции.",
            "Сироп наносили на тарелки уже после кухни.",
            "Перед подачей у тележки несколько минут находился личный помощник пострадавшего.",
        ),
        "suspects": ("Шеф-повар", "Официант", "Личный помощник", "Сосед по столу"),
        "guilty": "Личный помощник",
        "explanation": "Яд был добавлен после приготовления, а помощник имел доступ к конкретной порции.",
    },
    {
        "title": "Пропавший прототип",
        "intro": "Из лаборатории исчез единственный прототип устройства. Следов взлома нет.",
        "clues": (
            "Дверь открывалась только служебными картами.",
            "В 22:14 система зафиксировала карту инженера испытаний.",
            "Сам инженер утверждал, что в это время уже ехал домой, но его машина оставалась на парковке.",
        ),
        "suspects": ("Инженер испытаний", "Директор", "Стажёр", "Курьер"),
        "guilty": "Инженер испытаний",
        "explanation": "Его карта открыла лабораторию, а алиби противоречило данным парковки.",
    },
    {
        "title": "Сломанный сейф",
        "intro": "Утром сейф офиса оказался пуст, хотя код знали только трое сотрудников.",
        "clues": (
            "Код был введён правильно с первой попытки.",
            "В журнале принтера ночью есть задание на печать договора бухгалтера.",
            "Бухгалтер заявлял, что весь вечер был дома и к офисной сети не подключался.",
        ),
        "suspects": ("Бухгалтер", "Директор", "Юрист", "Клиент"),
        "guilty": "Бухгалтер",
        "explanation": "Правильный код и ночная активность в офисной сети опровергают алиби бухгалтера.",
    },
    {
        "title": "Подменённая посылка",
        "intro": "Ценная посылка прибыла на склад, но внутри оказался дешёвый муляж.",
        "clues": (
            "Пломба перевозчика цела, но складская пломба наклеена повторно.",
            "Вес посылки изменился уже после приёмки на складе.",
            "К кладовой в этот период заходил только начальник смены.",
        ),
        "suspects": ("Курьер", "Начальник смены", "Получатель", "Диспетчер"),
        "guilty": "Начальник смены",
        "explanation": "Подмена произошла после приёмки, а единственный доступ к кладовой был у начальника смены.",
    },
    {
        "title": "Ночной звонок",
        "intro": "Из кабинета директора ночью отправили конфиденциальный звонок конкуренту.",
        "clues": (
            "Звонок сделан со стационарного телефона кабинета.",
            "Электронный замок открылся мастер-картой администратора здания.",
            "Администратор сначала отрицал вход, но камера лифта зафиксировала его на этаже.",
        ),
        "suspects": ("Администратор здания", "Секретарь", "Директор", "Клиент"),
        "guilty": "Администратор здания",
        "explanation": "Мастер-карта и запись лифта напрямую связывают администратора с кабинетом.",
    },
)


class DetectiveGame(BaseGame):
    definition = detective_definition

    async def _players(self, session: AsyncSession, game_id: int, *, for_update: bool = False) -> list[GamePlayer]:
        query = select(GamePlayer).where(GamePlayer.game_id == game_id).order_by(GamePlayer.id)
        if for_update:
            query = query.with_for_update()
        return list((await session.scalars(query)).all())

    async def start(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        players = [player for player in await self._players(session, game.id, for_update=True) if player.status == "joined"]
        if len(players) < self.definition.min_players:
            raise ValueError("not enough players for detective")
        rng = random.SystemRandom()
        selected = rng.sample(list(CASE_BANK), ROUNDS)
        cases: list[dict] = []
        for item in selected:
            suspects = list(item["suspects"])
            rng.shuffle(suspects)
            cases.append({
                "title": item["title"],
                "intro": item["intro"],
                "clues": list(item["clues"]),
                "suspects": suspects,
                "correct_index": suspects.index(item["guilty"]) + 1,
                "guilty": item["guilty"],
                "explanation": item["explanation"],
            })
        for player in players:
            player.status = "alive"
            player.score = 0
            player.afk_count = 0
            player.state_json = {"result_applied": False}
        game.phase = DetectivePhase.INVESTIGATION.value
        game.phase_seq += 1
        game.round_no = 1
        game.state_json = {
            "cases": cases,
            "case_index": 0,
            "last_case": None,
            "timers": {"case": CASE_SECONDS, "result": RESULT_SECONDS},
        }
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=CASE_SECONDS)
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
        if action != "accuse":
            raise ValueError("unsupported detective action")
        await self.accuse(session, game, actor_telegram_id=actor_telegram_id, number=int(value or 0))

    async def accuse(
        self,
        session: AsyncSession,
        game: GameSession,
        *,
        actor_telegram_id: int,
        number: int,
    ) -> tuple[bool, bool]:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != DetectivePhase.INVESTIGATION.value:
            raise ValueError("detective round inactive")
        player = await session.scalar(select(GamePlayer).where(
            GamePlayer.game_id == game.id,
            GamePlayer.user_telegram_id == actor_telegram_id,
            GamePlayer.status == "alive",
        ).with_for_update())
        if player is None:
            raise PermissionError("not a detective player")
        existing = await session.scalar(select(GameAction).where(
            GameAction.game_id == game.id,
            GameAction.phase_seq == game.phase_seq,
            GameAction.actor_telegram_id == actor_telegram_id,
            GameAction.action_type == "detective_accuse",
        ))
        if existing is not None:
            return bool((existing.payload_json or {}).get("correct")), False
        state = dict(game.state_json or {})
        cases = list(state.get("cases") or [])
        index = int(state.get("case_index") or 0)
        if index < 0 or index >= len(cases):
            raise ValueError("detective case missing")
        current = dict(cases[index])
        suspects = list(current.get("suspects") or [])
        if number < 1 or number > len(suspects):
            raise ValueError("invalid suspect number")
        correct = number == int(current.get("correct_index") or 0)
        session.add(GameAction(
            game_id=game.id,
            round_no=game.round_no,
            phase_seq=game.phase_seq,
            actor_telegram_id=actor_telegram_id,
            action_type="detective_accuse",
            payload_json={"number": number, "correct": correct},
        ))
        if correct:
            player.score += 1
        await session.commit()
        return correct, True

    async def maybe_advance_if_ready(self, session: AsyncSession, game: GameSession) -> bool:
        game = await session.get(GameSession, game.id)
        if game is None or game.status != GameSessionStatus.RUNNING.value or game.phase != DetectivePhase.INVESTIGATION.value:
            return False
        total = len([player for player in await self._players(session, game.id) if player.status == "alive"])
        answered = len(list((await session.scalars(select(GameAction.id).where(
            GameAction.game_id == game.id,
            GameAction.phase_seq == game.phase_seq,
            GameAction.action_type == "detective_accuse",
        ))).all()))
        if answered < total:
            return False
        return await self._close_case(session, game, expected_phase_seq=game.phase_seq)

    async def _close_case(self, session: AsyncSession, game: GameSession, *, expected_phase_seq: int) -> bool:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != DetectivePhase.INVESTIGATION.value
            or game.phase_seq != expected_phase_seq
        ):
            return False
        state = dict(game.state_json or {})
        cases = list(state.get("cases") or [])
        current = dict(cases[int(state.get("case_index") or 0)])
        actions = list((await session.scalars(select(GameAction).where(
            GameAction.game_id == game.id,
            GameAction.phase_seq == game.phase_seq,
            GameAction.action_type == "detective_accuse",
        ))).all())
        state["last_case"] = {
            "title": current.get("title"),
            "guilty": current.get("guilty"),
            "explanation": current.get("explanation"),
            "answered": len(actions),
            "correct_count": sum(1 for action in actions if bool((action.payload_json or {}).get("correct"))),
        }
        game.state_json = state
        game.phase = DetectivePhase.ROUND_RESULT.value
        game.phase_seq += 1
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=RESULT_SECONDS)
        await session.commit()
        return True

    async def _next_case_or_finish(self, session: AsyncSession, game: GameSession, *, expected_phase_seq: int) -> bool:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if (
            game is None
            or game.status != GameSessionStatus.RUNNING.value
            or game.phase != DetectivePhase.ROUND_RESULT.value
            or game.phase_seq != expected_phase_seq
        ):
            return False
        state = dict(game.state_json or {})
        cases = list(state.get("cases") or [])
        next_index = int(state.get("case_index") or 0) + 1
        if next_index >= len(cases):
            await self._finish(session, game)
            return True
        state["case_index"] = next_index
        game.state_json = state
        game.round_no += 1
        game.phase = DetectivePhase.INVESTIGATION.value
        game.phase_seq += 1
        game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=CASE_SECONDS)
        await session.commit()
        return True

    async def _finish(self, session: AsyncSession, game: GameSession) -> None:
        players = [player for player in await self._players(session, game.id, for_update=True) if player.status == "alive"]
        if not players:
            return
        winner = sorted(players, key=lambda player: (-player.score, player.id))[0]
        settings = await session.get(GameGroupSettings, game.group_id)
        rating_enabled = settings.rating_enabled if settings is not None else True
        now = datetime.now(timezone.utc)
        game.status = GameSessionStatus.FINISHED.value
        game.phase = DetectivePhase.FINISHED.value
        game.phase_seq += 1
        game.deadline_at = None
        game.finished_at = now
        game.finish_reason = f"winner:{winner.user_telegram_id}"
        if await session.scalar(select(GameResult).where(GameResult.game_id == game.id)) is None:
            session.add(GameResult(
                game_id=game.id,
                group_id=game.group_id,
                game_type=game.game_type,
                winner_type="player",
                winner_json={"user_id": winner.user_telegram_id},
                summary_json={"rounds": game.round_no, "players": len(players)},
                duration_seconds=int((now - game.started_at).total_seconds()) if game.started_at else None,
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
                won=player.user_telegram_id == winner.user_telegram_id,
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
        """Убирает игрока. Если остался 1 живой — завершает игру через _finish."""
        players = list((await session.scalars(
            select(GamePlayer).where(GamePlayer.game_id == game.id)
        )).all())
        alive = [p for p in players if p.status == "alive" and p.user_telegram_id != actor_telegram_id]
        if len(alive) <= 1:
            await self._finish(session, game)
        return LeaveResult.REMOVED

    async def handle_timeout(self, session: AsyncSession, game: GameSession) -> None:
        if game.phase == DetectivePhase.INVESTIGATION.value:
            await self._close_case(session, game, expected_phase_seq=game.phase_seq)
            return
        if game.phase == DetectivePhase.ROUND_RESULT.value:
            await self._next_case_or_finish(session, game, expected_phase_seq=game.phase_seq)

    async def restore(self, session: AsyncSession, game: GameSession) -> None:
        game = await session.scalar(select(GameSession).where(GameSession.id == game.id).with_for_update())
        if game is None:
            return
        if game.phase == "recovering":
            state = dict(game.state_json or {})
            if not state.get("cases"):
                game.status = GameSessionStatus.RUNNING.value
                await session.commit()
                await self.start(session, game)
                return
            game.status = GameSessionStatus.RUNNING.value
            game.phase = DetectivePhase.INVESTIGATION.value
            game.phase_seq += 1
        if game.phase not in {
            DetectivePhase.INVESTIGATION.value,
            DetectivePhase.ROUND_RESULT.value,
            DetectivePhase.FINISHED.value,
        }:
            raise ValueError(f"unknown detective phase: {game.phase}")
        if game.status == GameSessionStatus.RUNNING.value and game.deadline_at is None:
            seconds = RESULT_SECONDS if game.phase == DetectivePhase.ROUND_RESULT.value else CASE_SECONDS
            game.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await session.commit()

    async def sync_ui(self, bot, session: AsyncSession, game: GameSession) -> None:
        from app.games.detective.presentation import sync_detective_ui

        await sync_detective_ui(bot, session, game)
