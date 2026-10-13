from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source() -> str:
    return (ROOT / "app/handlers/admin_access_mode.py").read_text(encoding="utf-8")


def _handler(source: str, name: str) -> str:
    body = source.split(f"async def {name}(", 1)[1]
    next_handler = body.find("\nasync def ")
    next_router = body.find("\n@router.")
    boundaries = [index for index in (next_handler, next_router) if index >= 0]
    return body[: min(boundaries)] if boundaries else body


def test_group_resolver_supports_for_update() -> None:
    source = _source()
    helper = source.split("async def _group(", 1)[1].split("async def _owner_group(", 1)[0]
    assert "for_update: bool = False" in helper
    assert "query = query.with_for_update()" in helper

    owner = source.split("async def _owner_group(", 1)[1].split("async def _apply_assignment(", 1)[0]
    assert "for_update: bool = False" in owner
    assert "_group(session, group_id, for_update=for_update)" in owner


def test_owner_admin_apply_locks_before_assignment_side_effects() -> None:
    source = _source()
    body = _handler(source, "admin_access_apply")
    lock = body.index("for_update=True")
    apply = body.index("await _apply_assignment(")
    commit = body.index("await session.commit()")
    assert lock < apply < commit


def test_rank_change_locks_before_live_rank_authorization() -> None:
    source = _source()
    body = _handler(source, "rank_change_keep_mode")
    lock = body.index("for_update=True")
    apply = body.index("await _apply_assignment(")
    commit = body.index("await session.commit()")
    assert lock < apply < commit


def test_assignment_authorization_precedes_telegram_admin_effects() -> None:
    source = _source()
    helper = source.split("async def _apply_assignment(", 1)[1].split("def _mode_markup", 1)[0]
    auth = helper.index("await can_assign_rank(")
    promote = helper.index("await promote_with_title(")
    demote = helper.index("await demote_telegram_admin(")
    flush = helper.index("await session.flush()")
    assert auth < promote < flush
    assert auth < demote < flush


def test_intermediate_rank_screens_remain_nonlocking() -> None:
    source = _source()
    for name in ("admin_access_home", "admin_access_user", "admin_access_rank", "rank_quick_choose_mode", "rank_add_choose_mode"):
        assert "for_update=True" not in _handler(source, name)
