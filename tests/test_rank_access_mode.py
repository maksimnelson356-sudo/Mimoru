from types import SimpleNamespace

import pytest

from app.services import rank_access


@pytest.mark.asyncio
async def test_stale_telegram_assignment_fails_closed(monkeypatch):
    assignment = SimpleNamespace(access_mode="telegram")
    actor = SimpleNamespace(level=80, code="chief_admin", assignment=assignment)
    group = SimpleNamespace(id=1, telegram_chat_id=-100123)

    async def fake_actor(*args, **kwargs):
        return actor

    async def fake_is_admin(*args, **kwargs):
        return False

    monkeypatch.setattr(rank_access, "get_actor_rank", fake_actor)
    monkeypatch.setattr(rank_access, "is_telegram_admin", fake_is_admin)

    resolved = await rank_access.get_actor_rank_with_access(object(), object(), group, 42)
    assert resolved is None


@pytest.mark.asyncio
async def test_bot_only_assignment_does_not_require_telegram_admin(monkeypatch):
    assignment = SimpleNamespace(access_mode="bot_only")
    actor = SimpleNamespace(level=80, code="chief_admin", assignment=assignment)
    group = SimpleNamespace(id=1, telegram_chat_id=-100123)
    telegram_checks = 0

    async def fake_actor(*args, **kwargs):
        return actor

    async def fake_is_admin(*args, **kwargs):
        nonlocal telegram_checks
        telegram_checks += 1
        return False

    monkeypatch.setattr(rank_access, "get_actor_rank", fake_actor)
    monkeypatch.setattr(rank_access, "is_telegram_admin", fake_is_admin)

    resolved = await rank_access.get_actor_rank_with_access(object(), object(), group, 42)
    assert resolved is actor
    assert telegram_checks == 0


@pytest.mark.asyncio
async def test_unknown_access_mode_fails_closed(monkeypatch):
    assignment = SimpleNamespace(access_mode="legacy_unknown")
    actor = SimpleNamespace(level=80, code="chief_admin", assignment=assignment)
    group = SimpleNamespace(id=1, telegram_chat_id=-100123)

    async def fake_actor(*args, **kwargs):
        return actor

    monkeypatch.setattr(rank_access, "get_actor_rank", fake_actor)
    resolved = await rank_access.get_actor_rank_with_access(object(), object(), group, 42)
    assert resolved is None


@pytest.mark.asyncio
async def test_group_stats_uses_access_mode_aware_actor(monkeypatch):
    from app.handlers import group_stats_v2

    group = SimpleNamespace(id=1, telegram_chat_id=-100123)
    calls: list[tuple[object, object, object, int]] = []

    async def fake_actor(bot, session, resolved_group, user_id):
        calls.append((bot, session, resolved_group, user_id))
        return None

    monkeypatch.setattr(group_stats_v2, "get_actor_rank_with_access", fake_actor)
    bot = object()
    session = object()
    allowed = await group_stats_v2._allowed(bot, session, group, 42)

    assert allowed is False
    assert calls == [(bot, session, group, 42)]


@pytest.mark.asyncio
async def test_group_stats_allows_live_admin_actor(monkeypatch):
    from app.handlers import group_stats_v2

    group = SimpleNamespace(id=1, telegram_chat_id=-100123)
    actor = SimpleNamespace(code="chief_admin", level=80, assignment=SimpleNamespace())

    async def fake_actor(*args, **kwargs):
        return actor

    monkeypatch.setattr(group_stats_v2, "get_actor_rank_with_access", fake_actor)

    assert await group_stats_v2._allowed(object(), object(), group, 42) is True


def test_deferred_bans_use_access_mode_aware_guard():
    source = open("app/handlers/deferred_bans.py", encoding="utf-8").read()
    assert "from app.services.access import can_moderate" in source
    assert 'await can_moderate(bot, session, group, message.from_user.id, "ban")' in source
    assert 'await can_moderate(bot, session, group, message.from_user.id, "unban")' in source
    assert "actor_has_permission" not in source
