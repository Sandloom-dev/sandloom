"""How the test harness builds and empties a database's schema, per database.

:func:`sandloom.core.testing.database.reset_schema` leaves every table of a
metadata present and empty before each test. The SQL that does it is a
database's own (PostgreSQL's extensions, ``row_security``, ``TRUNCATE …
RESTART IDENTITY`` and ``pg_sequences``; SQLite's ``sqlite_sequence``), so it
lives here, beside the renderers, and the harness asks
:func:`schema_reset_for` (``design/infra-agnostic/spec.md``, slice 8c).

* :class:`SchemaReset`, the plain path: ``drop_all`` + ``create_all``, then a
  ``DELETE`` of every table children-first. It serves any database
  SQLAlchemy can create a schema on.
* :class:`SqliteSchemaReset`: the plain path, plus ``sqlite_sequence`` (an
  ``AUTOINCREMENT`` table's counter) emptied with the tables. The ``db``
  fixture's session and the sessions a test opens itself begin their
  transaction at their first write (:meth:`SchemaReset.unit_engine`,
  :meth:`SchemaReset.caller_engine`; the driver's ``BEGIN_AT_FIRST_WRITE``):
  until then each read sees the last commit and holds no lock, as a
  PostgreSQL test transaction under READ COMMITTED, so the application's own
  units commit beside it. Its first write takes the database's one write
  lock: a nested unit of the same task then joins it as a SAVEPOINT.
* :class:`PostgresqlSchemaReset`: the extensions the schemas use, a probe of
  the tables that hold a row (``row_security = off``, so a policy never hides
  one), ``DELETE`` children-first or ``TRUNCATE`` for a large table, and every
  owned sequence rewound. Each reset transaction waits at most
  :data:`PG_RESET_LOCK_TIMEOUT` for a lock.

Test-only: the application never calls it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import MetaData, delete, func, literal, select, text, union_all
from sqlalchemy.ext.asyncio import AsyncEngine

from sandloom.core.drivers.dbapi_aiosqlite import BEGIN_AT_FIRST_WRITE

#: The extensions the framework's PostgreSQL schemas use.
PG_EXTENSIONS = ("uuid-ossp", "citext", "pg_trgm")

#: How long a reset statement waits for a lock another session holds (another
#: run's open transaction on a database shared by mistake): it then fails
#: with ``lock_timeout`` instead of waiting without bound (risk fix 1).
PG_RESET_LOCK_TIMEOUT = "30s"
_SET_LOCK_TIMEOUT = f"SET LOCAL lock_timeout = '{PG_RESET_LOCK_TIMEOUT}'"
_LOCK_NOT_AVAILABLE = "55P03"

_OWNED_SEQUENCES = """
SELECT quote_ident(sn.nspname) || '.' || quote_ident(s.relname)
  FROM pg_class s
  JOIN pg_namespace sn ON sn.oid = s.relnamespace
  JOIN pg_depend d ON d.objid = s.oid AND d.classid = 'pg_class'::regclass
                  AND d.deptype IN ('a', 'i')
 WHERE s.relkind = 'S' AND d.refobjid = ANY(CAST(:tables AS regclass[]))
"""

_REWIND_SEQUENCES = (
    "SELECT setval(CAST(format('%I.%I', schemaname, sequencename)"
    " AS regclass), start_value, false) FROM pg_sequences"
    " WHERE last_value IS NOT NULL AND"
    " CAST(format('%I.%I', schemaname, sequencename) AS regclass)"
    " = ANY(CAST(:seqs AS regclass[]))"
)


def qualified_tables(engine: AsyncEngine, metadata: MetaData) -> list[str]:
    """The quoted names of ``metadata``'s tables, parents first."""
    fmt = engine.dialect.identifier_preparer.format_table
    return [fmt(t) for t in metadata.sorted_tables]


class SchemaReset:
    """The plain path (module doc): any database ``create_all`` serves."""

    async def rebuild(self, engine: AsyncEngine, metadata: MetaData) -> None:
        """Drop and create every table of ``metadata``."""
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
            await conn.run_sync(metadata.create_all)

    async def owned_sequences(
        self, engine: AsyncEngine, metadata: MetaData
    ) -> list[str]:
        """The sequences the tables own (rewound by :meth:`empty_dirty`)."""
        return []

    async def empty_dirty(
        self,
        engine: AsyncEngine,
        metadata: MetaData,
        sequences: list[str],
        *,
        row_limit: int,
    ) -> None:
        """Empty every table that holds a row (here: every table)."""
        await self.empty_all(engine, metadata)

    async def empty_all(self, engine: AsyncEngine, metadata: MetaData) -> None:
        """Empty every table, children first."""
        async with engine.begin() as conn:
            for table in reversed(metadata.sorted_tables):
                await conn.execute(delete(table))
            await self._after_delete(conn)

    async def _after_delete(self, conn: Any) -> None:
        """What else a database keeps per table (its id counters)."""

    def is_lock_timeout(self, error: BaseException) -> bool:
        """Whether ``error`` is a reset statement giving up on another
        session's lock (the harness then fails at once instead of falling
        back to a reset that waits again)."""
        return False

    def caller_engine(self, engine: AsyncEngine) -> AsyncEngine:
        """The engine of the sessions a test opens itself (``db_engine``)."""
        return engine

    def unit_engine(self, engine: AsyncEngine) -> AsyncEngine:
        """The engine of the ``db`` fixture's session, which stands in for
        the request's unit."""
        return engine


class SqliteSchemaReset(SchemaReset):
    """SQLite: the plain path; ``AUTOINCREMENT`` counters start again; the
    harness's sessions begin their transaction at their first write."""

    def caller_engine(self, engine: AsyncEngine) -> AsyncEngine:
        return engine.execution_options(**{BEGIN_AT_FIRST_WRITE: True})

    def unit_engine(self, engine: AsyncEngine) -> AsyncEngine:
        # begun at its first write too: a ``db`` that only read holds no
        # lock and sees every commit, as on PostgreSQL; its first write takes
        # the lock, and a nested unit of the task then joins it
        return self.caller_engine(engine)

    async def _after_delete(self, conn: Any) -> None:
        exists = await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'")
        )
        if exists.first() is not None:
            await conn.execute(text("DELETE FROM sqlite_sequence"))


class PostgresqlSchemaReset(SchemaReset):
    """PostgreSQL (module doc)."""

    def is_lock_timeout(self, error: BaseException) -> bool:
        """SQLSTATE ``55P03`` (``lock_not_available``: ``lock_timeout``)."""
        orig = getattr(error, "orig", error)
        code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        return code == _LOCK_NOT_AVAILABLE

    async def rebuild(self, engine: AsyncEngine, metadata: MetaData) -> None:
        async with engine.begin() as conn:
            await conn.execute(text(_SET_LOCK_TIMEOUT))
            for ext in PG_EXTENSIONS:
                await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{ext}"'))
            await conn.run_sync(metadata.drop_all)
            await conn.run_sync(metadata.create_all)

    async def owned_sequences(
        self, engine: AsyncEngine, metadata: MetaData
    ) -> list[str]:
        tables = qualified_tables(engine, metadata)
        if not tables:
            return []
        async with engine.connect() as conn:
            rows = await conn.execute(text(_OWNED_SEQUENCES), {"tables": tables})
            return [str(name) for (name,) in rows]

    async def empty_dirty(
        self,
        engine: AsyncEngine,
        metadata: MetaData,
        sequences: list[str],
        *,
        row_limit: int,
    ) -> None:
        """Empty the tables that hold a row; rewind every used sequence.

        ``metadata.sorted_tables`` is in dependency order (parents first).
        Small dirty tables are emptied with ``DELETE`` children-first (no new
        files, unlike ``TRUNCATE``, whose ``CASCADE`` also rewrites every
        referencing table); a table holding more than ``row_limit`` rows is
        truncated. Any error (a row-security policy, a foreign key from
        outside the metadata) propagates and the caller empties everything.
        """
        sorted_tables = metadata.sorted_tables
        tables = qualified_tables(engine, metadata)
        probe = union_all(
            *(
                select(literal(n), func.count())
                .select_from(
                    select(literal(1)).select_from(t).limit(row_limit + 1).subquery()
                )
                .having(func.count() > 0)
                for n, t in enumerate(sorted_tables)
            )
        )
        async with engine.begin() as conn:
            # Never let a row-level-security policy hide a row from the probe
            # (or spare one from the DELETE): with row_security off, a
            # statement RLS would filter fails instead.
            await conn.execute(text("SET LOCAL row_security = off"))
            await conn.execute(text(_SET_LOCK_TIMEOUT))
            rows = {int(n): int(c) for n, c in await conn.execute(probe)}
            big = sorted(n for n, c in rows.items() if c > row_limit)
            if big:
                await conn.execute(
                    text(
                        f"TRUNCATE {', '.join(tables[n] for n in big)} "
                        "RESTART IDENTITY CASCADE"
                    )
                )
            for n in sorted(set(rows) - set(big), reverse=True):  # children first
                await conn.execute(delete(sorted_tables[n]))
            if sequences:
                # DELETE keeps sequences where they are, and a rolled-back
                # insert into a table that is empty now advanced one too:
                # rewind every used one to its start (RESTART IDENTITY's work).
                await conn.execute(text(_REWIND_SEQUENCES), {"seqs": sequences})

    async def empty_all(self, engine: AsyncEngine, metadata: MetaData) -> None:
        tables = qualified_tables(engine, metadata)
        async with engine.begin() as conn:
            await conn.execute(text(_SET_LOCK_TIMEOUT))
            await conn.execute(
                text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
            )


_RESETS: dict[str, SchemaReset] = {
    "postgresql": PostgresqlSchemaReset(),
    "sqlite": SqliteSchemaReset(),
}
_PLAIN = SchemaReset()


def schema_reset_for(engine: AsyncEngine) -> SchemaReset:
    """The schema reset of ``engine``'s database (the plain path for one
    without its own)."""
    return _RESETS.get(engine.dialect.name, _PLAIN)


__all__ = [
    "PG_EXTENSIONS",
    "PG_RESET_LOCK_TIMEOUT",
    "PostgresqlSchemaReset",
    "SchemaReset",
    "SqliteSchemaReset",
    "qualified_tables",
    "schema_reset_for",
]
