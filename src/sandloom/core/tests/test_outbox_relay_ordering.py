"""The relay never lets a later event overtake an earlier one parked in backoff.

Regression (code review 2026-10-21, Eventing #9): the post-commit dispatcher
stops a UoW's events at the first failure (the rest is left to the relay), but
the relay charged a failing row its attempt + backoff and went on publishing
the rows behind it — so a later event of the same UoW (same channel: the only
place a consumer can observe order) reached the stream first, and the next
relay batches kept publishing them while the earlier one waited out its
backoff. Now the rows of the claimed batch that follow a failed (or
broker-parked) row on the SAME channel are held: released without an attempt,
due again exactly when the blocking row is, so the claim (``ORDER BY
emitted_at``) publishes the blocking row first. A dead-lettered blocker
releases them at once. Other channels are not held.
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
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core.config.settings import settings as _settings
from sandloom.core.infra.contracts import BrokerUnavailableError
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.tasks import relay
from sandloom.core.testing import reload_test_env

A = "core.order_a.v1"
B = "core.order_b.v1"


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


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> Any:
    sm = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return lambda: SQLAlchemyUnitOfWork(session_factory=sm)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed(factory: Any, channels: list[str]) -> list[uuid.UUID]:
    """One UoW's events, in emission order."""
    base = _now() - timedelta(minutes=5)
    ids = [uuid.uuid4() for _ in channels]
    async with factory() as uow:
        for i, (msg_id, channel) in enumerate(zip(ids, channels, strict=True)):
            uow.session.add(
                OutboxMessage(
                    id=msg_id,
                    channel=channel,
                    schema_version="v1",
                    payload={"n": i},
                    emitted_at=base + timedelta(milliseconds=i),
                )
            )
    return ids


async def _rows(factory: Any) -> dict[uuid.UUID, OutboxMessage]:
    async with factory() as uow:
        rows = (await uow.session.execute(select(OutboxMessage))).scalars().all()
        return {r.id: r for r in rows}


class _Bus:
    def __init__(self, *, fail: set[int] | None = None, down: set[int] | None = None):
        self.fail = fail or set()
        self.down = down or set()
        self.published: list[int] = []

    async def publish_event(
        self, channel: str, payload: dict[str, Any], *_a: Any, **_k: Any
    ) -> None:
        n = payload["n"]
        if n in self.down:
            raise BrokerUnavailableError("connection refused")
        if n in self.fail:
            raise RuntimeError("poison")
        self.published.append(n)


async def _batch(factory: Any, bus: _Bus) -> Any:
    async def publish(channel: str, payload: dict, version: str, event_id: str) -> None:
        await bus.publish_event(channel, payload, version, event_id=event_id)

    return await sql_alchemy_outbox_store().deliver_batch(
        factory,
        publish,
        max_attempts=_settings.outbox_relay_max_attempts,
        grace_seconds=0,
        limit=50,
        backoff=lambda _n: 60.0,
    )


async def _make_all_due(factory: Any) -> None:
    async with factory() as uow:
        await uow.session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.next_attempt_at.is_not(None))
            .values(next_attempt_at=_now() - timedelta(seconds=1))
        )


@pytest.mark.asyncio
async def test_rows_behind_a_failed_row_on_its_channel_are_held(factory: Any) -> None:
    ids = await _seed(factory, [A, A, A, A])
    bus = _Bus(fail={1})
    result = await _batch(factory, bus)
    # Before: [0, 2, 3] — rows 2 and 3 overtook row 1 parked in backoff.
    assert bus.published == [0]
    assert (result.delivered, result.failed) == (1, 1)
    rows = await _rows(factory)
    blocker = rows[ids[1]]
    assert blocker.delivery_attempts == 1 and blocker.next_attempt_at is not None
    for i in ids[2:]:
        held = rows[i]
        assert held.delivered_at is None and held.delivery_attempts == 0
        assert held.next_attempt_at == blocker.next_attempt_at
        assert held.locked_by is None and held.locked_until is None

    # Once due, the blocking row goes first, then the held ones, in order.
    await _make_all_due(factory)
    bus.fail.clear()
    await _batch(factory, bus)
    assert bus.published == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_other_channels_are_not_held(factory: Any) -> None:
    ids = await _seed(factory, [A, B, A, B])
    bus = _Bus(fail={0})
    await _batch(factory, bus)
    assert bus.published == [1, 3]
    rows = await _rows(factory)
    assert rows[ids[2]].delivered_at is None
    assert rows[ids[2]].next_attempt_at == rows[ids[0]].next_attempt_at


@pytest.mark.asyncio
async def test_a_dead_lettered_blocker_releases_the_held_rows(
    factory: Any,
) -> None:
    ids = await _seed(factory, [A, A, A])
    async with factory() as uow:
        await uow.session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == ids[0])
            .values(delivery_attempts=_settings.outbox_relay_max_attempts - 1)
        )
    bus = _Bus(fail={0})
    result = await _batch(factory, bus)
    assert result.dead_ids == (ids[0],)
    # A dead row will never be delivered: nothing waits behind it.
    assert bus.published == [1, 2]
    rows = await _rows(factory)
    assert all(rows[i].delivered_at is not None for i in ids[1:])


@pytest.mark.asyncio
async def test_rows_behind_a_broker_parked_row_wait_for_it(factory: Any) -> None:
    ids = await _seed(factory, [A, A, B])
    bus = _Bus(down={0})
    result = await _batch(factory, bus)
    assert result.unavailable
    rows = await _rows(factory)
    parked = rows[ids[0]]
    assert parked.delivery_attempts == 0 and parked.next_attempt_at is not None
    assert rows[ids[1]].next_attempt_at == parked.next_attempt_at
    assert rows[ids[1]].delivery_attempts == 0
    # Another channel is just released (due at once), as before.
    assert rows[ids[2]].next_attempt_at is None
