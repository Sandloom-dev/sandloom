"""The built-in SQL dialect renderers: PostgreSQL and SQLite.

A renderer writes the portable forms of ``sandloom.sdk.data``
(:mod:`sandloom.core.sql_alchemy.portable`) in one database's SQL. These two
are named in the ``sandloom.sql_dialects`` entry-point group of the
``sandloom`` package; a driver package for another database ships its own
renderer the same way. This is the only place the forms meet a dialect.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import String, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TypeEngine

from sandloom.core.sql_alchemy.portable import ConflictInsert, ErrorKind


def _causes(exc: BaseException) -> list[BaseException]:
    """``exc``, the DBAPI error SQLAlchemy wraps, and their causes."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or any(current is s for s in seen):
            continue
        seen.append(current)
        pending.extend([getattr(current, "orig", None), current.__cause__])
    return seen


#: PostgreSQL SQLSTATE codes and what they mean to the kernel.
_PG_SQLSTATES: dict[str, ErrorKind] = {
    "23505": "unique_violation",
    "40001": "serialization_failure",
    "40P01": "deadlock",
    "55P03": "lock_not_available",
    "57P01": "connection_lost",
    "57P02": "connection_lost",
    "57P03": "connection_lost",
}


__all__ = ["POSTGRESQL", "SQLITE", "PostgresqlSql", "SqliteSql"]


class PostgresqlSql:
    """PostgreSQL: ``citext``, ``WHERE`` indexes, ``pg_trgm`` GIN, advisory locks."""

    contract_version = 2
    name = "postgresql"
    #: The oldest PostgreSQL release still supported by its vendor.
    minimum_server_version: tuple[int, ...] | None = (14,)

    def features(self) -> frozenset[str]:
        return frozenset(
            {
                "skip_locked",
                "transaction_locks",
                "snapshot_reads",
                "readonly_transactions",
            }
        )

    def classify_error(self, exc: BaseException) -> ErrorKind:
        for error in _causes(exc):
            code = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
            if isinstance(code, str):
                if code in _PG_SQLSTATES:
                    return _PG_SQLSTATES[code]
                if code.startswith("08"):
                    return "connection_lost"
        return "other"

    def case_insensitive_text(self, length: int | None) -> TypeEngine[Any]:
        # citext has no length; a cap, where a model sets one, is the model's
        # validation, not the column's.
        return postgresql.CITEXT()

    def partial_index_options(self, where: Any) -> dict[str, Any]:
        return {"postgresql_where": where}

    def search_index_options(self) -> dict[str, Any]:
        return {"postgresql_using": "gin"}

    def search_operand(self, column: str, case_insensitive: bool) -> str:
        # A text opclass cannot serve citext's own ILIKE operator, so a
        # case-insensitive column is indexed (and searched) as ``::text``.
        if case_insensitive:
            return f"({column}::text) gin_trgm_ops"
        return f"{column} gin_trgm_ops"

    def conflict_insert(self, table: Any) -> ConflictInsert:
        return cast("ConflictInsert", postgresql.insert(table))

    async def transaction_lock(self, session: AsyncSession, key: int) -> bool:
        await session.execute(select(func.pg_advisory_xact_lock(key)))
        return True


class SqliteSql:
    """SQLite, for development and tests: ``COLLATE NOCASE``, ``WHERE`` indexes,
    ``ON CONFLICT``; no substring-search index and no transaction lock."""

    contract_version = 2
    name = "sqlite"
    #: ``RETURNING`` (3.35) and ``ON CONFLICT`` (3.24) are needed.
    minimum_server_version: tuple[int, ...] | None = (3, 35)

    def features(self) -> frozenset[str]:
        # Serial writers (BEGIN IMMEDIATE, WAL) come with the driver's SQLite
        # path; until then SQLite declares nothing (spec section 10, slice 8a).
        return frozenset()

    def classify_error(self, exc: BaseException) -> ErrorKind:
        for error in _causes(exc):
            name = getattr(error, "sqlite_errorname", None)
            if not isinstance(name, str):
                continue
            if name in ("SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"):
                return "unique_violation"
            if name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
                return "lock_not_available"
        return "other"

    def case_insensitive_text(self, length: int | None) -> TypeEngine[Any]:
        return String(length, collation="NOCASE")

    def partial_index_options(self, where: Any) -> dict[str, Any]:
        return {"sqlite_where": where}

    def search_index_options(self) -> None:
        # No index serves LIKE '%term%' in SQLite; the search runs unindexed.
        return None

    def search_operand(self, column: str, case_insensitive: bool) -> None:
        return None

    def conflict_insert(self, table: Any) -> ConflictInsert:
        return cast("ConflictInsert", sqlite.insert(table))

    async def transaction_lock(self, session: AsyncSession, key: int) -> bool:
        # SQLite has no lock a transaction can take before its first write:
        # reads before it run outside any lock. Refuse rather than run a
        # check-then-act section unprotected.
        return False


POSTGRESQL = PostgresqlSql()
SQLITE = SqliteSql()
