"""NOGROUP recovery recreates the group where it stopped — never silently at 0.

Regression (code review 2026-10-21, Eventing #8): after a NOGROUP (group
destroyed, stream key deleted) the transport recreated the group from id
``"0"``, replaying the whole stream retention (7 days) while the cache inbox
that dedupes redeliveries only remembers 24 h — up to a week of events re-run.
Now the group is recreated at the last delivered id this process knows (or
the one heartbeated into the subscription inventory); a stream that was reset
under it (ids behind that position, or another entry at it) is read from its
start, and with no known position the group starts at ``"$"`` with an error
log and ``streams_group_recreated_at_tail_total``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import orjson
import pytest

from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.streams_transport import RedisStreamsTransport
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing import reload_test_env

redis_asyncio = pytest.importorskip("redis.asyncio")
prometheus_client = pytest.importorskip("prometheus_client")


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _env(event_id: str) -> EventEnvelope:
    return EventEnvelope(
        id=event_id,
        channel="core.recreate.v1",
        schema_version="v1",
        trace_id=None,
        emitted_at="2026-01-01T00:00:00+00:00",
        payload={"n": 1},
    )


STREAM = "test.recreate.stream.v1"
INVENTORY = "sandloom:consumers"


@pytest.fixture
async def transport():
    cache_url = os.getenv("CACHE_URL")
    if not cache_url:
        pytest.skip("CACHE_URL not set; Redis Streams integration skipped")
    client = redis_asyncio.from_url(cache_url, encoding="utf-8", decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip("Redis unreachable")
    await client.delete(STREAM, f"{STREAM}.dlq", INVENTORY)

    async def _factory():
        return client

    yield RedisStreamsTransport(client_factory=_factory, retry_base_ms=0), client
    await client.delete(STREAM, f"{STREAM}.dlq", INVENTORY)
    await client.aclose()


async def _drain(t: RedisStreamsTransport) -> list[str]:
    batch = await t.consume(STREAM, group="g", consumer="c1", count=100)
    for delivery in batch:
        await t.ack(delivery)
    return [str(d.envelope["id"]) for d in batch]


def _tail_metric() -> float:
    value = prometheus_client.REGISTRY.get_sample_value(
        "streams_group_recreated_at_tail_total", {"stream": STREAM}
    )
    return float(value or 0.0)


@pytest.mark.asyncio
async def test_destroyed_group_resumes_after_last_delivered_not_from_zero(
    transport,
) -> None:
    t, client = transport
    await t.ensure_group(STREAM, "g")
    for i in range(3):
        await t.publish(STREAM, _env(f"old{i}"))
    assert await _drain(t) == ["old0", "old1", "old2"]

    await client.xgroup_destroy(STREAM, "g")
    await t.publish(STREAM, _env("new"))
    # Before: recreated at "0" -> old0..old2 replayed (up to 7 days of them).
    assert await _drain(t) == ["new"]


@pytest.mark.asyncio
async def test_position_falls_back_to_the_inventory_heartbeat(
    transport,
) -> None:
    t, client = transport
    await t.ensure_group(STREAM, "g")
    for i in range(3):
        await t.publish(STREAM, _env(f"old{i}"))
    assert await _drain(t) == ["old0", "old1", "old2"]
    groups = await t.group_info(STREAM)
    position = next(g for g in groups if g["group"] == "g")["last_delivered_id"]
    await t.register_consumer(STREAM, "g", {"last_delivered_id": position})

    t._last_delivered.clear()  # this process lost its in-memory position
    await client.xgroup_destroy(STREAM, "g")
    await t.publish(STREAM, _env("new"))
    assert await _drain(t) == ["new"]


@pytest.mark.asyncio
async def test_unknown_position_recreates_at_tail_loudly(
    transport,
) -> None:
    t, client = transport
    await t.ensure_group(STREAM, "g")
    for i in range(3):
        await t.publish(STREAM, _env(f"old{i}"))
    assert await _drain(t) == ["old0", "old1", "old2"]
    t._last_delivered.clear()
    await client.hdel(INVENTORY, f"{STREAM}|g")
    await client.xgroup_destroy(STREAM, "g")

    before = _tail_metric()
    assert await _drain(t) == []  # recreated at "$": nothing replayed
    assert _tail_metric() == before + 1
    await t.publish(STREAM, _env("new"))
    assert await _drain(t) == ["new"]


@pytest.mark.asyncio
async def test_stream_reset_behind_the_position_is_read_from_start(
    transport,
) -> None:
    """The key was deleted and the new stream's ids are BELOW our position
    (e.g. a Redis restored on a host with an earlier clock): read it all."""
    t, client = transport
    await t.ensure_group(STREAM, "g")
    await t.publish(STREAM, _env("old"))
    assert await _drain(t) == ["old"]

    await client.delete(STREAM)
    data = orjson.dumps(_env("reset")).decode()
    await client.xadd(STREAM, {"data": data}, id="1-1")
    assert await _drain(t) == ["reset"]


@pytest.mark.asyncio
async def test_stream_reset_colliding_with_the_position_is_read_from_start(
    transport,
) -> None:
    """A rebuilt stream holding ANOTHER entry at our exact position (same
    millisecond) is detected by the envelope id and read from its start."""
    t, client = transport
    await t.ensure_group(STREAM, "g")
    await client.xadd(STREAM, {"data": orjson.dumps(_env("old")).decode()}, id="5-1")
    assert await _drain(t) == ["old"]

    await client.delete(STREAM)
    await client.xadd(STREAM, {"data": orjson.dumps(_env("a")).decode()}, id="5-0")
    await client.xadd(STREAM, {"data": orjson.dumps(_env("b")).decode()}, id="5-1")
    assert await _drain(t) == ["a", "b"]


@pytest.mark.asyncio
async def test_bus_heartbeat_records_the_position_in_the_inventory(
    transport,
) -> None:
    t, client = transport
    bus = StreamsEventBus(InMemorySchemaRegistry(), t, block_ms=20)
    seen: list[str] = []

    async def handler(env: EventEnvelope) -> None:
        seen.append(str(env["id"]))

    await bus.subscribe(STREAM, handler, group="g")
    msg_id = await t.publish(STREAM, _env("e1"))
    for _ in range(300):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen == ["e1"]
    await bus._heartbeat(STREAM, "g")
    entry = orjson.loads(await client.hget(INVENTORY, f"{STREAM}|g"))
    assert entry["last_delivered_id"] == msg_id
    await bus.close()
