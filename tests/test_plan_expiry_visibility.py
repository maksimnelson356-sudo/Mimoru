"""Видимость состояния тарифа для владельца группы.

Истёкший пробный период молча отключает ежедневные отчёты и снижает
лимиты. Здесь проверяется, что причина объясняется прямо, а для
осознанно выбранного FREE и активного платного тарифа лишнего текста
не появляется.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.plans import (
    effective_plan,
    feature_available,
    plan_limit,
    plan_limit_hint,
    plan_notice,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _group(plan_code: str = "trial", *, expires_in_days: int | None = 7):
    expires_at = (
        None if expires_in_days is None else NOW + timedelta(days=expires_in_days)
    )
    return SimpleNamespace(plan_code=plan_code, plan_expires_at=expires_at)


# --- корень проблемы ----------------------------------------------------


def test_expired_trial_silently_becomes_free():
    group = _group("trial", expires_in_days=-1)

    assert effective_plan(group, now=NOW) == "free"
    assert feature_available(group, "daily_reports") is False
    assert plan_limit(group, "words") == 10


def test_fresh_trial_keeps_reports_and_limits():
    group = _group("trial", expires_in_days=3)

    assert effective_plan(group, now=NOW) == "trial"
    assert feature_available(group, "daily_reports") is True
    assert plan_limit(group, "words") == 100


# --- видимое предупреждение --------------------------------------------


def test_expired_trial_is_explained():
    notice = plan_notice(_group("trial", expires_in_days=-3), now=NOW)

    assert notice is not None
    assert "Пробный период истёк" in notice
    assert "ежедневные отчёты" in notice
    assert "10 запрещённых слов" in notice
    assert "5 причин наказаний" in notice
    assert "STANDARD" in notice


def test_trial_about_to_expire_is_explained():
    notice = plan_notice(_group("trial", expires_in_days=1), now=NOW)

    assert notice is not None
    assert "истекает" in notice
    assert "FREE" in notice


def test_deliberate_free_plan_has_no_warning():
    """FREE без истёкшего триала — осознанный выбор, а не забытый триал."""
    assert plan_notice(_group("free", expires_in_days=None), now=NOW) is None
    assert plan_notice(_group("free", expires_in_days=-30), now=NOW) is None


def test_comfortable_trial_has_no_warning():
    assert plan_notice(_group("trial", expires_in_days=20), now=NOW) is None


def test_active_paid_plan_has_no_warning():
    assert plan_notice(_group("pro", expires_in_days=25), now=NOW) is None


# --- пояснение к отказу по лимиту --------------------------------------


def test_limit_hint_explains_expired_trial():
    hint = plan_limit_hint(_group("trial", expires_in_days=-3))

    assert "Пробный период истёк" in hint
    assert "STANDARD" in hint


def test_limit_hint_is_empty_without_expired_trial():
    assert plan_limit_hint(_group("trial", expires_in_days=5)) == ""
    assert plan_limit_hint(_group("free", expires_in_days=None)) == ""


# --- панель группы ------------------------------------------------------


def test_panel_notice_is_appended_to_card() -> None:
    from app.handlers.panel import with_plan_notice

    text = with_plan_notice("Карточка группы", _group("trial", expires_in_days=-2))

    assert text.startswith("Карточка группы")
    assert "Пробный период истёк" in text


def test_panel_notice_leaves_text_untouched_when_healthy() -> None:
    from app.handlers.panel import with_plan_notice

    assert with_plan_notice("Карточка группы", _group("pro", expires_in_days=10)) == (
        "Карточка группы"
    )