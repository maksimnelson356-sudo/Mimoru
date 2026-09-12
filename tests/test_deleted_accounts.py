from types import SimpleNamespace
from pathlib import Path

from app.utils.telegram_users import is_deleted_profile


def user(first_name: str, username=None, *, is_bot=False):
    return SimpleNamespace(first_name=first_name, username=username, is_bot=is_bot)


def test_deleted_profile_detection_is_conservative():
    assert is_deleted_profile(user("Deleted Account"))
    assert is_deleted_profile(user("Удалённый аккаунт"))
    assert not is_deleted_profile(user("Deleted Account", "real_username"))
    assert not is_deleted_profile(user("Deleted User"))
    assert not is_deleted_profile(user("Deleted Account", is_bot=True))


def test_deleted_accounts_are_exposed_in_members_menu():
    source = Path("app/keyboards/panel.py").read_text(encoding="utf-8")
    assert "🪦 Удалённые аккаунты" in source
    assert "deleted_accounts_scan:" in source
    assert "deleted_accounts_remove_confirm:" in source


def test_deleted_accounts_router_is_registered():
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert "deleted_accounts.router" in source


def test_group_statistics_include_deleted_accounts():
    source = Path("app/handlers/dashboard.py").read_text(encoding="utf-8")
    assert "GroupMember.is_deleted_account.is_(True)" in source
    assert "🪦 Удалённых аккаунтов" in source


def test_cleanup_requires_confirmation_and_rescans():
    source = Path("app/handlers/deleted_accounts.py").read_text(encoding="utf-8")
    assert "deleted_accounts_remove_confirm" in source
    assert "scan_known_members(bot, session, group)" in source
    assert "remove_deleted_accounts(bot, session, group)" in source
