import asyncio
import pytest

import app.preflight as preflight


class FakeConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def execute(self, query):
        return None


class FakeEngine:
    def connect(self):
        return FakeConn()


class FakeRedis:
    def __init__(self, ping_result=True):
        self._ping_result = ping_result

    async def ping(self):
        return self._ping_result

    async def aclose(self):
        pass


class DummySettings:
    bot_token = "123456:abcdef"
    redis_url = "redis://localhost:6379/0"


@pytest.mark.asyncio
async def test_run_preflight_success(monkeypatch):
    monkeypatch.setattr(preflight, "engine", FakeEngine())
    monkeypatch.setattr(preflight, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(preflight.Redis, "from_url", lambda url, decode_responses=True: FakeRedis())

    # Should complete without raising
    await preflight.run_preflight()


@pytest.mark.asyncio
async def test_run_preflight_redis_failure(monkeypatch):
    monkeypatch.setattr(preflight, "engine", FakeEngine())
    monkeypatch.setattr(preflight, "get_settings", lambda: DummySettings())
    # Redis ping will fail
    monkeypatch.setattr(preflight.Redis, "from_url", lambda url, decode_responses=True: FakeRedis(ping_result=False))

    with pytest.raises(RuntimeError):
        await preflight.run_preflight()
