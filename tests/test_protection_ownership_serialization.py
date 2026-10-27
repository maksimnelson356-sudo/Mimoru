from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


MODULES = {
    "slow_mode.py": {
        "mutating": ("slow_mode_enable", "slow_mode_disable"),
        "read_only": ("slow_mode_status",),
    },
    "quarantine.py": {
        "mutating": ("quarantine_enable", "quarantine_disable", "quarantine_rule"),
        "read_only": ("quarantine_status",),
    },
    "mentions.py": {
        "mutating": ("mentions_toggle", "mentions_limit", "hashtags_limit", "mentions_punishment"),
        "read_only": ("mentions_status",),
    },
    "sender_chats.py": {
        "mutating": ("toggle", "allow_sender", "deny_sender"),
        "read_only": ("status", "list_senders"),
    },
    "edit_protection.py": {
        "mutating": ("edit_toggle", "edit_window"),
        "read_only": ("edit_status",),
    },
}


def _source(name: str) -> str:
    return (ROOT / "app/handlers" / name).read_text(encoding="utf-8")


def _handler_body(source: str, name: str) -> str:
    return source.split(f"async def {name}(", 1)[1].split("@router.message", 1)[0]


def test_shared_boundary_locks_before_live_authorization() -> None:
    source = (ROOT / "app/services/owner_management.py").read_text(encoding="utf-8")
    assert ".with_for_update()" in source
    assert source.index(".with_for_update()") < source.index("await can_manage_group(")
    assert "Group.is_active.is_(True)" in source


def test_all_protection_mutations_request_locked_boundary() -> None:
    for filename, contract in MODULES.items():
        source = _source(filename)
        assert "managed_group_for_message" in source
        for name in contract["mutating"]:
            body = _handler_body(source, name)
            assert "for_update=True" in body
            assert "await session.commit()" in body
            assert body.index("for_update=True") < body.index("await session.commit()")


def test_protection_status_and_list_handlers_remain_nonlocking() -> None:
    for filename, contract in MODULES.items():
        source = _source(filename)
        for name in contract["read_only"]:
            body = _handler_body(source, name)
            assert "for_update=True" not in body
