"""Work-queue claims without ``skip_locked`` (design/infra-agnostic/spec.md,
section 3 rows "outbox relay" and "sagas"), forced on real PostgreSQL.

A database whose renderer lacks ``skip_locked`` claims an outbox row by
compare-and-set on its lease (``locked_until``/``locked_by``) and a due saga
instance by compare-and-set on ``swept_at``. The path is forced here on
PostgreSQL so it cannot rot: two claimers never take the same row, an expired
lease is claimed again (a backed-off, dead or exhausted row is not), only the
lease's owner settles, two sweeps claim an instance once, and an instance a
transition moved after the sweep read it is not stamped.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import sandloom.core.sql_alchemy.outbox_store.relay as _relay
import sandloom.core.sql_alchemy.saga_store as _saga_store
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.models.saga import SagaInstance
from sandloom.core.sql_alchemy.outbox_store import (
    SQLAlchemyOutboxStore,
    sql_alchemy_outbox_store,
)
from sandloom.core.sql_alchemy.portable import skips_locked_rows
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.tasks import relay
from sandloom.core.testing import reload_test_env
from sandloom.core.tests.saga_fixtures import without_skip_locked_renderer

SAGA_TYPE = "core_test.queue_claim"


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


@pytest.fixture
def without_skip_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The renderer lacks ``skip_locked``: the compare-and-set claims."""
    without_skip_locked_renderer(monkeypatch)


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured for tests")
    eng = create_async_engine(url, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: OutboxMessage.__table__.drop(c, checkfirst=True))
        await conn.run_sync(OutboxMessage.metadata.create_all)
        await conn.execute(text("DELETE FROM outbox_messages"))
        await conn.execute(
            delete(SagaInstance.__table__).where(
                SagaInstance.__table__.c.saga_type == SAGA_TYPE
            )
        )
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_messages"))
        await conn.execute(
            delete(SagaInstance.__table__).where(
                SagaInstance.__table__.c.saga_type == SAGA_TYPE
            )
        )
    await eng.dispose()


@pytest_asyncio.fixture
async def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def factory(sessions: async_sessionmaker[AsyncSession]) -> Any:
    return lambda: SQLAlchemyUnitOfWork(session_factory=sessions)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed(factory: Any, n: int, **fields: Any) -> list[uuid.UUID]:
    base = _now() - timedelta(minutes=5)
    ids = [uuid.uuid4() for _ in range(n)]
    async with factory() as uow:
        for i, msg_id in enumerate(ids):
            uow.session.add(
                OutboxMessage(
                    id=msg_id,
                    channel=f"core.test_event_{i}.v1",
                    schema_version="v1",
                    payload={"n": i},
                    emitted_at=base + timedelta(milliseconds=i),
                    **fields,
                )
            )
    return ids


async def _row(factory: Any, msg_id: uuid.UUID) -> OutboxMessage:
    async with factory() as uow:
        found = await uow.session.execute(
            select(OutboxMessage).where(OutboxMessage.id == msg_id)
        )
        return found.scalars().one()


class _Bus:
    def __init__(self) -> None:
        self.published: list[int] = []

    async def publish(
        self, _channel: str, payload: dict[str, Any], _version: str, _id: str
    ) -> None:
        await asyncio.sleep(0)  # let the other relays interleave
        self.published.append(payload["n"])


def _lease() -> tuple[datetime, datetime]:
    now = _now()
    return now, now + timedelta(seconds=30)


def test_postgresql_skips_locked_rows(engine: AsyncEngine) -> None:
    assert skips_locked_rows(engine)


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_skip_locked")
async def test_the_forced_path_claims_without_skip_locked(
    engine: AsyncEngine, factory: Any
) -> None:
    statements: list[str] = []

    def _seen(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        statements.append(statement)

    await _seed(factory, 2)
    event.listen(engine.sync_engine, "before_cursor_execute", _seen)
    try:
        bus = _Bus()
        result = await sql_alchemy_outbox_store().deliver_batch(
            factory, bus.publish, max_attempts=5, grace_seconds=0, limit=10
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _seen)
    assert (result.claimed, result.delivered) == (2, 2)
    assert sorted(bus.published) == [0, 1]
    assert not any("SKIP LOCKED" in s for s in statements)
    assert any(
        s.startswith("UPDATE outbox_messages SET locked_until") for s in statements
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_skip_locked")
async def test_two_claimers_never_take_the_same_row(
    sessions: async_sessionmaker[AsyncSession], factory: Any
) -> None:
    (msg_id,) = await _seed(factory, 1)
    now, until = _lease()

    async def claim(session: AsyncSession) -> Any:
        async with session.begin():
            return await SQLAlchemyOutboxStore._claim_by_lease(
                session, msg_id, now, until, uuid.uuid4()
            )

    async with sessions() as first, sessions() as second:
        async with first.begin():
            won = await SQLAlchemyOutboxStore._claim_by_lease(
                first, msg_id, now, until, uuid.uuid4()
            )
            # The second claimer waits on the first's row, then re-checks.
            racing = asyncio.create_task(claim(second))
            await asyncio.sleep(0.2)
            assert not racing.done()
        lost = await racing
    assert won is not None and won[3] == str(msg_id)
    assert lost is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_skip_locked")
async def test_parallel_relays_publish_each_row_once(factory: Any) -> None:
    await _seed(factory, 12)
    bus = _Bus()
    store = sql_alchemy_outbox_store()
    results = await asyncio.gather(
        *(
            store.deliver_batch(
                factory, bus.publish, max_attempts=5, grace_seconds=0, limit=12
            )
            for _ in range(4)
        )
    )
    assert sorted(bus.published) == list(range(12))
    assert sum(r.delivered for r in results) == 12


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_skip_locked")
async def test_a_live_lease_is_not_claimed_an_expired_one_is(factory: Any) -> None:
    old = uuid.uuid4()
    (live,) = await _seed(
        factory, 1, locked_by=old, locked_until=_now() + timedelta(seconds=30)
    )
    (expired,) = await _seed(
        factory, 1, locked_by=old, locked_until=_now() - timedelta(seconds=1)
    )
    store = sql_alchemy_outbox_store()
    assert await store._claim(factory, live) is None
    claim = await store._claim(factory, expired)
    assert claim is not None
    token = claim[4]
    assert token != old
    row = await _row(factory, expired)
    assert row.locked_by == token and row.locked_until is not None
    assert row.locked_until > _now()


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_skip_locked")
async def test_only_the_lease_owner_settles(factory: Any) -> None:
    (msg_id,) = await _seed(factory, 1)
    store = sql_alchemy_outbox_store()
    claim = await store._claim(factory, msg_id)
    assert claim is not None
    assert await store._settle_success(factory, msg_id, uuid.uuid4()) == "skipped"
    assert (await _row(factory, msg_id)).delivered_at is None
    assert await store._settle_success(factory, msg_id, claim[4]) == "delivered"
    row = await _row(factory, msg_id)
    assert row.delivered_at is not None and row.locked_by is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_skip_locked")
async def test_deliver_pending_without_skip_locked(factory: Any) -> None:
    (msg_id,) = await _seed(factory, 1)
    bus = _Bus()
    store = sql_alchemy_outbox_store()
    status = await store.deliver_pending(factory, msg_id, bus.publish, max_attempts=3)
    assert status == "delivered" and bus.published == [0]
    again = await store.deliver_pending(factory, msg_id, bus.publish, max_attempts=3)
    assert again == "skipped" and bus.published == [0]


# -- the saga sweep ----------------------------------------------------------


async def _seed_instance(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    now = _now()
    instance_id = uuid.uuid4()
    async with sessions() as session, session.begin():
        await session.execute(
            insert(SagaInstance.__table__).values(
                id=instance_id,
                saga_type=SAGA_TYPE,
                definition_version=1,
                saga_id=str(instance_id),
                tenant_id=None,
                status="running",
                step_index=0,
                direction="forward",
                resolving=False,
                attempt=1,
                next_attempt_at=now - timedelta(seconds=10),
                deadline_at=now + timedelta(hours=1),
                input={},
                results={},
                applied=[],
                version=3,
                created_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
    return instance_id


async def _swept_at(
    sessions: async_sessionmaker[AsyncSession], instance_id: uuid.UUID
) -> datetime | None:
    async with sessions() as session:
        table = SagaInstance.__table__
        found = await session.execute(
            select(table.c.swept_at).where(table.c.id == instance_id)
        )
        value: datetime | None = found.scalar_one()
        return value


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_skip_locked")
async def test_two_sweeps_claim_an_instance_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    instance_id = await _seed_instance(sessions)
    now = _now()
    swept_before = now - timedelta(seconds=5)

    async def sweep(session: AsyncSession) -> list[Any]:
        async with session.begin():
            return await _saga_store.claim_due(
                session, now=now, swept_before=swept_before, limit=10
            )

    async with sessions() as first, sessions() as second:
        async with first.begin():
            won = await _saga_store.claim_due(
                first, now=now, swept_before=swept_before, limit=10
            )
            racing = asyncio.create_task(sweep(second))
            await asyncio.sleep(0.2)
            assert not racing.done()  # waits on the stamped row
        lost = await racing
    ours = [d for d in won if d.id == instance_id]
    assert len(ours) == 1 and ours[0].version == 3
    assert [d for d in lost if d.id == instance_id] == []
    assert await _swept_at(sessions, instance_id) == now

    # Within the interval the next sweep leaves it alone; after it, claims again.
    async with sessions() as session, session.begin():
        again = await _saga_store.claim_due(
            session, now=now, swept_before=swept_before, limit=10
        )
    assert [d for d in again if d.id == instance_id] == []
    later = now + timedelta(seconds=10)
    async with sessions() as session, session.begin():
        after = await _saga_store.claim_due(
            session, now=later, swept_before=later - timedelta(seconds=5), limit=10
        )
    assert [d.id for d in after if d.id == instance_id] == [instance_id]


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_skip_locked")
async def test_a_transition_waits_for_the_instance_lock(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Without ``skip_locked`` the advance path takes the row lock waiting."""
    instance_id = await _seed_instance(sessions)

    async def lock(session: AsyncSession) -> Any:
        async with session.begin():
            return await _saga_store.lock_instance(
                session, instance_id, tenant=_saga_store.ANY_TENANT
            )

    async with sessions() as first, sessions() as second:
        async with first.begin():
            held = await _saga_store.lock_instance(
                first, instance_id, tenant=_saga_store.ANY_TENANT
            )
            waiting = asyncio.create_task(lock(second))
            await asyncio.sleep(0.2)
            assert not waiting.done()
        got = await waiting
    assert held is not None and got is not None and got.id == instance_id


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_skip_locked")
async def test_a_backed_off_dead_or_exhausted_row_is_not_claimed(
    factory: Any,
) -> None:
    expired = _now() - timedelta(seconds=1)
    (backed_off,) = await _seed(
        factory,
        1,
        locked_until=expired,
        delivery_attempts=1,
        next_attempt_at=_now() + timedelta(minutes=5),
    )
    (dead,) = await _seed(factory, 1, dead_at=_now(), delivery_attempts=5)
    (exhausted,) = await _seed(factory, 1, delivery_attempts=5)
    store = sql_alchemy_outbox_store()
    for msg_id in (backed_off, dead, exhausted):
        assert await store._claim(factory, msg_id, max_attempts=5) is None
        assert (await _row(factory, msg_id)).locked_by is None
    (due,) = await _seed(
        factory, 1, delivery_attempts=1, next_attempt_at=_now() - timedelta(seconds=1)
    )
    assert await store._claim(factory, due, max_attempts=5) is not None


def test_the_batched_claim_needs_update_returning(engine: AsyncEngine) -> None:
    class _Session:
        def __init__(self, returning: bool) -> None:
            self.dialect = engine.dialect
            self._returning = returning

        def get_bind(self) -> Any:
            return SimpleNamespace(
                dialect=SimpleNamespace(update_returning=self._returning)
            )

    assert _relay._claims_batches(_Session(True))
    assert not _relay._claims_batches(_Session(False))


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_skip_locked")
async def test_a_transition_between_read_and_claim_is_not_stamped(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    instance_id = await _seed_instance(sessions)
    now = _now()
    swept_before = now - timedelta(seconds=5)
    async with sessions() as session, session.begin():
        (candidate,) = [
            d
            for d in await _saga_store.due_candidates(
                session, now=now, swept_before=swept_before, limit=10
            )
            if d.id == instance_id
        ]
    table = SagaInstance.__table__
    async with sessions() as session, session.begin():  # a step commits
        await session.execute(
            update(table).where(table.c.id == instance_id).values(version=4)
        )
    async with sessions() as session, session.begin():
        stamped = await _saga_store.stamp_due(
            session, candidate, now=now, swept_before=swept_before
        )
    assert not stamped
    assert await _swept_at(sessions, instance_id) is None
