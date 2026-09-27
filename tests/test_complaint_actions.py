from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import complaint_actions


def _callback(data: str = "complaint:ban:confirm:7") -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=42),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=42),
            message_id=100,
            edit_reply_markup=AsyncMock(),
        ),
        answer=AsyncMock(),
    )


def _context() -> tuple[SimpleNamespace, SimpleNamespace]:
    complaint = SimpleNamespace(
        id=7,
        group_id=3,
        target_telegram_id=99,
        reporter_telegram_id=42,
        status="pending",
        resolution=None,
        reviewed_by_telegram_id=None,
    )
    group = SimpleNamespace(
        id=3,
        telegram_chat_id=-1003,
        is_active=True,
        settings=SimpleNamespace(warnings_limit=3, default_mute_seconds=3600),
    )
    return complaint, group


@pytest.mark.asyncio
async def test_non_recipient_notification_is_rejected() -> None:
    session = AsyncMock()
    session.scalar.return_value = None
    callback = _callback("complaint:ack:7")

    allowed = await complaint_actions._is_notification_recipient(
        session, callback, 7
    )

    assert allowed is False


@pytest.mark.asyncio
async def test_ban_confirm_has_no_side_effects_when_context_is_rejected(
    monkeypatch,
) -> None:
    callback = _callback()
    bot = SimpleNamespace()
    session = AsyncMock()
    load_context = AsyncMock(return_value=None)
    execute = AsyncMock()
    delete_messages = AsyncMock()
    monkeypatch.setattr(complaint_actions, "_load_complaint_context", load_context)
    monkeypatch.setattr(complaint_actions, "execute", execute)
    monkeypatch.setattr(complaint_actions, "_delete_user_messages", delete_messages)

    await complaint_actions.complaint_ban_confirm(callback, bot, session)

    load_context.assert_awaited_once()
    execute.assert_not_awaited()
    delete_messages.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_ban_clean_does_not_fallback_after_failed_execute(
    monkeypatch,
) -> None:
    callback = _callback("complaint:ban:clean:7")
    bot = SimpleNamespace(
        unban_chat_member=AsyncMock(),
        ban_chat_member=AsyncMock(),
        send_message=AsyncMock(),
    )
    session = AsyncMock()
    complaint, group = _context()
    monkeypatch.setattr(
        complaint_actions,
        "_load_complaint_context",
        AsyncMock(return_value=(complaint, group)),
    )
    monkeypatch.setattr(
        complaint_actions,
        "execute",
        AsyncMock(return_value=SimpleNamespace(success=False, commit=False)),
    )
    delete_messages = AsyncMock()
    monkeypatch.setattr(complaint_actions, "_delete_user_messages", delete_messages)

    await complaint_actions.complaint_ban_clean(callback, bot, session)

    delete_messages.assert_not_awaited()
    bot.unban_chat_member.assert_not_awaited()
    bot.ban_chat_member.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_ban_confirm_authorizes_before_deleting_messages(
    monkeypatch,
) -> None:
    callback = _callback()
    bot = SimpleNamespace(send_message=AsyncMock())
    session = AsyncMock()
    complaint, group = _context()
    order: list[str] = []

    async def fake_execute(**kwargs):
        order.append("execute")
        return SimpleNamespace(success=True, commit=True)

    async def fake_delete(*args, **kwargs):
        order.append("delete")
        return 2

    monkeypatch.setattr(
        complaint_actions,
        "_load_complaint_context",
        AsyncMock(return_value=(complaint, group)),
    )
    monkeypatch.setattr(complaint_actions, "execute", fake_execute)
    monkeypatch.setattr(complaint_actions, "_delete_user_messages", fake_delete)
    monkeypatch.setattr(
        complaint_actions,
        "_clear_complaint_buttons",
        AsyncMock(),
    )

    await complaint_actions.complaint_ban_confirm(callback, bot, session)

    assert order == ["execute", "delete"]
    assert complaint.resolution == "ban"
    session.commit.assert_awaited()
