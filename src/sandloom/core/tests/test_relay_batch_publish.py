"""Relay batch publish: a claimed batch goes out in ONE transport round trip.

The per-row accounting is unchanged: success settles every row delivered; a
broker outage parks the first row without counting an attempt and trips the
circuit; any other batch failure falls back to per-row publishing so the
attempt / backoff / dead-letter lands on the row that caused it. Buses without
a native batch and stores with the older ``deliver_batch`` signature keep the
per-row path.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
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

from sandloom.core.config.settings import settings as _settings
from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.transport_base import TransportBatchError
from sandloom.core.infra.contracts import BrokerUnavailableError
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import (
    SQLAlchemyOutboxStore,
    sql_alchemy_outbox_store,
)
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.tasks import relay
from sandloom.core.testing import reload_test_env
from sandloom.core.tests._pipelined_support import RecordingTransport, streams_bus
from sandloom.core.unit_of_work import dispatch as uow_mod_dispatch

CHANNEL = "core.test_event.v1"


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


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[OutboxMessage]:
    async with sessions() as s:
        return list(
            (await s.execute(select(OutboxMessage).order_by(OutboxMessage.emitted_at)))
            .scalars()
            .all()
        )


async def _delivered(sessions: async_sessionmaker[AsyncSession]) -> set[uuid.UUID]:
    return {r.id for r in await _rows(sessions) if r.delivered_at is not None}


# == relay ======================================================================
async def _seed(factory: Any, n: int) -> list[uuid.UUID]:
    base = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    ids = [uuid.uuid4() for _ in range(n)]
    async with factory() as uow:
        for i, msg_id in enumerate(ids):
            uow.session.add(
                OutboxMessage(
                    id=msg_id,
                    channel=CHANNEL,
                    schema_version="v1",
                    payload={"n": i},
                    emitted_at=base + timedelta(milliseconds=i),
                )
            )
    return ids


@pytest.mark.asyncio
async def test_relay_publishes_a_claimed_batch_in_one_round_trip(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _seed(factory, 7)
    t = RecordingTransport()
    result = await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), streams_bus(t), sweep=False
    )
    assert result.delivered == 7
    assert t.batches == [[str(i) for i in ids]]
    assert t.single == []
    assert await _delivered(sessions) == set(ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_relay_preserves_each_events_transactional_origin(
    factory: Any, sessions: async_sessionmaker[AsyncSession], count: int
) -> None:
    ids = await _seed(factory, count)
    originals = {}
    async with factory() as uow:
        for index, event_id in enumerate(ids):
            row = await uow.session.get(OutboxMessage, event_id)
            row.trace_id = f"trace-{index}"
            originals[str(event_id)] = (
                row.trace_id,
                row.emitted_at.replace(tzinfo=UTC).isoformat(),
            )
    transport = RecordingTransport()
    result = await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), streams_bus(transport), sweep=False
    )
    assert result.delivered == count
    for _message_id, envelope in transport._log[CHANNEL]:
        assert (envelope["trace_id"], envelope["emitted_at"]) == originals[
            envelope["id"]
        ]


@pytest.mark.asyncio
async def test_relay_batch_outage_counts_no_attempt_and_trips_circuit(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(factory, 5)
    t = RecordingTransport()
    t.fail_next = ConnectionError("connection refused")
    result = await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), streams_bus(t), sweep=False
    )
    assert result.unavailable and result.delivered == 0 and result.failed == 0
    assert relay.broker_circuit().is_open()
    rows = await _rows(sessions)
    assert all(r.delivery_attempts == 0 for r in rows)
    assert all(r.locked_by is None and r.delivered_at is None for r in rows)
    assert rows[0].last_error and "broker unavailable" in rows[0].last_error


@pytest.mark.asyncio
async def test_relay_batch_poison_falls_back_per_row(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """A non-outage batch failure is attributed to the one row that caused it."""
    ids = await _seed(factory, 4)

    class _PoisonTransport(RecordingTransport):
        async def publish(self, stream: str, envelope: EventEnvelope) -> str:
            if envelope["payload"]["n"] == 2:
                raise RuntimeError("poison")
            return await super().publish(stream, envelope)

    t = _PoisonTransport()
    t.partial_next = 2  # the batch stops at the poison row
    result = await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), streams_bus(t), sweep=False
    )
    assert result.delivered == 2 and result.failed == 1
    rows = {r.id: r for r in await _rows(sessions)}
    assert rows[ids[2]].delivery_attempts == 1 and rows[ids[2]].delivered_at is None
    assert all(rows[i].delivery_attempts == 0 for i in (ids[0], ids[1], ids[3]))
    assert all(rows[i].delivered_at is not None for i in (ids[0], ids[1]))
    # Row 3 (same channel, behind the backed-off poison row) is held, never
    # published ahead of it — see test_outbox_relay_ordering.py.
    assert rows[ids[3]].delivered_at is None
    assert rows[ids[3]].next_attempt_at == rows[ids[2]].next_attempt_at
    assert not relay.broker_circuit().is_open()


@pytest.mark.asyncio
async def test_relay_poison_row_still_dead_letters(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _seed(factory, 3)

    class _PoisonTransport(RecordingTransport):
        async def publish(self, stream: str, envelope: EventEnvelope) -> str:
            if envelope["payload"]["n"] == 1:
                raise RuntimeError("poison")
            return await super().publish(stream, envelope)

        async def publish_many(
            self, items: Sequence[tuple[str, EventEnvelope]]
        ) -> list[str]:
            raise TransportBatchError([], "x") from RuntimeError("poison")

    t = _PoisonTransport()
    bus = streams_bus(t)
    async with factory() as uow:
        await uow.session.execute(
            text("UPDATE outbox_messages SET delivery_attempts = :a WHERE id = :i"),
            {"a": _settings.outbox_relay_max_attempts - 1, "i": ids[1]},
        )
    result = await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), bus, sweep=False
    )
    assert result.dead_ids == (ids[1],)
    rows = {r.id: r for r in await _rows(sessions)}
    assert rows[ids[1]].dead_at is not None
    assert rows[ids[0]].delivered_at is not None
    assert rows[ids[2]].delivered_at is not None


@pytest.mark.asyncio
async def test_relay_keeps_per_row_for_buses_without_native_batch(
    factory: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _seed(factory, 3)
    bus = InMemoryEventBus(InMemorySchemaRegistry())
    calls: list[str] = []
    original = bus.publish_event

    async def spy(*a: Any, **k: Any) -> None:
        calls.append(str(k["event_id"]))
        await original(*a, **k)

    bus.publish_event = spy  # type: ignore[method-assign]
    result = await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), bus, sweep=False
    )
    assert result.delivered == 3 and calls == [str(i) for i in ids]


@pytest.mark.asyncio
async def test_relay_old_store_signature_is_called_without_publish_many(
    factory: Any,
) -> None:
    class _OldStore(SQLAlchemyOutboxStore):
        async def deliver_batch(  # type: ignore[override]
            self,
            uow_factory: Any,
            publish: Any,
            *,
            max_attempts: int,
            grace_seconds: float,
            limit: int,
            backoff: Any = None,
        ) -> Any:
            return await super().deliver_batch(
                uow_factory,
                publish,
                max_attempts=max_attempts,
                grace_seconds=grace_seconds,
                limit=limit,
                backoff=backoff,
            )

    await _seed(factory, 2)
    t = RecordingTransport()
    result = await relay._relay_batch_for_store(
        factory, _OldStore(), streams_bus(t), sweep=False
    )
    assert result.delivered == 2
    assert t.batches == [] and len(t.single) == 2


@pytest.mark.asyncio
async def test_relay_batch_rejects_nothing_on_outage_exception_type(
    factory: Any,
) -> None:
    """A BrokerUnavailableError raised by the batch keeps its meaning."""
    await _seed(factory, 2)

    class _Down(RecordingTransport):
        async def publish_many(
            self, items: Sequence[tuple[str, EventEnvelope]]
        ) -> list[str]:
            raise BrokerUnavailableError("down")

    result = await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), streams_bus(_Down()), sweep=False
    )
    assert result.unavailable and result.failed == 0
