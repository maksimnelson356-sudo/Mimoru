from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from redis.exceptions import RedisError

import app.services.background_leader as background_leader
from app.services.background_leader import (
    _acquire_lease,
    _release_lease,
    _renew_lease,
    _renew_lease_once,
    leader_background_loop,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    async def eval(self, script: str, _numkeys: int, key: str, token: str, *args):
        if self.values.get(key) != token:
            return 0
        if "expire" in script:
            self.expirations[key] = int(args[0])
            return 1
        if "del" in script:
            del self.values[key]
            self.expirations.pop(key, None)
            return 1
        raise AssertionError("unexpected script")


@pytest.mark.asyncio
async def test_background_lease_has_single_owner_and_owner_only_release() -> None:
    redis = FakeRedis()

    assert await _acquire_lease(redis, "worker-a") is True
    assert await _acquire_lease(redis, "worker-b") is False
    assert await _renew_lease_once(redis, "worker-a") is True
    assert await _renew_lease_once(redis, "worker-b") is False

    await _release_lease(redis, "worker-b")
    assert await _acquire_lease(redis, "worker-b") is False

    await _release_lease(redis, "worker-a")
    assert await _acquire_lease(redis, "worker-b") is True


@pytest.mark.asyncio
async def test_renewal_redis_error_becomes_safe_lease_loss(monkeypatch) -> None:
    class RenewErrorRedis(FakeRedis):
        async def eval(self, script: str, _numkeys: int, key: str, token: str, *args):
            raise RedisError("temporary renewal failure")

    monkeypatch.setattr(background_leader, "RENEW_SECONDS", 0.001)
    local_stop = asyncio.Event()

    renewed = await _renew_lease(RenewErrorRedis(), "worker-a", local_stop)

    assert renewed is False
    assert local_stop.is_set() is False


@pytest.mark.asyncio
async def test_acquisition_redis_error_retries_without_killing_leader(monkeypatch) -> None:
    stop_event = asyncio.Event()

    class FlakyAcquireRedis:
        def __init__(self) -> None:
            self.calls = 0

        async def set(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RedisError("temporary acquire failure")
            stop_event.set()
            return False

    redis = FlakyAcquireRedis()
    monkeypatch.setattr(background_leader, "RETRY_SECONDS", 0.001)

    await asyncio.wait_for(
        leader_background_loop(None, redis, stop_event),  # type: ignore[arg-type]
        timeout=1,
    )

    assert redis.calls == 2
    assert stop_event.is_set()


def test_main_starts_leased_background_scheduler() -> None:
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "from app.services.background_leader import leader_background_loop" in source
    assert "from app.services.background_leader import background_loop" not in source
    assert "leader_background_loop(bot, redis, stop_event)" in source


def test_lease_uses_compare_and_set_style_redis_scripts() -> None:
    source = (ROOT / "app/services/background_leader.py").read_text(encoding="utf-8")
    assert "nx=True" in source
    assert "ex=LEASE_SECONDS" in source
    assert "redis.call('get', KEYS[1]) == ARGV[1]" in source
    assert "redis.call('expire', KEYS[1], ARGV[2])" in source
    assert "redis.call('del', KEYS[1])" in source
