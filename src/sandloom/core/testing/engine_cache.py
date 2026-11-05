"""One test engine per process and event loop, kept between tests.

pytest-asyncio gives every test its own event loop unless the suite sets
``asyncio_default_test_loop_scope`` and ``asyncio_default_fixture_loop_scope``
to ``"session"``. A pooled asyncpg connection belongs to the loop that opened
it, so with per-test loops ``db_engine`` builds, resets and disposes an engine
for every test (:func:`sandloom.core.testing.database.open_test_engine`).

With one loop for the whole run (one per pytest-xdist worker) the engine and
its pool live as long as the loop: :class:`LoopEngines` keeps one per database
URL and metadata, and each test only resets the rows
(:func:`~sandloom.core.testing.database.reset_schema`). What a fresh engine gave
every test for free is restored the first time a pooled connection is checked
out in a new test:

- the session state: ``RESET ALL`` (settings, ``SET ROLE``,
  ``search_path``), session advisory locks, ``LISTEN``, open cursors,
  temporary tables;
- the prepared-statement cache, whose plans are stale once a test dropped
  and recreated a table (a schema rebuild, a migration test).

Test-only: never use this from application code.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import AsyncEngine

from sandloom.core._internal.db_provider import build_disposable_engine
from sandloom.core.sql_alchemy.base import Base
from sandloom.core.testing.connections import (
    TEST_ENGINE_MAX_OVERFLOW,
    TEST_ENGINE_POOL_SIZE,
)
from sandloom.core.testing.database import (
    assert_disposable_test_database,
    reset_schema,
)
from sandloom.core.testing.ownership import claim_database

#: A pooled connection back to how a new one starts (asyncpg's own pool runs
#: much the same on release). One simple-protocol round trip.
RESET_SESSION_SQL = (
    "CLOSE ALL; UNLISTEN *; SELECT pg_advisory_unlock_all(); DISCARD TEMP; RESET ALL"
)

_GENERATION = "sandloom_test_generation"


class LoopEngines:
    """The test engines of one event loop, reused by every test on it."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._engines: dict[tuple[str, int], AsyncEngine] = {}
        #: One per test; a connection last used in an older one is reset on
        #: its next checkout.
        self.generation = 0

    def _on_checkout(self, dbapi_connection: Any, record: Any, _proxy: Any) -> None:
        seen = record.info.get(_GENERATION)
        if seen == self.generation:
            return
        if seen is not None:  # opened in an earlier test
            dbapi_connection.run_async(lambda conn: conn.execute(RESET_SESSION_SQL))
            # SQLAlchemy's asyncpg adapter caches prepared statements per
            # connection: drop them, a table may have been recreated since.
            dbapi_connection._prepared_statement_cache.clear()
        record.info[_GENERATION] = self.generation

    def _engine(self, url: str, metadata: MetaData) -> AsyncEngine:
        key = (url, id(metadata))
        engine = self._engines.get(key)
        if engine is None:
            engine = build_disposable_engine(
                url,
                pool_size=TEST_ENGINE_POOL_SIZE,
                max_overflow=TEST_ENGINE_MAX_OVERFLOW,
            )
            event.listen(engine.sync_engine, "checkout", self._on_checkout)
            self._engines[key] = engine
        return engine

    @asynccontextmanager
    async def test_engine(
        self, database_url: str | None = None, *, metadata: MetaData | None = None
    ) -> AsyncIterator[AsyncEngine]:
        """Like :func:`~sandloom.core.testing.database.open_test_engine`, but
        the engine outlives the test: only its rows are reset."""
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("LoopEngines used outside the event loop it serves")
        url = database_url or os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set: add it to .env.test (the "
                "sandloom.core.testing plugin loads it) or export it"
            )
        assert_disposable_test_database(url)
        claim_database(url)
        if metadata is None:
            metadata = Base.metadata
        self.generation += 1
        engine = self._engine(url, metadata)
        await reset_schema(engine, metadata)
        yield engine

    async def aclose(self) -> None:
        engines, self._engines = list(self._engines.values()), {}
        for engine in engines:
            await engine.dispose()


__all__ = ["RESET_SESSION_SQL", "LoopEngines"]
