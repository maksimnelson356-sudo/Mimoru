"""Delivery-failure resilience for the owner notification boundary.

The daily-report and subscription-notice workers commit their pre-send claim
*before* calling the Telegram API. If a Telegram failure escaped the delivery
helper, the claim would stay committed and the notification would be silently
suppressed for the rest of its window. These tests lock the contract that every
Telegram-side failure is *reported* to the caller instead of raising.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage

from app.services import owner_notifications
from app.services.owner_notifications import send_to_current_group_owner

ROOT = Path(__file__).resolve().parents[1]
OWNER_ID = 4242
GROUP_ID = 7

_METHOD = SendMessage(chat_id=OWNER_ID, text="report")


def _install_session(monkeypatch, group) -> AsyncMock:
    """Route the helper's SessionFactory to an in-memory fake session."""
    session = AsyncMock()
    session.scalar.return_value = group
    factory_cm = AsyncMock()
    factory_cm.__aenter__.return_value = session
    factory_cm.__aexit__.return_value = False
    monkeypatch.setattr(owner_notifications, "SessionFactory", lambda: factory_cm)
    return session


def _active_group() -> SimpleNamespace:
    return SimpleNamespace(is_active=True, owner_telegram_id=OWNER_ID)


def _bot_failing_with(error: Exception) -> AsyncMock:
    bot = AsyncMock()
    bot.send_message.side_effect = error
    return bot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(TelegramNetworkError(method=_METHOD, message="connection reset"), id="network"),
        pytest.param(TelegramServerError(method=_METHOD, message="502 Bad Gateway"), id="server_5xx"),
        pytest.param(
            TelegramRetryAfter(method=_METHOD, message="Too Many Requests", retry_after=42),
            id="retry_after",
        ),
        pytest.param(TelegramForbiddenError(method=_METHOD, message="bot was blocked"), id="forbidden"),
        pytest.param(TelegramBadRequest(method=_METHOD, message="chat not found"), id="bad_request"),
        pytest.param(TimeoutError(), id="timeout"),
    ],
)
async def test_telegram_failures_are_reported_so_the_pre_send_claim_is_released(
    monkeypatch, error
) -> None:
    """A failed delivery must return a failure tuple, never raise."""
    session = _install_session(monkeypatch, _active_group())

    sent, owner_id, reported = await send_to_current_group_owner(
        _bot_failing_with(error), group_id=GROUP_ID, text="report"
    )

    assert sent is False
    assert owner_id == OWNER_ID
    assert reported not in (None, "group_unavailable")
    # The lock must be released so the caller can retry without a stuck row lock.
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_delivery_reports_sent_without_error(monkeypatch) -> None:
    session = _install_session(monkeypatch, _active_group())
    bot = AsyncMock()

    sent, owner_id, reported = await send_to_current_group_owner(
        bot, group_id=GROUP_ID, text="report"
    )

    assert (sent, owner_id, reported) == (True, OWNER_ID, None)
    bot.send_message.assert_awaited_once_with(OWNER_ID, "report")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group",
    [
        pytest.param(None, id="missing_group"),
        pytest.param(SimpleNamespace(is_active=False, owner_telegram_id=OWNER_ID), id="inactive_group"),
        pytest.param(SimpleNamespace(is_active=True, owner_telegram_id=None), id="no_owner"),
    ],
)
async def test_unavailable_group_skips_the_external_call(monkeypatch, group) -> None:
    session = _install_session(monkeypatch, group)
    bot = AsyncMock()

    sent, owner_id, reported = await send_to_current_group_owner(
        bot, group_id=GROUP_ID, text="report"
    )

    assert (sent, owner_id, reported) == (False, None, "group_unavailable")
    bot.send_message.assert_not_awaited()
    session.commit.assert_not_awaited()


def test_delivery_boundary_catches_the_telegram_api_base_class() -> None:
    """Guard the import: narrowing this tuple reintroduces stranded claims."""
    source = (ROOT / "app/services/owner_notifications.py").read_text(encoding="utf-8")
    body = source.split("async def send_to_current_group_owner", 1)[1]

    assert "except (TelegramAPIError, TimeoutError) as error:" in body
    assert "TelegramBadRequest" not in body
    assert "TelegramForbiddenError" not in body


def test_both_notice_workers_release_the_claim_when_delivery_fails() -> None:
    """Both call sites must release their pre-send claim on a failure tuple."""
    source = (ROOT / "app/tasks_delivery.py").read_text(encoding="utf-8")

    daily = source.split("async def send_daily_reports", 1)[1].split(
        "async def _claim_subscription_notice", 1
    )[0]
    notices = source.split("async def send_subscription_notices", 1)[1].split(
        "async def recover_interrupted_scheduled_messages", 1
    )[0]

    for body, release in (
        (daily, "await _release_daily_report_claim(group.id, local_today)"),
        (notices, "await _release_subscription_notice_claim("),
    ):
        failure_branch = body.split("if not sent:", 1)[1]
        assert release in failure_branch
