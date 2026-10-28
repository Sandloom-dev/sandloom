"""Batch publish API: ``MessageTransport.publish_many`` and
``publish_event_batch`` (one broker round trip per batch, same envelopes).

* the default ``publish_many`` is an ordered loop that stops at the first
  failure and reports the confirmed prefix;
* Redis Streams appends a batch in ONE ``MULTI``/``EXEC`` (order kept,
  ``MAXLEN ~`` on every entry, age trim once per stream per batch); an
  ``EXECABORT`` confirms nothing, an EXEC-time ``WRONGTYPE`` only the prefix;
* the Streams bus builds exactly ``publish_event``'s envelopes, keeps the
  preflight / prevalidated checks, RETURNS failures and counts metrics per
  event; buses / transports without the batch API keep working.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandloom.core.eventing.bus_metrics import get_event_bus_metrics
from sandloom.core.eventing.envelope import EventEnvelope, OutboundEvent
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.streams_transport import RedisStreamsTransport
from sandloom.core.eventing.transport_base import TransportBatchError
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tasks import relay
from sandloom.core.testing import reload_test_env
from sandloom.core.tests._pipelined_support import (
    CHANNEL,
    RecordingTransport,
    streams_bus,
)


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


# == transport ==================================================================
@pytest.mark.asyncio
async def test_default_publish_many_is_ordered_and_stops_at_first_failure() -> None:
    class _Flaky(MemoryTransport):
        async def publish(self, stream: str, envelope: EventEnvelope) -> str:
            if envelope["payload"]["n"] == 2:
                raise ConnectionError("down")
            return await super().publish(stream, envelope)

    t = _Flaky()
    items = [(CHANNEL, _env(str(i), i)) for i in range(4)]
    with pytest.raises(TransportBatchError) as info:
        await t.publish_many(items)
    assert len(info.value.published) == 2
    assert isinstance(info.value.__cause__, ConnectionError)
    assert [env["id"] for _m, env in t._log[CHANNEL]] == ["0", "1"]


def _env(event_id: str, n: int = 0, channel: str = CHANNEL) -> EventEnvelope:
    return EventEnvelope(
        id=event_id,
        channel=channel,
        schema_version="v1",
        trace_id=None,
        emitted_at="2026-01-01T00:00:00+00:00",
        payload={"n": n},
    )


class _FakePipe:
    def __init__(self, client: _FakeRedis) -> None:
        self.client = client
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakePipe:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def xadd(self, key: str, fields: dict[str, Any], **kw: Any) -> _FakePipe:
        self.calls.append((key, kw))
        return self

    async def execute(self, raise_on_error: bool = True) -> list[Any]:
        self.client.executes.append((raise_on_error, list(self.calls)))
        if self.client.execute_error is not None:
            raise self.client.execute_error
        return [f"{i + 1}-0" for i in range(len(self.calls))]


class _FakeRedis:
    def __init__(self) -> None:
        self.pipelines: list[bool] = []
        self.executes: list[tuple[bool, list[Any]]] = []
        self.xtrims: list[str] = []
        self.execute_error: BaseException | None = None

    def pipeline(self, transaction: bool = True) -> _FakePipe:
        self.pipelines.append(transaction)
        return _FakePipe(self)

    async def xtrim(self, key: str, **_kw: Any) -> int:
        self.xtrims.append(key)
        return 0


@pytest.mark.asyncio
async def test_redis_publish_many_is_one_multi_exec_with_maxlen_and_one_trim() -> None:
    client = _FakeRedis()

    async def factory() -> Any:
        return client

    t = RedisStreamsTransport(
        client_factory=factory, maxlen=1000, retention_seconds=3600, prefix="p:"
    )
    items = [(CHANNEL, _env(str(i), i)) for i in range(5)] + [
        ("core.other.v1", _env("x", 9, "core.other.v1"))
    ]
    ids = await t.publish_many(items)
    assert ids == [f"{i}-0" for i in range(1, 7)]
    assert client.pipelines == [True]  # ONE transactional pipeline
    assert len(client.executes) == 1
    raise_on_error, calls = client.executes[0]
    assert raise_on_error is False
    assert [k for k, _kw in calls] == [f"p:{CHANNEL}"] * 5 + ["p:core.other.v1"]
    assert all(kw == {"maxlen": 1000, "approximate": True} for _k, kw in calls)
    # the age trim is checked once per stream per batch (and throttled)
    assert sorted(client.xtrims) == sorted([f"p:{CHANNEL}", "p:core.other.v1"])
    await t.publish_many(items)
    assert len(client.xtrims) == 2


@pytest.mark.asyncio
async def test_redis_publish_many_exec_abort_reports_nothing_appended() -> None:
    client = _FakeRedis()
    client.execute_error = RuntimeError("EXECABORT Transaction discarded (OOM)")

    async def factory() -> Any:
        return client

    t = RedisStreamsTransport(client_factory=factory)
    with pytest.raises(TransportBatchError) as info:
        await t.publish_many([(CHANNEL, _env("a")), (CHANNEL, _env("b"))])
    assert info.value.published == []
    assert client.xtrims == []


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[Any, None]:
    redis_asyncio = pytest.importorskip("redis.asyncio")
    url = os.getenv("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not set")
    client = redis_asyncio.from_url(url, encoding="utf-8", decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip("Redis unreachable")
    prefix = f"test:pipelined:{uuid.uuid4().hex[:8]}:"
    yield client, prefix
    keys = [k async for k in client.scan_iter(match=f"{prefix}*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


@pytest.mark.asyncio
async def test_real_redis_publish_many_preserves_order(redis_client: Any) -> None:
    client, prefix = redis_client

    async def factory() -> Any:
        return client

    t = RedisStreamsTransport(client_factory=factory, prefix=prefix, maxlen=10_000)
    items = [(CHANNEL, _env(str(i), i)) for i in range(50)]
    ids = await t.publish_many(items)
    entries = await client.xrange(f"{prefix}{CHANNEL}")
    assert [mid for mid, _f in entries] == ids
    assert [orjson_id(f) for _mid, f in entries] == [str(i) for i in range(50)]


def orjson_id(fields: dict[str, str]) -> str:
    import orjson

    return str(orjson.loads(fields["data"])["id"])


@pytest.mark.asyncio
async def test_real_redis_publish_many_runtime_error_reports_the_prefix(
    redis_client: Any,
) -> None:
    """WRONGTYPE at EXEC time: only the leading successes are confirmed."""
    client, prefix = redis_client
    await client.set(f"{prefix}core.broken.v1", "not a stream")

    async def factory() -> Any:
        return client

    t = RedisStreamsTransport(client_factory=factory, prefix=prefix)
    items = [
        (CHANNEL, _env("a")),
        ("core.broken.v1", _env("b", channel="core.broken.v1")),
        (CHANNEL, _env("c")),
    ]
    with pytest.raises(TransportBatchError) as info:
        await t.publish_many(items)
    assert len(info.value.published) == 1
    # "c" was appended by Redis but is NOT confirmed: a retry duplicates it
    entries = await client.xrange(f"{prefix}{CHANNEL}")
    assert [orjson_id(f) for _m, f in entries] == ["a", "c"]


# == bus ========================================================================
@pytest.mark.asyncio
async def test_streams_batch_envelopes_match_publish_event() -> None:
    single, batched = RecordingTransport(), RecordingTransport()
    await streams_bus(single).publish_event(
        CHANNEL, {"n": 1}, "v1", event_id="e1", prevalidated=True
    )
    before = get_event_bus_metrics()["events_published"]
    res = await streams_bus(batched).publish_event_batch(
        [OutboundEvent(CHANNEL, {"n": 1}, "v1", "e1")], prevalidated=True
    )
    assert res.complete and res.published == 1
    assert get_event_bus_metrics()["events_published"] == before + 1
    a = single._log[CHANNEL][0][1]
    b = batched._log[CHANNEL][0][1]
    for key in ("id", "channel", "schema_version", "payload", "trace_id"):
        assert a[key] == b[key]
    assert batched.batches == [["e1"]]


@pytest.mark.asyncio
async def test_streams_batch_validation_failure_publishes_only_the_prefix() -> None:
    t = RecordingTransport()
    bus = streams_bus(t)
    failures = get_event_bus_metrics()["events_publish_failures"]
    res = await bus.publish_event_batch(
        [
            OutboundEvent(CHANNEL, {"n": 1}, "v1", "a"),
            OutboundEvent("Not A Channel!", {"n": 2}, "v1", "b"),  # rejected
            OutboundEvent(CHANNEL, {"n": 3}, "v1", "c"),
        ]
    )
    assert res.published == 1 and res.error is not None
    assert t.stream_ids() == ["a"]
    assert get_event_bus_metrics()["events_publish_failures"] == failures + 1


@pytest.mark.asyncio
async def test_streams_batch_transport_failure_is_returned_not_raised() -> None:
    t = RecordingTransport()
    t.fail_next = ConnectionError("reset")
    res = await streams_bus(t).publish_event_batch(
        [OutboundEvent(CHANNEL, {"n": i}, "v1", str(i)) for i in range(3)],
        prevalidated=True,
    )
    assert res.published == 0
    assert isinstance(res.error, TransportBatchError)
    assert relay.is_broker_unavailable(res.error)  # cause chain is kept


@pytest.mark.asyncio
async def test_streams_bus_without_publish_many_falls_back_per_event() -> None:
    class _Old:
        def __init__(self) -> None:
            self.published: list[str] = []

        async def publish(self, stream: str, envelope: EventEnvelope) -> str:
            self.published.append(str(envelope["id"]))
            return "1-0"

        async def consume(self, *a: Any, **k: Any) -> list[Any]:  # pragma: no cover
            return []

        async def ensure_group(self, *a: Any, **k: Any) -> None:  # pragma: no cover
            return None

    old = _Old()
    bus = StreamsEventBus(InMemorySchemaRegistry(), old)
    assert bus.native_batch_publish is False
    res = await bus.publish_event_batch(
        [OutboundEvent(CHANNEL, {"n": i}, "v1", str(i)) for i in range(3)],
        prevalidated=True,
    )
    assert res.complete and old.published == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_memory_bus_default_batch_is_sequential_and_delivers() -> None:
    bus = InMemoryEventBus(InMemorySchemaRegistry())
    got: list[str] = []

    async def handler(env: EventEnvelope) -> None:
        got.append(env["id"])

    await bus.subscribe(CHANNEL, handler)
    assert bus.native_batch_publish is False
    res = await bus.publish_event_batch(
        [OutboundEvent(CHANNEL, {"n": i}, "v1", str(i)) for i in range(3)],
        prevalidated=True,
    )
    await bus.drain()
    assert res.complete and sorted(got) == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_streams_prometheus_publish_metrics_cover_single_and_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.eventing import bus_base as bus_base_mod
    from sandloom.core.eventing import streams_bus as streams_bus_mod

    increments = []
    for module in (bus_base_mod, streams_bus_mod):
        monkeypatch.setattr(
            module,
            "safe_inc",
            lambda name, labels=None: increments.append((name, labels)),
        )
    transport = RecordingTransport()
    bus = streams_bus(transport)
    await bus.publish_event(CHANNEL, {"n": 1}, event_id="one")
    result = await bus.publish_event_batch(
        [OutboundEvent(CHANNEL, {"n": n}, event_id=str(n)) for n in (2, 3)]
    )
    assert result.published == 2
    transport.fail_next = ConnectionError("broker unavailable")
    result = await bus.publish_event_batch(
        [OutboundEvent(CHANNEL, {"n": 4}, event_id="four")]
    )
    assert result.published == 0 and result.error is not None
    assert sum(name == "events_published_total" for name, _labels in increments) == 3
    assert (
        sum(name == "events_publish_failures_total" for name, _labels in increments)
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prevalidated", "size_checks"),
    [(False, 2), (True, 2), ("preflighted", 0)],
)
async def test_preflighted_events_skip_the_repeated_size_check(
    monkeypatch: pytest.MonkeyPatch, prevalidated: Any, size_checks: int
) -> None:
    """A payload THIS bus preflighted in its transaction (the UoW's own
    pending events) is not size-encoded again at dispatch; the relay's
    ``prevalidated=True`` keeps its transport-independent checks."""
    from sandloom.core.eventing.envelope import PREFLIGHTED

    assert PREFLIGHTED == "preflighted"
    seen: list[str] = []
    original = StreamsEventBus._enforce_payload_size

    def spy(channel: str, payload: dict[str, Any]) -> None:
        seen.append(channel)
        original(channel, payload)

    monkeypatch.setattr(StreamsEventBus, "_enforce_payload_size", staticmethod(spy))
    t = RecordingTransport()
    bus = streams_bus(t)
    assert bus.supports_preflighted is True
    await bus.publish_event(
        CHANNEL, {"n": 1}, "v1", event_id="one", prevalidated=prevalidated
    )
    res = await bus.publish_event_batch(
        [OutboundEvent(CHANNEL, {"n": 2}, "v1", "two")], prevalidated=prevalidated
    )
    assert res.complete
    assert t.stream_ids() == ["one", "two"]
    assert len(seen) == size_checks


# == broker availability classification =========================================
@pytest.mark.parametrize(
    "reply",
    [
        "LOADING Redis is loading the dataset in memory",
        "READONLY You can't write against a read only replica.",
        "MASTERDOWN Link with MASTER is down and replica-serve-stale-data is no",
        "TRYAGAIN Multiple keys request during rehashing of slot",
        "CLUSTERDOWN The cluster is down",
    ],
)
def test_redis_availability_replies_do_not_burn_relay_attempts(reply: str) -> None:
    # GLM review: "Redis LOADING is not treated as unavailable". LOADING is
    # parsed as BusyLoadingError (a ConnectionError) and was already covered;
    # a failover's READONLY / MASTERDOWN and cluster TRYAGAIN / CLUSTERDOWN
    # replies (plain ResponseErrors) were not, so a failover burned every
    # event's OUTBOX_RELAY_MAX_ATTEMPTS and could dead-letter valid events.
    from redis._parsers.base import BaseParser

    err = BaseParser().parse_error(reply)
    assert relay.is_broker_unavailable(err)
    wrapped = RuntimeError("publish failed")
    wrapped.__cause__ = err
    assert relay.is_broker_unavailable(wrapped)


def test_a_poison_reply_still_burns_attempts() -> None:
    from redis._parsers.base import BaseParser

    assert not relay.is_broker_unavailable(
        BaseParser().parse_error("ERR value is not an integer or out of range")
    )
    assert not relay.is_broker_unavailable(ValueError("LOADING-looking payload"))
