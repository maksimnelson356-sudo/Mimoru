from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_warning_expiry_rechecks_live_settings_under_group_lock() -> None:
    source = (ROOT / "app/tasks_warning_expiry.py").read_text(encoding="utf-8")
    body = source.split("async def expire_warnings", 1)[1]

    candidates = body.index("select(Group.id)")
    group_lock = body.index("select(Group)", candidates + 1)
    for_update = body.index(".with_for_update()", group_lock)
    automation = body.index("group.settings.automation_enabled")
    hours = body.index("group.settings.warning_expire_hours")
    cutoff = body.index("warning_expiry_cutoff(hours)")
    warnings = body.index("select(Warning)", cutoff)
    warning_lock = body.index(".with_for_update()", warnings)
    mutate = body.index("row.active = False", warning_lock)

    assert candidates < group_lock < for_update < automation < hours < cutoff < warnings < warning_lock < mutate


def test_warning_expiry_zero_or_disabled_settings_stop_before_warning_mutation() -> None:
    source = (ROOT / "app/tasks_warning_expiry.py").read_text(encoding="utf-8")
    body = source.split("async def expire_warnings", 1)[1]

    disabled = body.index("not group.settings.automation_enabled")
    cutoff = body.index("warning_expiry_cutoff(hours)")
    no_cutoff = body.index("if cutoff is None:", cutoff)
    warnings = body.index("select(Warning)", no_cutoff)
    mutate = body.index("row.active = False", warnings)

    assert disabled < cutoff < no_cutoff < warnings < mutate


def test_production_scheduler_uses_hardened_warning_expiry_only() -> None:
    delivery = (ROOT / "app/tasks_delivery.py").read_text(encoding="utf-8")

    assert "from app.tasks_warning_expiry import expire_warnings" in delivery
    assert "from app.tasks import (\n    expire_warnings,\n)" not in delivery
    assert "await expire_warnings()" in delivery


def test_legacy_tasks_keep_other_shadowed_workers_out_of_production_imports() -> None:
    delivery = (ROOT / "app/tasks_delivery.py").read_text(encoding="utf-8")
    legacy = (ROOT / "app/tasks.py").read_text(encoding="utf-8")

    assert "async def expire_punishments" in legacy
    assert "async def expire_captcha_sessions" in legacy
    assert "async def expire_warnings" in legacy
    assert "from app.services.punishment_expiry import expire_punishments" in delivery
    assert "from app.tasks_captcha import expire_captcha_sessions" in delivery
    assert "from app.tasks_warning_expiry import expire_warnings" in delivery
    assert "from app.tasks import" not in delivery
