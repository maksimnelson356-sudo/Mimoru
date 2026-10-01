from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _handler_source() -> str:
    return (ROOT / "app/handlers/deleted_accounts.py").read_text(encoding="utf-8")


def test_owned_group_supports_locked_ownership_lookup() -> None:
    source = _handler_source()
    start = source.index("async def owned_group")
    end = source.index("async def _screen", start)
    body = source[start:end]

    assert "for_update: bool = False" in body
    assert "owner_or_admin_clause(user_id)" in body
    assert "if for_update:" in body
    assert "query = query.with_for_update()" in body


def test_deleted_account_scan_uses_locked_group_through_commit() -> None:
    source = _handler_source()
    start = source.index("async def deleted_accounts_scan")
    end = source.index("async def deleted_accounts_remove_confirm", start)
    body = source[start:end]

    lock = body.index("for_update=True")
    scan = body.index("await scan_known_members(bot, session, group)")
    commit = body.index("await session.commit()")
    assert lock < scan < commit


def test_deleted_account_cleanup_holds_lock_through_scan_bans_and_commit() -> None:
    handlers = _handler_source()
    start = handlers.index("async def deleted_accounts_remove(")
    body = handlers[start:]

    lock = body.index("for_update=True")
    scan = body.index("await scan_known_members(bot, session, group)")
    cleanup = body.index("await remove_deleted_accounts(bot, session, group)")
    audit = body.index("log_action(")
    commit = body.index("await session.commit()")
    assert lock < scan < cleanup < audit < commit

    service = (ROOT / "app/services/deleted_accounts.py").read_text(encoding="utf-8")
    cleanup_start = service.index("async def remove_deleted_accounts")
    cleanup_body = service[cleanup_start:]
    assert "bot.ban_chat_member(" in cleanup_body


def test_read_only_deleted_account_callbacks_remain_nonlocking() -> None:
    source = _handler_source()

    screen_start = source.index("async def deleted_accounts_screen")
    screen_end = source.index("async def deleted_accounts_scan", screen_start)
    screen = source[screen_start:screen_end]

    confirm_start = source.index("async def deleted_accounts_remove_confirm")
    confirm_end = source.index("async def deleted_accounts_remove(", confirm_start)
    confirm = source[confirm_start:confirm_end]

    assert "for_update=True" not in screen
    assert "for_update=True" not in confirm


def test_deleted_account_cleanup_router_is_live() -> None:
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "deleted_accounts.router," in main
