"""The test engine kept on the session loop (``sandloom.core.testing.engine_cache``).

The suite runs every async test on one loop per process, so ``db_engine`` keeps
one engine and pool for the run and resets only the rows. A pooled connection
then serves many tests: whatever one test left on its database session must be
gone when the next test checks it out, as it was when every test opened new
connections: settings and role, session advisory locks, ``LISTEN``,
temporary tables, and prepared statements a recreated table made stale.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from sandloom.core.testing.connections import (
    TEST_ENGINE_MAX_OVERFLOW,
    TEST_ENGINE_POOL_SIZE,
)
from sandloom.core.testing.fixtures import *  # noqa: F403 - db_engine and its kept engines
from sandloom.core.testing.fixtures import runs_on_session_loop

_PROBE = "loop_engines_probe"


@pytest.fixture(autouse=True)
def _needs_session_loop(request: pytest.FixtureRequest) -> None:
    if not runs_on_session_loop(request.node):
        pytest.skip("the suite runs with a loop per test: no engine is kept")


async def test_db_engine_is_the_kept_engine_of_the_session_loop(
    db_engine: Any, _sandloom_loop_engines: Any
) -> None:
    assert db_engine in _sandloom_loop_engines._engines.values()
    async with _sandloom_loop_engines.test_engine() as again:
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
    _sandloom_loop_engines: Any,
) -> None:
    engines = _sandloom_loop_engines
    async with engines.test_engine() as engine:  # one test
        async with engine.connect() as conn:
            pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar()
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
