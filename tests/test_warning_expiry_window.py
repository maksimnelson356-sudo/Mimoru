"""Warnings must expire on their own: a 24h window is the default contract.

Warnings used to accumulate indefinitely by default (30 days, and the menu
offered no shorter option), so a user who warned twice over a month could
reach the mute threshold without a single recent offence. The storage unit is
days, so "через сутки" is 1 day.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.safety import warning_expiry_cutoff


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_default_window_is_one_day() -> None:
    models = _source("app/db/models.py")
    assert (
        "warning_expire_days: Mapped[int] = mapped_column(Integer, default=1)"
        in models
    )


def test_menu_offers_the_one_day_option() -> None:
    panel = _source("app/keyboards/panel.py")
    menu = panel.split("def automation_warning_menu", 1)[1].split("\ndef ", 1)[0]

    assert '(1,"1 день")' in menu
    # «Не удалять» остаётся осознанным выбором владельца, а не отсутствием
    # коротких сроков.
    assert '(0,"Не удалять")' in menu


def test_one_day_is_accepted_by_the_set_handler() -> None:
    automation = _source("app/handlers/automation.py")
    setter = automation.split("async def warnings_set", 1)[1].split("\n@router", 1)[0]

    assert 'automation_warning_set:\\d+:(0|1|7|30|90)' in automation
    assert "group.settings.warning_expire_days = days" in setter


def test_one_day_actually_expires() -> None:
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    # Сутки назад — сгорает, минута назад — ещё живое.
    assert warning_expiry_cutoff(1, now) == now - timedelta(days=1)
    assert warning_expiry_cutoff(0, now) is None


@pytest.mark.parametrize("days", [1, 7, 30, 90])
def test_supported_options_round_trip(days: int) -> None:
    assert warning_expiry_cutoff(days) is not None


def test_expiry_revalidates_settings_and_stops_on_zero() -> None:
    body = _source("app/tasks_warning_expiry.py").split(
        "async def expire_warnings", 1
    )[1]

    lock = body.index(".with_for_update()")
    automation = body.index("automation_enabled", lock)
    cutoff = body.index("warning_expiry_cutoff(days)", automation)
    guard = body.index("if cutoff is None:", cutoff)
    rows = body.index("Warning.active.is_(True)", guard)

    # Порогок берётся из живых настроек под блокировкой группы, а не из
    # значения, прочитанного до неё.
    assert lock < automation < cutoff < guard < rows


def test_expiry_runs_on_a_short_cadence() -> None:
    scheduler = _source("app/tasks_scheduler.py")

    assert "WARNING_TASK_SECONDS = 60.0" in scheduler
    assert '"expire_warnings", expire_warnings' in scheduler
    # Продвижение суток не может ждать суточного цикла.
    assert 0 < 60 <= 3600


def test_existing_groups_on_the_old_default_are_migrated() -> None:
    """ORM default only covers new groups; live rows keep their stored value."""
    migration = _source("alembic/versions/0051_warning_expiry_one_day.py")

    upgrade = migration.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    # Только прежний дефолт 30 переводится на сутки.
    assert "warning_expire_days = 1" in upgrade
    assert "WHERE warning_expire_days = 30" in upgrade
    # Явный выбор владельца (0 = не удалять, 7, 90) не должен переписываться:
    # условие обязано быть точным равенством, а не «больше 1».
    assert ">= 1" not in upgrade
    assert "!= 30" not in upgrade


def test_expiry_is_wired_into_the_production_scheduler() -> None:
    scheduler = _source("app/tasks_scheduler.py")
    assert "from app.tasks_warning_expiry import expire_warnings" in scheduler

    delivery = _source("app/tasks_delivery.py")
    # Устаревшая копия цикла не должна импортировать другую реализацию.
    assert "from app.tasks import (\n    expire_warnings,\n)" not in delivery