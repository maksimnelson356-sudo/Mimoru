from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source() -> str:
    return (ROOT / "app/handlers/automation.py").read_text(encoding="utf-8")


def test_owned_group_supports_locked_ownership_lookup() -> None:
    source = _source()
    helper = source.split("async def owned_group", 1)[1].split(
        "@router.callback_query", 1
    )[0]
    assert "for_update: bool = False" in helper
    assert "owner_or_admin_clause(user_id)" in helper
    assert "if for_update:" in helper
    assert "query = query.with_for_update()" in helper


def test_mutating_automation_callbacks_lock_group_through_commit() -> None:
    source = _source()
    handlers = [
        "automation_toggle",
        "cleanup_set",
        "warnings_set",
        "newcomer_set",
    ]
    boundaries = {
        "automation_toggle": "cleanup_screen",
        "cleanup_set": "warnings_screen",
        "warnings_set": "newcomer_screen",
        "newcomer_set": "automation_logs",
    }
    mutations = {
        "automation_toggle": "group.settings.automation_enabled =",
        "cleanup_set": "group.settings.deleted_cleanup_schedule =",
        "warnings_set": "group.settings.warning_expire_days =",
        "newcomer_set": "s = group.settings",
    }

    for name in handlers:
        body = source.split(f"async def {name}", 1)[1].split(
            f"async def {boundaries[name]}", 1
        )[0]
        lock = body.index("for_update=True")
        mutation = body.index(mutations[name])
        commit = body.index("await session.commit()")
        assert lock < mutation < commit


def test_read_only_automation_callbacks_remain_nonlocking() -> None:
    source = _source()
    read_only = [
        ("automation_home", "automation_toggle"),
        ("cleanup_screen", "cleanup_set"),
        ("warnings_screen", "warnings_set"),
        ("newcomer_screen", "newcomer_set"),
        ("automation_logs", None),
    ]
    for name, next_name in read_only:
        body = source.split(f"async def {name}", 1)[1]
        if next_name is not None:
            body = body.split(f"async def {next_name}", 1)[0]
        assert "for_update=True" not in body
