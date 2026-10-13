"""Участник покинул группу, пока на нём висел активный мут.

Регрессия к порядку вещей: Telegram отклоняет разжалование с
PARTICIPANT_ID_INVALID, воркер откатывал транзакцию, запись оставалась
активной, и бот опрашивал её каждые пять секунд — с warning в логах и без
всякой надежды на успех. Теперь такое наказание закрывается сразу.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import RestrictChatMember
from aiogram.types import ChatPermissions

from app.services import punishment_expiry as expiry


def _departed_error() -> TelegramBadRequest:
    return TelegramBadRequest(
        method=RestrictChatMember(
            chat_id=-100123, user_id=555, permissions=ChatPermissions()
        ),
        message="Bad Request: PARTICIPANT_ID_INVALID",
    )


class _Session:
    """Минимальная сессия: Group, Punishment, затем «других активных нет»."""

    def __init__(self, group: SimpleNamespace, punishment: SimpleNamespace) -> None:
        self._group = group
        self._punishment = punishment
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self._scalar_calls = 0

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(all=lambda: [(self._punishment.id, self._group.id)])

    async def scalar(self, *_args, **_kwargs):
        self._scalar_calls += 1
        if self._scalar_calls == 1:
            return self._group
        if self._scalar_calls == 2:
            return self._punishment
        return None  # another_active не найдено

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:
        self.flushes += 1

    def __call__(self) -> "_SessionContext":
        return _SessionContext(self)


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> _Session:
        return self._session

    async def __aexit__(self, *_exc) -> bool:
        return False


@pytest.mark.asyncio
async def test_departed_member_mute_is_closed_instead_of_retried(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    punishment = SimpleNamespace(
        id=592,
        group_id=7,
        user_telegram_id=555,
        kind="mute",
        active=True,
        ends_at=now - timedelta(minutes=5),
    )
    group = SimpleNamespace(id=7, telegram_chat_id=-100123, is_active=True)
    session = _Session(group, punishment)
    monkeypatch.setattr(expiry, "SessionFactory", session)

    bot = SimpleNamespace(restrict_chat_member=AsyncMock(side_effect=_departed_error()))
    redis = SimpleNamespace(get=AsyncMock(return_value=None))

    events = capture_structlog(monkeypatch)
    await expiry.expire_punishments(bot, redis)

    # Наказание закрыто и сохранено: следующий проход его не увидит.
    assert punishment.active is False
    assert session.commits >= 1
    # Отката быть не должно — он вернул бы запись в активное состояние.
    assert session.rollbacks == 0
    # В лог ушло состояние группы, а не предупреждение о сбое.
    assert "punishment_expiry_target_gone" in events
    assert "punishment_expiry_failed" not in events


@pytest.mark.asyncio
async def test_retryable_failure_still_rolls_back(monkeypatch) -> None:
    """Права бота могут восстановиться, поэтому откат и попытка позже — верно."""
    now = datetime.now(timezone.utc)
    punishment = SimpleNamespace(
        id=593,
        group_id=7,
        user_telegram_id=556,
        kind="mute",
        active=True,
        ends_at=now - timedelta(minutes=5),
    )
    group = SimpleNamespace(id=7, telegram_chat_id=-100123, is_active=True)
    session = _Session(group, punishment)
    monkeypatch.setattr(expiry, "SessionFactory", session)

    bot = SimpleNamespace(
        restrict_chat_member=AsyncMock(
            side_effect=TelegramBadRequest(
                method=RestrictChatMember(
                    chat_id=-100123, user_id=556, permissions=ChatPermissions()
                ),
                message="Bad Request: not enough rights",
            )
        )
    )
    redis = SimpleNamespace(get=AsyncMock(return_value=None))

    await expiry.expire_punishments(bot, redis)

    assert session.rollbacks == 1
    assert session.commits == 0


def capture_structlog(monkeypatch) -> list[str]:
    """Собрать имена событий, которые залогировал код под тестом."""
    import structlog

    captured: list[str] = []

    class _Logger:
        def debug(self, event, **kwargs):
            captured.append(event)

        def warning(self, event, **kwargs):
            captured.append(event)

    monkeypatch.setattr(structlog, "get_logger", lambda *a, **k: _Logger())
    return captured