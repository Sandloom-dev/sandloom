"""Outbox relay failure policy (real PostgreSQL).

H1: a broker outage must not burn delivery attempts and dead-letter committed
events en masse; real failures back off exponentially (``next_attempt_at``);
rows over a lowered budget are surfaced as dead; dead rows can be requeued.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core.config.settings import settings as _settings
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.tasks import relay
from sandloom.core.testing import reload_test_env


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.fixture(autouse=True)
def _reset_circuit() -> Any:
    relay.broker_circuit().reset()
    yield
    relay.broker_circuit().reset()


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured for tests")
    eng = create_async_engine(database_url, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: OutboxMessage.__table__.drop(c, checkfirst=True))
        await conn.run_sync(OutboxMessage.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_messages"))
    await eng.dispose()


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> Any:
    sm = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    def _factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(session_factory=sm)

    return _factory


async def _seed(factory: Any, n: int = 1, **fields: Any) -> list[uuid.UUID]:
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    ids = [uuid.uuid4() for _ in range(n)]
    async with factory() as uow:
        for i, msg_id in enumerate(ids):
            uow.session.add(
                OutboxMessage(
                    id=msg_id,
                    channel="core.test_event.v1",
                    schema_version="v1",
                    payload={"n": i},
                    emitted_at=old,
                    **fields,
                )
            )
    return ids


async def _rows(factory: Any) -> list[OutboxMessage]:
    from sqlalchemy import select

    async with factory() as uow:
        return list((await uow.session.execute(select(OutboxMessage))).scalars())


class _RedisLikeConnectionError(Exception):
    """Mimics redis.exceptions.ConnectionError (does NOT subclass builtins)."""


_RedisLikeConnectionError.__name__ = "ConnectionError"


class _DownBus:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def publish_event(self, *_a: Any, **_k: Any) -> None:
        self.calls += 1
        raise self.exc


class _FailBus:
    async def publish_event(self, *_a: Any, **_k: Any) -> None:
        raise RuntimeError("poison: consumer contract rejected it")


class _OkBus:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish_event(self, channel: str, *_a: Any, **_k: Any) -> None:
        self.published.append(channel)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [ConnectionRefusedError("refused"), _RedisLikeConnectionError("redis down")],
)
async def test_broker_outage_burns_no_attempts_and_stops_batch(
    factory: Any, monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    monkeypatch.setattr(_settings, "outbox_relay_max_attempts", 1)
    await _seed(factory, n=3)
    bus = _DownBus(exc)

    attempted = await relay._relay_once_for_store(
        factory, sql_alchemy_outbox_store(), bus
    )

    assert attempted == 0
    assert bus.calls == 1  # circuit breaker: the batch stopped at the first outage
    rows = await _rows(factory)
    assert all(r.dead_at is None for r in rows)  # nothing dead-lettered
    assert all(r.delivery_attempts == 0 for r in rows)  # no budget burnt
    assert relay.broker_circuit().is_open()


@pytest.mark.asyncio
async def test_open_circuit_skips_relay_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Batches are skipped; only the (cadenced, index-only) outbox-age probe may
    # still run while the circuit is open — covered in test_operational_metrics.
    age_schedule = relay._PurgeSchedule(3600.0)
    age_schedule.due()  # consumed: not due again during this test
    monkeypatch.setattr(relay, "_AGE_SCHEDULE", age_schedule)
    relay.broker_circuit().trip()
    core = SimpleNamespace(
        event_bus=None,
        infra=SimpleNamespace(instances=lambda _cap: pytest.fail("must not scan DBs")),
    )
    assert await relay.relay_once(core) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_failed_publish_defers_retry_with_backoff(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_settings, "outbox_relay_max_attempts", 10)
    monkeypatch.setattr(_settings, "outbox_relay_backoff_base_seconds", 60)
    [msg_id] = await _seed(factory)
    store = sql_alchemy_outbox_store()

    await relay._relay_once_for_store(factory, store, _FailBus())
    [row] = await _rows(factory)
    assert row.delivery_attempts == 1
    assert row.dead_at is None
    assert row.next_attempt_at is not None
    assert row.next_attempt_at > datetime.now(UTC).replace(tzinfo=None)

    # Not due: the next tick does not retry it (no tight retry loop).
    assert (
        await store.fetch_pending_ids(
            factory, max_attempts=10, grace_seconds=0, limit=10
        )
        == []
    )
    await relay._relay_once_for_store(factory, store, _FailBus())
    [row] = await _rows(factory)
    assert row.delivery_attempts == 1

    # Once due it is retried and delivered.
    async with factory() as uow:
        r = await uow.session.get(OutboxMessage, msg_id)
        r.next_attempt_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=1
        )
    bus = _OkBus()
    assert await relay._relay_once_for_store(factory, store, bus) == 1
    [row] = await _rows(factory)
    assert row.delivered_at is not None and row.next_attempt_at is None


def test_relay_backoff_grows_and_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings, "outbox_relay_backoff_base_seconds", 2)
    monkeypatch.setattr(_settings, "outbox_relay_backoff_max_seconds", 100)
    assert 1.0 <= relay.relay_backoff(1) <= 2.0
    assert 4.0 <= relay.relay_backoff(3) <= 8.0
    assert 50.0 <= relay.relay_backoff(30) <= 100.0


@pytest.mark.asyncio
async def test_rows_over_lowered_budget_are_dead_lettered(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Low: lowering OUTBOX_RELAY_MAX_ATTEMPTS must not strand rows forever."""
    monkeypatch.setattr(_settings, "outbox_relay_max_attempts", 3)
    await _seed(factory, n=2, delivery_attempts=5)
    await relay._relay_once_for_store(factory, sql_alchemy_outbox_store(), _OkBus())
    rows = await _rows(factory)
    assert all(r.dead_at is not None for r in rows)
    assert all(r.delivered_at is None for r in rows)


@pytest.mark.asyncio
async def test_requeue_dead_returns_rows_to_pending(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.tasks.event_ops import outbox_list_dead, outbox_requeue

    monkeypatch.setattr(_settings, "outbox_relay_max_attempts", 1)
    ids = await _seed(factory, n=2)
    store = sql_alchemy_outbox_store()
    await relay._relay_once_for_store(factory, store, _FailBus())
    assert all(r.dead_at is not None for r in await _rows(factory))

    core = SimpleNamespace(
        database=lambda _name: SimpleNamespace(outbox=lambda: store),
        get_uow_factory=lambda _name: factory,
    )
    dead = await outbox_list_dead(core)
    assert {row["id"] for row in dead} == set(ids)

    # Requeue only one by id, via the CLI helper.
    assert await outbox_requeue(core, ids=[ids[0]]) == 1
    by_id = {r.id: r for r in await _rows(factory)}
    assert by_id[ids[0]].dead_at is None and by_id[ids[0]].delivery_attempts == 0
    assert by_id[ids[1]].dead_at is not None

    bus = _OkBus()
    assert await relay._relay_once_for_store(factory, store, bus) == 1
    assert (await _rows_by_id(factory))[ids[0]].delivered_at is not None


async def _rows_by_id(factory: Any) -> dict[uuid.UUID, OutboxMessage]:
    return {r.id: r for r in await _rows(factory)}


@pytest.mark.asyncio
async def test_fetch_pending_takes_no_row_locks(factory: Any) -> None:
    """Low: the pending read must not pretend to partition via row locks."""
    await _seed(factory)
    store = sql_alchemy_outbox_store()
    async with factory() as holder:
        # Another worker holds a row lock (e.g. mid-claim): the fetch still sees
        # the row (a plain read), the claim then skips it cheaply.
        await holder.session.execute(text("SELECT id FROM outbox_messages FOR UPDATE"))
        ids = await store.fetch_pending_ids(
            factory, max_attempts=10, grace_seconds=0, limit=10
        )
        assert len(ids) == 1
        assert (
            await store.deliver_pending(
                factory, ids[0], _OkBus().publish_event, max_attempts=10
            )
            == "skipped"
        )


def test_start_relay_background_never_uses_deprecated_get_event_loop() -> None:
    """GLM review #12: asyncio.get_event_loop() outside a running loop is
    deprecated (a DeprecationWarning — an error in apps' test suites — and a
    RuntimeError from Python 3.14). The relay must bind to the RUNNING loop."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "from sandloom.core.tasks.relay import start_relay_background\n"
        "start_relay_background(object())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[4],  # backend/ (its .env)
        timeout=60,
        check=False,
    )
    assert proc.returncode != 0
    assert "DeprecationWarning" not in proc.stderr, proc.stderr
    assert "no running event loop" in proc.stderr, proc.stderr
