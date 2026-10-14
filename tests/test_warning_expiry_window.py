"""Warnings must expire on their own: a 24h window is the default contract.

Warnings used to accumulate indefinitely by default (30 days, and the menu
offered no shorter option), so a user who warned twice over a month could
reach the mute threshold without a single recent offence. Days were then
replaced by hours, because an integer count of days cannot express "exactly
24h": a warning issued at 23:50 outlived one issued at 00:10 by nearly a day.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.safety import warning_expiry_cutoff, warning_expiry_label


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_default_window_is_exactly_one_day() -> None:
    models = _source("app/db/models.py")
    assert (
        "warning_expire_hours: Mapped[int] = mapped_column(Integer, default=24)"
        in models
    )
    assert "warning_expire_days" not in models


def test_twenty_four_hours_is_exactly_a_day() -> None:
    now = datetime(2026, 10, 14, 23, 50, tzinfo=timezone.utc)

    cutoff = warning_expiry_cutoff(24, now)
    assert cutoff == now - timedelta(hours=24)
    # Ровно сутки, а не календарная граница: предупреждение вечернего выдачи
    # должно сгореть через 24 часа, то есть утром следующего дня, а не после
    # полуночи того же дня.
    assert cutoff == datetime(2026, 10, 13, 23, 50, tzinfo=timezone.utc)


def test_zero_never_expires() -> None:
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    assert warning_expiry_cutoff(0, now) is None


@pytest.mark.parametrize("hours", [1, 24, 168, 720, 2160])
def test_supported_options_have_a_cutoff(hours: int) -> None:
    assert warning_expiry_cutoff(hours) is not None


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(0, "не удалять"), (24, "1 дн."), (168, "7 дн."), (12, "12 ч."), (1, "1 ч.")],
)
def test_label_reads_naturally(hours: int, expected: str) -> None:
    assert warning_expiry_label(hours) == expected


def test_menu_offers_hours_and_keeps_longer_options() -> None:
    menu = _source("app/keyboards/panel.py").split(
        "def automation_warning_menu", 1
    )[1].split("\ndef ", 1)[0]

    assert '(24,"24 часа")' in menu
    assert '(168,"7 дней")' in menu
    assert '(720,"30 дней")' in menu
    assert '(2160,"90 дней")' in menu
    # «Не удалять» остаётся осознанным выбором владельца.
    assert '(0,"Не удалять")' in menu
    # Значения в днях больше не принимаются: колонка хранит часы.
    assert '(30,"30 дней")' not in menu


def test_set_handler_accepts_the_hour_values() -> None:
    automation = _source("app/handlers/automation.py")

    assert "automation_warning_set:\\d+:(0|24|168|720|2160)" in automation
    assert "group.settings.warning_expire_hours = hours" in automation


def test_group_command_accepts_hours_and_requires_a_unit() -> None:
    body = _source("app/handlers/safety.py").split(
        "async def warning_expiry", 1
    )[1].split("\n@router", 1)[0]

    # Без единицы измерения «преды срок 24» неоднозначно: сутки это или месяц.
    assert 'unit not in {"ч", "ч.", "д", "д."}' in body
    assert 'hours = amount if unit in {"ч", "ч."} else amount * 24' in body
    assert "group.settings.warning_expire_hours = hours" in body


def test_expiry_revalidates_settings_under_a_group_lock() -> None:
    body = _source("app/tasks_warning_expiry.py").split(
        "async def expire_warnings", 1
    )[1]

    lock = body.index(".with_for_update()")
    automation = body.index("automation_enabled", lock)
    cutoff = body.index("warning_expiry_cutoff(hours)", automation)
    guard = body.index("if cutoff is None:", cutoff)
    rows = body.index("Warning.active.is_(True)", guard)

    # Порог берётся из живых настроек под блокировкой группы.
    assert lock < automation < cutoff < guard < rows


def test_expiry_runs_on_a_short_cadence() -> None:
    scheduler = _source("app/tasks_scheduler.py")

    assert "WARNING_TASK_SECONDS = 60.0" in scheduler
    assert '"expire_warnings", expire_warnings' in scheduler
    assert 0 < 60 <= 3600


def test_existing_choices_are_converted_to_hours_not_reset() -> None:
    """Migration must preserve the owner's window instead of forcing 24h."""
    migration = _source("alembic/versions/0052_warning_expiry_hours.py")
    upgrade = migration.split("def upgrade", 1)[1].split("def downgrade", 1)[0]

    # Пересчёт, а не присваивание константы: 7 дней должны стать 168 часов.
    assert "warning_expire_hours = warning_expire_days * 24" in upgrade
    # Перенос обязан случиться до удаления старой колонки.
    assert upgrade.index("warning_expire_days * 24") < upgrade.index(
        "drop_column"
    )


def test_stored_values_are_validated_in_hours() -> None:
    settings_io = _source("app/services/settings_io.py")

    assert '"warning_expire_hours": (0, 87_600)' in settings_io
    assert "warning_expire_days" not in settings_io