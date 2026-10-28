"""Integration tests for the UoW-governed request path + transactional outbox.

Covers the Wave-3 contract:
- publish_event inside a UoW scope writes to outbox_messages in the SAME
  transaction (rollback discards both business data and events);
- after commit, pending events are dispatched to the bus and marked delivered;
- outside a UoW scope publish_event RAISES (0.7); ``required=False`` is the
  explicit best-effort form that goes straight to the bus.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
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
from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.testing import reload_test_env
from sandloom.core.unit_of_work import dispatch as uow_mod_dispatch
from sandloom.core.unit_of_work import scope as uow_mod_scope


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    backend_dir = Path(__file__).resolve().parents[4]
    env_test = backend_dir / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def outbox_engine() -> AsyncGenerator[AsyncEngine, None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured for tests")
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        # Recreate the outbox table so a DB left by an older schema revision
        # (create_all never ALTERs) picks up new columns.
        await conn.run_sync(lambda c: OutboxMessage.__table__.drop(c, checkfirst=True))
        await conn.run_sync(OutboxMessage.metadata.create_all)
        await conn.execute(text("DELETE FROM outbox_messages"))
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_messages"))
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    outbox_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=outbox_engine, class_=AsyncSession, expire_on_commit=False
    )


class RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.event_ids: list[str | None] = []
        self.preflighted: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    def preflight(self, channel: str, payload: dict[str, Any]) -> None:
        # publish_event runs preflight inside the transaction before the
        # outbox row is written; record it so tests can assert it happened.
        self.preflighted.append((channel, payload))

    async def publish_event(
        self,
        channel: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        event_id: str | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("bus down")
        self.published.append((channel, payload))
        self.event_ids.append(event_id)


def _use_bus(monkeypatch: pytest.MonkeyPatch, bus: RecordingBus) -> RecordingBus:
    """Make ``bus`` the active runtime's event bus for this test.

    Dispatch never reads a process-global bus: a UoW session carries the
    app-owned bus (``_EVENT_BUS_KEY``, see :func:`_bind_database_runtime`) and
    everything else resolves ``sandloom.core.runtime.active_event_bus()`` — the
    active core's bus once any app was built (``create_app`` binds the process
    core). Patching ``sandloom.core.eventing.event_bus.event_bus`` therefore only
    worked while no app had been built earlier in the same process.
    """
    import sandloom.core.runtime as runtime

    monkeypatch.setattr(runtime, "active_event_bus", lambda: bus)
    return bus


def _bind_database_runtime(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # What every UoW-opening path binds: the outbox store, the UoW factory and
    # the app-owned event bus (here the active runtime's — the test's bus).
    import sandloom.core.runtime as runtime

    session.info[uow_mod_scope._EVENT_BUS_KEY] = runtime.active_event_bus()
    session.info[uow_mod_scope._OUTBOX_STORE_KEY] = sql_alchemy_outbox_store()
    session.info[uow_mod_scope._UOW_FACTORY_KEY] = lambda: SQLAlchemyUnitOfWork(
        session_factory=session_factory
    )


@pytest.mark.asyncio
async def test_publish_event_enqueues_to_outbox_in_same_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside a UoW scope the event lands in outbox_messages, not on the bus."""
    bus = RecordingBus()
    _use_bus(monkeypatch, bus)

    uow = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with uow:
        assert uow.session is not None
        _bind_database_runtime(uow.session, session_factory)
        token = uow_mod_scope._current_uow_session.set(uow.session)
        try:
            await di.publish_event("core.test_event.v1", {"n": 1})
            # Not on the bus yet — transaction is still open
            assert bus.published == []
            result = await uow.session.execute(select(OutboxMessage))
            rows = list(result.scalars())
            assert len(rows) == 1
            assert rows[0].channel == "core.test_event.v1"
        finally:
            uow_mod_scope._current_uow_session.reset(token)
        await uow_mod._dispatch_outbox_after_commit(
            uow.session
        )  # before commit: noop run
        # request-path dispatch runs in the background coalescer
        await uow_mod_dispatch.drain_post_commit()
        # dispatch consumed the pending list and published post-"commit"
        assert [c for c, _ in bus.published] == ["core.test_event.v1"]


@pytest.mark.asyncio
async def test_rollback_discards_outbox_rows_and_nothing_is_published(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = RecordingBus()
    _use_bus(monkeypatch, bus)

    uow = SQLAlchemyUnitOfWork(session_factory=session_factory)
    with pytest.raises(RuntimeError, match="boom"):
        async with uow:
            assert uow.session is not None
            _bind_database_runtime(uow.session, session_factory)
            token = uow_mod_scope._current_uow_session.set(uow.session)
            try:
                await di.publish_event("core.test_event.v1", {"n": 2})
            finally:
                uow_mod_scope._current_uow_session.reset(token)
            raise RuntimeError("boom")

    assert bus.published == []
    verify = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with verify:
        assert verify.session is not None
        result = await verify.session.execute(select(OutboxMessage))
        assert list(result.scalars()) == []


@pytest.mark.asyncio
async def test_committed_events_marked_delivered_after_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = RecordingBus()
    _use_bus(monkeypatch, bus)
    uow = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with uow:
        assert uow.session is not None
        session = uow.session
        _bind_database_runtime(session, session_factory)
        token = uow_mod_scope._current_uow_session.set(session)
        try:
            await di.publish_event("core.test_event.v1", {"n": 3})
        finally:
            uow_mod_scope._current_uow_session.reset(token)
    # Committed; now dispatch (this is what di.get_db does after the UoW exits)
    await uow_mod._dispatch_outbox_after_commit(session)
    await uow_mod_dispatch.drain_post_commit()  # background coalescer
    assert [c for c, _ in bus.published] == ["core.test_event.v1"]

    verify = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with verify:
        assert verify.session is not None
        result = await verify.session.execute(select(OutboxMessage))
        rows = list(result.scalars())
        assert len(rows) == 1
        assert rows[0].delivered_at is not None
        # Envelope id must be the outbox row id (stable across redeliveries)
        assert bus.event_ids == [str(rows[0].id)]


@pytest.mark.asyncio
async def test_dispatch_failure_leaves_message_pending_for_relay(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = RecordingBus()
    bus.fail = True
    _use_bus(monkeypatch, bus)

    uow = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with uow:
        assert uow.session is not None
        session = uow.session
        _bind_database_runtime(session, session_factory)
        token = uow_mod_scope._current_uow_session.set(session)
        try:
            await di.publish_event("core.test_event.v1", {"n": 4})
        finally:
            uow_mod_scope._current_uow_session.reset(token)
    await uow_mod._dispatch_outbox_after_commit(session)  # bus down: swallowed
    await uow_mod_dispatch.drain_post_commit()  # background coalescer

    verify = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with verify:
        assert verify.session is not None
        result = await verify.session.execute(select(OutboxMessage))
        rows = list(result.scalars())
        assert len(rows) == 1
        assert rows[0].delivered_at is None  # relay will pick it up


@pytest.mark.asyncio
async def test_publish_event_outside_uow_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.7: no UoW → no outbox transaction → the guaranteed form refuses."""
    bus = RecordingBus()
    _use_bus(monkeypatch, bus)
    assert uow_mod_scope._current_uow_session.get() is None
    with pytest.raises(di.UnitOfWorkRequiredError, match="required=False"):
        await di.publish_event("core.test_event.v1", {"n": 5})
    with pytest.raises(di.UnitOfWorkRequiredError):
        await di.publish_event("core.test_event.v1", {"n": 5}, required=True)
    assert bus.published == []


@pytest.mark.asyncio
async def test_publish_event_best_effort_outside_uow_goes_straight_to_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = RecordingBus()
    _use_bus(monkeypatch, bus)
    assert uow_mod_scope._current_uow_session.get() is None
    await di.publish_event("core.test_event.v1", {"n": 5}, required=False)
    assert [c for c, _ in bus.published] == ["core.test_event.v1"]


@pytest.mark.asyncio
async def test_publish_event_best_effort_outside_uow_is_preflighted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GLM review: "required=False outside a UoW skips preflight" — it does
    # not: the bus's non-prevalidated publish_event runs the full preflight
    # (channel name, schema, size) and the caller gets the rejection.
    from sandloom.core.contracts.validators import ContractValidationError
    from sandloom.core.eventing.memory_bus import InMemoryEventBus
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    bus = InMemoryEventBus(InMemorySchemaRegistry())
    import sandloom.core.runtime as runtime

    monkeypatch.setattr(runtime, "active_event_bus", lambda: bus)
    preflights: list[str] = []
    real = bus.preflight

    def spy(channel: str, payload: dict[str, Any]) -> None:
        preflights.append(channel)
        real(channel, payload)

    monkeypatch.setattr(bus, "preflight", spy)
    with pytest.raises((ContractValidationError, ValueError)):
        await di.publish_event("Not A Channel!", {"n": 1}, required=False)
    assert preflights == ["Not A Channel!"]


@pytest.mark.asyncio
async def test_publish_event_versions_outside_uow_raises_unless_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = RecordingBus()
    _use_bus(monkeypatch, bus)
    payloads: dict[str, dict[str, Any]] = {
        "core.test_event.v1": {"n": 1},
        "core.test_event.v2": {"n": 1},
    }
    with pytest.raises(di.UnitOfWorkRequiredError):
        await di.publish_event_versions(payloads)
    await di.publish_event_versions(payloads, required=False)
    assert [c for c, _ in bus.published] == [
        "core.test_event.v1",
        "core.test_event.v2",
    ]


@pytest.mark.asyncio
async def test_best_effort_inside_uow_still_uses_the_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``required=False`` never weakens a call that HAS a UoW."""
    bus = RecordingBus()
    _use_bus(monkeypatch, bus)
    uow = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with uow:
        assert uow.session is not None
        _bind_database_runtime(uow.session, session_factory)
        token = uow_mod_scope._current_uow_session.set(uow.session)
        try:
            await di.publish_event("core.test_event.v1", {"n": 1}, required=False)
            assert bus.published == []
            result = await uow.session.execute(select(OutboxMessage))
            assert [r.channel for r in result.scalars()] == ["core.test_event.v1"]
        finally:
            uow_mod_scope._current_uow_session.reset(token)


@pytest.mark.asyncio
async def test_post_commit_dispatch_preserves_unit_of_work_order(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One UoW's events reach the broker in emission order (no gather race).

    The first publish is the SLOWEST: concurrent dispatch let the later events
    overtake it on the broker ("updated" before "created"). A failure stops the
    batch so later events are left for the relay instead of overtaking.
    """
    import asyncio

    class _SlowFirstBus(RecordingBus):
        async def publish_event(
            self,
            channel: str,
            payload: dict[str, Any],
            schema_version: str = "v1",
            event_id: str | None = None,
        ) -> None:
            await asyncio.sleep(0.05 if payload["n"] == 0 else 0)
            if payload["n"] == 3:
                raise RuntimeError("broker hiccup")
            self.published.append((channel, payload))

    bus = _SlowFirstBus()
    _use_bus(monkeypatch, bus)
    uow = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with uow:
        assert uow.session is not None
        session = uow.session
        _bind_database_runtime(session, session_factory)
        token = uow_mod_scope._current_uow_session.set(session)
        try:
            for n in range(5):
                await di.publish_event("core.test_event.v1", {"n": n})
        finally:
            uow_mod_scope._current_uow_session.reset(token)
    await uow_mod._dispatch_outbox_after_commit(session)
    await uow_mod_dispatch.drain_post_commit()  # background coalescer
    assert [p["n"] for _c, p in bus.published] == [0, 1, 2]

    verify = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with verify:
        assert verify.session is not None
        rows = list((await verify.session.execute(select(OutboxMessage))).scalars())
        delivered = sorted(r.payload["n"] for r in rows if r.delivered_at is not None)
        # 3 failed and 4 never went; a failed dispatch round marks nothing
        # delivered, so the relay redelivers all five (0-2 as duplicates).
        assert delivered == []
