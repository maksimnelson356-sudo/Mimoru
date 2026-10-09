"""Жалобы: прямой бан без карточки и снятие сообщений через 45 секунд.

Раньше админ получал уведомление с одной кнопкой «🚫 Забанить», которая
открывала вторую карточку «Подтверждение бана» — два нажатия и лишнее
сообщение в личке. Теперь обе кнопки бана стоят в самом уведомлении.

Ответы о жалобах в группе снимаются через COMPLAINT_MESSAGE_TTL_SECONDS.
Уведомление в личке админа TTL не получает: там кнопки, и без срока
модератор может не успеть нажать.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.handlers import complaint_actions, group_commands
from app.services.message_ttl import COMPLAINT_MESSAGE_TTL_SECONDS


def _group() -> SimpleNamespace:
    return SimpleNamespace(
        id=3,
        telegram_chat_id=-1003,
        title="Тестовая группа",
        owner_telegram_id=1,
        is_active=True,
    )


def _complaint() -> SimpleNamespace:
    return SimpleNamespace(id=7, message_text="плохое сообщение")


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=100),
        answer=AsyncMock(),
    )


def _buttons(markup) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]


async def _notification_markup() -> list[tuple[str, str]]:
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    session = SimpleNamespace(add=Mock(), commit=AsyncMock(), scalars=AsyncMock())
    session.scalars.return_value = SimpleNamespace(all=lambda: [])
    group = _group()

    await group_commands._notify_complaint_recipients(
        bot,
        session,
        group,
        42,
        "Жалобщик",
        99,
        "Нарушитель",
        555,
        "плохое сообщение",
        _complaint(),
    )

    assert bot.send_message.await_count == 1
    return _buttons(bot.send_message.await_args.kwargs["reply_markup"])


# --- кнопки бана -------------------------------------------------------


@pytest.mark.asyncio
async def test_notification_offers_both_bans_directly(monkeypatch) -> None:
    monkeypatch.setattr(group_commands, "get_assignment", AsyncMock(return_value=None))

    buttons = await _notification_markup()
    payloads = {data for _text, data in buttons}

    assert ("🚫 Забанить", "complaint:ban:confirm:7") in buttons
    assert ("🚫🗑 Бан + очистка", "complaint:ban:clean:7") in buttons
    assert "complaint:ban:7" not in payloads


@pytest.mark.asyncio
async def test_notification_keeps_the_other_complaint_actions(monkeypatch) -> None:
    monkeypatch.setattr(group_commands, "get_assignment", AsyncMock(return_value=None))

    payloads = {data for _text, data in await _notification_markup()}

    assert {"complaint:ack:7", "complaint:warn:7", "complaint:mute_reporter:7"} <= payloads


@pytest.mark.asyncio
async def test_legacy_ban_button_bans_without_showing_a_prompt(monkeypatch) -> None:
    """Кнопка «🚫 Забанить» из истории не должна открывать карточку."""
    confirm = AsyncMock()
    monkeypatch.setattr(complaint_actions, "complaint_ban_confirm", confirm)
    callback = _callback("complaint:ban:7")

    await complaint_actions.complaint_ban_prompt(
        callback, SimpleNamespace(), SimpleNamespace()
    )

    confirm.assert_awaited_once()
    redirected = confirm.await_args.args[0]
    assert redirected.data == "complaint:ban:confirm:7"


def test_ban_confirmation_prompt_is_gone() -> None:
    from pathlib import Path

    source = Path("app/handlers/complaint_actions.py").read_text(encoding="utf-8")
    assert "Подтверждение бана" not in source


# --- снятие сообщений через 45 секунд ----------------------------------


def test_complaint_ttl_is_45_seconds() -> None:
    assert COMPLAINT_MESSAGE_TTL_SECONDS == 45


@pytest.mark.asyncio
async def test_group_complaint_reply_is_queued_for_removal(monkeypatch) -> None:
    schedule = AsyncMock(return_value=True)
    monkeypatch.setattr(group_commands, "schedule_message_deletion", schedule)
    monkeypatch.setattr(group_commands, "bound_redis", lambda: "redis")

    sent = SimpleNamespace(chat=SimpleNamespace(id=-1003), message_id=777)
    message = SimpleNamespace(
        reply=AsyncMock(return_value=sent),
        chat=SimpleNamespace(id=-1003),
        message_id=555,
    )

    await group_commands._reply_complaint_notice(message, "✅ Жалоба принята.")

    message.reply.assert_awaited_once_with("✅ Жалоба принята.")
    assert schedule.await_count == 2
    # First call: bot's reply
    assert schedule.await_args_list[0].args == ("redis", -1003, 777)
    # Second call: user's original message
    assert schedule.await_args_list[1].args == ("redis", -1003, 555)
    for call in schedule.await_args_list:
        assert call.kwargs["delay_seconds"] == COMPLAINT_MESSAGE_TTL_SECONDS


@pytest.mark.asyncio
async def test_report_word_reply_is_queued_for_removal(monkeypatch) -> None:
    from app.handlers import features

    schedule = AsyncMock(return_value=True)
    monkeypatch.setattr(features, "schedule_message_deletion", schedule)
    monkeypatch.setattr(features, "bound_redis", lambda: "redis")

    sent = SimpleNamespace(chat=SimpleNamespace(id=-1003), message_id=778)
    message = SimpleNamespace(reply=AsyncMock(return_value=sent))

    await features._reply_complaint_notice(message, "✅ Жалоба принята.")

    schedule.assert_awaited_once()
    assert schedule.await_args.kwargs["delay_seconds"] == COMPLAINT_MESSAGE_TTL_SECONDS


@pytest.mark.asyncio
async def test_admin_notification_gets_no_ttl(monkeypatch) -> None:
    """Кнопки в личке админа должны пережить 45 секунд."""
    monkeypatch.setattr(group_commands, "get_assignment", AsyncMock(return_value=None))
    schedule = AsyncMock(return_value=True)
    monkeypatch.setattr(group_commands, "schedule_message_deletion", schedule)

    await _notification_markup()

    schedule.assert_not_awaited()


# --- доставка админам --------------------------------------------------


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        add=Mock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )


def _complaint_command(monkeypatch, delivered: int, attempted: int) -> SimpleNamespace:
    monkeypatch.setattr(
        group_commands, "_active_group", AsyncMock(return_value=_group())
    )
    monkeypatch.setattr(
        group_commands,
        "_target_from_reply",
        lambda _m: SimpleNamespace(id=99, full_name="Нарушитель", username=None),
    )
    monkeypatch.setattr(
        group_commands,
        "_notify_complaint_recipients",
        AsyncMock(return_value=(delivered, attempted)),
    )
    monkeypatch.setattr(group_commands, "schedule_message_deletion", AsyncMock())

    replied = SimpleNamespace(chat=SimpleNamespace(id=-1003), message_id=1)
    message = SimpleNamespace(
        text="жалоба",
        chat=SimpleNamespace(id=-1003),
        from_user=SimpleNamespace(id=42, full_name="Жалобщик", username="reporter"),
        reply_to_message=SimpleNamespace(
            message_id=555, text="плохое сообщение", caption=None
        ),
        reply=AsyncMock(return_value=replied),
        message_id=555,
    )
    return message


def _reply_text(message: SimpleNamespace) -> str:
    return message.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_full_delivery_reports_all_admins_notified(monkeypatch) -> None:
    message = _complaint_command(monkeypatch, delivered=3, attempted=3)
    session = _session()

    await group_commands.group_complaint(
        message, SimpleNamespace(), session
    )

    assert "Администраторы группы получили уведомление" in _reply_text(message)


@pytest.mark.asyncio
async def test_partial_delivery_names_the_gap(monkeypatch) -> None:
    """Раньше при уходе только владельцу группа всё равно слышала «все получили»."""
    message = _complaint_command(monkeypatch, delivered=1, attempted=4)
    session = _session()

    await group_commands.group_complaint(
        message, SimpleNamespace(), session
    )

    text = _reply_text(message)
    assert "1 из 4" in text
    assert "/start" in text
    assert "Администраторы группы получили" not in text


@pytest.mark.asyncio
async def test_failed_recipient_is_logged(monkeypatch) -> None:
    """Отказ Telegram больше не уходит в молчание."""
    from aiogram.exceptions import TelegramBadRequest

    monkeypatch.setattr(group_commands, "get_assignment", AsyncMock(return_value=None))
    warning = Mock()
    monkeypatch.setattr(group_commands, "log", warning)

    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=[
                    SimpleNamespace(message_id=1),
                    TelegramBadRequest(method="sendMessage", message="chat not found"),
                    SimpleNamespace(message_id=2),
                ]
        )
    )
    session = SimpleNamespace(add=Mock(), commit=AsyncMock(), scalars=AsyncMock())
    session.scalars.return_value = SimpleNamespace(all=lambda: [111, 222])

    delivered, attempted = await group_commands._notify_complaint_recipients(
        bot,
        session,
        _group(),
        42,
        "Жалобщик",
        99,
        "Нарушитель",
        555,
        "плохое сообщение",
        _complaint(),
    )

    assert (delivered, attempted) == (2, 3)
    assert warning.warning.call_args.args[0] == "complaint_notification_failed"
    assert warning.warning.call_args.kwargs["admin_telegram_id"] in {111, 222}