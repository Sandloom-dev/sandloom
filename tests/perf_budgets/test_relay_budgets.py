"""PostgreSQL round-trip budgets of the outbox relay and the post-commit marks.

The relay settles a batch of 50 in a CONSTANT number of round trips (one claim
transaction + one settle transaction), where the per-row path cost ~6 per row
(perf3: 0.25 RT/row was the prototype target). The post-commit coalescer marks
any number of delivered events in ONE UPDATE.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from sandloom.core.drivers.sqlalchemy_database import create_sqlalchemy_database
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import OutboxStore
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import (
    SQLAlchemyOutboxStore,
    sql_alchemy_outbox_store,
)
from sandloom.core.tasks import relay

from .conftest import proxied_url
from .pgcount import PgCountingProxy

pytestmark = pytest.mark.asyncio

#: Ceilings (steady state, warm connection): claim txn (BEGIN, claim, COMMIT)
#: + settle txn (BEGIN, UPDATE … = ANY(:ids), COMMIT).
RELAY_BATCH_OF_50_BUDGET = 6
MARK_DELIVERED_BUDGET = 1  # one autocommit UPDATE: no BEGIN / COMMIT


@pytest_asyncio.fixture
async def proxied_factory(
    db_engine: Any, pg_proxy: PgCountingProxy
) -> AsyncIterator[Any]:
    provider = create_sqlalchemy_database(
        InstanceConfig(
            "database", "relaybudget", "sqlalchemy", {"url": proxied_url(pg_proxy)}
        )
    )
    try:
        yield provider.uow_factory()
    finally:
        await provider.aclose()


async def _seed(factory: Any, n: int) -> list[uuid.UUID]:
    base = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    ids = [uuid.uuid4() for _ in range(n)]
    async with factory() as uow:
        uow.session.add_all(
            OutboxMessage(
                id=msg_id,
                channel="core.test_event.v1",
                schema_version="v1",
                payload={"n": i},
                emitted_at=base + timedelta(microseconds=i),
            )
            for i, msg_id in enumerate(ids)
        )
    return ids


class _Bus:
    def __init__(self) -> None:
        self.published = 0

    async def publish_event(self, *_a: Any, **_k: Any) -> None:
        self.published += 1


async def _undelivered(factory: Any) -> int:
    async with factory() as uow:
        return int(
            (
                await uow.session.execute(
                    select(func.count()).where(OutboxMessage.delivered_at.is_(None))
                )
            ).scalar_one()
        )


async def test_relay_batch_of_50_budget(
    proxied_factory: Any, pg_proxy: PgCountingProxy
) -> None:
    store = sql_alchemy_outbox_store()
    await _seed(proxied_factory, 100)
    bus = _Bus()
    # Warm-up batch: prepares the claim/settle statements on the connection.
    await relay._relay_once_for_store(proxied_factory, store, bus, sweep=False)
    pg_proxy.reset()
    attempted = await relay._relay_once_for_store(
        proxied_factory, store, bus, sweep=False
    )
    trace = pg_proxy.reset()
    assert attempted == 50 and bus.published == 100
    assert await _undelivered(proxied_factory) == 0
    print(
        f"\nrelay batch of 50: {len(trace)} round trips "
        f"({len(trace) / 50:.2f}/row)\n{pg_proxy.report(trace)}"
    )
    assert len(trace) <= RELAY_BATCH_OF_50_BUDGET, pg_proxy.report(trace)


async def test_per_row_relay_reference(
    proxied_factory: Any, pg_proxy: PgCountingProxy
) -> None:
    """The per-row contract default (pre-batching relay), for the record."""

    class _PerRow(SQLAlchemyOutboxStore):
        deliver_batch = OutboxStore.deliver_batch  # type: ignore[assignment]

    store = _PerRow()
    await _seed(proxied_factory, 100)
    bus = _Bus()
    await relay._relay_once_for_store(proxied_factory, store, bus, sweep=False)
    pg_proxy.reset()
    await relay._relay_once_for_store(proxied_factory, store, bus, sweep=False)
    trace = pg_proxy.reset()
    print(
        f"\nper-row relay, 50 rows: {len(trace)} round trips ({len(trace) / 50:.2f}/row)"
    )
    assert len(trace) > RELAY_BATCH_OF_50_BUDGET * 10  # the batching matters


async def test_mark_delivered_is_one_statement_for_any_count(
    proxied_factory: Any, pg_proxy: PgCountingProxy
) -> None:
    store = sql_alchemy_outbox_store()
    ids = await _seed(proxied_factory, 200)
    await store.mark_delivered(proxied_factory, ids[:1])  # warm (prepare)
    pg_proxy.reset()
    await store.mark_delivered(proxied_factory, ids[1:])
    trace = pg_proxy.reset()
    assert await _undelivered(proxied_factory) == 0
    assert len(trace) <= MARK_DELIVERED_BUDGET, pg_proxy.report(trace)


async def test_mark_delivered_probe_fails_stays_transactional(
    proxied_factory: Any, pg_proxy: PgCountingProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLAlchemy's ``SessionTransaction._connections`` changed (probe reports
    it missing): the SESSION path of mark-delivered (factories without a
    cached AUTOCOMMIT engine view, e.g. sessions joined to a connection)
    falls back to a normal transaction — BEGIN / UPDATE / COMMIT — and still
    marks every row. The engine-view path does not use the probe."""
    from sandloom.core.sql_alchemy import driver_compat, outbox_store

    monkeypatch.setattr(outbox_store, "_autocommit_view", lambda _factory: None)

    monkeypatch.setitem(
        driver_compat.PROBES,
        driver_compat.SESSION_CONNECTIONS,
        lambda: ["simulated: SessionTransaction._connections missing"],
    )
    driver_compat.reset_driver_probes()
    try:
        store = sql_alchemy_outbox_store()
        ids = await _seed(proxied_factory, 20)
        await store.mark_delivered(proxied_factory, ids[:1])  # warm (prepare)
        pg_proxy.reset()
        await store.mark_delivered(proxied_factory, ids[1:])
        trace = pg_proxy.reset()
        labels = [" ".join(rt.sql).upper() for rt in trace]
        assert await _undelivered(proxied_factory) == 0
        assert len(trace) == 3, pg_proxy.report(trace)
        assert labels[0].startswith("BEGIN") and labels[2].startswith("COMMIT")
        assert "UPDATE OUTBOX_MESSAGES" in labels[1]
    finally:
        driver_compat.reset_driver_probes()
