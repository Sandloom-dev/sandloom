"""The test engine kept on the session loop (``sandloom.core.testing.database.LoopEngines``).

The suite runs every async test on one loop per process, so ``db_engine`` keeps
one engine and pool for the run and resets only the rows. A pooled connection
then serves many tests: whatever one test left on its database session must be
gone when the next test checks it out, as it was when every test opened new
connections: settings and role, session advisory locks, ``LISTEN``,
temporary tables, and prepared statements a recreated table made stale.

On SQLite (no reset statement) a kept connection is opened anew for the next
test, and a test's own sessions get the caller engine, begun at their first
write, as from a per-test engine: a read beside the ``db`` fixture's write
lock is never refused as a second writer of the task (SANDLOOM-R016).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, func, insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from sandloom.core.sql_alchemy.portable import serial_writers
from sandloom.core.testing.connections import (
    TEST_ENGINE_MAX_OVERFLOW,
    TEST_ENGINE_POOL_SIZE,
)
from sandloom.core.testing.database import (
    LoopEngines,
    loop_engines,
    open_test_session,
)
from sandloom.core.testing.fixtures import *  # noqa: F403 - db_engine and its kept engines
from sandloom.core.testing.fixtures import runs_on_session_loop

_PROBE = "loop_engines_probe"


@pytest.fixture(autouse=True)
def _needs_session_loop(request: pytest.FixtureRequest) -> None:
    if not runs_on_session_loop(request.node):
        pytest.skip("the suite runs with a loop per test: no engine is kept")


def _engines() -> LoopEngines:
    return loop_engines(asyncio.get_running_loop())


async def test_db_engine_is_the_kept_engine_of_the_session_loop(
    db_engine: Any,
) -> None:
    assert db_engine in _engines()._callers.values()
    async with _engines().test_engine() as again:
        assert again is db_engine  # the next test gets the same engine


async def _connection_with_pid(engine: Any, pid: int) -> Any:
    """Check pooled connections out until the one of backend ``pid`` comes."""
    held: list[Any] = []
    try:
        for _ in range(TEST_ENGINE_POOL_SIZE + TEST_ENGINE_MAX_OVERFLOW):
            conn = await engine.connect()
            found = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar()
            if found == pid:
                return conn, held
            held.append(conn)
        raise AssertionError(f"backend {pid} is no longer pooled")
    except BaseException:
        for conn in held:
            await conn.close()
        raise


async def test_a_pooled_connection_starts_the_next_test_clean(
    db_engine: Any,
) -> None:
    if db_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL's session state (a SQLite run: see below)")
    engines = _engines()
    async with engines.test_engine() as engine:  # one test
        async with engine.connect() as conn:
            pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await conn.execute(text(f"DROP TABLE IF EXISTS {_PROBE}"))
            await conn.execute(text(f"CREATE TABLE {_PROBE} (a int)"))
            await conn.execute(text(f"INSERT INTO {_PROBE} VALUES (1)"))
            # a prepared statement the connection caches
            assert (await conn.execute(text(f"SELECT * FROM {_PROBE}"))).all()
            await conn.execute(text("SET search_path TO pg_catalog"))
            await conn.execute(text("SELECT set_config('sandloom.probe', 'x', false)"))
            await conn.execute(text("SELECT pg_advisory_lock(424242)"))
            await conn.execute(text("CREATE TEMP TABLE loop_engines_temp (a int)"))
            await conn.execute(text("LISTEN loop_engines_channel"))
            await conn.commit()
        # another connection recreates the table with other columns
        async with engine.begin() as other:
            await other.execute(text(f"DROP TABLE public.{_PROBE}"))
            await other.execute(text(f"CREATE TABLE public.{_PROBE} (a text, b int)"))
            await other.execute(text(f"INSERT INTO public.{_PROBE} VALUES ('y', 2)"))

    try:
        async with engines.test_engine() as engine:  # the next test
            conn, held = await _connection_with_pid(engine, pid)
            try:
                state = (
                    await conn.execute(
                        text(
                            "SELECT current_setting('search_path'),"
                            " current_setting('sandloom.probe', true),"
                            " (SELECT count(*) FROM pg_locks WHERE"
                            "  locktype = 'advisory' AND pid = pg_backend_pid()),"
                            " to_regclass('pg_temp.loop_engines_temp'),"
                            " (SELECT count(*) FROM pg_listening_channels())"
                        )
                    )
                ).one()
                assert state[0] == '"$user", public'
                assert state[1] in (None, "")
                assert state[2:] == (0, None, 0)
                # the cached plan of the old table is gone, not reused
                rows = (await conn.execute(text(f"SELECT * FROM {_PROBE}"))).all()
                assert [tuple(r) for r in rows] == [("y", 2)]
            finally:
                await conn.close()
                for other in held:
                    await other.close()
    finally:
        async with engine.begin() as cleanup:
            await cleanup.execute(text(f"DROP TABLE IF EXISTS public.{_PROBE}"))


# --------------------------------------------------------------------------- #
# SQLite: serial writers
# --------------------------------------------------------------------------- #

_SQLITE_PROBE = MetaData()
_sqlite_probe = Table("loop_engines_sqlite_probe", _SQLITE_PROBE, Column("a", Integer))


def _needs_serial_writers(engine: Any) -> None:
    if not serial_writers(engine.dialect.name):
        pytest.skip("the test database has concurrent writers (PostgreSQL)")


async def test_a_tests_own_session_reads_beside_the_db_write_lock(
    db_engine: Any,
) -> None:
    """Two tests in a row on the kept engine. In each, the ``db`` fixture's
    session writes (it holds the database's one write lock for the test),
    then the test reads through a session of its own on ``db_engine``, in the
    same task. Begun at its first write, the read holds no lock; a
    ``BEGIN IMMEDIATE`` would be a second writer of the task: R016."""
    _needs_serial_writers(db_engine)
    engines = _engines()
    for n in range(2):  # one test, then the next
        async with engines.test_engine(metadata=_SQLITE_PROBE) as engine:
            async with open_test_session(engine) as db:  # the ``db`` fixture
                await db.execute(insert(_sqlite_probe).values(a=n))
                own = async_sessionmaker(engine)
                async with own() as session:
                    count = select(func.count()).select_from(_sqlite_probe)
                    assert (await session.execute(count)).scalar_one() == 0


async def test_a_kept_sqlite_connection_is_opened_anew_for_the_next_test(
    db_engine: Any,
) -> None:
    """SQLite has no reset statement: a connection an earlier test used is
    replaced, so nothing it left (a temporary table here) reaches the next."""
    _needs_serial_writers(db_engine)
    engines = _engines()
    async with engines.test_engine(metadata=_SQLITE_PROBE) as engine:  # one test
        async with engine.connect() as conn:
            await conn.execute(text("CREATE TEMP TABLE loop_engines_temp (a int)"))
            await conn.commit()
    async with engines.test_engine(metadata=_SQLITE_PROBE) as engine:  # the next
        held = [
            await engine.connect()
            for _ in range(TEST_ENGINE_POOL_SIZE + TEST_ENGINE_MAX_OVERFLOW)
        ]
        try:
            for conn in held:
                temp = await conn.execute(
                    text("SELECT count(*) FROM sqlite_temp_master")
                )
                assert temp.scalar_one() == 0
        finally:
            for conn in held:
                await conn.close()
