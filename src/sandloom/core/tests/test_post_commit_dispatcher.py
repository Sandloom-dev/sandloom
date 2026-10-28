"""Post-commit dispatch runs OFF the request path (background coalescer).

The request path (``DbSession`` / ``Database(name)`` / contract calls) only
enqueues its committed events; one per-loop task publishes them in commit
order and marks the delivered rows in ONE batched UPDATE per database.
``db_session()`` (workers/scripts) keeps publishing and marking in line.
At-least-once is preserved: whatever the coalescer does not finish stays
undelivered for the relay.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core import di
from sandloom.core import unit_of_work as uow_mod
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import SQLAlchemyOutboxStore
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.tenant_context import bind_tenant, current_tenant_id_optional
from sandloom.core.testing import reload_test_env
from sandloom.core.tests._pipelined_support import RecordingTransport, streams_bus
from sandloom.core.unit_of_work import _dispatch as uow_dispatch
from sandloom.core.unit_of_work import dispatch as uow_mod_dispatch
from sandloom.core.unit_of_work import scope as uow_mod_scope


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured for tests")
    eng = create_async_engine(url, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: OutboxMessage.__table__.drop(c, checkfirst=True))
        await conn.run_sync(OutboxMessage.metadata.create_all)
    yield eng
    await uow_mod_dispatch.drain_post_commit()
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_messages"))
    await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


class _CountingStore(SQLAlchemyOutboxStore):
    def __init__(self) -> None:
        self.mark_calls: list[list[Any]] = []

    async def mark_delivered(self, uow_factory: Any, ids: Sequence[Any]) -> None:
        self.mark_calls.append(list(ids))
        await super().mark_delivered(uow_factory, ids)


class _Bus:
    in_process = False

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.tenants: list[Any] = []
        self.gate: asyncio.Event | None = None

    def preflight(self, channel: str, payload: dict[str, Any]) -> None:
        pass

    async def publish_event(
        self, channel: str, payload: dict[str, Any], *_a: Any, **_k: Any
    ) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.tenants.append(current_tenant_id_optional())
        self.published.append(payload)


async def _commit_events(
    sessions: async_sessionmaker[AsyncSession],
    store: SQLAlchemyOutboxStore,
    bus: Any,
    factory: Any,
    payloads: list[dict[str, Any]],
    *,
    wait: bool = False,
) -> None:
    async with SQLAlchemyUnitOfWork(session_factory=sessions) as uow:
        session = uow.session
        assert session is not None
        session.info[uow_mod_scope._OUTBOX_STORE_KEY] = store
        session.info[uow_mod_scope._UOW_FACTORY_KEY] = factory
        session.info[uow_mod_scope._EVENT_BUS_KEY] = bus
        with uow_mod_scope._bind_uow_scope(session, "default"):
            for payload in payloads:
                await di.publish_event("core.test_event.v1", payload)
    await uow_mod._dispatch_outbox_after_commit(session, wait=wait)


async def _delivered(sessions: async_sessionmaker[AsyncSession]) -> list[int]:
    async with sessions() as s:
        rows = (await s.execute(select(OutboxMessage))).scalars().all()
        return sorted(r.payload["n"] for r in rows if r.delivered_at is not None)


@pytest.mark.asyncio
async def test_request_path_does_not_wait_for_the_broker(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, bus = _CountingStore(), _Bus()
    bus.gate = asyncio.Event()  # the "broker" hangs until released
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731

    await asyncio.wait_for(
        _commit_events(sessions, store, bus, factory, [{"n": 0}]), timeout=1.0
    )
    assert bus.published == []  # returned before the publish happened
    assert await _delivered(sessions) == []
    bus.gate.set()
    await uow_mod_dispatch.drain_post_commit()
    assert bus.published == [{"n": 0}]
    assert await _delivered(sessions) == [0]


@pytest.mark.asyncio
async def test_background_dispatch_preserves_origin_from_request_transaction(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.sql_alchemy import outbox_store

    monkeypatch.setattr(outbox_store, "get_current_trace_id", lambda: "request-origin")
    transport = RecordingTransport()
    bus = streams_bus(transport)
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    await _commit_events(sessions, SQLAlchemyOutboxStore(), bus, factory, [{"n": 1}])
    await uow_mod_dispatch.drain_post_commit()
    async with sessions() as session:
        row = (await session.execute(select(OutboxMessage))).scalar_one()
    envelope = transport._log["core.test_event.v1"][0][1]
    assert envelope["trace_id"] == row.trace_id == "request-origin"
    assert envelope["emitted_at"] == row.emitted_at.replace(tzinfo=UTC).isoformat()


@pytest.mark.asyncio
async def test_marks_are_coalesced_into_one_update_per_database(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deterministic on a loaded machine: no time-based flush, only the drain.
    monkeypatch.setattr(uow_dispatch, "_MARK_FLUSH_INTERVAL_SECONDS", 3600.0)
    store, bus = _CountingStore(), _Bus()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    for n in range(5):
        await _commit_events(
            sessions, store, bus, factory, [{"n": 2 * n}, {"n": 2 * n + 1}]
        )
    await uow_mod_dispatch.drain_post_commit()
    # Commit order kept across UoWs, emission order within each.
    assert [p["n"] for p in bus.published] == list(range(10))
    assert len(store.mark_calls) == 1 and len(store.mark_calls[0]) == 10
    assert await _delivered(sessions) == list(range(10))


@pytest.mark.asyncio
async def test_coalescer_runs_in_a_clean_context(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    import uuid

    store, bus = _CountingStore(), _Bus()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    with bind_tenant(uuid.uuid4()):
        await _commit_events(sessions, store, bus, factory, [{"n": 1}])
    await uow_mod_dispatch.drain_post_commit()
    assert bus.tenants == [None]  # no request state leaked into the dispatch


@pytest.mark.asyncio
async def test_db_session_path_publishes_and_marks_in_line(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, bus = _CountingStore(), _Bus()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    await _commit_events(sessions, store, bus, factory, [{"n": 7}], wait=True)
    assert bus.published == [{"n": 7}]
    assert await _delivered(sessions) == [7]


@pytest.mark.asyncio
async def test_in_process_bus_gets_events_in_line_marks_deferred(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, bus = _CountingStore(), _Bus()
    bus.in_process = True
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    await _commit_events(sessions, store, bus, factory, [{"n": 3}])
    assert bus.published == [{"n": 3}]  # deterministic: already handed over
    assert store.mark_calls == []  # the second checkout is off the path
    await uow_mod_dispatch.drain_post_commit()
    assert await _delivered(sessions) == [3]


@pytest.mark.asyncio
async def test_failed_publish_leaves_the_row_to_the_relay(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    class _Down(_Bus):
        async def publish_event(self, *_a: Any, **_k: Any) -> None:
            raise ConnectionError("broker down")

    store = _CountingStore()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    await _commit_events(sessions, store, _Down(), factory, [{"n": 1}])
    await uow_mod_dispatch.drain_post_commit()
    assert store.mark_calls == []
    async with sessions() as s:
        row = (await s.execute(select(OutboxMessage))).scalars().one()
    assert row.delivered_at is None  # the relay delivers it


@pytest.mark.asyncio
async def test_queue_overflow_drops_to_the_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uow_dispatch, "_DISPATCH_QUEUE_MAX", 1)
    dispatcher = uow_mod_dispatch.PostCommitDispatcher(asyncio.get_running_loop())
    import uuid

    from sandloom.core.infra.contracts import OutboxHandle

    class _NoDbStore(_CountingStore):
        async def mark_delivered(self, uow_factory: Any, ids: Sequence[Any]) -> None:
            self.mark_calls.append(list(ids))

    event = uow_mod_scope._PendingEvent(
        OutboxHandle(id=uuid.uuid4()), "c.e.v1", "v1", {}
    )
    store = _NoDbStore()
    job = uow_mod_dispatch._DispatchJob(_Bus(), store, lambda: None, (event,), ())
    before = uow_mod_dispatch._METRIC_POST_COMMIT_DROPPED._value.get()
    dispatcher.submit(job)
    dispatcher.submit(job)  # the queue is full: left to the relay
    after = uow_mod_dispatch._METRIC_POST_COMMIT_DROPPED._value.get()
    assert dispatcher.pending() == 1
    assert after == before + 1
    await dispatcher.drain(timeout=5)
    await dispatcher.aclose()
    assert store.mark_calls == [[event.handle.id]]  # only the accepted job


@pytest.mark.asyncio
async def test_large_payloads_hit_byte_budget_before_job_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    from sandloom.core.infra.contracts import OutboxHandle

    monkeypatch.setattr(uow_dispatch, "_DISPATCH_QUEUE_BYTES_MAX", 180_000)
    dispatcher = uow_mod_dispatch.PostCommitDispatcher(asyncio.get_running_loop())

    class Store(_CountingStore):
        async def mark_delivered(self, factory: Any, ids: Sequence[Any]) -> None:
            self.mark_calls.append(list(ids))

    store = Store()
    event = uow_mod_scope._PendingEvent(
        OutboxHandle(id=uuid.uuid4()), "c.e.v1", "v1", {"blob": "x" * 100_000}
    )
    job = uow_mod_dispatch._DispatchJob(_Bus(), store, lambda: None, (event,), ())
    before = uow_mod_dispatch._METRIC_POST_COMMIT_DROPPED._value.get()
    dispatcher.submit(job)
    dispatcher.submit(job)
    assert dispatcher.pending() == 1
    assert uow_mod_dispatch._METRIC_POST_COMMIT_DROPPED._value.get() == before + 1
    await dispatcher.drain(timeout=5)
    await dispatcher.aclose()
    assert dispatcher._retained_bytes == 0
    assert store.mark_calls == [[event.handle.id]]


@pytest.mark.asyncio
async def test_graceful_shutdown_drains_the_queue(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom.core.app_factory import _drain_post_commit_on_shutdown

    store, bus = _CountingStore(), _Bus()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    await _commit_events(sessions, store, bus, factory, [{"n": 4}])
    await _drain_post_commit_on_shutdown()
    assert bus.published == [{"n": 4}]
    assert await _delivered(sessions) == [4]


@pytest.mark.asyncio
async def test_failing_broker_does_not_block_the_queue_head_of_line(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uow_dispatch, "_PUBLISH_COOLDOWN_SECONDS", 3600.0)
    calls: list[Any] = []

    class _Down(_Bus):
        async def publish_event(self, *_a: Any, **_k: Any) -> None:
            calls.append(1)
            raise ConnectionError("broker down")

    store, bus = _CountingStore(), _Down()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    for n in range(3):
        await _commit_events(sessions, store, bus, factory, [{"n": n}])
    await uow_mod_dispatch.drain_post_commit()
    assert len(calls) == 1  # later jobs skipped during the cool-down
    assert await _delivered(sessions) == []  # all three left to the relay


# --------------------------------------------------------------------------- #
# Mark-delivered is ONE autocommit statement (no BEGIN / COMMIT / GUC)
# --------------------------------------------------------------------------- #
class _Recorder:
    """Every statement the engine sends, as SQLAlchemy hands it to the driver."""

    def __init__(self, engine: AsyncEngine) -> None:
        from sqlalchemy import event

        self.sql: list[str] = []
        self.statements: list[str] = []
        self.autocommit: list[bool] = []
        self._engine = engine.sync_engine

        def _before(conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
            self.sql.append(statement.split(None, 1)[0].upper())
            self.statements.append(statement)
            self.autocommit.append(conn._is_autocommit_isolation())

        self._listener = _before
        event.listen(self._engine, "before_cursor_execute", _before)

    def close(self) -> None:
        from sqlalchemy import event

        event.remove(self._engine, "before_cursor_execute", self._listener)


async def _seed_rows(
    sessions: async_sessionmaker[AsyncSession], n: int, **fields: Any
) -> list[Any]:
    import uuid as _uuid

    ids = [_uuid.uuid4() for _ in range(n)]
    async with sessions() as s, s.begin():
        s.add_all(
            OutboxMessage(
                id=i,
                channel="core.test_event.v1",
                schema_version="v1",
                payload={"n": k},
                **fields,
            )
            for k, i in enumerate(ids)
        )
    return ids


@pytest.mark.asyncio
async def test_mark_delivered_is_one_autocommit_statement(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    from datetime import datetime

    store = SQLAlchemyOutboxStore()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    fresh = await _seed_rows(sessions, 3)
    earlier = datetime(2020, 1, 1)
    dead = await _seed_rows(sessions, 1, dead_at=earlier)
    done = await _seed_rows(sessions, 1, delivered_at=earlier)
    rec = _Recorder(engine)
    try:
        await store.mark_delivered(factory, [*fresh, *dead, *done])
    finally:
        rec.close()
    # one UPDATE, sent in autocommit: no transaction around it
    assert rec.sql == ["UPDATE"] and rec.autocommit == [True]
    async with sessions() as s:
        rows = {r.id: r for r in (await s.execute(select(OutboxMessage))).scalars()}
    assert all(rows[i].delivered_at is not None for i in fresh)
    # the guards hold: a dead-lettered row is never resurrected, a delivered
    # row keeps its original timestamp
    assert rows[dead[0]].delivered_at is None and rows[dead[0]].dead_at == earlier
    assert rows[done[0]].delivered_at == earlier
    # the pooled connection went back transactional: a rolled-back write of
    # the next UoW leaves nothing behind
    async with sessions() as s:
        await s.begin()
        conn = await s.connection()
        assert not conn.sync_connection._is_autocommit_isolation()
        await s.execute(text("DELETE FROM outbox_messages"))
        await s.rollback()
    async with sessions() as s:
        assert len((await s.execute(select(OutboxMessage))).scalars().all()) == 5


@pytest.mark.asyncio
async def test_mark_delivered_skips_only_its_own_wal_flush_wait(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The mark runs with synchronous_commit = off LOCAL to its transaction:
    the next UoW on the same pooled connection commits synchronously."""
    store = SQLAlchemyOutboxStore()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    ids = await _seed_rows(sessions, 2)
    rec = _Recorder(engine)
    try:
        await store.mark_delivered(factory, ids)
    finally:
        rec.close()
    assert rec.sql == ["UPDATE"]
    assert (
        "set_config" in rec.statements[0]
        and "synchronous_commit" in (rec.statements[0])
    )
    async with sessions() as s:
        rows = (await s.execute(select(OutboxMessage))).scalars().all()
        assert all(r.delivered_at is not None for r in rows if r.id in ids)
        # same pool, next transaction: back to the server default (on)
        assert (await s.execute(text("SHOW synchronous_commit"))).scalar() == "on"
    # inside a transaction the statement switches only that transaction
    async with sessions() as s, s.begin():
        more = await _seed_rows(sessions, 1)
        from sandloom.core.sql_alchemy.outbox_store import _mark_delivered_statement

        await s.execute(
            _mark_delivered_statement(),
            {
                "outbox_ids": more,
                "delivered_at": datetime.now(UTC).replace(tzinfo=None),
            },
        )
        assert (await s.execute(text("SHOW synchronous_commit"))).scalar() == "off"
    async with sessions() as s:
        assert (await s.execute(text("SHOW synchronous_commit"))).scalar() == "on"


@pytest.mark.asyncio
async def test_mark_delivered_reuses_one_prebuilt_statement(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom.core.sql_alchemy import outbox_store

    store = SQLAlchemyOutboxStore()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    await store.mark_delivered(factory, await _seed_rows(sessions, 2))
    first = outbox_store._mark_delivered_pg
    assert first is not None
    later = await _seed_rows(sessions, 3)
    await store.mark_delivered(factory, later)
    assert outbox_store._mark_delivered_pg is first  # built once, bound per call
    async with sessions() as s:
        rows = {r.id: r for r in (await s.execute(select(OutboxMessage))).scalars()}
    assert all(rows[i].delivered_at is not None for i in later)


@pytest.mark.asyncio
async def test_rows_are_marked_iff_delivered(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    class _FailsOnTwo(_Bus):
        async def publish_event(
            self, channel: str, payload: dict[str, Any], *_a: Any, **_k: Any
        ) -> None:
            if payload["n"] == 2:
                raise ConnectionError("broker hiccup")
            await super().publish_event(channel, payload)

    store, bus = _CountingStore(), _FailsOnTwo()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    await _commit_events(sessions, store, bus, factory, [{"n": 1}, {"n": 2}, {"n": 3}])
    await uow_mod_dispatch.drain_post_commit()
    # 1 reached the broker; 2 failed and 3 (after it, order kept) never went.
    # A failed dispatch round marks NOTHING delivered: the relay redelivers
    # all three (1 again — a duplicate consumers dedupe on its stable id).
    assert bus.published == [{"n": 1}]
    assert await _delivered(sessions) == []


@pytest.mark.asyncio
async def test_crash_between_publish_and_mark_is_redelivered_by_the_relay(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The event reached the broker but the mark never ran (process died):
    the row stays undelivered and the relay re-publishes it under the SAME
    event id, so consumers dedupe it (at-least-once)."""
    from sqlalchemy import update

    from sandloom.core.tasks import relay

    class _Crashes(_CountingStore):
        async def mark_delivered(self, uow_factory: Any, ids: Sequence[Any]) -> None:
            self.mark_calls.append(list(ids))
            raise ConnectionResetError("process died before the mark")

    class _IdBus(_Bus):
        def __init__(self) -> None:
            super().__init__()
            self.ids: list[str] = []

        async def publish_event(
            self, channel: str, payload: dict[str, Any], *a: Any, **k: Any
        ) -> None:
            self.ids.append(str(k.get("event_id") or a[1]))
            await super().publish_event(channel, payload)

    store, bus = _Crashes(), _IdBus()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    await _commit_events(sessions, store, bus, factory, [{"n": 7}])
    await uow_mod_dispatch.drain_post_commit()
    assert len(store.mark_calls) == 1 and await _delivered(sessions) == []
    # past the relay's grace period
    async with sessions() as s, s.begin():
        await s.execute(
            update(OutboxMessage).values(
                emitted_at=OutboxMessage.emitted_at - text("interval '1 hour'")
            )
        )
    attempted = await relay._relay_once_for_store(
        factory, SQLAlchemyOutboxStore(), bus, sweep=False
    )
    assert attempted == 1
    assert bus.published == [{"n": 7}, {"n": 7}]
    assert len(bus.ids) == 2 and bus.ids[0] == bus.ids[1]  # same event id
    assert await _delivered(sessions) == [7]


@pytest.mark.asyncio
async def test_concurrent_drains_never_strand_each_other(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests drain at once (tests, shutdown racing a request): the
    first drainer to finish must not cancel the coalescer while the other's
    job is still being published, or that job is abandoned mid-publish."""
    monkeypatch.setattr(uow_dispatch, "_MARK_FLUSH_INTERVAL_SECONDS", 3600.0)
    flushing, release = asyncio.Event(), asyncio.Event()

    class _SlowFirstMark(_CountingStore):
        async def mark_delivered(self, uow_factory: Any, ids: Sequence[Any]) -> None:
            if not self.mark_calls:
                flushing.set()
                await release.wait()
            await super().mark_delivered(uow_factory, ids)

    store, bus = _SlowFirstMark(), _Bus()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731
    await _commit_events(sessions, store, bus, factory, [{"n": 0}])
    first = asyncio.create_task(uow_mod_dispatch.drain_post_commit())
    await asyncio.wait_for(flushing.wait(), 5)  # A is inside its final flush
    bus.gate = asyncio.Event()  # B's publish is in flight when A finishes
    await _commit_events(sessions, store, bus, factory, [{"n": 1}])
    second = asyncio.create_task(uow_mod_dispatch.drain_post_commit())
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(first, timeout=5)
    bus.gate.set()
    await asyncio.wait_for(second, timeout=5)
    assert sorted(p["n"] for p in bus.published) == [0, 1]
    assert await _delivered(sessions) == [0, 1]


@pytest.mark.parametrize(
    "payload",
    [
        {"org_id": "0" * 36, "user_id": "1" * 36, "role": "member", "version": 3},
        {"items": [{"k": i, "name": "x" * 10} for i in range(20)]},
        {"xs": list(range(1000))},
        {"xs": [i / 7 for i in range(1000)]},
        {"blob": "x" * 10_000},
        {"big": 2**80, 1: "non-str key"},
    ],
)
def test_dispatch_job_bytes_tracks_the_retained_python_size(
    payload: dict[Any, Any],
) -> None:
    """The JSON-based estimate stays within a small factor of the real size
    (``sys.getsizeof`` over every node) — no Python walk of the payload."""
    import sys
    import uuid

    from sandloom.core.infra.contracts import OutboxHandle

    seen: set[int] = set()
    stack: list[Any] = [payload]
    walked = 0
    while stack:
        value = stack.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        walked += sys.getsizeof(value)
        if isinstance(value, dict):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    event = uow_mod_scope._PendingEvent(
        OutboxHandle(id=uuid.uuid4()), "c", "v1", payload
    )
    job = uow_mod_dispatch._DispatchJob(None, None, lambda: None, (event,), ())  # type: ignore[arg-type]
    estimate = uow_mod_dispatch._dispatch_job_bytes(job)
    assert walked * 0.9 <= estimate <= walked * 4 + 1024


@pytest.mark.asyncio
async def test_mark_delivered_keeps_a_connection_bound_session_in_its_transaction(
    engine: AsyncEngine,
) -> None:
    """A factory whose sessions join an outer transaction (test harnesses)
    gets no AUTOCOMMIT view: its mark stays inside that transaction."""
    from sandloom.core.sql_alchemy import outbox_store

    store = SQLAlchemyOutboxStore()
    async with engine.connect() as conn:
        await conn.begin()
        joined = async_sessionmaker(
            bind=conn,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        factory = lambda: SQLAlchemyUnitOfWork(session_factory=joined)  # noqa: E731
        ids = await _seed_rows(joined, 1)
        assert outbox_store._autocommit_view(factory) is None
        await store.mark_delivered(factory, ids)
        marked = (
            await conn.execute(
                select(OutboxMessage.delivered_at).where(OutboxMessage.id == ids[0])
            )
        ).scalar_one()
        assert marked is not None
        await conn.rollback()
    async with async_sessionmaker(bind=engine)() as s:
        gone = await s.execute(select(OutboxMessage).where(OutboxMessage.id == ids[0]))
        assert gone.first() is None  # rolled back with the outer transaction


@pytest.mark.asyncio
async def test_consumer_session_hands_its_events_to_the_coalescer(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``consumer_db_session`` (a consumer's delivery UoW) does not wait for
    the broker nor the mark before its block exits — the delivery is acked
    sooner; ``db_session()`` keeps both in line (Celery task bodies)."""
    from sandloom.core import runtime

    store = _CountingStore()
    factory = lambda: SQLAlchemyUnitOfWork(session_factory=sessions)  # noqa: E731

    class _Provider:
        def outbox(self) -> Any:
            return store

        def audit(self) -> None:
            return None

    class _Core:
        settings = None

        def get_uow_factory(self, name: str) -> Any:
            return factory

        def database(self, name: str) -> _Provider:
            return _Provider()

    bus = _Bus()
    monkeypatch.setattr(runtime, "get_active_core", lambda: _Core())
    monkeypatch.setattr(runtime, "active_event_bus", lambda: bus)

    bus.gate = asyncio.Event()  # the "broker" hangs until released
    async with uow_mod.consumer_db_session() as _session:
        await di.publish_event("core.test_event.v1", {"n": 1})
    assert bus.published == [] and await _delivered(sessions) == []
    bus.gate.set()
    await uow_mod_dispatch.drain_post_commit()
    assert bus.published == [{"n": 1}] and await _delivered(sessions) == [1]

    async with uow_mod.db_session() as _session:
        await di.publish_event("core.test_event.v1", {"n": 2})
    assert bus.published[-1] == {"n": 2} and await _delivered(sessions) == [1, 2]


class _Marker:
    """A weak-referenceable payload value: tracks what keeps a job alive."""


@pytest.mark.asyncio
async def test_a_drain_cancelling_the_coalescers_flush_leaves_no_cyclic_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under load the drainer's mark UPDATE outlasts the mark-flush interval:
    the coalescer's timed wait for the next job times out, it flushes too and
    waits for the drainer's mark lock, and the drain cancels it there. That
    cancellation must leave nothing of the job in a reference cycle
    (production freezes the heap and tunes the GC thresholds: cyclic garbage
    is GC time), and an idle coalescer must hold no finished job.

    Event-driven, not timed: the interval never elapses on its own, and the
    coalescer's timed wait times out only once the drainer is marking."""
    import gc
    import uuid
    import weakref

    from sandloom.core.infra.contracts import OutboxHandle

    monkeypatch.setattr(uow_dispatch, "_MARK_FLUSH_INTERVAL_SECONDS", 3600.0)
    dispatcher = uow_mod_dispatch.post_commit_dispatcher()
    drainer_marking = asyncio.Event()
    coalescer_flushing = asyncio.Event()
    coalescer_cancelled_in_flush: list[bool] = []
    real_wait_for, real_flush = asyncio.wait_for, dispatcher.flush

    async def wait_for(aw: Any, timeout: float | None) -> Any:
        if getattr(aw, "__qualname__", "") != "Queue.get":  # not the timed wait
            return await real_wait_for(aw, timeout)
        await drainer_marking.wait()
        return await real_wait_for(aw, 1e-6)  # the interval elapses now

    async def flush() -> None:
        if asyncio.current_task() is not dispatcher._task:
            await real_flush()
            return
        coalescer_flushing.set()
        try:
            await real_flush()
        except asyncio.CancelledError:
            coalescer_cancelled_in_flush.append(True)
            raise

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    monkeypatch.setattr(dispatcher, "flush", flush)
    marker = _Marker()
    alive_while_idle: list[bool] = []

    class _SlowMarkStore:
        async def mark_delivered(self, uow_factory: Any, ids: Sequence[Any]) -> None:
            # The round is done and the coalescer idles in its timed wait.
            alive_while_idle.append(marker_ref() is not None)
            drainer_marking.set()
            await coalescer_flushing.wait()

    event = uow_mod_scope._PendingEvent(
        OutboxHandle(id=uuid.uuid4()), "c", "v1", {"m": marker}
    )
    job = uow_mod_dispatch._DispatchJob(
        _Bus(),
        _SlowMarkStore(),  # type: ignore[arg-type]
        lambda: None,  # type: ignore[arg-type,return-value]
        (event,),
        (),
    )
    marker_ref = weakref.ref(marker)
    gc.collect()
    gc.set_debug(gc.DEBUG_SAVEALL)
    gc.disable()
    try:
        dispatcher.submit(job)
        del job, event, marker
        await uow_mod_dispatch.drain_post_commit()
        for _ in range(3):  # the stop's gather callbacks still reference the task
            await asyncio.sleep(0)
        gc.collect()
        ours = [
            obj
            for obj in gc.garbage
            if type(obj).__module__.startswith("sandloom.") or isinstance(obj, _Marker)
        ]
    finally:
        gc.set_debug(0)
        gc.garbage.clear()
        gc.enable()
    assert coalescer_cancelled_in_flush == [True], "scenario not reproduced"
    assert ours == [], sorted({type(obj).__qualname__ for obj in ours})
    assert alive_while_idle == [False]
