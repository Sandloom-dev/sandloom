"""The relational purge watermark (``projection_watermarks``, spec §6.4
item 5) on real PostgreSQL: monotonic under racing purges, the rebuild flag
visible to a concurrent purge, never lowered by a rebuild."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sandloom.core.projections.operations import watermark_store
from sandloom.core.runtime import use_active_core
from sandloom.core.sql_alchemy.models.projection_watermark import ProjectionWatermark
from sandloom.core.sql_alchemy.projection_watermarks import RelationalWatermarkStore
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.testing.fixtures import _configured_driver, _required_env
from sandloom.core.testing.infra import override_instance
from sandloom.core.tests.test_projection_kernel import (
    a_rebuild_never_enters_a_running_purge,
    concurrent_rebuilds_keep_the_purge_excluded,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store(db_engine: Any) -> AsyncIterator[RelationalWatermarkStore]:
    async with db_engine.begin() as conn:
        await conn.run_sync(
            ProjectionWatermark.metadata.create_all,
            tables=[ProjectionWatermark.__table__],
        )
        await conn.execute(text("DELETE FROM projection_watermarks"))
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def open_unit() -> AsyncIterator[AsyncSession]:
        async with maker() as session, session.begin():
            yield session

    yield RelationalWatermarkStore(open_unit)


async def test_the_watermark_only_ever_rises(store: RelationalWatermarkStore) -> None:
    early, late = datetime(2026, 1, 1), datetime(2026, 6, 1)
    assert (await store.get("p")).purged_before is None
    assert await store.raise_purged_before("p", late) == late
    assert await store.raise_purged_before("p", early) == late  # never lowered
    results = await asyncio.gather(
        *(store.raise_purged_before("p", datetime(2026, m, 1)) for m in range(1, 13))
    )
    assert max(results) == datetime(2026, 12, 1)
    assert (await store.get("p")).purged_before == datetime(2026, 12, 1)


async def test_the_rebuild_flag_is_set_and_cleared_without_touching_the_mark(
    store: RelationalWatermarkStore,
) -> None:
    await store.raise_purged_before("q", datetime(2026, 3, 1))
    await store.begin_rebuild("q", datetime(2026, 10, 26, 12))
    mark = await store.get("q")
    assert mark.rebuild_started_at == datetime(2026, 10, 26, 12)
    assert mark.purged_before == datetime(2026, 3, 1)
    await store.end_rebuild("q")
    mark = await store.get("q")
    assert mark.rebuild_started_at is None
    assert mark.purged_before == datetime(2026, 3, 1)


async def test_concurrent_rebuilds_keep_the_purge_excluded_in_the_table(
    store: RelationalWatermarkStore,
) -> None:
    """G-MSTORE-09: the relational marker counts running rebuilds; the
    first one to finish never clears the exclusion of another."""
    await concurrent_rebuilds_keep_the_purge_excluded(store)


async def test_racing_rebuild_marks_are_counted_exactly(
    store: RelationalWatermarkStore,
) -> None:
    """Begins and ends racing on the row (each its own unit) never lose a
    count: the purge stays refused until the last rebuild ended."""
    at = datetime(2026, 10, 26, 12)
    await asyncio.gather(*(store.begin_rebuild("s", at) for _ in range(8)))
    await asyncio.gather(*(store.end_rebuild("s") for _ in range(7)))
    assert (await store.get("s")).rebuild_started_at == at
    await store.end_rebuild("s")
    assert (await store.get("s")).rebuild_started_at is None


def _advisory_waiter(db_engine: Any) -> Callable[[], Awaitable[bool]]:
    """Whether a backend of this test database waits on an advisory lock."""

    async def entry_waits() -> bool:
        async with db_engine.connect() as conn:
            waiting = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks l JOIN pg_database d "
                    "ON d.oid = l.database WHERE l.locktype = 'advisory' "
                    "AND NOT l.granted AND d.datname = current_database()"
                )
            )
            return int(waiting.scalar_one()) > 0

    return entry_waits


async def test_a_rebuild_never_enters_a_running_purge_in_the_table(
    store: RelationalWatermarkStore, db_engine: Any
) -> None:
    """Round 3 (G-MSTORE-09) on PostgreSQL: the purge reads "no rebuild",
    a rebuild begins, the purge raises and deletes. The rebuild's begin
    waits on the purge's per-projection lock until the delete is done."""
    await a_rebuild_never_enters_a_running_purge(store, _advisory_waiter(db_engine))


async def test_the_purge_window_holds_through_the_operations_units(
    store: RelationalWatermarkStore, sandloom_core: Any, db_engine: Any
) -> None:
    """The same race through the store ``sandloom projection purge`` uses
    (``db_session`` units, the raise and the delete nested in the window)."""
    async with AsyncExitStack() as stack:
        stack.enter_context(use_active_core(sandloom_core))
        await stack.enter_async_context(
            override_instance(
                sandloom_core,
                "database",
                driver=_configured_driver(sandloom_core, "database", "sqlalchemy"),
                url=_required_env("DATABASE_URL"),
            )
        )
        await a_rebuild_never_enters_a_running_purge(
            watermark_store("default"), _advisory_waiter(db_engine)
        )
