"""Operational metrics of the review map (B-07, H-09 core subset, C-07, H-14).

Each metric is driven through the code that emits it in production: the relay
age probe against a real outbox table, the Streams heartbeat, the consumer
wrapper, the pool sampler, the Redis cache driver, the rate limiter, the
revocation hook, the RPC client and the client-IP resolver — plus the
Gunicorn multiprocess contract (every gauge is ``live*`` and renders from a
shared directory).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from prometheus_client import REGISTRY, Gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core.testing import reload_test_env


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _value(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# -- B-07: outbox oldest pending age -----------------------------------------


@pytest_asyncio.fixture
async def outbox_engine() -> AsyncGenerator[AsyncEngine, None]:
    from sandloom.core.sql_alchemy.models.outbox import OutboxMessage

    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured for tests")
    eng = create_async_engine(url, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: OutboxMessage.__table__.drop(c, checkfirst=True))
        await conn.run_sync(OutboxMessage.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_messages"))
    await eng.dispose()


def _factory(engine: AsyncEngine) -> Any:
    from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork

    sm = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return lambda: SQLAlchemyUnitOfWork(session_factory=sm)


async def _insert(engine: AsyncEngine, age_s: float, **cols: Any) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO outbox_messages (id, channel, schema_version, payload, "
                "emitted_at, delivered_at, dead_at, delivery_attempts) VALUES "
                "(:id, 'x.y.v1', 'v1', '{}', :emitted, :delivered, :dead, 0)"
            ),
            {
                "id": uuid.uuid4(),
                "emitted": now - timedelta(seconds=age_s),
                "delivered": cols.get("delivered") and now,
                "dead": cols.get("dead") and now,
            },
        )


async def test_outbox_oldest_pending_age_ignores_delivered_and_dead(
    outbox_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
    from sandloom.core.tasks import relay

    store = sql_alchemy_outbox_store()
    factory = _factory(outbox_engine)
    assert await store.oldest_pending_age_seconds(factory) == 0.0

    await _insert(outbox_engine, 5000, dead=True)
    await _insert(outbox_engine, 3000, delivered=True)
    await _insert(outbox_engine, 120)
    await _insert(outbox_engine, 10)
    age = await store.oldest_pending_age_seconds(factory)
    assert age is not None and 119 <= age < 180

    monkeypatch.setattr(
        relay, "_database_targets", lambda _core: [("default", factory, store)]
    )
    ages = await relay.record_oldest_pending_age(SimpleNamespace())
    assert 119 <= ages["default"] < 180
    gauge = _value(
        "sandloom_outbox_oldest_pending_age_seconds", {"database": "default"}
    )
    assert 119 <= gauge < 180


async def test_outbox_age_query_is_answerable_from_the_relay_index(
    outbox_engine: AsyncEngine,
) -> None:
    """No new full scan: the partial relay index answers ``min(emitted_at)``."""
    async with outbox_engine.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in await conn.execute(
                text(
                    "EXPLAIN SELECT min(emitted_at) FROM outbox_messages "
                    "WHERE delivered_at IS NULL AND dead_at IS NULL"
                )
            )
        )
    assert "ix_outbox_messages_relay" in plan, plan


async def test_relay_tick_refreshes_age_while_the_broker_circuit_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.tasks import relay

    calls: list[object] = []

    async def record(core: object) -> dict[str, float]:
        calls.append(core)
        return {}

    monkeypatch.setattr(relay, "record_oldest_pending_age", record)
    monkeypatch.setattr(relay, "_AGE_SCHEDULE", relay._PurgeSchedule(10.0))
    circuit = relay.broker_circuit()
    circuit.reset()
    circuit.trip()
    try:
        core = SimpleNamespace(event_bus=None)
        assert await relay._relay_tick(core) == (0, False)  # type: ignore[arg-type]
        assert await relay._relay_tick(core) == (0, False)  # type: ignore[arg-type]
    finally:
        circuit.reset()
    assert calls == [core]  # ran once (cadence), although the circuit is open


# -- B-07: Streams per-group oldest unacked age ------------------------------


class _FakeStreamsClient:
    def __init__(self, pending_min: str | None, after: list[Any]) -> None:
        self.pending_min = pending_min
        self.after = after
        self.calls: list[tuple[str, Any]] = []

    async def xpending(self, key: str, group: str) -> dict[str, Any]:
        self.calls.append(("xpending", group))
        return {"pending": 1, "min": self.pending_min}

    async def xrange(self, key: str, *, min: str, count: int) -> list[Any]:
        self.calls.append(("xrange", min))
        return self.after


def _redis_transport(client: Any) -> Any:
    from sandloom.core.eventing.streams_transport import RedisStreamsTransport

    transport = RedisStreamsTransport.__new__(RedisStreamsTransport)
    transport._prefix = ""

    async def _client() -> Any:
        return client

    transport._client = _client  # type: ignore[method-assign]
    return transport


async def test_streams_oldest_unacked_age_from_entry_ids() -> None:
    now_ms = int(time.time() * 1000)
    pending = _FakeStreamsClient(f"{now_ms - 42_000}-0", [])
    transport = _redis_transport(pending)
    age = await transport.oldest_unacked_age_seconds(
        "s.v1", "g", last_delivered_id="1-0", pending=3, lag=5
    )
    assert 41 <= age < 60
    assert pending.calls == [("xpending", "g")]  # one command

    lagging = _FakeStreamsClient(None, [(f"{now_ms - 7_000}-1", {})])
    transport = _redis_transport(lagging)
    age = await transport.oldest_unacked_age_seconds(
        "s.v1", "g", last_delivered_id="5-0", pending=0, lag=2
    )
    assert 6 <= age < 30
    assert lagging.calls == [("xrange", "(5-0")]  # exclusive start

    idle = _FakeStreamsClient(None, [])
    transport = _redis_transport(idle)
    assert (
        await transport.oldest_unacked_age_seconds(
            "s.v1", "g", last_delivered_id="5-0", pending=0, lag=0
        )
        == 0.0
    )
    assert idle.calls == []  # nothing pending, nothing lagging: no command


async def test_heartbeat_sets_the_group_oldest_age_gauge() -> None:
    from sandloom.core.eventing.streams_bus import StreamsEventBus

    class _Transport:
        async def consume(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
            return []

        async def ensure_group(self, *a: Any, **k: Any) -> None:  # pragma: no cover
            return None

        async def group_info(self, stream: str) -> list[dict[str, Any]]:
            return [{"group": "g1", "pending": 2, "lag": 0, "last_delivered_id": "9-0"}]

        async def oldest_unacked_age_seconds(self, *a: Any, **k: Any) -> float:
            assert k == {"last_delivered_id": "9-0", "pending": 2, "lag": 0}
            return 12.5

    bus = StreamsEventBus(SimpleNamespace(), _Transport())  # type: ignore[arg-type]
    await bus._heartbeat("orders.paid.v1", "g1")
    assert (
        _value(
            "sandloom_streams_group_oldest_unacked_age_seconds",
            {"stream": "orders.paid.v1", "group": "g1"},
        )
        == 12.5
    )
    stats = bus.group_stats("orders.paid.v1", "g1")
    assert stats is not None and stats["oldest_unacked_age_seconds"] == 12.5


# -- B-07 / H-09: consumer end-to-end latency + handler duration -------------


async def test_consumer_observes_end_to_end_latency_on_success_only() -> None:
    from contextlib import asynccontextmanager

    from sandloom.core.consumers.callbacks import build_consumer_callback

    @asynccontextmanager
    async def opener(_db: str) -> Any:
        yield object()

    failing = {"on": False}

    async def handler(_session: Any, _envelope: Any) -> None:
        if failing["on"]:
            raise RuntimeError("boom")

    name = f"metrics-test-{uuid.uuid4().hex[:6]}"
    callback = build_consumer_callback(
        name=name,
        database="default",
        handler=handler,
        durable=False,
        opener=opener,
        metric_processed="event_consumer_events_processed_total",
        metric_failed="event_consumer_events_failed_total",
        labels={"consumer": name},
    )
    emitted = (datetime.now(UTC) - timedelta(seconds=3)).isoformat()
    await callback({"id": "e1", "emitted_at": emitted})
    labels = {"consumer": name}
    assert _value("sandloom_event_end_to_end_latency_seconds_count", labels) == 1
    total = _value("sandloom_event_end_to_end_latency_seconds_sum", labels)
    assert 3 <= total < 30
    assert _value("sandloom_event_consumer_handler_duration_seconds_count", labels) == 1

    failing["on"] = True
    with pytest.raises(RuntimeError):
        await callback({"id": "e2", "emitted_at": emitted})
    assert _value("sandloom_event_end_to_end_latency_seconds_count", labels) == 1


# -- C-07 / H-09: DB pool gauges ---------------------------------------------


async def test_pool_sampler_reports_checked_out_idle_and_capacity() -> None:
    from sandloom.core.drivers.linger_pool import LingeringQueuePool
    from sandloom.core.drivers.sqlalchemy_database import _pool_snapshot
    from sandloom.core.tasks.pool_metrics import sample_db_pools

    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured for tests")
    engine = create_async_engine(
        url, poolclass=LingeringQueuePool, pool_size=2, max_overflow=3
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            snap = _pool_snapshot(engine.pool)
            assert snap is not None
            assert snap["checked_out"] == 1 and snap["max"] == 5
            assert snap["size"] == 2 and snap["waiters"] == 0
            provider = SimpleNamespace(
                pool_stats=lambda: [{"pool": "main", **_pool_snapshot(engine.pool)}]  # type: ignore[dict-item]
            )
            core = SimpleNamespace(
                infra=SimpleNamespace(instances=lambda _cap: ["pooltest"]),
                database=lambda _name: provider,
            )
            assert sample_db_pools(core) == 1
        labels = {"database": "pooltest", "pool": "main"}
        assert (
            _value("sandloom_db_pool_connections", {**labels, "state": "checked_out"})
            == 1
        )
        assert _value("sandloom_db_pool_max_connections", labels) == 5
        assert _value("sandloom_db_pool_checkout_waiters", labels) == 0
        snap = _pool_snapshot(engine.pool)
        assert snap is not None and snap["checked_out"] == 0 and snap["idle"] == 1
    finally:
        await engine.dispose()


def test_pool_snapshot_skips_pools_without_accounting() -> None:
    from sqlalchemy.pool import NullPool

    from sandloom.core.drivers.sqlalchemy_database import _pool_snapshot

    assert _pool_snapshot(NullPool(lambda: None)) is None


# -- H-09: cache errors, rate limit, revocation, RPC --------------------------


async def test_redis_cache_counts_backend_errors_not_caller_errors() -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    from sandloom.core.drivers.redis_cache import RedisCacheProvider

    class _Client:
        async def get(self, key: str) -> str:
            raise RedisConnectionError("down")

    provider = RedisCacheProvider(
        name="errtest", driver="redis", options={}, shared=False
    )

    async def _client_obj() -> Any:
        return _Client()

    provider._client_obj = _client_obj  # type: ignore[method-assign]
    labels = {"instance": "errtest", "operation": "get"}
    before = _value("sandloom_cache_operation_errors_total", labels)
    with pytest.raises(RedisConnectionError):
        await provider.get("k")
    assert _value("sandloom_cache_operation_errors_total", labels) == before + 1
    with pytest.raises(ValueError):
        await provider.set_max("k", "not-a-number")
    assert (
        _value(
            "sandloom_cache_operation_errors_total",
            {"instance": "errtest", "operation": "set_max"},
        )
        == 0
    )


async def test_rate_limiter_counts_rejections_and_cache_outages() -> None:
    from sandloom.core.ingress.rate_limit import rate_limiter
    from sandloom.core.testing import InMemoryCache

    cfg = SimpleNamespace(is_prod=True, rate_limit_enabled=True)
    request = SimpleNamespace(
        client=SimpleNamespace(host="198.51.100.9"),
        url=SimpleNamespace(path="/x"),
        headers={},
        scope={"route": SimpleNamespace(path="/items/{id}")},
    )
    limiter = rate_limiter(1, 60)
    rejected = {"route": "/items/{id}"}
    before = _value("sandloom_rate_limit_rejected_total", rejected)
    with (
        patch("sandloom.core.ingress.rate_limit.settings", cfg),
        patch("sandloom.core.ingress.rate_limit.cache", return_value=InMemoryCache()),
    ):
        await limiter(request)  # type: ignore[arg-type]
        with pytest.raises(HTTPException):
            await limiter(request)  # type: ignore[arg-type]
    assert _value("sandloom_rate_limit_rejected_total", rejected) == before + 1

    def _down() -> Any:
        raise ConnectionError("cache down")

    opened = _value("sandloom_rate_limit_unavailable_total", {"outcome": "open"})
    with (
        patch("sandloom.core.ingress.rate_limit.settings", cfg),
        patch("sandloom.core.ingress.rate_limit.cache", side_effect=_down),
    ):
        await limiter(request)  # type: ignore[arg-type]
    assert (
        _value("sandloom_rate_limit_unavailable_total", {"outcome": "open"})
        == opened + 1
    )


async def test_revocation_check_latency_by_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.auth import verification

    async def status(_claims: Any) -> str:
        return verification.TOKEN_OK

    monkeypatch.setattr(verification, "token_access_status", status)
    strict = _value(
        "sandloom_auth_revocation_check_duration_seconds_count", {"mode": "strict"}
    )
    request = SimpleNamespace(state=SimpleNamespace())
    claims = {"sub": "u1", "jti": "j", "iat": 1}
    await verification.ensure_token_not_revoked(request, claims)
    await verification.ensure_token_not_revoked(request, claims)  # memo hit: not timed
    assert (
        _value(
            "sandloom_auth_revocation_check_duration_seconds_count", {"mode": "strict"}
        )
        == strict + 1
    )


async def test_rpc_client_latency_per_peer_and_outcome() -> None:
    from sandloom.core.config.settings import settings
    from sandloom.core.contract_testing import rpc_settings
    from sandloom.core.rpc import RemoteServiceProxy, RemoteServiceUnavailable

    peer = f"http://peer-{uuid.uuid4().hex[:6]}:8000"

    class _Client:
        async def post(self, *a: Any, **k: Any) -> Any:
            raise httpx.ConnectTimeout("slow")

    cfg = settings.model_copy(update=rpc_settings("svc"))
    proxy = RemoteServiceProxy(
        service_name="x.y.v1",
        binding={"base_url": peer, "service_id": "svc"},
        default_timeout_seconds=1.0,
        client=_Client(),  # type: ignore[arg-type]
        settings=cfg,
    )
    headers, signer = proxy._post_headers("x", None, b"", None)
    with pytest.raises(RemoteServiceUnavailable):
        await proxy._post(f"{peer}/_sandloom/rpc/x.y.v1/x", b"", headers, signer)
    labels = {"peer": peer, "outcome": "timeout"}
    assert _value("sandloom_rpc_client_request_duration_seconds_count", labels) == 2
    # The retry delay between the attempts is not part of either attempt.
    assert _value("sandloom_rpc_client_request_duration_seconds_sum", labels) < 0.1


# -- H-14 ---------------------------------------------------------------------


def test_forwarded_header_without_trust_is_counted_every_time() -> None:
    from sandloom.core.ingress.client_ip import get_client_ip

    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"x-forwarded-for": "203.0.113.7"},
    )
    off = SimpleNamespace(trust_proxy_headers=False, trusted_proxy_ips=[], is_prod=True)
    on = SimpleNamespace(
        trust_proxy_headers=True, trusted_proxy_ips=["10.0.0.0/8"], is_prod=True
    )
    before = _value("sandloom_http_untrusted_forwarded_headers_total")
    assert get_client_ip(request, config=off) == "10.0.0.5"
    assert get_client_ip(request, config=off) == "10.0.0.5"
    assert get_client_ip(request, config=on) == "203.0.113.7"
    assert _value("sandloom_http_untrusted_forwarded_headers_total") == before + 2


# -- Gunicorn multiprocess ----------------------------------------------------


def test_every_gauge_declares_a_live_multiprocess_mode() -> None:
    """A plain ``all`` gauge keeps a dead worker's value and labels by pid."""
    import sandloom.core.auth.marks
    import sandloom.core.eventing.event_bus  # registers its gauges
    import sandloom.core.tasks.relay  # noqa: F401

    collectors = list(getattr(REGISTRY, "_collector_to_names", {}))
    gauges = [c for c in collectors if isinstance(c, Gauge)]
    assert gauges
    bad = sorted(
        c._name
        for c in gauges
        if not str(getattr(c, "_multiprocess_mode", "")).startswith("live")
    )
    assert not bad, f"gauges without a live* multiprocess_mode: {bad}"


@pytest.mark.subprocess_probe
def test_new_gauges_render_from_the_multiprocess_directory(tmp_path: Path) -> None:
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    code = (
        "from sandloom.core.observability import metrics as m; "
        "m.outbox_oldest_pending_age_seconds.labels('default').set(7); "
        "m.db_pool_connections.labels('default','main','checked_out').set({n}); "
        "m.http_untrusted_forwarded_headers_total.inc()"
    )
    for n in (2, 3):
        subprocess.run(
            [sys.executable, "-c", code.format(n=n)],
            check=True,
            env=env,
            capture_output=True,
        )
    rendered = subprocess.run(
        [
            sys.executable,
            "-c",
            "from sandloom.core.observability.metrics import render_metrics; "
            "print(render_metrics().decode())",
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    ).stdout
    # Writers exited without mark_process_dead: their live values still sit
    # in the directory, which is what the scrape reads.
    assert (
        'sandloom_outbox_oldest_pending_age_seconds{database="default"} 7.0' in rendered
    )
    assert (
        'sandloom_db_pool_connections{database="default",pool="main",state="checked_out"} 5.0'
        in rendered
    )
    assert "sandloom_http_untrusted_forwarded_headers_total 2.0" in rendered
