"""The built-in SQL dialect renderers: PostgreSQL and SQLite.

A renderer writes the portable forms of ``sandloom.sdk.data``
(:mod:`sandloom.core.sql_alchemy.portable`) in one database's SQL. These two
are named in the ``sandloom.sql_dialects`` entry-point group of the
``sandloom`` package; a driver package for another database ships its own
renderer the same way. This is the only place the forms meet a dialect.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import ColumnElement, String, any_, bindparam, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TypeEngine, Uuid

from sandloom.core.sql_alchemy.portable import ArrayInsert, ConflictInsert, ErrorKind

#: :data:`~sandloom.core.sql_alchemy.portable.ARRAY_INSERT_TYPES` as
#: PostgreSQL array types.
_PG_ARRAYS = {
    "uuid": "UUID[]",
    "varchar": "VARCHAR[]",
    "json": "JSON[]",
    "timestamp": "TIMESTAMP[]",
    "integer": "INTEGER[]",
}
#: The CTE name of a table's insert (the kernel's statement text since it was
#: written by hand: ``WITH audit AS (...)``).
_CTE_NAMES = {"audit_log": "audit"}


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

    def greatest(self, *exprs: Any) -> ColumnElement[Any]:
        return func.greatest(*exprs)

    def id_in(
        self, column: Any, ids: Sequence[Any], *, key: str
    ) -> ColumnElement[bool]:
        # ONE array bind parameter: the statement text (and asyncpg's prepared
        # statement) is the same for every number of ids, unlike IN (...).
        element_type: Any = column.type
        if isinstance(element_type, Uuid) and not isinstance(
            element_type, postgresql.UUID
        ):
            element_type = postgresql.UUID(as_uuid=True)
            if not column.type.as_uuid:
                element_type = postgresql.UUID(as_uuid=False)
        array = postgresql.ARRAY(element_type)
        return cast(
            "ColumnElement[bool]",
            column == any_(bindparam(key, value=list(ids), type_=array)),
        )

    def array_insert_sql(self, inserts: Sequence[ArrayInsert]) -> str:
        # ``INSERT ... SELECT * FROM unnest(CAST(:x AS T[]), ...)``: one array
        # bind per column, so the text (and asyncpg's prepared statement) is
        # the same for any number of rows; every insert but the last runs as
        # a data-modifying CTE, all in one round trip.
        parts = [
            f"INSERT INTO {table} ("  # noqa: S608 - kernel table/column constants, values bound
            + ", ".join(column for column, _ in columns)
            + ") SELECT * FROM unnest("
            + ", ".join(
                f"CAST(:{prefix}_{column} AS {_PG_ARRAYS[kind]})"
                for column, kind in columns
            )
            + ")"
            for table, prefix, columns in inserts
        ]
        *ctes, last = parts
        if not ctes:
            return last
        names = [table for table, _, _ in inserts[:-1]]
        with_ = ", ".join(
            f"{_CTE_NAMES.get(n, n)} AS ({sql})"
            for n, sql in zip(names, ctes, strict=True)
        )
        return f"WITH {with_} {last}"

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

    def greatest(self, *exprs: Any) -> ColumnElement[Any]:
        # SQLite's multi-argument max() is its scalar greatest.
        return func.max(*exprs)

    def id_in(
        self, column: Any, ids: Sequence[Any], *, key: str
    ) -> ColumnElement[bool]:
        return cast(
            "ColumnElement[bool]",
            column.in_(bindparam(key, value=list(ids), expanding=True)),
        )

    def array_insert_sql(self, inserts: Sequence[ArrayInsert]) -> None:
        # No arrays: the kernel writes one multi-row INSERT per table.
        return None

    async def transaction_lock(self, session: AsyncSession, key: int) -> bool:
        # SQLite has no lock a transaction can take before its first write:
        # reads before it run outside any lock. Refuse rather than run a
        # check-then-act section unprotected.
        return False


POSTGRESQL = PostgresqlSql()
SQLITE = SqliteSql()
