"""The kernel's volatile event bus over the ``redis`` broadcast driver of
``sandloom-redis`` (the driver's own tests and kit run live in
``packages/sandloom-redis/tests``).

Skipped when no Redis is reachable at ``SANDLOOM_TEST_CACHE_URL`` / ``CACHE_URL``,
a failure in the ``redis-drivers`` lane (``SANDLOOM_REDIS_REQUIRED``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from _redis_required import redis_unavailable
from sandloom_redis.broadcast import RedisBroadcastProvider

from sandloom.core.testing import reload_test_env

redis_asyncio = pytest.importorskip("redis.asyncio")


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[3] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _url() -> str:
    url = os.getenv("SANDLOOM_TEST_CACHE_URL") or os.getenv("CACHE_URL")
    if not url:
        redis_unavailable("no test Redis URL: Redis broadcast tests skipped")
    return url


@pytest.fixture
async def _redis() -> AsyncIterator[Any]:
    client = redis_asyncio.from_url(_url())
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip, or fail when required
        redis_unavailable("Redis unreachable")
    yield client
    await client.aclose()


class _Proxy:
    """A TCP relay in front of the test Redis that counts what it relays."""

    def __init__(self) -> None:
        self.relayed = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self.port = 0

    async def start(self) -> _Proxy:
        target = urlsplit(_url())
        host, port = target.hostname or "127.0.0.1", target.port or 6379

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
            self._writers += [writer, upstream_writer]
            await asyncio.gather(
                self._pump(reader, upstream_writer),
                self._pump(upstream_reader, writer),
                return_exceptions=True,
            )

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _pump(
        self, source: asyncio.StreamReader, sink: asyncio.StreamWriter
    ) -> None:
        while data := await source.read(65536):
            self.relayed += len(data)
            sink.write(data)
            await sink.drain()

    def url(self, db: int = 0) -> str:
        parts = urlsplit(_url())
        return urlunsplit(
            parts._replace(netloc=f"127.0.0.1:{self.port}", path=f"/{db}")
        )

    async def stop(self) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()


async def test_the_volatile_bus_delivers_over_redis_broadcast(_redis: Any) -> None:
    from sandloom.core.eventing.pubsub_bus import RedisEventBus
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    provider = RedisBroadcastProvider(
        name="default",
        driver="redis",
        options={"url": _url(), "prefix": f"bus{uuid4().hex[:8]}:"},
    )
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


async def test_the_redis_bus_runs_on_the_redis_of_its_broadcast_instance(
    _redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named eventbus instance publishes and subscribes through the broadcast
    instance its ``broadcast`` option names, on that instance's Redis (its
    network, TLS and credentials), never on the cache Redis."""
    from sandloom.core.config.settings import settings
    from sandloom.core.drivers.bootstrap import build_infra_registry
    from sandloom.core.infra.active import use_infra
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    proxy = await _Proxy().start()
    channel = "core.sidebus.v1"
    monkeypatch.setattr(
        settings,
        "infra",
        {
            "broadcast": {"side": {"driver": "redis", "url": proxy.url(5)}},
            "eventbus": {"sidebus": {"driver": "redis", "broadcast": "side"}},
        },
    )
    schemas = InMemorySchemaRegistry()
    schemas.register(channel, "SideBus", {"type": "object"})
    registry = build_infra_registry(schemas)
    got: list[Any] = []

    async def on_event(envelope: Any) -> None:
        got.append(envelope)

    eventbus = registry.provider("eventbus", "sidebus")
    with use_infra(registry):
        try:
            await eventbus.aopen()
            assert (await eventbus.health()).status == "ok"
            bus = eventbus.bus()  # type: ignore[attr-defined]
            handle = await bus.subscribe(channel, on_event)
            await asyncio.wait_for(handle.wait_ready(timeout=5), timeout=6)
            await bus.publish_event(channel, {"n": 1})
            for _ in range(300):
                if got:
                    break
                await asyncio.sleep(0.01)
            assert [e["payload"] for e in got] == [{"n": 1}]
            assert proxy.relayed > 0  # it went over the instance's own Redis
        finally:
            await eventbus.aclose()
            await registry.provider("broadcast", "side").aclose()
            await proxy.stop()
