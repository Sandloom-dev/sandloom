"""Tests for the kernel reliability hardening pass.

Covers: streams DLQ after a delivery budget, the durable consumer inbox
(exactly-once effect), outbox dead-lettering of poison rows, runtime-bound bus
resolution, and the savepoint phantom-publish guard.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core import di
from sandloom.core import unit_of_work as uow_mod
from sandloom.core.config.settings import settings
from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.sql_alchemy.durable_inbox import DurableConsumerInbox
from sandloom.core.sql_alchemy.models.consumer_inbox import ConsumerInboxEntry
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.testing import reload_test_env
from sandloom.core.unit_of_work import dispatch as uow_mod_dispatch
from sandloom.core.unit_of_work import scope as uow_mod_scope


def _env(event_id: str) -> EventEnvelope:
    return EventEnvelope(
        id=event_id,
        channel="core.test_event.v1",
        schema_version="v1",
        trace_id=None,
        emitted_at="2026-01-01T00:00:00+00:00",
        payload={"n": 1},
    )


# --------------------------------------------------------------------------- #
# Streams transport DLQ (no DB)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_memory_transport_dead_letters_after_budget() -> None:
    transport = MemoryTransport(max_deliveries=3)
    await transport.ensure_group("core.test_event.v1", "g")
    await transport.publish("core.test_event.v1", _env("e1"))

    # Consume without ever acking: the message is redelivered until the budget
    # is exhausted, then routed to the DLQ and no longer delivered.
    delivered = 0
    for _ in range(5):
        batch = await transport.consume(
            "core.test_event.v1", group="g", consumer="c", count=10
        )
        delivered += len(batch)

    assert delivered == 3  # delivered exactly max_deliveries times
    assert transport.dlq_depth("core.test_event.v1") == 1
    assert transport.pending_count("core.test_event.v1", "g") == 0


# --------------------------------------------------------------------------- #
# Runtime-bound bus (no DB)
# --------------------------------------------------------------------------- #
def test_active_event_bus_prefers_bound_core() -> None:
    from sandloom.core.runtime import (
        active_event_bus,
        clear_active_core,
        set_active_core,
    )

    core = CoreFacade(app=FastAPI())
    sentinel = object()
    core.set_event_bus(sentinel)
    set_active_core(core)
    try:
        assert active_event_bus() is sentinel
    finally:
        clear_active_core()


# --------------------------------------------------------------------------- #
# DB-backed fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    backend_dir = Path(__file__).resolve().parents[4]
    env_test = backend_dir / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured for tests")
    eng = create_async_engine(database_url, echo=False)
    async with eng.begin() as conn:
        # Recreate the outbox table so a DB left by an older schema revision
        # (create_all never ALTERs) picks up new columns.
        await conn.run_sync(lambda c: OutboxMessage.__table__.drop(c, checkfirst=True))
        await conn.run_sync(OutboxMessage.metadata.create_all)
        await conn.execute(text("DELETE FROM outbox_messages"))
        await conn.execute(text("DELETE FROM consumer_inbox"))
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_messages"))
        await conn.execute(text("DELETE FROM consumer_inbox"))
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


# --------------------------------------------------------------------------- #
# Durable consumer inbox (DB) — exactly-once effect
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_durable_inbox_claims_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    inbox = DurableConsumerInbox("billing.credits@orders.paid.v1")

    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as uow:
        assert uow.session is not None
        assert await inbox.claim(uow.session, "evt-1") is True

    # A duplicate delivery of the same event is rejected; a different event id
    # is claimed fine — and the rejection does not poison the transaction.
    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as uow:
        assert uow.session is not None
        assert await inbox.claim(uow.session, "evt-1") is False
        assert await inbox.claim(uow.session, "evt-2") is True

    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as verify:
        assert verify.session is not None
        rows = list(
            (await verify.session.execute(select(ConsumerInboxEntry))).scalars()
        )
        assert {r.event_id for r in rows} == {"evt-1", "evt-2"}


# --------------------------------------------------------------------------- #
# Outbox dead-letter (DB)
# --------------------------------------------------------------------------- #
class _FailBus:
    async def publish_event(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("transport down")


@pytest.mark.asyncio
async def test_outbox_relay_dead_letters_poison_message(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
    from sandloom.core.tasks.relay import _relay_once_for_store

    monkeypatch.setattr(settings, "outbox_relay_max_attempts", 1)
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    msg_id = uuid.uuid4()
    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as uow:
        assert uow.session is not None
        uow.session.add(
            OutboxMessage(
                id=msg_id,
                channel="core.test_event.v1",
                schema_version="v1",
                payload={"n": 1},
                emitted_at=old,
            )
        )

    def factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(session_factory=session_factory)

    await _relay_once_for_store(factory, sql_alchemy_outbox_store(), _FailBus())

    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as verify:
        assert verify.session is not None
        row = await verify.session.get(OutboxMessage, msg_id)
        assert row is not None
        assert row.delivery_attempts == 1
        assert row.dead_at is not None  # poison row moved to terminal state
        assert row.delivered_at is None


@pytest.mark.asyncio
async def test_relay_does_not_revalidate_foreign_module_schema(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relay may deliver rows produced by modules it did not load locally."""
    from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
    from sandloom.core.tasks.relay import _relay_once_for_store

    monkeypatch.setattr(settings, "outbox_relay_max_attempts", 1)
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    msg_id = uuid.uuid4()
    channel = "foreign.orders.paid.v1"

    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as uow:
        assert uow.session is not None
        uow.session.add(
            OutboxMessage(
                id=msg_id,
                channel=channel,
                schema_version="v1",
                payload={"order_id": "o-1"},
                emitted_at=old,
            )
        )

    delivered: list[EventEnvelope] = []
    bus = InMemoryEventBus(InMemorySchemaRegistry())

    async def _capture(envelope: EventEnvelope) -> None:
        delivered.append(envelope)

    await bus.subscribe(channel, _capture)

    def factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(session_factory=session_factory)

    attempted = await _relay_once_for_store(factory, sql_alchemy_outbox_store(), bus)
    assert attempted == 1
    await bus.drain()
    assert [event["id"] for event in delivered] == [str(msg_id)]

    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as verify:
        assert verify.session is not None
        row = await verify.session.get(OutboxMessage, msg_id)
        assert row is not None
        assert row.delivered_at is not None
        assert row.dead_at is None
        assert row.delivery_attempts == 0


class _SelectiveBus:
    """Publishes successfully except for one channel (which always fails)."""

    def __init__(self, fail_channel: str) -> None:
        self._fail_channel = fail_channel
        self.published: list[str] = []

    async def publish_event(self, channel: str, *args: Any, **kwargs: Any) -> None:
        if channel == self._fail_channel:
            raise RuntimeError("transport down for this channel")
        self.published.append(channel)


@pytest.mark.asyncio
async def test_relay_processes_each_message_independently(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failing message must not block delivery of the others in the batch.

    The relay processes each message in its own transaction, so a per-message
    publish failure (and, by extension, a per-message commit failure) cannot
    roll back the rows that already delivered.
    """
    from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
    from sandloom.core.tasks.relay import _relay_once_for_store

    monkeypatch.setattr(settings, "outbox_relay_max_attempts", 1)
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    good_id, bad_id = uuid.uuid4(), uuid.uuid4()
    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as uow:
        assert uow.session is not None
        uow.session.add(
            OutboxMessage(
                id=good_id,
                channel="core.good.v1",
                schema_version="v1",
                payload={"n": 1},
                emitted_at=old,
            )
        )
        uow.session.add(
            OutboxMessage(
                id=bad_id,
                channel="core.bad.v1",
                schema_version="v1",
                payload={"n": 2},
                emitted_at=old,
            )
        )

    def factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(session_factory=session_factory)

    bus = _SelectiveBus(fail_channel="core.bad.v1")
    attempted = await _relay_once_for_store(factory, sql_alchemy_outbox_store(), bus)
    assert attempted == 2
    assert bus.published == ["core.good.v1"]

    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as verify:
        assert verify.session is not None
        good = await verify.session.get(OutboxMessage, good_id)
        bad = await verify.session.get(OutboxMessage, bad_id)
        assert good is not None and good.delivered_at is not None
        # The failing message made no progress for the good one's transaction.
        assert bad is not None and bad.delivered_at is None
        assert bad.delivery_attempts == 1
        assert bad.dead_at is not None


# --------------------------------------------------------------------------- #
# Savepoint phantom-publish guard (DB)
# --------------------------------------------------------------------------- #
class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def preflight(self, channel: str, payload: dict[str, Any]) -> None:
        pass

    async def publish_event(
        self,
        channel: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        event_id: str | None = None,
    ) -> None:
        self.published.append((channel, payload))


@pytest.mark.asyncio
async def test_publish_in_rolled_back_savepoint_is_not_dispatched(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _RecordingBus()
    # Bind the bus the way every UoW-opening path does (session-scoped), not
    # via the legacy module global: once any app was built in this process,
    # dispatch resolves the active core's bus and ignores that global.
    uow = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with uow:
        session = uow.session
        assert session is not None
        session.info[uow_mod_scope._EVENT_BUS_KEY] = bus
        session.info[uow_mod_scope._OUTBOX_STORE_KEY] = sql_alchemy_outbox_store()
        session.info[uow_mod_scope._UOW_FACTORY_KEY] = lambda: SQLAlchemyUnitOfWork(
            session_factory=session_factory
        )
        token = uow_mod_scope._current_uow_session.set(session)
        try:
            await di.publish_event("core.test_event.v1", {"keep": 1})
            try:
                async with session.begin_nested():
                    await di.publish_event("core.test_event.v1", {"drop": 1})
                    raise RuntimeError("roll back this savepoint")
            except RuntimeError:
                pass
        finally:
            uow_mod_scope._current_uow_session.reset(token)
    await uow_mod._dispatch_outbox_after_commit(session)
    await uow_mod_dispatch.drain_post_commit()  # background coalescer

    payloads = [p for _, p in bus.published]
    assert {"keep": 1} in payloads
    # The event published inside the rolled-back savepoint must NOT be
    # dispatched (its outbox row was never committed).
    assert {"drop": 1} not in payloads

    # And the durable outbox agrees: exactly one row exists.
    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as verify:
        assert verify.session is not None
        rows = list((await verify.session.execute(select(OutboxMessage))).scalars())
        assert len(rows) == 1
        assert rows[0].payload == {"keep": 1}


# --------------------------------------------------------------------------- #
# Outbox claim ownership token (DB)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_outbox_settle_does_not_stomp_a_reclaimed_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A worker whose lease expired must not clobber the new owner's claim.

    Worker A claims a row, its lease expires, worker B reclaims it; A's late
    publish failure must NOT bump attempts or clear B's lease (settle is gated
    on the ownership token).
    """
    from sandloom.core.sql_alchemy.models.outbox import _utc_naive_now
    from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store

    store = sql_alchemy_outbox_store()
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    msg_id = uuid.uuid4()
    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as uow:
        assert uow.session is not None
        uow.session.add(
            OutboxMessage(
                id=msg_id,
                channel="core.test_event.v1",
                schema_version="v1",
                payload={"n": 1},
                emitted_at=old,
            )
        )

    def factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(session_factory=session_factory)

    # Worker A claims (mints token A).
    claim_a = await store._claim(factory, msg_id)
    assert claim_a is not None
    token_a = claim_a[4]

    # A's lease expires: force locked_until into the past so B can reclaim.
    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as uow:
        assert uow.session is not None
        row = await uow.session.get(OutboxMessage, msg_id)
        assert row is not None
        row.locked_until = _utc_naive_now() - timedelta(seconds=1)

    # Worker B reclaims (mints token B, distinct owner).
    claim_b = await store._claim(factory, msg_id)
    assert claim_b is not None
    token_b = claim_b[4]
    assert token_b != token_a

    # A's late publish fails and tries to settle with its STALE token — must be
    # a no-op: no attempt bump, B's lease intact.
    status = await store._settle_failure(factory, msg_id, token_a, "late", 10)
    assert status == "skipped"

    async with SQLAlchemyUnitOfWork(session_factory=session_factory) as verify:
        assert verify.session is not None
        row = await verify.session.get(OutboxMessage, msg_id)
        assert row is not None
        assert row.locked_by == token_b  # B still owns the lease
        assert row.delivery_attempts == 0  # A did not bump B's accounting
        assert row.dead_at is None
        assert row.delivered_at is None

    # B settles success with ITS token — delivered.
    assert await store._settle_success(factory, msg_id, token_b) == "delivered"
