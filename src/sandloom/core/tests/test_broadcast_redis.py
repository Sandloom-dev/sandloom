"""The ``redis`` broadcast driver against a real Redis (``BroadcastConformance``).

Skipped when no Redis is reachable at ``SANDLOOM_TEST_CACHE_URL`` / ``CACHE_URL``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from sandloom.core.drivers.redis_broadcast import RedisBroadcastProvider
from sandloom.core.infra.ports.broadcast import BroadcastProvider
from sandloom.core.testing import reload_test_env
from sandloom.driverkit.testing import BroadcastConformance

redis_asyncio = pytest.importorskip("redis.asyncio")

_PAYLOAD_LIMIT = 8 * 1024 * 1024


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _url() -> str:
    url = os.getenv("SANDLOOM_TEST_CACHE_URL") or os.getenv("CACHE_URL")
    if not url:
        pytest.skip("no test Redis URL: Redis broadcast kit skipped")
    return url


@pytest.fixture
async def _redis() -> AsyncIterator[Any]:
    client = redis_asyncio.from_url(_url())
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip("Redis unreachable")
    yield client
    await client.aclose()


def _provider(**options: Any) -> RedisBroadcastProvider:
    return RedisBroadcastProvider(
        name="default", driver="redis", options={"url": _url(), **options}
    )


class TestTheRedisDriver(BroadcastConformance):
    payload_limit_bytes = _PAYLOAD_LIMIT
    health_timeout_seconds = 5.0

    @pytest.fixture
    def make_provider(self, _redis: Any) -> Callable[[], BroadcastProvider]:
        return _provider

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], BroadcastProvider]:
        marker = self.leak_marker
        return lambda: RedisBroadcastProvider(
            name="default",
            driver="redis",
            options={"url": f"redis://:{marker}@127.0.0.1:1/0"},
        )

    async def interrupt_connections(self, provider: BroadcastProvider) -> None:
        client = redis_asyncio.from_url(_url())
        try:
            await client.execute_command("CLIENT", "KILL", "TYPE", "pubsub")
        finally:
            await client.aclose()


async def test_the_prefix_keeps_two_instances_apart(_redis: Any) -> None:
    first, second = _provider(prefix="one:"), _provider(prefix="two:")
    await first.aopen()
    await second.aopen()
    try:
        in_second = await second.subscribe(["same"])
        in_first = await first.subscribe(["same"])
        await first.publish("same", b"for one")
        message = await _next(in_first)
        assert message.channel == "same"
        assert message.payload == b"for one"
        assert await _quiet(in_second)
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_name_with_glob_characters_is_a_literal_prefix(_redis: Any) -> None:
    provider = _provider(prefix="a[b]*:")
    await provider.aopen()
    try:
        subscription = await provider.subscribe(["x.*"])
        await provider.publish("x.y", b"hit")
        assert (await _next(subscription)).payload == b"hit"
        other = _provider(prefix="aXb:")
        await other.aopen()
        try:
            await other.publish("x.y", b"miss")
            assert await _quiet(subscription)
        finally:
            await other.aclose()
    finally:
        await provider.aclose()


async def test_the_volatile_bus_delivers_over_redis_broadcast(_redis: Any) -> None:
    from sandloom.core.eventing.pubsub_bus import RedisEventBus
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    provider = _provider(prefix=f"bus{uuid4().hex[:8]}:")
    await provider.aopen()
    registry = InMemorySchemaRegistry()
    registry.register("core.redisbus.v1", "RedisBus", {"type": "object"})
    bus = RedisEventBus(registry, broadcast=provider)
    got: list[Any] = []

    async def on_event(envelope: Any) -> None:
        got.append(envelope)

    try:
        handle = await bus.subscribe("core.redisbus.v1", on_event)
        for _ in range(200):
            if handle.state.value == "running":
                break
            await asyncio.sleep(0.01)
        assert handle.state.value == "running"
        await bus.publish_event("core.redisbus.v1", {"n": 1})
        for _ in range(200):
            if got:
                break
            await asyncio.sleep(0.01)
        assert [e["payload"] for e in got] == [{"n": 1}]
    finally:
        await bus.close()
        await provider.aclose()


async def test_the_test_isolation_hook_prefixes_the_channels() -> None:
    options = RedisBroadcastProvider.test_isolation({"url": "redis://x"}, "gw3")
    assert options == {"url": "redis://x", "prefix": "wgw3:"}


async def _next(subscription: Any) -> Any:
    return await asyncio.wait_for(subscription.__aiter__().__anext__(), timeout=5)


async def _quiet(subscription: Any) -> bool:
    try:
        await asyncio.wait_for(subscription.__aiter__().__anext__(), timeout=0.3)
    except TimeoutError:
        return True
    return False
