import asyncio
import pytest

import app.health as health


class DummyCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass
    async def execute(self, query):
        return None


class DummyRedisOK:
    async def ping(self):
        return True

    async def aclose(self):
        pass


class DummyRedisFail:
    async def ping(self):
        raise RuntimeError("ping fail")

    async def aclose(self):
        pass


class DummySettings:
    bot_token = "123456:abcdef"
    redis_url = "redis://localhost:6379/0"


@pytest.mark.asyncio
async def test_concurrent_dependencies_ok_concurrent_calls(monkeypatch):
    monkeypatch.setattr(health, "SessionFactory", lambda: DummyCtx())
    sr = DummyRedisOK()
    h = health.HealthServer(sr, host="0.0.0.0", port=0)
    # Run multiple checks concurrently to ensure thread-safety / coroutine-safety
    results = await asyncio.gather(*(h._dependencies_ok() for _ in range(20)))
    assert all(results)


@pytest.mark.asyncio
async def test_concurrent_dependencies_ok_redis_failure(monkeypatch):
    monkeypatch.setattr(health, "SessionFactory", lambda: DummyCtx())
    sr = DummyRedisFail()
    h = health.HealthServer(sr, host="0.0.0.0", port=0)
    results = await asyncio.gather(*(h._dependencies_ok() for _ in range(5)))
    # Any failure in redis should yield False for that call
    assert all(res is False for res in results)
