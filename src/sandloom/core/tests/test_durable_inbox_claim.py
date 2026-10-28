"""DurableConsumerInbox.claim must only read ITS OWN conflict as a duplicate (M5).

Real PostgreSQL. The old implementation flushed the whole session inside the
claim savepoint, so a constraint violation in the handler's pending objects was
reported as "duplicate → skip" and the side effect was silently lost.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core.sql_alchemy.durable_inbox import DurableConsumerInbox
from sandloom.core.sql_alchemy.models.consumer_inbox import ConsumerInboxEntry
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.testing import reload_test_env


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured for tests")
    eng: AsyncEngine = create_async_engine(database_url, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: OutboxMessage.__table__.drop(c, checkfirst=True))
        await conn.run_sync(
            lambda c: ConsumerInboxEntry.__table__.create(c, checkfirst=True)
        )
        await conn.run_sync(
            lambda c: OutboxMessage.__table__.create(c, checkfirst=True)
        )
        await conn.execute(text("DELETE FROM consumer_inbox"))
    yield async_sessionmaker(bind=eng, expire_on_commit=False)
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM consumer_inbox"))
        await conn.execute(text("DELETE FROM outbox_messages"))
    await eng.dispose()


@pytest.mark.asyncio
async def test_duplicate_claim_is_skipped(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    inbox = DurableConsumerInbox("test@dup")
    async with sessionmaker() as s, s.begin():
        assert await inbox.claim(s, "e1") is True
    async with sessionmaker() as s, s.begin():
        assert await inbox.claim(s, "e1") is False
        # The surrounding transaction stays usable after a duplicate.
        assert await inbox.claim(s, "e2") is True


@pytest.mark.asyncio
async def test_handler_constraint_violation_is_not_read_as_duplicate(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    existing = uuid.uuid4()
    async with sessionmaker() as s, s.begin():
        s.add(
            OutboxMessage(
                id=existing, channel="core.x.v1", payload={}, schema_version="v1"
            )
        )

    inbox = DurableConsumerInbox("test@own-violation")
    async with sessionmaker() as s:
        with pytest.raises(IntegrityError):
            async with s.begin():
                # The handler's own pending write violates a PK constraint ...
                s.add(
                    OutboxMessage(
                        id=existing,
                        channel="core.x.v1",
                        payload={},
                        schema_version="v1",
                    )
                )
                # ... the claim must NOT report it as "already processed".
                assert await inbox.claim(s, "e-own") is True
                await s.flush()  # the handler's violation surfaces -> rollback

    # Rolled back as a whole: the event stays claimable for its redelivery.
    async with sessionmaker() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(ConsumerInboxEntry)
            .where(ConsumerInboxEntry.consumer == "test@own-violation")
        )
        assert count == 0


@pytest.mark.asyncio
async def test_claims_reuse_one_prebuilt_statement_per_dialect(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom.core.sql_alchemy import durable_inbox

    async with sessionmaker() as s, s.begin():
        assert await DurableConsumerInbox("test@a").claim(s, "e1") is True
        stmt = durable_inbox._claim_statements["postgresql"]
        # another consumer / event: the same statement, other bound values
        assert await DurableConsumerInbox("test@b").claim(s, "e1") is True
        assert await DurableConsumerInbox("test@a").claim(s, "e1") is False
        assert durable_inbox._claim_statements["postgresql"] is stmt
    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(ConsumerInboxEntry.consumer, ConsumerInboxEntry.event_id)
                .where(ConsumerInboxEntry.consumer.in_(["test@a", "test@b"]))
                .order_by(ConsumerInboxEntry.consumer)
            )
        ).all()
    assert [tuple(r) for r in rows] == [("test@a", "e1"), ("test@b", "e1")]


async def _seed_claims(
    sessionmaker: async_sessionmaker[AsyncSession], consumer: str, ages_days: list[int]
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    async with sessionmaker() as s, s.begin():
        s.add_all(
            ConsumerInboxEntry(
                consumer=consumer,
                event_id=f"e{i}",
                processed_at=now - timedelta(days=age),
            )
            for i, age in enumerate(ages_days)
        )


async def _remaining(
    sessionmaker: async_sessionmaker[AsyncSession], consumer: str
) -> list[str]:
    async with sessionmaker() as s:
        rows = await s.scalars(
            select(ConsumerInboxEntry.event_id)
            .where(ConsumerInboxEntry.consumer == consumer)
            .order_by(ConsumerInboxEntry.event_id)
        )
        return list(rows)


@pytest.mark.asyncio
async def test_purge_with_limit_deletes_old_claims_in_bounded_batches(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_claims(sessionmaker, "test@purge", [40, 40, 40, 1])
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
    async with sessionmaker() as s, s.begin():
        assert (
            await DurableConsumerInbox.purge_processed_before(s, cutoff, limit=2) == 2
        )
    async with sessionmaker() as s, s.begin():
        assert (
            await DurableConsumerInbox.purge_processed_before(s, cutoff, limit=2) == 1
        )
        assert (
            await DurableConsumerInbox.purge_processed_before(s, cutoff, limit=0) == 0
        )
    # Only the recent claim survives: a late duplicate of it is still rejected.
    assert await _remaining(sessionmaker, "test@purge") == ["e3"]


@pytest.mark.asyncio
async def test_inbox_purge_cli_loops_batches_in_separate_units_of_work(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom.core.cli.events import inbox_purge

    await _seed_claims(sessionmaker, "test@cli", [40] * 5 + [1])
    units = 0

    class _Uow:
        async def __aenter__(self) -> _Uow:
            nonlocal units
            units += 1
            self.session = sessionmaker()
            await self.session.begin()
            return self

        async def __aexit__(self, *exc: object) -> None:
            await self.session.commit()
            await self.session.close()

    week = SimpleNamespace(event_stream_retention_seconds=7 * 86400)
    core = SimpleNamespace(get_uow_factory=lambda _name: _Uow, settings=week)
    assert await inbox_purge(core, days=30, batch=2) == 5
    # 2 + 2 + 1: the short last batch ends the loop; each batch committed alone.
    assert units == 3
    assert await _remaining(sessionmaker, "test@cli") == ["e5"]
    with pytest.raises(SystemExit):
        await inbox_purge(core, days=30, batch=0)


@pytest.mark.asyncio
async def test_inbox_purge_inside_the_redelivery_window_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A claim purged while its event can still be redelivered (stream
    retention + 1 day) would let it apply twice: refused unless forced."""
    from sandloom.core.cli.events import inbox_purge

    await _seed_claims(sessionmaker, "test@window", [5])

    class _Uow:
        async def __aenter__(self) -> _Uow:
            self.session = sessionmaker()
            await self.session.begin()
            return self

        async def __aexit__(self, *exc: object) -> None:
            await self.session.commit()
            await self.session.close()

    week = SimpleNamespace(event_stream_retention_seconds=7 * 86400)
    core = SimpleNamespace(get_uow_factory=lambda _name: _Uow, settings=week)
    with pytest.raises(SystemExit, match="EVENT_STREAM_RETENTION_SECONDS"):
        await inbox_purge(core, days=7.5, batch=10)
    assert await _remaining(sessionmaker, "test@window") == ["e0"]
    unbounded = SimpleNamespace(event_stream_retention_seconds=0)
    with pytest.raises(SystemExit, match="unbounded"):
        await inbox_purge(
            SimpleNamespace(get_uow_factory=lambda _name: _Uow, settings=unbounded),
            days=365,
        )
    assert await inbox_purge(core, days=3, batch=10, force=True) >= 1
    assert await _remaining(sessionmaker, "test@window") == []


def test_consumer_inbox_declares_the_purge_index() -> None:
    indexes = {ix.name: ix for ix in ConsumerInboxEntry.__table__.indexes}
    ix = indexes["ix_consumer_inbox_processed_at"]
    assert [c.name for c in ix.columns] == ["processed_at"]
