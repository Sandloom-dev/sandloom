"""The built-in SQL dialect renderers: PostgreSQL and SQLite.

A renderer writes the portable forms of ``sandloom.sdk.data``
(:mod:`sandloom.core.sql_alchemy.portable`) in one database's SQL. These two
are named in the ``sandloom.sql_dialects`` entry-point group of the
``sandloom`` package; a driver package for another database ships its own
renderer the same way. This is the only place the forms meet a dialect.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Select,
    String,
    any_,
    bindparam,
    column,
    func,
    select,
    text,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.sql.base import Executable
from sqlalchemy.types import NullType, TypeEngine, Uuid

from sandloom.core.sql_alchemy.portable import (
    ArrayInsert,
    ConflictInsert,
    ErrorKind,
    LockKey,
    LockScope,
    check_lock_key,
)

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


#: PostgreSQL's advisory lock function per ``(scope, wait)``.
_PG_LOCKS = {
    ("transaction", True): "pg_advisory_xact_lock",
    ("transaction", False): "pg_try_advisory_xact_lock",
    ("session", True): "pg_advisory_lock",
    ("session", False): "pg_try_advisory_lock",
}


@cache
def _pg_textual_lock(
    function: str, hashed: str | None, pair: bool, answers: bool
) -> Any:
    """``SELECT <function>(...)`` for a string or pair key, with UNTYPED
    binds (``$1``, as the hand-written statements had), built once."""
    if hashed is None:
        key = ":k"
    elif hashed == "hashtextextended":
        key = "hashtextextended(:k, 0)"
    else:
        key = f"{hashed}(:k)"
    args = f":ns, {key}" if pair else key
    names = ("ns", "k") if pair else ("k",)
    sql = f"SELECT {function}({args})"
    statement: Any = text(sql).bindparams(
        *(bindparam(name, type_=NullType()) for name in names)
    )
    if answers:
        statement = statement.columns(column("locked", Boolean))
    return statement


def _pg_lock(function: str, key: LockKey, *, answers: bool) -> Executable:
    """``function`` (an advisory lock or unlock) on ``key``; ``answers``: it
    returns whether it locked (or unlocked), as a boolean column."""
    check_lock_key(key)
    if isinstance(key, int):
        # A bound bigint: ``transaction_lock``'s statement since it was written.
        call = getattr(func, function)
        return select(call(key, type_=Boolean) if answers else call(key))
    if isinstance(key, str):
        # Hashed ON THE SERVER, so processes of two releases exclude each
        # other during a rolling deploy.
        base: Any = _pg_textual_lock(function, "hashtextextended", False, answers)
        return cast("Executable", base.bindparams(k=key))
    namespace, inner = key
    hashed = "hashtext" if isinstance(inner, str) else None
    base = _pg_textual_lock(function, hashed, True, answers)
    return cast("Executable", base.bindparams(ns=namespace, k=inner))


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

    def lock_statement(
        self, key: LockKey, *, scope: LockScope, wait: bool
    ) -> Executable:
        # Advisory locks: an int is one bigint key, a pair two int4 keys (a
        # string second key hashed with hashtext), a string one bigint hashed
        # with hashtextextended(:k, 0).
        return _pg_lock(_PG_LOCKS[(scope, wait)], key, answers=not wait)

    def unlock_statement(self, key: LockKey) -> Executable:
        return _pg_lock("pg_advisory_unlock", key, answers=True)

    def for_update(
        self, stmt: Select[Any], *, skip_locked: bool, read: bool = False
    ) -> Select[Any]:
        return stmt.with_for_update(skip_locked=skip_locked, read=read)

    def relaxed_commit_clause(self) -> ColumnElement[bool]:
        # ``SET LOCAL synchronous_commit = off`` evaluated as a WHERE term, so
        # it costs no round trip of its own.
        return cast(
            "ColumnElement[bool]",
            text("set_config('synchronous_commit', 'off', true) IS NOT NULL"),
        )


class SqliteSql:
    """SQLite, for development and tests: ``COLLATE NOCASE``, ``WHERE`` indexes,
    ``ON CONFLICT``; no substring-search index and no named lock."""

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

    def lock_statement(
        self, key: LockKey, *, scope: LockScope, wait: bool
    ) -> Executable | None:
        # SQLite has no named lock: serial writers (BEGIN IMMEDIATE) arrive
        # with the driver's SQLite path (spec, slice 8a). Until then the
        # kernel refuses rather than run a check-then-act section unlocked.
        check_lock_key(key)
        return None

    def unlock_statement(self, key: LockKey) -> Executable | None:
        check_lock_key(key)
        return None

    def for_update(
        self, stmt: Select[Any], *, skip_locked: bool, read: bool = False
    ) -> Select[Any]:
        # No row locks: SQLite writers are serial (BEGIN IMMEDIATE, slice 8a),
        # and it does not declare skip_locked, so no queue relies on skipping.
        return stmt

    def relaxed_commit_clause(self) -> None:
        return None


POSTGRESQL = PostgresqlSql()
SQLITE = SqliteSql()
