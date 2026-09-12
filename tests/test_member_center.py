from pathlib import Path


def test_member_center_router_is_registered():
    source = Path('app/main.py').read_text(encoding="utf-8")
    assert 'member_center.router' in source


def test_members_menu_exposes_member_tools():
    source = Path('app/keyboards/panel.py').read_text(encoding="utf-8")
    for callback in ('member_find:', 'active_punishments:', 'member_history:'):
        assert callback in source


def test_complaint_queue_controls_exist():
    source = Path('app/keyboards/panel.py').read_text(encoding="utf-8")
    for callback in ('complaints:', 'complaint_close:', 'complaint_reject:'):
        assert callback in source


def test_member_center_has_panel_actions():
    source = Path('app/handlers/member_center.py').read_text(encoding="utf-8")
    for action in ('unwarn', 'unmute', 'unban'):
        assert action in source
    assert 'deactivate_punishments' in source


def test_member_card_includes_activity_and_active_restrictions():
    source = Path('app/handlers/member_center.py').read_text(encoding="utf-8")
    assert 'DailyStat.messages_count' in source
    assert 'Warning.active.is_(True)' in source
    assert 'Punishment.active.is_(True)' in source
