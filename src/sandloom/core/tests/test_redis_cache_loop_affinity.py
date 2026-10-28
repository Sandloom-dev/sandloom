"""The redis cache driver's clients follow the running event loop.

A ``redis.asyncio`` client is bound to the loop it first ran on. pytest-asyncio
gives every test its own loop (and a CLI may ``asyncio.run`` twice), so a
process-wide client memoized on loop A used to fail on loop B with "Event loop
is closed" — module conftests carried a ``_reset_cache_client`` hack poking
the driver's private singleton. The driver now rebuilds the client when the
running loop changes (closing the stale one best-effort).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.drivers import redis_cache
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.testing.workers import worker_env_values


class _FakeClient:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        # The stale client's loop is gone: closing it may blow up — the driver
        # must swallow that.
        raise RuntimeError("Event loop is closed")

    async def get(self, key: str) -> str | None:
        assert asyncio.get_running_loop() is self.loop, "used on a foreign loop"
        return None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> list[_FakeClient]:
    built: list[_FakeClient] = []

    def _build(url: str) -> _FakeClient:
        client = _FakeClient()
        built.append(client)
        return client

    monkeypatch.setattr(redis_cache, "_build_client", _build)
    monkeypatch.setattr(redis_cache, "_default_client", None)
    monkeypatch.setattr(redis_cache, "_default_client_origin", None)
    return built


def test_default_client_is_rebuilt_on_a_new_loop(fake_redis: list[_FakeClient]) -> None:
    async def use() -> Any:
        first = await redis_cache.get_default_client()
        again = await redis_cache.get_default_client()
        assert first is again  # memoized within one loop
        await first.get("k")
        return first

    a = asyncio.run(use())
    b = asyncio.run(use())
    assert a is not b
    assert a.closed  # stale client closed best-effort (its error swallowed)
    assert len(fake_redis) == 2


def test_injected_default_client_is_never_replaced(
    fake_redis: list[_FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    injected = object()

    async def first() -> None:
        await redis_cache.get_default_client()  # driver-built on loop 1

    asyncio.run(first())
    monkeypatch.setattr(redis_cache, "_default_client", injected)

    async def second() -> Any:
        return await redis_cache.get_default_client()

    assert asyncio.run(second()) is injected


def test_named_instance_client_follows_the_loop(fake_redis: list[_FakeClient]) -> None:
    provider = redis_cache.create_redis_cache(
        InstanceConfig(
            capability="cache",
            name="sessions",
            driver="redis",
            options={"url": "redis://example.invalid/3"},
        )
    )

    async def use() -> None:
        await provider.get("k")

    asyncio.run(use())
    asyncio.run(use())
    assert len(fake_redis) == 2
    assert fake_redis[0].closed


def _test_cache_url() -> str | None:
    env = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    return os.getenv("CACHE_URL") or env.get("CACHE_URL")


def test_real_redis_client_survives_two_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end against the test Redis: two loops, no 'Event loop is closed'."""
    url = _test_cache_url()
    if not url:
        pytest.skip("no test CACHE_URL configured")
    monkeypatch.setattr(redis_cache.settings, "cache_url", url)
    monkeypatch.setattr(redis_cache, "_default_client", None)
    monkeypatch.setattr(redis_cache, "_default_client_origin", None)

    async def roundtrip() -> None:
        client = await redis_cache.get_default_client()
        try:
            await client.ping()
        except Exception as exc:  # noqa: BLE001 - test Redis unreachable: skip
            pytest.skip(f"test Redis not reachable: {exc}")
        await client.set("sandloom:loop-affinity", "1", ex=5)
        assert await client.get("sandloom:loop-affinity") == "1"

    asyncio.run(roundtrip())
    asyncio.run(roundtrip())  # used to raise "Event loop is closed"

    async def close() -> None:
        await redis_cache.close_default_client()

    asyncio.run(close())
