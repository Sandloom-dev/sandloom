"""Post-commit dispatcher rounds: queued UoWs go out as ONE ordered batch.

Pins what must not change: commit before dispatch, nothing on rollback,
FIFO commit order + per-UoW emission order, envelope id == outbox row id,
bounded rounds, and at-least-once — a failed / partial / timed-out batch marks
NOTHING delivered, the relay redelivers it with the same ids (duplicates
allowed, never loss). In-process buses, ``db_session()`` and plain test buses
without the batch API keep working.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Sequence
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
from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import (
    SQLAlchemyOutboxStore,
    sql_alchemy_outbox_store,
)
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.tasks import relay
from sandloom.core.testing import reload_test_env
from sandloom.core.tests._pipelined_support import (
    CHANNEL,
    RecordingTransport,
    streams_bus,
)
from sandloom.core.tests._schemas import PermissiveSchemaRegistry
from sandloom.core.unit_of_work import _dispatch as uow_dispatch
from sandloom.core.unit_of_work import dispatch as uow_mod_dispatch
from sandloom.core.unit_of_work import scope as uow_mod_scope


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


# -- transports / buses -------------------------------------------------------
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


@pytest.fixture
def factory(sessions: async_sessionmaker[AsyncSession]) -> Any:
    return lambda: SQLAlchemyUnitOfWork(session_factory=sessions)


async def _commit(
    factory: Any,
    bus: Any,
    payloads: list[dict[str, Any]],
    *,
    store: SQLAlchemyOutboxStore | None = None,
    fail: bool = False,
) -> list[uuid.UUID]:
    """One UoW publishing ``payloads``; returns the outbox row ids it wrote."""
    store = store or sql_alchemy_outbox_store()
    uow = factory()
    session: Any = None
    try:
        async with uow:
            session = uow.session
            session.info[uow_mod_scope._OUTBOX_STORE_KEY] = store
            session.info[uow_mod_scope._UOW_FACTORY_KEY] = factory
            session.info[uow_mod_scope._EVENT_BUS_KEY] = bus
            with uow_mod_scope._bind_uow_scope(session, "default"):
                for payload in payloads:
                    await di.publish_event(CHANNEL, payload)
                ids = [
                    ev.handle.id
                    for ev in session.info[uow_mod_scope._OUTBOX_PENDING_KEY]
                ]
                if fail:
                    raise RuntimeError("business failure")
    except RuntimeError:
        return []
    await uow_mod._dispatch_outbox_after_commit(session)
    return ids


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[OutboxMessage]:
    async with sessions() as s:
        return list(
            (await s.execute(select(OutboxMessage).order_by(OutboxMessage.emitted_at)))
            .scalars()
            .all()
        )


async def _delivered(sessions: async_sessionmaker[AsyncSession]) -> set[uuid.UUID]:
    return {r.id for r in await _rows(sessions) if r.delivered_at is not None}


async def _age_rows(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Make every row due for the relay (past its grace period)."""
    async with sessions() as s, s.begin():
        await s.execute(
            text(
                "UPDATE outbox_messages SET emitted_at = emitted_at - interval '1 hour'"
            )
        )


# == post-commit dispatcher =====================================================
@pytest.mark.asyncio
async def test_queued_uows_go_out_in_one_ordered_batch(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    t = RecordingTransport()
    t.gate = asyncio.Event()  # hold the first round so the rest queue up
    bus = streams_bus(t)
    first = await _commit(factory, bus, [{"n": 0}])
    await asyncio.sleep(0)  # the coalescer takes job 1 and blocks on the gate
    later = [
        await _commit(factory, bus, [{"n": 10 * i + j} for j in range(3)])
        for i in range(1, 6)
    ]
    t.gate.set()
    await uow_mod_dispatch.drain_post_commit()
    expected = [str(i) for i in first] + [str(i) for ids in later for i in ids]
    # round 1 = the first job, round 2 = the five queued jobs in ONE batch
    assert t.batches == [[str(first[0])], expected[1:]]
    assert t.single == []
    # FIFO across UoWs + emission order inside each; envelope id == row id
    assert t.stream_ids() == expected
    rows = await _rows(sessions)
    assert {r.id for r in rows} == {uuid.UUID(i) for i in expected}
    assert await _delivered(sessions) == {r.id for r in rows}


@pytest.mark.asyncio
async def test_round_is_bounded(factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uow_dispatch, "_ROUND_MAX_JOBS", 4)
    t = RecordingTransport()
    t.gate = asyncio.Event()
    bus = streams_bus(t)
    for i in range(10):
        await _commit(factory, bus, [{"n": i}])
    t.gate.set()
    await uow_mod_dispatch.drain_post_commit()
    assert [len(b) for b in t.batches] == [1, 4, 4, 1]


@pytest.mark.asyncio
async def test_commit_happens_before_dispatch(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    t = RecordingTransport()
    seen: list[int] = []

    async def check(items: Sequence[tuple[str, EventEnvelope]]) -> None:
        # Another connection already sees the committed rows.
        ids = {uuid.UUID(str(env["id"])) for _s, env in items}
        rows = {r.id for r in await _rows(sessions)}
        seen.append(len(ids & rows))

    t.on_batch = check
    await _commit(factory, streams_bus(t), [{"n": 1}, {"n": 2}])
    await uow_mod_dispatch.drain_post_commit()
    assert seen == [2]


@pytest.mark.asyncio
async def test_rollback_publishes_nothing(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    t = RecordingTransport()
    assert await _commit(factory, streams_bus(t), [{"n": 1}], fail=True) == []
    await uow_mod_dispatch.drain_post_commit()
    assert t.batches == [] and t.single == []
    assert await _rows(sessions) == []


@pytest.mark.parametrize("failure", ["raise", "partial", "timeout"])
@pytest.mark.asyncio
async def test_failed_batch_marks_nothing_and_relay_redelivers_same_ids(
    failure: str,
    factory: Any,
    sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = RecordingTransport()
    bus = streams_bus(t)
    if failure == "raise":
        t.fail_next = ConnectionError("connection reset")
    elif failure == "partial":
        t.partial_next = 2  # Redis confirmed only the first two
    else:
        monkeypatch.setattr(uow_dispatch, "_POST_COMMIT_DISPATCH_TIMEOUT_SECONDS", 0.05)
        t.delay = 0.2
    ids = await _commit(factory, bus, [{"n": i} for i in range(4)])
    await uow_mod_dispatch.drain_post_commit()
    assert await _delivered(sessions) == set()  # a failed batch marks NOTHING
    t.delay = 0.0
    await _age_rows(sessions)
    result = await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), bus, sweep=False
    )
    assert result.delivered == 4
    assert await _delivered(sessions) == set(ids)
    log = t.stream_ids()
    # every event reached the stream with its outbox row id (never lost) ...
    assert set(log) == {str(i) for i in ids}
    # ... the relay re-sent all four with the SAME ids (any earlier copy that
    # did reach the stream is a duplicate consumers dedupe on the id). The
    # relay orders by emitted_at only: no per-UoW order promise there.
    assert sorted(log[-4:]) == sorted(str(i) for i in ids)


@pytest.mark.asyncio
async def test_cooldown_after_failure_leaves_later_rounds_to_the_relay(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    t = RecordingTransport()
    bus = streams_bus(t)
    t.fail_next = ConnectionError("down")
    await _commit(factory, bus, [{"n": 1}])
    await uow_mod_dispatch.drain_post_commit()
    await _commit(factory, bus, [{"n": 2}])  # inside the cool-down
    await uow_mod_dispatch.drain_post_commit()
    assert len(t.batches) == 1  # the second round did not even try
    assert await _delivered(sessions) == set()


@pytest.mark.asyncio
async def test_plain_test_bus_without_batch_api_still_works(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    class _Bus:
        in_process = False

        def __init__(self) -> None:
            self.published: list[str] = []

        def preflight(self, channel: str, payload: dict[str, Any]) -> None:
            pass

        async def publish_event(
            self, channel: str, payload: dict[str, Any], *_a: Any, **k: Any
        ) -> None:
            self.published.append(str(k["event_id"]))

    bus = _Bus()
    ids = await _commit(factory, bus, [{"n": 1}, {"n": 2}])
    await uow_mod_dispatch.drain_post_commit()
    assert bus.published == [str(i) for i in ids]
    assert await _delivered(sessions) == set(ids)


@pytest.mark.asyncio
async def test_memory_bus_inline_path_unchanged(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    bus = InMemoryEventBus(PermissiveSchemaRegistry())
    got: list[str] = []

    async def handler(env: EventEnvelope) -> None:
        got.append(env["id"])

    await bus.subscribe(CHANNEL, handler)
    ids = await _commit(factory, bus, [{"n": 1}, {"n": 2}])
    await bus.drain()
    assert got == [str(i) for i in ids]  # handed over before the response
    await uow_mod_dispatch.drain_post_commit()
    assert await _delivered(sessions) == set(ids)


@pytest.mark.asyncio
async def test_db_session_path_publishes_one_batch_in_line(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    t = RecordingTransport()
    bus = streams_bus(t)
    uow = factory()
    async with uow:
        session = uow.session
        session.info[uow_mod_scope._OUTBOX_STORE_KEY] = sql_alchemy_outbox_store()
        session.info[uow_mod_scope._UOW_FACTORY_KEY] = factory
        session.info[uow_mod_scope._EVENT_BUS_KEY] = bus
        with uow_mod_scope._bind_uow_scope(session, "default"):
            for i in range(3):
                await di.publish_event(CHANNEL, {"n": i})
    await uow_mod._dispatch_outbox_after_commit(session, wait=True)
    assert len(t.batches) == 1 and len(t.batches[0]) == 3
    assert len(await _delivered(sessions)) == 3
