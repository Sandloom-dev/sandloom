"""``UtcDateTime`` per database: aware values everywhere (UTC on asyncpg and SQLite).

Where the renderer states ``aware_times`` (PostgreSQL: ``timestamptz`` keeps
the instant, the DBAPI reads aware values: UTC on asyncpg, the session's
``TimeZone`` on psycopg) the type converts nothing: its
bind processor only refuses a naive time and it has no result processor of
its own (measured: ``HTr``, ``benchmarks/ladder/STANDS.md`` section D).
Elsewhere (SQLite) it binds the time in UTC and attaches UTC on load.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import asyncpg as pg_asyncpg
from sqlalchemy.ext.asyncio import AsyncEngine

from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.sql_alchemy.portable import (
    NaiveDateTimeError,
    UtcDateTime,
    aware_times,
)
from sandloom.core.testing.workers import worker_env_values

REPO = Path(__file__).resolve().parents[4]

_PLUS_TWO = timezone(timedelta(hours=2))
_INSTANT = datetime(2026, 11, 5, 12, 30, 15, 123456, tzinfo=_PLUS_TWO)


def _table() -> sa.Table:
    return sa.Table(
        "perf_utc_datetime",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("at", UtcDateTime()),
    )


def test_postgresql_states_aware_times_and_sqlite_does_not() -> None:
    assert aware_times("postgresql")
    assert not aware_times("sqlite")
    assert not aware_times("no-such-database")


@pytest.mark.parametrize(
    "dialect",
    [pg_asyncpg.dialect(), postgresql.dialect()],  # type: ignore[no-untyped-call]
    ids=["asyncpg", "psycopg2"],
)
def test_postgresql_processing_only_refuses_a_naive_time(dialect: sa.Dialect) -> None:
    kind = UtcDateTime()
    assert kind._cached_result_processor(dialect, None) is None
    bind = kind._cached_bind_processor(dialect)
    if bind is not None:
        # Bound as given: the database keeps the instant of any offset.
        assert bind(_INSTANT) is _INSTANT
        assert bind(None) is None
        with pytest.raises(NaiveDateTimeError):
            bind(datetime(2026, 1, 1))


def test_sqlite_processing_converts_to_utc() -> None:
    dialect = sa.create_engine("sqlite://").dialect
    kind = UtcDateTime()
    bind = kind._cached_bind_processor(dialect)
    result = kind._cached_result_processor(dialect, None)
    assert bind is not None
    assert result is not None


async def _round_trip(engine: AsyncEngine) -> None:
    table = _table()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.run_sync(table.metadata.create_all)
            await connection.execute(sa.insert(table).values(id=1, at=_INSTANT))
            loaded = (await connection.execute(sa.select(table.c.at))).scalar_one()
            assert loaded == _INSTANT
            assert loaded.utcoffset() == timedelta(0)
            assert loaded.tzinfo is not None
            assert loaded == _INSTANT.astimezone(UTC)
            found = await connection.execute(
                sa.select(table.c.id).where(table.c.at == _INSTANT.astimezone(UTC))
            )
            assert found.scalar_one() == 1
            with pytest.raises(sa.exc.StatementError) as raised:
                await connection.execute(
                    sa.insert(table).values(id=2, at=datetime(2026, 1, 1))
                )
            assert isinstance(raised.value.orig, NaiveDateTimeError)
            with pytest.raises(sa.exc.StatementError) as raised:
                await connection.execute(
                    sa.select(table.c.id).where(table.c.at < datetime(2026, 1, 1))
                )
            assert isinstance(raised.value.orig, NaiveDateTimeError)
        finally:
            await transaction.rollback()


@pytest_asyncio.fixture
async def pg() -> AsyncGenerator[AsyncEngine, None]:
    url = worker_env_values(REPO / ".env.test")["DATABASE_URL"]
    if not url.startswith("postgresql"):
        pytest.skip("the test database is not PostgreSQL")
    engine = driver.build_disposable_engine(url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def lite(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    engine = driver._build_engine(f"sqlite+aiosqlite:///{tmp_path / 'utc.db'}")
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_round_trip_on_postgresql(pg: AsyncEngine) -> None:
    await _round_trip(pg)


@pytest.mark.asyncio
async def test_round_trip_on_sqlite(lite: AsyncEngine) -> None:
    await _round_trip(lite)
