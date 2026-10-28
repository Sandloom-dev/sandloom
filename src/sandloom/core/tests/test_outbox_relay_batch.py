"""Batched outbox relay (real PostgreSQL): one claim + one settle per batch.

The lease/token semantics of the per-row two-phase claim are preserved for the
whole batch: claimed rows carry the batch's token, every settle statement is
guarded by it, a broker outage burns no attempt, failures back off / dead-letter
per row, concurrent relays partition the backlog (SKIP LOCKED). Delivered rows
past the retention window are purged; dead and pending rows never are.
"""

from __future__ import annotations

import asyncio
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
from sandloom.core.infra.contracts import BrokerUnavailableError, OutboxStore
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import (
    SQLAlchemyOutboxStore,
    sql_alchemy_outbox_store,
)
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


async def _seed(factory: Any, n: int, **fields: Any) -> list[uuid.UUID]:
    base = _now() - timedelta(minutes=5)
    ids = [uuid.uuid4() for _ in range(n)]
    async with factory() as uow:
        for i, msg_id in enumerate(ids):
            uow.session.add(
                OutboxMessage(
                    id=msg_id,
                    channel="core.test_event.v1",
                    schema_version="v1",
                    payload={"n": i},
                    emitted_at=base + timedelta(milliseconds=i),
                    **fields,
                )
            )
    return ids


async def _rows(factory: Any) -> dict[uuid.UUID, OutboxMessage]:
    async with factory() as uow:
        rows = (await uow.session.execute(select(OutboxMessage))).scalars().all()
        return {r.id: r for r in rows}


class _Bus:
    def __init__(
        self, *, fail: set[int] | None = None, down_at: int | None = None
    ) -> None:
        self.fail = fail or set()
        self.down_at = down_at
        self.published: list[int] = []

    async def publish_event(
        self, channel: str, payload: dict[str, Any], *_a: Any, **_k: Any
    ) -> None:
        n = payload["n"]
        if self.down_at is not None and n == self.down_at:
            raise BrokerUnavailableError("connection refused")
        if n in self.fail:
            raise RuntimeError("poison")
        self.published.append(n)


async def _batch(factory: Any, bus: Any, *, limit: int = 50) -> Any:
    async def publish(channel: str, payload: dict, version: str, event_id: str) -> None:
        await bus.publish_event(channel, payload, version, event_id=event_id)

    return await sql_alchemy_outbox_store().deliver_batch(
        factory,
        publish,
        max_attempts=_settings.outbox_relay_max_attempts,
        grace_seconds=0,
        limit=limit,
        backoff=lambda _n: 60.0,
    )


@pytest.mark.asyncio
async def test_batch_delivers_in_emission_order(factory: Any) -> None:
    ids = await _seed(factory, 7)
    bus = _Bus()
    result = await _batch(factory, bus, limit=5)
    assert (result.claimed, result.delivered, result.full) == (5, 5, True)
    assert bus.published == [0, 1, 2, 3, 4]
    result = await _batch(factory, bus, limit=5)
    assert (result.claimed, result.delivered, result.full) == (2, 2, False)
    rows = await _rows(factory)
    assert all(rows[i].delivered_at is not None for i in ids)
    assert all(rows[i].locked_by is None and rows[i].locked_until is None for i in ids)


@pytest.mark.asyncio
async def test_failures_back_off_per_row_and_dead_letter(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_settings, "outbox_relay_max_attempts", 2)
    ids = await _seed(factory, 3)
    async with factory() as uow:  # row 2 already failed once
        # (on another channel: a failed row holds the rows behind it on ITS
        # channel — see test_outbox_relay_ordering.py)
        await uow.session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == ids[2])
            .values(delivery_attempts=1, channel="core.test_other.v1")
        )
    result = await _batch(factory, _Bus(fail={1, 2}))
    assert (result.delivered, result.failed, result.dead) == (1, 2, 1)
    assert result.dead_ids == (ids[2],)
    rows = await _rows(factory)
    assert rows[ids[0]].delivered_at is not None
    assert rows[ids[1]].delivery_attempts == 1
    assert rows[ids[1]].next_attempt_at is not None
    assert rows[ids[1]].next_attempt_at > _now()  # backed off
    assert rows[ids[1]].last_error == "poison"
    assert rows[ids[2]].dead_at is not None and rows[ids[2]].delivery_attempts == 2
    assert all(r.locked_by is None for r in rows.values())


@pytest.mark.asyncio
async def test_broker_outage_parks_one_row_and_releases_the_rest(factory: Any) -> None:
    ids = await _seed(factory, 5)
    result = await _batch(factory, _Bus(down_at=2))
    assert result.unavailable and result.delivered == 2 and result.failed == 0
    rows = await _rows(factory)
    assert [rows[i].delivered_at is not None for i in ids] == [
        True,
        True,
        False,
        False,
        False,
    ]
    assert all(r.delivery_attempts == 0 for r in rows.values())  # no budget burnt
    assert rows[ids[2]].next_attempt_at is not None  # parked briefly
    assert (rows[ids[2]].last_error or "").startswith("broker unavailable")
    for i in ids[3:]:  # same channel: held behind the parked row, no attempt
        assert rows[i].locked_by is None
        assert rows[i].next_attempt_at == rows[ids[2]].next_attempt_at


@pytest.mark.asyncio
async def test_relay_trips_circuit_on_outage_even_after_deliveries(
    factory: Any,
) -> None:
    await _seed(factory, 3)
    attempted = await relay._relay_once_for_store(
        factory, sql_alchemy_outbox_store(), _Bus(down_at=1)
    )
    assert attempted == 1
    assert relay.broker_circuit().is_open()


@pytest.mark.asyncio
async def test_settle_never_touches_a_reclaimed_lease(factory: Any) -> None:
    """A batch whose lease expired and was reclaimed settles nothing it lost."""
    ids = await _seed(factory, 2)
    stolen = uuid.uuid4()

    class _SlowBus(_Bus):
        async def publish_event(
            self, channel: str, payload: dict, *a: Any, **k: Any
        ) -> None:
            if payload["n"] == 0:
                # Meanwhile the lease of row 1 expires and another relay takes it.
                async with factory() as uow:
                    await uow.session.execute(
                        update(OutboxMessage)
                        .where(OutboxMessage.id == ids[1])
                        .values(locked_by=stolen)
                    )
            await super().publish_event(channel, payload, *a, **k)

    result = await _batch(factory, _SlowBus())
    assert (result.claimed, result.delivered, result.skipped) == (2, 1, 1)
    rows = await _rows(factory)
    assert rows[ids[0]].delivered_at is not None
    assert rows[ids[1]].delivered_at is None and rows[ids[1]].locked_by == stolen


@pytest.mark.asyncio
async def test_concurrent_relays_partition_the_backlog(factory: Any) -> None:
    await _seed(factory, 20)
    a, b = _Bus(), _Bus()
    gate = asyncio.Event()

    class _Gated(_Bus):
        async def publish_event(self, *args: Any, **kw: Any) -> None:
            publishing.set()
            await gate.wait()
            await a.publish_event(*args, **kw)

    publishing = asyncio.Event()
    first = asyncio.create_task(_batch(factory, _Gated(), limit=10))
    # First relay has claimed (committed) and is "publishing".
    await asyncio.wait_for(publishing.wait(), timeout=10)
    second = await _batch(factory, b, limit=50)
    gate.set()
    res_a = await first
    assert res_a.delivered == 10 and second.delivered == 10
    assert set(a.published).isdisjoint(b.published)
    assert sorted(a.published + b.published) == list(range(20))


@pytest.mark.asyncio
async def test_retention_purge_deletes_only_old_delivered_rows(factory: Any) -> None:
    old = _now() - timedelta(hours=200)
    [old_delivered] = await _seed(factory, 1, delivered_at=old)
    [recent] = await _seed(factory, 1, delivered_at=_now())
    [dead] = await _seed(factory, 1, dead_at=old)
    [pending] = await _seed(factory, 1)
    deleted = await relay.purge_delivered(
        factory, sql_alchemy_outbox_store(), retention_hours=168
    )
    assert deleted == 1
    assert set(await _rows(factory)) == {recent, dead, pending}
    assert old_delivered not in await _rows(factory)
    assert (
        await relay.purge_delivered(
            factory, sql_alchemy_outbox_store(), retention_hours=0
        )
        == 0
    )


@pytest.mark.asyncio
async def test_purge_cli_helper(factory: Any) -> None:
    from types import SimpleNamespace

    from sandloom.core.tasks.event_ops import outbox_purge

    await _seed(factory, 3, delivered_at=_now() - timedelta(hours=10))
    store = sql_alchemy_outbox_store()
    core = SimpleNamespace(
        database=lambda _name: SimpleNamespace(outbox=lambda: store),
        get_uow_factory=lambda _name: factory,
    )
    assert await outbox_purge(core, hours=1, batch=2) == 3
    assert await _rows(factory) == {}


@pytest.mark.asyncio
async def test_base_contract_default_batches_per_row(factory: Any) -> None:
    """A backend without its own deliver_batch keeps the per-row path."""
    await _seed(factory, 3)

    class _PerRow(SQLAlchemyOutboxStore):
        deliver_batch = OutboxStore.deliver_batch  # type: ignore[assignment]

    async def publish(channel: str, payload: dict, version: str, event_id: str) -> None:
        return None

    result = await _PerRow().deliver_batch(
        factory, publish, max_attempts=10, grace_seconds=0, limit=2
    )
    assert (result.claimed, result.delivered, result.full) == (2, 2, True)


@pytest.mark.asyncio
async def test_full_batch_skips_the_relay_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks: list[int] = []

    async def fake_tick(_core: Any) -> tuple[int, bool]:
        ticks.append(1)
        if len(ticks) >= 3:
            stop.set()
        return 50, True

    stop = asyncio.Event()
    monkeypatch.setattr(relay, "_relay_tick", fake_tick)
    monkeypatch.setattr(relay, "_database_targets", lambda _core: [object()])
    # idle_backoff/interval large: only the "full" fast path can reach 3 ticks.
    await asyncio.wait_for(
        relay._relay_loop(object(), stop, interval=30, idle_backoff=30),  # type: ignore[arg-type]
        timeout=5,
    )
    assert len(ticks) == 3


# --------------------------------------------------------------------------- #
# The post-commit mark (one autocommit UPDATE) racing the relay
#
# The dispatcher is not a lease holder: it marks ONLY ids it published itself,
# guarded by ``delivered_at IS NULL AND dead_at IS NULL``. Against a relay
# that holds a lease on the same row the outcome is harmless by construction:
# the event was published (twice: consumers dedupe on the id), the row ends
# delivered exactly once, and the relay's token-guarded settle skips it.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_post_commit_mark_racing_a_relay_lease_is_harmless(
    factory: Any,
) -> None:
    ids = await _seed(factory, 2)
    store = sql_alchemy_outbox_store()

    class _Racing(_Bus):
        async def publish_event(
            self, channel: str, payload: dict, *a: Any, **k: Any
        ) -> None:
            await super().publish_event(channel, payload, *a, **k)
            if payload["n"] == 0:
                # the post-commit dispatcher published row 0 too, and marks it
                # while the relay still holds its lease
                await store.mark_delivered(factory, [ids[0]])

    result = await _batch(factory, _Racing())
    assert (result.claimed, result.delivered, result.skipped) == (2, 1, 1)
    rows = await _rows(factory)
    assert all(rows[i].delivered_at is not None for i in ids)
    assert rows[ids[0]].delivery_attempts == 0 and rows[ids[0]].dead_at is None


@pytest.mark.asyncio
async def test_stale_mark_after_the_relay_settled_changes_nothing(
    factory: Any,
) -> None:
    ids = await _seed(factory, 1)
    await _batch(factory, _Bus())
    settled = (await _rows(factory))[ids[0]].delivered_at
    await sql_alchemy_outbox_store().mark_delivered(factory, ids)  # late mark
    assert (await _rows(factory))[ids[0]].delivered_at == settled


@pytest.mark.asyncio
async def test_post_commit_mark_never_resurrects_a_dead_letter(factory: Any) -> None:
    """A slow dispatcher's mark after the relay dead-lettered the row."""
    ids = await _seed(factory, 1, delivery_attempts=99)
    await sql_alchemy_outbox_store().dead_letter_exhausted(factory, max_attempts=3)
    await sql_alchemy_outbox_store().mark_delivered(factory, ids)
    row = (await _rows(factory))[ids[0]]
    assert row.dead_at is not None and row.delivered_at is None


@pytest.mark.asyncio
async def test_concurrent_relays_and_marks_settle_every_row_once(
    factory: Any,
) -> None:
    ids = await _seed(factory, 30)
    store = sql_alchemy_outbox_store()
    a, b = _Bus(), _Bus()
    await asyncio.gather(
        _batch(factory, a, limit=10),
        _batch(factory, b, limit=10),
        store.mark_delivered(factory, ids[::2]),  # the dispatcher, concurrently
        store.mark_delivered(factory, ids[::3]),
    )
    await _batch(factory, _Bus(), limit=50)  # whatever is left
    rows = await _rows(factory)
    assert all(r.delivered_at is not None and r.dead_at is None for r in rows.values())
    assert set(a.published).isdisjoint(b.published)  # the relays partition


@pytest.mark.asyncio
async def test_failed_mark_leaves_rows_to_the_relay(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UPDATE fails / the connection drops mid-flush: nothing is marked,
    the relay re-publishes every row under its own (stable) event id."""
    from sqlalchemy.exc import OperationalError

    from sandloom.core.sql_alchemy import outbox_store as store_mod

    ids = await _seed(factory, 3)

    def _drops() -> Any:
        raise OperationalError("UPDATE outbox_messages", {}, ConnectionResetError())

    monkeypatch.setattr(store_mod, "_mark_delivered_statement", _drops)
    with pytest.raises(OperationalError):
        await sql_alchemy_outbox_store().mark_delivered(factory, ids)
    monkeypatch.undo()
    rows = await _rows(factory)
    assert all(r.delivered_at is None for r in rows.values())
    seen: list[str] = []

    async def publish(channel: str, payload: dict, version: str, event_id: str) -> None:
        seen.append(event_id)

    result = await sql_alchemy_outbox_store().deliver_batch(
        factory, publish, max_attempts=10, grace_seconds=0, limit=10
    )
    assert result.delivered == 3 and sorted(seen) == sorted(str(i) for i in ids)
