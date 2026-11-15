"""Redis Streams ``publish_many``: ONE ``MULTI``/``EXEC`` per batch.

* the batch is appended in one transactional pipeline (order kept, ``MAXLEN ~``
  on every entry, the age trim once per stream per batch);
* an ``EXECABORT`` confirms nothing, an EXEC-time ``WRONGTYPE`` only the prefix
  (against a real Redis).

The kernel's side of the batch API (the default ordered ``publish_many``, the
bus building the same envelopes as ``publish_event``) stays in
``src/sandloom/core/tests/test_batch_publish.py``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import orjson
import pytest
import pytest_asyncio
from _redis_required import redis_unavailable
from sandloom_redis.eventbus.streams_transport import RedisStreamsTransport

from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.infra.ports.eventbus import PartialPublish
from sandloom.core.testing import reload_test_env

CHANNEL = "core.test_event.v1"


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[3] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


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
    with pytest.raises(PartialPublish) as info:
        await t.publish_many([(CHANNEL, _env("a")), (CHANNEL, _env("b"))])
    assert info.value.published == []
    assert client.xtrims == []


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[Any, None]:
    import redis.asyncio as redis_asyncio

    url = os.getenv("CACHE_URL")
    if not url:
        redis_unavailable("CACHE_URL not set")
    client = redis_asyncio.from_url(url, encoding="utf-8", decode_responses=True)
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip, or fail when required
        await client.aclose()
        redis_unavailable("Redis unreachable")
    prefix = f"test:pipelined:{uuid.uuid4().hex[:8]}:"
    yield client, prefix
    keys = [k async for k in client.scan_iter(match=f"{prefix}*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


def _logged_id(fields: dict[str, str]) -> str:
    return str(orjson.loads(fields["data"])["id"])


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
    assert [_logged_id(f) for _mid, f in entries] == [str(i) for i in range(50)]


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
    with pytest.raises(PartialPublish) as info:
        await t.publish_many(items)
    assert len(info.value.published) == 1
    # "c" was appended by Redis but is NOT confirmed: a retry duplicates it
    entries = await client.xrange(f"{prefix}{CHANNEL}")
    assert [_logged_id(f) for _m, f in entries] == ["a", "c"]
