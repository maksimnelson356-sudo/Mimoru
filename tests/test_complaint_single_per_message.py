"""Одна жалоба на одно сообщение.

Правило проверяется на обоих путях подачи жалобы: в группе через
команду из COMPLAINT_WORDS (group_commands.group_complaint) и через
слово «пожаловаться» (features.complaint).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import Complaint
from app.handlers import features, group_commands
from app.services.complaints import (
    COMPLAINT_DUPLICATE_TEXT,
    complaint_exists_for_message,
)


def _session() -> SimpleNamespace:
    """Сессия с асинхронными запросами и синхронным add."""
    return SimpleNamespace(
        scalar=AsyncMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=Mock(),
    )


def _group() -> SimpleNamespace:
    return SimpleNamespace(
        id=3,
        telegram_chat_id=-1003,
        title="Тестовая группа",
        owner_telegram_id=1,
        is_active=True,
    )


def _message(text: str = "жалоба") -> SimpleNamespace:
    replied = SimpleNamespace(
        message_id=555,
        from_user=SimpleNamespace(id=99, full_name="Нарушитель", username=None),
        text="плохое сообщение",
        caption=None,
    )
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=-1003),
        from_user=SimpleNamespace(
            id=42, full_name="Жалобщик", username="reporter"
        ),
        reply_to_message=replied,
        reply=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


def _replies(message: SimpleNamespace) -> list[str]:
    return [call.args[0] for call in message.reply.await_args_list]


# --- проверка самого правила в БД-части -------------------------------


@pytest.mark.asyncio
async def test_duplicate_lookup_ignores_reporter_and_status() -> None:
    """Ключ жалобы — сообщение, а не автор и не статус."""
    session = _session()
    session.scalar.return_value = None

    await complaint_exists_for_message(session, group_id=3, message_id=555)

    statement = session.scalar.await_args.args[0]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True})).casefold()
    assert "group_id" in sql
    assert "message_id" in sql
    assert "reporter_telegram_id" not in sql
    assert "status" not in sql


# --- путь в группе: жалоба / доложить / нарушитель ---------------------


@pytest.mark.asyncio
async def test_group_complaint_stops_when_message_already_reported(
    monkeypatch,
) -> None:
    session = _session()
    session.scalar.return_value = 17
    monkeypatch.setattr(group_commands, "_active_group", AsyncMock(return_value=_group()))
    monkeypatch.setattr(group_commands, "_target_from_reply", lambda _m: _message().reply_to_message.from_user)
    notify = AsyncMock(return_value=(1, 1))
    monkeypatch.setattr(group_commands, "_notify_complaint_recipients", notify)
    message = _message()

    await group_commands.group_complaint(message, SimpleNamespace(), session)

    assert _replies(message) == [COMPLAINT_DUPLICATE_TEXT]
    session.add.assert_not_called()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_complaint_creates_row_when_message_is_free(
    monkeypatch,
) -> None:
    session = _session()
    session.scalar.return_value = None
    monkeypatch.setattr(group_commands, "_active_group", AsyncMock(return_value=_group()))
    monkeypatch.setattr(group_commands, "_target_from_reply", lambda _m: _message().reply_to_message.from_user)
    notify = AsyncMock(return_value=(1, 1))
    monkeypatch.setattr(group_commands, "_notify_complaint_recipients", notify)
    message = _message()

    await group_commands.group_complaint(message, SimpleNamespace(), session)

    added = [call.args[0] for call in session.add.call_args_list]
    assert len(added) == 1
    assert isinstance(added[0], Complaint)
    assert added[0].message_id == 555
    assert _replies(message) != [COMPLAINT_DUPLICATE_TEXT]
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_complaint_reports_duplicate_on_concurrent_insert(
    monkeypatch,
) -> None:
    """Гонка двух жалоб: уникальный индекс не дал вставить вторую."""
    session = _session()
    session.scalar.return_value = None
    session.flush.side_effect = IntegrityError("INSERT", {}, Exception("uq"))
    monkeypatch.setattr(group_commands, "_active_group", AsyncMock(return_value=_group()))
    monkeypatch.setattr(group_commands, "_target_from_reply", lambda _m: _message().reply_to_message.from_user)
    notify = AsyncMock(return_value=(1, 1))
    monkeypatch.setattr(group_commands, "_notify_complaint_recipients", notify)
    message = _message()

    await group_commands.group_complaint(message, SimpleNamespace(), session)

    assert _replies(message) == [COMPLAINT_DUPLICATE_TEXT]
    session.rollback.assert_awaited_once()
    notify.assert_not_awaited()


# --- путь «пожаловаться» ----------------------------------------------


@pytest.mark.asyncio
async def test_report_word_enforces_same_rule(monkeypatch) -> None:
    session = _session()
    session.scalar.side_effect = [_group(), 23]
    monkeypatch.setattr(features, "get_or_create_group", AsyncMock(return_value=_group()))
    message = _message("пожаловаться")

    await features.complaint(message, session)

    assert _replies(message) == [COMPLAINT_DUPLICATE_TEXT]
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_report_word_creates_row_when_message_is_free(monkeypatch) -> None:
    session = _session()
    session.scalar.side_effect = [_group(), None]
    monkeypatch.setattr(features, "get_or_create_group", AsyncMock(return_value=_group()))
    message = _message("пожаловаться")

    await features.complaint(message, session)

    added = [call.args[0] for call in session.add.call_args_list]
    assert any(isinstance(item, Complaint) for item in added)
    session.commit.assert_awaited_once()
    assert _replies(message) != [COMPLAINT_DUPLICATE_TEXT]


@pytest.mark.asyncio
async def test_report_word_reports_duplicate_on_concurrent_insert(monkeypatch) -> None:
    session = _session()
    session.scalar.side_effect = [_group(), None]
    session.flush.side_effect = IntegrityError("INSERT", {}, Exception("uq"))
    monkeypatch.setattr(features, "get_or_create_group", AsyncMock(return_value=_group()))
    message = _message("пожаловаться")

    await features.complaint(message, session)

    assert _replies(message) == [COMPLAINT_DUPLICATE_TEXT]
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()