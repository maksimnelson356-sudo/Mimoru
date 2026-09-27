from unittest.mock import AsyncMock, Mock

import pytest

from app.db.required_reconcile_models import RequiredSubscriptionReconcile
from app.services import required_reconciles


@pytest.mark.asyncio
async def test_enqueue_required_reconcile_persists_intent() -> None:
    session = AsyncMock()
    session.scalar.return_value = None
    session.add = Mock()

    created = await required_reconciles.enqueue_required_reconcile(
        session,
        group_id=3,
        telegram_chat_id=-1001234567890,
        channel_username="@required_channel",
        dedupe_key="ad:42",
    )

    assert created is True
    session.add.assert_called_once()
    intent = session.add.call_args.args[0]
    assert isinstance(intent, RequiredSubscriptionReconcile)
    assert intent.group_id == 3
    assert intent.telegram_chat_id == -1001234567890
    assert intent.dedupe_key == "ad:42"
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_required_reconcile_is_idempotent() -> None:
    session = AsyncMock()
    session.scalar.return_value = 9
    session.add = Mock()

    created = await required_reconciles.enqueue_required_reconcile(
        session,
        group_id=3,
        telegram_chat_id=-1003,
        channel_username="@channel",
        dedupe_key="ad:42",
    )

    assert created is False
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


def test_scheduler_runs_required_reconcile_worker() -> None:
    from pathlib import Path

    source = Path("app/tasks_scheduler.py").read_text(encoding="utf-8")
    assert "process_required_subscription_reconciles" in source
    assert "REQUIRED_RECONCILE_SECONDS" in source
    assert '"process_required_subscription_reconciles"' in source


def test_restriction_intent_uses_backoff_and_stale_recovery() -> None:
    assert required_reconciles.RETRY_BASE_SECONDS > 0
    assert required_reconciles.MAX_RETRY_SECONDS >= required_reconciles.RETRY_BASE_SECONDS
    assert required_reconciles.PROCESSING_STALE_SECONDS > 0
    assert hasattr(required_reconciles, "recover_stale_reconcile_intents")
