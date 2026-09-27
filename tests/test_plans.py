from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from app.handlers.panel import _plan_comparison_text
from app.services.plans import effective_plan, plan_limit


def test_expired_plan_becomes_free():
    group = SimpleNamespace(plan_code="pro", plan_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert effective_plan(group) == "free"


def test_pro_limits_are_higher():
    group = SimpleNamespace(plan_code="pro", plan_expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    assert plan_limit(group, "words") >= 100
    assert plan_limit(group, "channels") >= 3


def test_panel_plan_comparison_uses_canonical_limits() -> None:
    text = _plan_comparison_text()

    assert "10 слов · 5 каналов" in text
    assert "100 слов · 5 каналов" in text
    assert "1000 слов · 5 каналов" in text
    assert "модераторы: без лимита" in text
    assert "1 канал" not in text
    assert "3 канала" not in text
    assert "10 модераторов" not in text
    assert "50 модераторов" not in text
