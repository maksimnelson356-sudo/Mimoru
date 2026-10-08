import asyncio
import types

import pytest

import app.health as health


class FakeAsyncContext:
    def __init__(self, raise_on_execute: bool = False):
        self.raise_on_execute = raise_on_execute

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def execute(self, query):
        if self.raise_on_execute:
            raise RuntimeError("db fail")
        return "OK"


def make_fake_context(raise_on_execute: bool = False) -> FakeAsyncContext:
    return FakeAsyncContext(raise_on_execute)


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
async def test_dependencies_ok_all_good(monkeypatch):
    # Both DB and Redis succeed
    monkeypatch.setattr(health, "SessionFactory", lambda: make_fake_context(False))
    monkeypatch.setattr(health, "Redis", object.__getattribute__(health, "Redis"))
    # Patch health.HealthServer.redis directly to a fake
    sr = FakeRedis(True)
    h = health.HealthServer(sr, host="0.0.0.0", port=0)
    res = await h._dependencies_ok()
    assert res is True


@pytest.mark.asyncio
async def test_dependencies_ok_db_failure(monkeypatch):
    # DB raises exception during execute
    monkeypatch.setattr(health, "SessionFactory", lambda: make_fake_context(True))
    sr = FakeRedis(True)
    h = health.HealthServer(sr, host="0.0.0.0", port=0)
    res = await h._dependencies_ok()
    assert res is False
