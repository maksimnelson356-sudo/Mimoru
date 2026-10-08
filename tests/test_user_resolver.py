from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.utils.user_resolver import resolve_target_user


def _msg(text: str, reply_user_id: int | None = None) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.reply_to_message = None
    if reply_user_id is not None:
        reply_user = MagicMock()
        reply_user.id = reply_user_id
        message.reply_to_message = MagicMock()
        message.reply_to_message.from_user = reply_user
    return message


@pytest.mark.asyncio
async def test_reply_takes_priority_over_text():
    msg = _msg("размут @someone", reply_user_id=111)
    session = AsyncMock()

    result_id, _ = await resolve_target_user(session, 999, msg, command_keyword="размут")
    assert result_id == 111


@pytest.mark.asyncio
async def test_reply_without_username():
    msg = _msg("говори", reply_user_id=222)
    session = AsyncMock()

    result_id, _ = await resolve_target_user(session, 999, msg, command_keyword="говори")
    assert result_id == 222


@pytest.mark.asyncio
async def test_numeric_id_from_text():
    msg = _msg("размут 12345")
    session = AsyncMock()

    result_id, _ = await resolve_target_user(session, 999, msg, command_keyword="размут")
    assert result_id == 12345


@pytest.mark.asyncio
async def test_no_reply_no_args_returns_none():
    msg = _msg("говори")
    session = AsyncMock()

    result_id, label = await resolve_target_user(session, 999, msg, command_keyword="говори")
    assert result_id is None
    assert label == ""


@pytest.mark.asyncio
async def test_bare_username_without_at():
    msg = _msg("размут john_doe")
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = 777

    with patch("app.utils.user_resolver._resolve_username", return_value=777):
        result_id, _ = await resolve_target_user(session, 999, msg, command_keyword="размут")
    assert result_id == 777


@pytest.mark.asyncio
async def test_unknown_username_returns_none():
    msg = _msg("размут @ghost")
    session = AsyncMock()

    with patch("app.utils.user_resolver._resolve_username", return_value=None):
        result_id, label = await resolve_target_user(session, 999, msg, command_keyword="размут")
    assert result_id is None
    assert "не найден" in label.lower()


@pytest.mark.asyncio
async def test_unrecognized_text_returns_none():
    msg = _msg("размут some random text here")
    session = AsyncMock()

    with patch("app.utils.user_resolver._resolve_username", return_value=None):
        result_id, label = await resolve_target_user(session, 999, msg, command_keyword="размут")
    assert result_id is None
    assert "не удалось" in label.lower() or "указать" in label.lower()
