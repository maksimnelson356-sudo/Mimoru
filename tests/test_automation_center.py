from pathlib import Path


def test_automation_migration_follows_people_center():
    source = Path("alembic/versions/0028_automation_center.py").read_text(encoding="utf-8")
    assert 'revision = "0028_automation_center"' in source
    assert 'down_revision = "0027_people_center"' in source


def test_automation_router_is_registered():
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert "automation.router" in source


def test_automation_callbacks_have_handlers():
    keyboard = Path("app/keyboards/panel.py").read_text(encoding="utf-8")
    handler = Path("app/handlers/automation.py").read_text(encoding="utf-8")
    callbacks = [
        "automation:",
        "automation_toggle:",
        "automation_cleanup:",
        "automation_cleanup_set:",
        "automation_warnings:",
        "automation_warning_set:",
        "automation_newcomer:",
        "automation_newcomer_set:",
        "automation_logs:",
    ]
    for callback in callbacks:
        assert callback in keyboard or callback in handler
        assert callback in handler
