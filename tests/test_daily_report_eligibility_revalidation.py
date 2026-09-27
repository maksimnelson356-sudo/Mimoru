from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import tasks_delivery


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "app/tasks_delivery.py").read_text(encoding="utf-8")


def _function(name: str) -> str:
    marker = f"async def {name}("
    start = SOURCE.index(marker)
    next_start = SOURCE.find("\n\nasync def ", start + len(marker))
    return SOURCE[start:] if next_start == -1 else SOURCE[start:next_start]


def test_daily_report_claim_revalidates_under_group_lock() -> None:
    claim = _function("_claim_daily_report")
    group_lock = claim.index("select(Group).where(Group.id == group_id).with_for_update()")
    enabled = claim.index("not settings.reports_enabled")
    entitlement = claim.index('not feature_available(locked_group, "daily_reports")')
    report_hour = claim.index("settings.report_hour_utc != local_now.hour")
    durable_claim = claim.index("update(GroupSettings)")
    commit = claim.index("await session.commit()")
    assert group_lock < enabled < entitlement < report_hour < durable_claim < commit


def test_daily_report_claim_invalidates_timezone_and_local_date_changes() -> None:
    claim = _function("_claim_daily_report")
    assert "settings.timezone_name != expected_timezone" in claim
    assert "current_local_today != local_today" in claim
    assert "settings.last_report_date == local_today" in claim


def test_daily_report_delivery_uses_locked_claim_snapshot() -> None:
    sender = _function("send_daily_reports")
    claim = sender.index("title = await _claim_daily_report")
    text = sender.index("report_text =", claim)
    delivery = sender.index("await send_to_current_group_owner", text)
    assert claim < text < delivery
    assert 'f"🏠 {title}\\n"' in sender[text:delivery]


def test_daily_report_claim_remains_durable_before_send() -> None:
    claim = _function("_claim_daily_report")
    sender = _function("send_daily_reports")
    assert ".values(last_report_date=local_today)" in claim
    assert "await session.commit()" in claim
    assert sender.index("await _claim_daily_report") < sender.index("await send_to_current_group_owner")


def test_production_background_loop_calls_hardened_daily_reports() -> None:
    scheduler = (ROOT / "app/tasks_scheduler.py").read_text(encoding="utf-8")
    leader = (ROOT / "app/services/background_leader.py").read_text(encoding="utf-8")
    assert "send_daily_reports," in scheduler
    assert 'await _run_job("send_daily_reports", lambda: send_daily_reports(bot))' in scheduler
    assert "REPORT_TASK_SECONDS = 60.0" in scheduler
    assert "from app.tasks_scheduler import background_loop" in leader


@pytest.mark.asyncio
async def test_failed_daily_report_releases_claim_for_retry(monkeypatch) -> None:
    session = AsyncMock()
    session.execute.return_value = None
    factory_cm = AsyncMock()
    factory_cm.__aenter__.return_value = session
    factory_cm.__aexit__.return_value = False
    monkeypatch.setattr(tasks_delivery, "SessionFactory", lambda: factory_cm)

    await tasks_delivery._release_daily_report_claim(3, "2026-09-25")

    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_subscription_notice_releases_claim_for_retry(monkeypatch) -> None:
    session = AsyncMock()
    factory_cm = AsyncMock()
    factory_cm.__aenter__.return_value = session
    factory_cm.__aexit__.return_value = False
    monkeypatch.setattr(tasks_delivery, "SessionFactory", lambda: factory_cm)

    await tasks_delivery._release_subscription_notice_claim(
        3,
        "expiry_notice_1",
        tasks_delivery.datetime(2026, 9, 25, tzinfo=tasks_delivery.timezone.utc),
    )

    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()
