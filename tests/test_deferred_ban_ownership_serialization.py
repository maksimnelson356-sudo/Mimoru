from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source() -> str:
    return (ROOT / "app/handlers/deferred_bans.py").read_text(encoding="utf-8")


def test_group_loader_supports_explicit_for_update_boundary() -> None:
    source = _source()
    start = source.index("async def _group(")
    end = source.index("async def _pending_for", start)
    body = source[start:end]

    assert "for_update: bool = False" in body
    assert "if for_update:" in body
    assert "query = query.with_for_update()" in body


def test_manual_ban_locks_group_before_live_permission_and_side_effects() -> None:
    source = _source()
    start = source.index("async def ban_reference")
    end = source.index("async def unban_reference", start)
    body = source[start:end]

    lock = body.index("await _group(session, message.chat.id, for_update=True)")
    permission = body.index('await can_moderate(bot, session, group, message.from_user.id, "ban")')
    target = body.index("await can_moderate_target(session, group, message.from_user.id, target_id)")
    pending = body.index("await _save_pending(")
    telegram = body.index("await bot.ban_chat_member(group.telegram_chat_id, target_id)")
    commit = body.index("await session.commit()")

    assert lock < permission < target < pending < telegram < commit
    assert "await _is_unmanaged_telegram_admin" in body


def test_unknown_username_pending_ban_is_committed_under_group_lock() -> None:
    source = _source()
    start = source.index("async def ban_reference")
    end = source.index("async def unban_reference", start)
    body = source[start:end]

    lock = body.index("await _group(session, message.chat.id, for_update=True)")
    pending = body.index("await _save_pending(")
    commit = body.index("await session.commit()")
    assert lock < pending < commit


def test_manual_unban_locks_group_before_permission_telegram_and_commit() -> None:
    source = _source()
    start = source.index("async def unban_reference")
    end = source.index("async def enforce_pending_ban_on_join", start)
    body = source[start:end]

    lock = body.index("await _group(session, message.chat.id, for_update=True)")
    permission = body.index('await can_moderate(bot, session, group, message.from_user.id, "unban")')
    telegram = body.index("await bot.unban_chat_member(group.telegram_chat_id, target_id, only_if_banned=True)")
    commit = body.index("await session.commit()")
    assert lock < permission < telegram < commit


def test_automatic_pending_ban_enforcement_uses_redis_claim_without_actor_permission_check() -> None:
    source = _source()
    start = source.index("async def enforce_pending_ban_on_join")
    body = source[start:]

    assert "await _group(session, message.chat.id)" in body
    assert "await _group(session, message.chat.id, for_update=True)" not in body
    assert "redis.set(claim_key" in body
    assert "can_moderate(" not in body
    assert "await bot.ban_chat_member(group.telegram_chat_id, member.id)" in body
