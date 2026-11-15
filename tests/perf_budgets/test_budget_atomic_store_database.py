"""Budget: the ``database`` atomic_store's hot operations are one SQL statement.

``get``, ``compare_and_set`` and ``add_or_get`` that creates the key each run
exactly one statement on the driver's engine (one round trip; the driver hints
``get`` and ``compare_and_set`` as ``one_round_trip``). ``add_or_get`` on a key
that is held reads its value in a second statement: that is the documented
price, asserted so it does not grow.

Deterministic: statements are counted with SQLAlchemy's ``before_cursor_execute``
on the provider's own engine, after ``aopen`` (which probes the table), on SQLite
(the statement shapes do not depend on the dialect).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import Table, event
from sqlalchemy.ext.asyncio import create_async_engine

from sandloom.core.drivers.database_atomic_store import DatabaseAtomicStoreProvider
from sandloom.core.sql_alchemy.models.atomic_store import InfraAtomicStore


class _Counter:
    def __init__(self, provider: DatabaseAtomicStoreProvider) -> None:
        engine = provider._engine
        assert engine is not None
        self.statements: list[str] = []
        event.listen(engine.sync_engine, "before_cursor_execute", self._record)

    def _record(self, conn, cursor, statement, *args) -> None:  # type: ignore[no-untyped-def]
        self.statements.append(statement)

    def take(self) -> int:
        count, self.statements = len(self.statements), []
        return count


@pytest_asyncio.fixture
async def provider(tmp_path: Path) -> AsyncIterator[DatabaseAtomicStoreProvider]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        table: Table = InfraAtomicStore.__table__  # type: ignore[assignment]
        await conn.run_sync(table.create)
    await engine.dispose()
    store = DatabaseAtomicStoreProvider(
        name="security", driver="database", options={"url": url}
    )
    await store.aopen()
    try:
        yield store
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_get_is_one_statement(provider: DatabaseAtomicStoreProvider) -> None:
    await provider.set("k", "v")
    counter = _Counter(provider)
    assert await provider.get("k") == "v"
    assert await provider.get("absent") is None
    assert counter.take() == 2  # one each


@pytest.mark.asyncio
async def test_compare_and_set_is_one_statement(
    provider: DatabaseAtomicStoreProvider,
) -> None:
    await provider.set("k", "a")
    counter = _Counter(provider)
    assert await provider.compare_and_set("k", "a", "b") is True
    assert counter.take() == 1
    assert await provider.compare_and_set("k", "a", "c") is False
    assert counter.take() == 1


@pytest.mark.asyncio
async def test_add_or_get_that_creates_is_one_statement_and_a_held_key_two(
    provider: DatabaseAtomicStoreProvider,
) -> None:
    counter = _Counter(provider)
    assert await provider.add_or_get("k", "first") == (True, None)
    assert counter.take() == 1
    assert await provider.add_or_get("k", "second") == (False, "first")
    assert counter.take() == 2
