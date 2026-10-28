"""Split-deployment eventing over a SHARED Redis Streams broker.

Each "service" below is an independent ``CoreFacade`` + ``StreamsEventBus`` +
``RedisStreamsTransport`` with its own Redis connection — from the broker's
point of view exactly what two deployments sharing one Redis look like. Proves:

* H1 — equally named consumers of different modules get DIFFERENT consumer
  groups (each service receives every event instead of splitting them);
  duplicate groups inside one process and a group already owned by another
  module / database fail startup.
* H2 — streams are trimmed (MAXLEN / MINID retention); ``start_from`` controls
  where a new group starts; group lag / pending is observable.
* M1 — a DLQ replay is delivered to the ORIGINAL group only.
* M4 — the subscription inventory lists live consumers; publishing a version
  nobody reads / starving a sibling version is reported.
* L1 — ``EVENT_STREAM_PREFIX`` namespaces every key.

Skipped when Redis (``CACHE_URL``) is unreachable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import orjson
import pytest
from fastapi import FastAPI

from sandloom.core.consumers.runtime import ConsumerConfigurationError, ConsumerRuntime
from sandloom.core.eventing.delivery import stream_start_id
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.streams_transport import RedisStreamsTransport
from sandloom.core.eventing.transport_base import GroupOwnershipError
from sandloom.core.facade import CoreFacade
from sandloom.core.testing import reload_test_env
from sandloom.core.tests._schemas import PermissiveSchemaRegistry

redis_asyncio = pytest.importorskip("redis.asyncio")


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.fixture(autouse=True)
def _fake_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bus' best-effort cache inbox; keep it process-local and hermetic.
    store: dict[str, str] = {}

    class _FakeRedis:
        async def set(self, key, value, nx=False, ex=None):
            if nx and key in store:
                return None
            store[key] = value
            return True

        async def get(self, key):
            return store.get(key)

    async def _get_cache():
        return _FakeRedis()

    monkeypatch.setattr(
        "sandloom.core.drivers.redis_cache.get_default_client", _get_cache
    )


@pytest.fixture
async def redis_url() -> AsyncIterator[str]:
    url = os.getenv("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not set; Redis Streams integration skipped")
    client = redis_asyncio.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip("Redis unreachable")
    finally:
        await client.aclose()
    yield url


@pytest.fixture
def prefix() -> str:
    # A unique key namespace per test: no cross-test (or cross-agent) bleed.
    return f"t{uuid.uuid4().hex[:10]}:"


@pytest.fixture
async def raw_redis(redis_url: str, prefix: str) -> AsyncIterator[Any]:
    client = redis_asyncio.from_url(redis_url, decode_responses=True)
    yield client
    keys = [k async for k in client.scan_iter(match=f"{prefix}*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


class _Service:
    """One deployment: its own facade, bus, transport and Redis connection."""

    def __init__(self, redis_url: str, prefix: str, **transport_opts: Any) -> None:
        self.client = redis_asyncio.from_url(redis_url, decode_responses=True)

        async def _factory() -> Any:
            return self.client

        self.transport = RedisStreamsTransport(
            client_factory=_factory, retry_base_ms=0, prefix=prefix, **transport_opts
        )
        self.registry = PermissiveSchemaRegistry()
        self.bus = StreamsEventBus(self.registry, self.transport, poll_interval=0.01)
        self.core = CoreFacade(app=FastAPI())
        self.core.register_schema_registry(self.registry)
        self.core.set_event_bus(self.bus)
        self.runtime: ConsumerRuntime | None = None

    def runtime_for(self, *, database_id: str = "db-shared") -> ConsumerRuntime:
        @asynccontextmanager
        async def _opener(_name: str) -> AsyncIterator[Any]:
            yield object()

        self.runtime = ConsumerRuntime(
            self.core,
            session_opener=_opener,
            startup_timeout=3.0,
            retry_delay=0.01,
            database_identity=lambda _db: database_id,
            service_name="svc",
        )
        return self.runtime

    async def close(self) -> None:
        if self.runtime is not None:
            await self.runtime.stop()
        await self.bus.close()
        await self.client.aclose()


def _register(
    service: _Service,
    module: str,
    channel: str,
    seen: list[str],
    *,
    name: str = "notify",
    **opts: Any,
) -> None:
    async def handler(_session: Any, envelope: Any) -> None:
        seen.append(str(envelope["payload"]["n"]))

    with service.core.module_registration(module) as api:
        api.register_event_consumer(channel, handler, name=name, durable=False, **opts)


async def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


# --------------------------------------------------------------------------- #
# H1 — consumer identity
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_same_name_in_two_services_each_receive_every_event(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    """Probe from the review: 20 events used to split ~3/17 between services."""
    channel = "demo.thing.happened.v1"
    a, b = _Service(redis_url, prefix), _Service(redis_url, prefix)
    seen_a: list[str] = []
    seen_b: list[str] = []
    try:
        _register(a, "svc_a.notifications", channel, seen_a)
        _register(b, "svc_b.notifications", channel, seen_b)
        await a.runtime_for(database_id="db-a").start()
        await b.runtime_for(database_id="db-b").start()
        for n in range(20):
            await a.bus.publish_event(channel, {"n": n})
        await _wait_for(lambda: len(seen_a) == 20 and len(seen_b) == 20)
        assert sorted(seen_a, key=int) == [str(n) for n in range(20)]
        assert sorted(seen_b, key=int) == [str(n) for n in range(20)]
        groups = {g["group"] for g in await a.transport.group_info(channel)}
        assert groups == {
            "svc_a.notifications:notify",
            "svc_b.notifications:notify",
        }
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_replicas_of_one_service_share_the_group(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    channel = "demo.thing.replicated.v1"
    r1, r2 = _Service(redis_url, prefix), _Service(redis_url, prefix)
    seen: list[str] = []
    try:
        _register(r1, "svc.notifications", channel, seen)
        _register(r2, "svc.notifications", channel, seen)
        await r1.runtime_for(database_id="db-svc").start()
        await r2.runtime_for(database_id="db-svc").start()  # same owner: OK
        for n in range(10):
            await r1.bus.publish_event(channel, {"n": n})
        await _wait_for(lambda: len(seen) >= 10)
        await asyncio.sleep(0.2)
        # One group shared by the replicas: every event handled exactly once.
        assert sorted(seen, key=int) == [str(n) for n in range(10)]
    finally:
        await r1.close()
        await r2.close()


@pytest.mark.asyncio
async def test_group_owned_by_other_database_fails_startup(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    """The same module deployed in two services with different databases."""
    channel = "demo.thing.owned.v1"
    a, b = _Service(redis_url, prefix), _Service(redis_url, prefix)
    try:
        _register(a, "svc.notifications", channel, [])
        _register(b, "svc.notifications", channel, [])
        await a.runtime_for(database_id="db-a").start()
        with pytest.raises(ConsumerConfigurationError, match="owned by"):
            await b.runtime_for(database_id="db-b").start()
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_explicit_group_reused_by_other_module_fails_startup(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    channel = "demo.thing.pinned.v1"
    a, b = _Service(redis_url, prefix), _Service(redis_url, prefix)
    try:
        _register(a, "svc_a.mod", channel, [], group="shared-group")
        _register(b, "svc_b.mod", channel, [], group="shared-group")
        await a.runtime_for().start()
        with pytest.raises(ConsumerConfigurationError, match="shared-group"):
            await b.runtime_for().start()
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_duplicate_group_in_one_process_fails_startup() -> None:
    """Two handlers resolving to one group: the second would never run."""
    from sandloom.core.consumers.callbacks import EventConsumerDescriptor

    async def _h(_s: Any, _e: Any) -> None:
        return None

    bus = StreamsEventBus(PermissiveSchemaRegistry(), MemoryTransport())
    core = CoreFacade(app=FastAPI())
    core.set_event_bus(bus)
    for _ in range(2):  # direct facade registration bypasses the draft preflight
        core.register_event_consumer(
            EventConsumerDescriptor(name="dup", channel="demo.x.y.v1", handler=_h)
        )

    @asynccontextmanager
    async def _opener(_name: str) -> AsyncIterator[Any]:
        yield object()

    runtime = ConsumerRuntime(
        core, session_opener=_opener, startup_timeout=1.0, retry_delay=0.01
    )
    with pytest.raises(ConsumerConfigurationError, match="same consumer group"):
        await runtime.start()


def test_service_name_is_part_of_consumer_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.config.settings import settings
    from sandloom.core.eventing import transport_base as transport_transport_base

    monkeypatch.setattr(settings, "sandloom_service_name", "billing-api")
    assert transport_transport_base.process_consumer_id().startswith("billing-api@")
    monkeypatch.setattr(settings, "sandloom_service_name", None)
    assert "@" not in transport_transport_base.process_consumer_id()


# --------------------------------------------------------------------------- #
# H2 — retention, start position, lag
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_publish_caps_stream_length(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    svc = _Service(redis_url, prefix, maxlen=100, retention_seconds=0)
    try:
        for n in range(1000):
            await svc.transport.publish(
                "demo.capped.v1",
                {"id": str(n), "payload": {}},  # type: ignore[typeddict-item]
            )
        length = await raw_redis.xlen(f"{prefix}demo.capped.v1")
        assert length < 400  # MAXLEN ~ trims whole radix nodes (~100 entries)
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_publish_trims_entries_older_than_retention(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    svc = _Service(redis_url, prefix, retention_seconds=1, trim_interval_seconds=0.0)
    try:
        for n in range(500):
            await svc.transport.publish(
                "demo.aged.v1",
                {"id": str(n), "payload": {}},  # type: ignore[typeddict-item]
            )
        await asyncio.sleep(1.2)
        for n in range(300):  # new node(s); the aged ones fall below MINID
            await svc.transport.publish(
                "demo.aged.v1",
                {"id": f"new{n}", "payload": {}},  # type: ignore[typeddict-item]
            )
        assert await raw_redis.xlen(f"{prefix}demo.aged.v1") < 700
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_start_from_latest_skips_history(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    channel = "demo.thing.latest.v1"
    svc = _Service(redis_url, prefix)
    seen: list[str] = []
    try:
        for n in range(5):
            await svc.bus.publish_event(channel, {"n": n})
        _register(svc, "svc.mod", channel, seen, start_from="latest")
        await svc.runtime_for().start()
        await svc.bus.publish_event(channel, {"n": 42})
        await _wait_for(lambda: seen == ["42"])
    finally:
        await svc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("client_clock_offset_seconds", [-5.0, 0.0, 5.0])
async def test_start_from_timestamp(
    redis_url: str,
    prefix: str,
    raw_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    client_clock_offset_seconds: float,
) -> None:
    channel = "demo.thing.since.v1"
    svc = _Service(redis_url, prefix)
    try:
        seconds, microseconds = await raw_redis.time()
        cut = seconds * 1000 + microseconds // 1000
        wall_clock = time.time
        monkeypatch.setattr(
            time, "time", lambda: wall_clock() + client_clock_offset_seconds
        )
        # XADD IDs belong to Redis' clock, not the test client's clock. Explicit
        # broker IDs establish the boundary without sleeps or cross-host skew
        # assumptions; the timestamp-to-group/consume path remains real Redis.
        for n in range(3):
            await raw_redis.xadd(
                f"{prefix}{channel}",
                {"data": orjson.dumps({"id": f"a{n}", "payload": {}})},
                id=f"{cut - 100 + n}-0",
            )
        for event_id, stream_id in (
            ("prior-ms-max-sequence", f"{cut - 1}-18446744073709551615"),
            ("boundary", f"{cut}-0"),
            ("same-ms", f"{cut}-1"),
        ):
            await raw_redis.xadd(
                f"{prefix}{channel}",
                {"data": orjson.dumps({"id": event_id, "payload": {}})},
                id=stream_id,
            )
        await raw_redis.xadd(
            f"{prefix}{channel}",
            {"data": orjson.dumps({"id": "b", "payload": {}})},
            id=f"{cut + 100}-0",
        )
        await svc.transport.ensure_group(
            channel, "since", start_id=stream_start_id(cut)
        )
        got = await svc.transport.consume(
            channel, group="since", consumer="c", count=10
        )
        assert [d.envelope["id"] for d in got] == ["boundary", "same-ms", "b"]
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_start_from_timestamp_zero_includes_first_valid_stream_id(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    channel = "demo.thing.epoch.v1"
    svc = _Service(redis_url, prefix)
    try:
        # Redis forbids 0-0, so 0-1 is the first possible stream entry.
        for stream_id in ("0-1", "0-2", "1-0"):
            await raw_redis.xadd(
                f"{prefix}{channel}",
                {"data": orjson.dumps({"id": stream_id, "payload": {}})},
                id=stream_id,
            )
        await svc.transport.ensure_group(channel, "epoch", start_id=stream_start_id(0))
        got = await svc.transport.consume(
            channel, group="epoch", consumer="c", count=10
        )
        assert [d.envelope["id"] for d in got] == ["0-1", "0-2", "1-0"]
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_group_lag_and_pending_are_reported(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    channel = "demo.thing.lag.v1"
    svc = _Service(redis_url, prefix)
    try:
        await svc.transport.ensure_group(channel, "g")
        for n in range(4):
            await svc.transport.publish(channel, {"id": str(n), "payload": {}})  # type: ignore[typeddict-item]
        await svc.transport.consume(channel, group="g", consumer="c", count=1)
        (info,) = await svc.transport.group_info(channel)
        assert info["group"] == "g"
        assert info["pending"] == 1
        assert info["lag"] in (3, None)  # None on Redis < 7
    finally:
        await svc.close()


# --------------------------------------------------------------------------- #
# M1 — DLQ replay targets the original group
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dlq_replay_reaches_only_the_failed_group(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    channel = "demo.thing.dlq.v1"
    svc = _Service(redis_url, prefix, max_deliveries=1)
    t = svc.transport
    try:
        await t.ensure_group(channel, "ok-group")
        await t.ensure_group(channel, "failing-group")
        await t.publish(channel, {"id": "e1", "payload": {}})  # type: ignore[typeddict-item]
        ok = await t.consume(channel, group="ok-group", consumer="c", count=10)
        for d in ok:
            await t.ack(d)
        failed = await t.consume(channel, group="failing-group", consumer="c", count=10)
        assert [d.envelope["id"] for d in failed] == ["e1"]  # left unacked
        assert await t.consume(channel, group="failing-group", consumer="c") == []
        (entry,) = await t.list_dlq(channel)
        assert entry["group"] == "failing-group"

        assert await t.replay_dlq(channel) == 1
        # The healthy group must NOT see the replay (its non-durable handler
        # would repeat the side effect); the failing group gets it back.
        assert await t.consume(channel, group="ok-group", consumer="c") == []
        again = await t.consume(channel, group="failing-group", consumer="c")
        assert [d.envelope["id"] for d in again] == ["e1"]
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_memory_dlq_replay_is_targeted_too() -> None:
    t = MemoryTransport(max_deliveries=1)
    await t.ensure_group("s.x.v1", "ok")
    await t.ensure_group("s.x.v1", "bad")
    await t.publish("s.x.v1", {"id": "e1", "payload": {}})  # type: ignore[typeddict-item]
    for d in await t.consume("s.x.v1", group="ok", consumer="c"):
        await t.ack(d)
    await t.consume("s.x.v1", group="bad", consumer="c")
    await t.consume("s.x.v1", group="bad", consumer="c")  # -> DLQ
    assert (await t.list_dlq("s.x.v1"))[0]["group"] == "bad"
    assert await t.replay_dlq("s.x.v1", group="ok") == 0  # not ok's entry
    assert await t.replay_dlq("s.x.v1") == 1
    assert await t.consume("s.x.v1", group="ok", consumer="c") == []
    assert [
        d.envelope["id"] for d in await t.consume("s.x.v1", group="bad", consumer="c")
    ] == ["e1"]


# --------------------------------------------------------------------------- #
# M4 — subscription inventory + version starvation warning
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_inventory_lists_live_consumers_and_flags_starved_versions(
    redis_url: str,
    prefix: str,
    raw_redis: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    consumer_svc, producer_svc = (
        _Service(redis_url, prefix),
        _Service(redis_url, prefix),
    )
    try:
        _register(consumer_svc, "svc_c.mod", "demo.order.paid.v1", [])
        await consumer_svc.runtime_for().start()
        inventory = await producer_svc.transport.list_consumers()
        assert [(e["stream"], e["group"], e["module"]) for e in inventory] == [
            ("demo.order.paid.v1", "svc_c.mod:notify", "svc_c.mod")
        ]
        producer_svc.bus._AUDIENCE_DELAY_SECONDS = 0.05
        with caplog.at_level(
            logging.WARNING, logger="sandloom.core.eventing.event_bus"
        ):
            await producer_svc.bus.publish_event("demo.order.paid.v2", {"n": 1})
            await asyncio.sleep(0.3)
        text = caplog.text
        assert "no live consumer group is registered" in text
        assert "demo.order.paid.v1" in text and "starve" in text
    finally:
        await consumer_svc.close()
        await producer_svc.close()


@pytest.mark.asyncio
async def test_dual_publish_does_not_warn_about_starvation(
    redis_url: str,
    prefix: str,
    raw_redis: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sandloom.core.unit_of_work.versioning import publish_event_versions

    consumer_svc, producer_svc = (
        _Service(redis_url, prefix),
        _Service(redis_url, prefix),
    )
    try:
        _register(consumer_svc, "svc_c.mod", "demo.order.shipped.v1", [])
        _register(consumer_svc, "svc_c.mod", "demo.order.shipped.v2", [], name="n2")
        await consumer_svc.runtime_for().start()
        producer_svc.bus._AUDIENCE_DELAY_SECONDS = 0.05
        with caplog.at_level(
            logging.WARNING, logger="sandloom.core.eventing.event_bus"
        ):
            await publish_event_versions(
                {"demo.order.shipped.v1": {"n": 1}, "demo.order.shipped.v2": {"n": 1}},
                bus=producer_svc.bus,
            )
            await asyncio.sleep(0.3)
        assert "starve" not in caplog.text
        assert "no live consumer" not in caplog.text
    finally:
        await consumer_svc.close()
        await producer_svc.close()


def test_publish_event_versions_is_reexported_for_modules() -> None:
    """Modules may import only ``sandloom.core.di`` — the helper must be there."""
    from sandloom.core import di
    from sandloom.core.unit_of_work import versioning

    assert di.publish_event_versions is versioning.publish_event_versions
    assert "publish_event_versions" in di.__all__


# --------------------------------------------------------------------------- #
# L1 — key namespace
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_prefix_namespaces_streams_and_groups(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    svc = _Service(redis_url, prefix)
    try:
        await svc.transport.ensure_group("demo.ns.v1", "g")
        await svc.transport.publish("demo.ns.v1", {"id": "1", "payload": {}})  # type: ignore[typeddict-item]
        await svc.transport.claim_group_owner("demo.ns.v1", "g", {"module": "m"})
        assert await raw_redis.exists(f"{prefix}demo.ns.v1") == 1
        assert await raw_redis.exists("demo.ns.v1") == 0
        assert await raw_redis.exists(f"{prefix}sandloom:group-owner:demo.ns.v1:g") == 1
        with pytest.raises(GroupOwnershipError):
            await svc.transport.claim_group_owner(
                "demo.ns.v1", "g", {"module": "other"}
            )
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_streams_cli_operations(
    redis_url: str, prefix: str, raw_redis: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandloom.core.tasks import event_ops as cli

    svc = _Service(redis_url, prefix, max_deliveries=1)
    t = svc.transport
    try:
        await t.ensure_group("demo.cli.v1", "a")
        await t.ensure_group("demo.cli.v1", "b")
        await t.publish("demo.cli.v1", {"id": "e1", "payload": {}})  # type: ignore[typeddict-item]
        await t.consume("demo.cli.v1", group="a", consumer="c")
        await t.consume("demo.cli.v1", group="a", consumer="c")  # -> DLQ (a)
        groups = await cli.streams_groups(svc.core, "demo.cli.v1")
        assert {g["group"] for g in groups} == {"a", "b"}
        assert await cli.streams_dlq_replay(svc.core, "demo.cli.v1", group="b") == 0
        assert await cli.streams_dlq_replay(svc.core, "demo.cli.v1", group="a") == 1
        await t.claim_group_owner("demo.cli.v1", "a", {"module": "m", "database": "d"})
        assert await cli.streams_release_group(svc.core, "demo.cli.v1", "a") is True
        await t.register_consumer("demo.cli.v1", "a", {"module": "m"})
        (entry,) = await cli.streams_consumers(svc.core)
        assert (entry["stream"], entry["group"]) == ("demo.cli.v1", "a")
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_dlq_inspection_continues_without_replaying_or_deleting(
    redis_url: str, prefix: str, raw_redis: Any
) -> None:
    svc = _Service(redis_url, prefix)
    channel = "inspection.paging.v1"
    transport = svc.transport
    try:
        for i in range(203):
            await raw_redis.xadd(
                transport._dlq_key(channel), {"reason": "probe"}, id=f"{i + 1}-0"
            )
        first = await transport.list_dlq(channel, count=200)
        second = await transport.list_dlq(channel, count=200, after=first[-1]["id"])
        assert len(first) == 200 and len(second) == 3
        assert len({r["id"] for r in first + second}) == 203
        assert await raw_redis.xlen(transport._dlq_key(channel)) == 203
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_memory_dlq_continuation_uses_numeric_stream_order() -> None:
    transport = MemoryTransport()
    transport._dlq["paging.v1"] = [(f"{i}-0", {}, 1, "group") for i in range(1, 13)]
    first = await transport.list_dlq("paging.v1", count=9)
    second = await transport.list_dlq("paging.v1", after=first[-1]["id"])
    assert [row["id"] for row in second] == ["10-0", "11-0", "12-0"]
