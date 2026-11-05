"""The built-in SQL dialect renderers: PostgreSQL and SQLite.

A renderer writes the portable forms of ``sandloom.sdk.data``
(:mod:`sandloom.core.sql_alchemy.portable`) in one database's SQL. These two
are named in the ``sandloom.sql_dialects`` entry-point group of the
``sandloom`` package; a driver package for another database ships its own
renderer the same way. This is the only place the forms meet a dialect.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from functools import cache
from types import MappingProxyType
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Select,
    String,
    Text,
    and_,
    any_,
    bindparam,
    case,
    column,
    func,
    literal,
    literal_column,
    select,
    text,
)
from sqlalchemy import (
    cast as sql_cast,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.types import NullType, TypeEngine, Uuid

from sandloom.core.sql_alchemy.portable import (
    TENANT_SETTING,
    ArrayInsert,
    ConflictInsert,
    ErrorKind,
    JsonPathKey,
    LockKey,
    LockScope,
    RowSecurity,
    check_lock_key,
    check_unit_shape,
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
    "25P02": "transaction_aborted",
    "40001": "serialization_failure",
    "40P01": "deadlock",
    "55P03": "lock_not_available",
    "57P01": "connection_lost",
    "57P02": "connection_lost",
    "57P03": "connection_lost",
}


def pg_connection_lost(code: str) -> bool:
    """True for a PostgreSQL SQLSTATE meaning the session is gone: class 08
    or an admin / crash shutdown (57P01-57P03). Shared with the asyncpg
    adapter, which reads the code off the driver's own error."""
    return _PG_SQLSTATES.get(code) == "connection_lost" or code.startswith("08")


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


# --- PostgreSQL row security ------------------------------------------------

#: An identifier PostgreSQL keeps whole (NAMEDATALEN - 1 bytes).
_PG_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
#: Canonical lowercase UUID text (what the driver binds).
_UUID_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _pg_quote(ident: str, what: str) -> str:
    if not _PG_IDENT.match(ident):
        raise ValueError(f"invalid {what} identifier for RLS: {ident!r}")
    return f'"{ident}"'


def _pg_qualified(table: str, schema: str | None) -> str:
    quoted = _pg_quote(table, "table")
    return f"{_pg_quote(schema, 'schema')}.{quoted}" if schema else quoted


def _pg_system_policy_name(table: str) -> str:
    name = f"{table}_system_access"[:63]
    if name == f"{table}_tenant_isolation"[:63]:  # pragma: no cover - 63-char names
        raise ValueError(f"table name too long for RLS policies: {table!r}")
    return name


def _pg_tenant_predicate(column: str) -> str:
    """USING and WITH CHECK of the tenant policy. No bypass branch; ``CASE``
    casts only a canonical UUID, so an unset, empty or malformed setting is
    NULL (zero rows, refused writes) instead of a cast error."""
    setting = f"current_setting('{TENANT_SETTING}', true)"
    expression = f"CASE WHEN {setting} ~ '{_UUID_RE}' THEN {setting}::uuid END"
    return f"({_pg_quote(column, 'column')} = ({expression}))"


_PG_ROLE_ATTRIBUTES = text(
    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
    "WHERE rolname = current_user"
)
#: Superuser / BYPASSRLS roles the current role could ``SET ROLE`` to.
_PG_PRIVILEGED_MEMBERSHIPS = text(
    "SELECT rolname FROM pg_roles WHERE (rolsuper OR rolbypassrls) "
    "AND rolname <> current_user AND pg_has_role(current_user, oid, 'MEMBER') "
    "ORDER BY rolname"
)
#: RLS tables the current role owns (directly or via membership) — an owner can
#: ALTER TABLE ... DISABLE/NO FORCE ROW LEVEL SECURITY or drop the policy.
_PG_OWNED_RLS_TABLES = text(
    "SELECT n.nspname || '.' || c.relname FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relrowsecurity AND pg_has_role(current_user, c.relowner, 'MEMBER') "
    "ORDER BY 1"
)
#: Unconditional (``USING (true)``) policies granted to a role the current role
#: belongs to — e.g. the system-channel policies.
_PG_UNCONDITIONAL_POLICIES = text(
    "SELECT DISTINCT p.schemaname || '.' || p.tablename || ':' || p.policyname "
    "FROM pg_policies p, unnest(p.roles) AS r(role) "
    "WHERE r.role <> 'public' AND pg_has_role(current_user, r.role, 'MEMBER') "
    "AND (p.qual = 'true' OR p.with_check = 'true') ORDER BY 1"
)
_PG_RLS_TABLES = text(
    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p') AND c.relname = ANY(:names) "
    "AND n.nspname = ANY(current_schemas(false))"
)
_PG_RLS_POLICIES = text(
    "SELECT tablename, policyname FROM pg_policies "
    "WHERE tablename = ANY(:names) AND schemaname = ANY(current_schemas(false))"
)


class PostgresqlRowSecurity:
    """PostgreSQL Row-Level Security: an ENABLEd and FORCEd policy per table
    comparing ``tenant_id`` with the transaction-local setting, a permissive
    policy for the system role on ``system_access`` tables, and the catalogue
    queries of the startup role and coverage checks."""

    def policy_name(self, table: str) -> str:
        return f"{table}_tenant_isolation"[:63]

    def enable_statements(
        self,
        table: str,
        *,
        column: str,
        schema: str | None,
        system_access: bool,
        system_role: str,
    ) -> list[str]:
        target = _pg_qualified(table, schema)
        policy = _pg_quote(self.policy_name(table), "policy")
        sys_policy = _pg_quote(_pg_system_policy_name(table), "policy")
        predicate = _pg_tenant_predicate(column)
        statements = [
            f"ALTER TABLE {target} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {target} FORCE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS {policy} ON {target}",
            f"CREATE POLICY {policy} ON {target} AS PERMISSIVE FOR ALL "
            f"USING {predicate} WITH CHECK {predicate}",
            f"DROP POLICY IF EXISTS {sys_policy} ON {target}",
        ]
        if system_access:
            role = _pg_quote(system_role, "role")
            statements += [
                f"CREATE POLICY {sys_policy} ON {target} AS PERMISSIVE FOR ALL "
                f"TO {role} USING (true) WITH CHECK (true)",
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {target} TO {role}",
            ]
        return statements

    def disable_statements(self, table: str, *, schema: str | None) -> list[str]:
        target = _pg_qualified(table, schema)
        policy = _pg_quote(self.policy_name(table), "policy")
        sys_policy = _pg_quote(_pg_system_policy_name(table), "policy")
        return [
            f"DROP POLICY IF EXISTS {sys_policy} ON {target}",
            f"DROP POLICY IF EXISTS {policy} ON {target}",
            f"ALTER TABLE {target} NO FORCE ROW LEVEL SECURITY",
            f"ALTER TABLE {target} DISABLE ROW LEVEL SECURITY",
        ]

    def bind_statement(self, placeholder: str) -> str:
        # ``is_local => true``: SET LOCAL, gone at COMMIT / ROLLBACK.
        return f"SELECT set_config('{TENANT_SETTING}', {placeholder}, true)"

    def begin_and_bind(self, begin: str, value: str) -> str:
        # PostgreSQL's simple protocol runs both in order, one ReadyForQuery.
        return f"{begin}; SELECT set_config('{TENANT_SETTING}', '{value}', true)"

    def role_attributes(self) -> TextClause:
        return _PG_ROLE_ATTRIBUTES

    def privileged_memberships(self) -> TextClause:
        return _PG_PRIVILEGED_MEMBERSHIPS

    def owned_secured_tables(self) -> TextClause:
        return _PG_OWNED_RLS_TABLES

    def unconditional_policies(self) -> TextClause:
        return _PG_UNCONDITIONAL_POLICIES

    def secured_tables(self) -> TextClause:
        return _PG_RLS_TABLES

    def table_policies(self) -> TextClause:
        return _PG_RLS_POLICIES


_PG_ROW_SECURITY = PostgresqlRowSecurity()

#: PostgreSQL's isolation of a unit: a fresh snapshot per statement (its
#: default) or one snapshot for the whole unit.
_PG_ISOLATION = {False: "READ COMMITTED", True: "REPEATABLE READ"}


def _pg_begin(*, write: bool, read_only: bool, snapshot: bool) -> str | None:
    if read_only:
        # Isolation stated explicitly: part of the read unit's contract.
        return f"BEGIN ISOLATION LEVEL {_PG_ISOLATION[snapshot]} READ ONLY"
    if snapshot:
        return f"BEGIN ISOLATION LEVEL {_PG_ISOLATION[True]}"
    return None  # the server's default isolation, writable


def _pg_options(*, read_only: bool, snapshot: bool | None) -> Mapping[str, Any]:
    options: dict[str, Any] = {}
    if read_only:
        # asyncpg begins ``BEGIN [ISOLATION LEVEL ...] READ ONLY`` from these.
        options["postgresql_readonly"] = True
    if snapshot is not None:
        options["isolation_level"] = _PG_ISOLATION[snapshot]
    return MappingProxyType(options)


_UNITS = [
    (write, read_only, snapshot)
    for write in (False, True)
    for read_only in (False, True)
    for snapshot in (False, True)
    if not (write and read_only)
]
#: Every unit's BEGIN and execution options, computed once (hot paths ask
#: per adapter, the planner's read units per checkout).
_PG_BEGINS = {
    (w, r, s): _pg_begin(write=w, read_only=r, snapshot=s) for w, r, s in _UNITS
}
_PG_OPTIONS = {
    (r, s): _pg_options(read_only=r, snapshot=s)
    for r in (False, True)
    for s in (None, False, True)
}
_NO_OPTIONS: Mapping[str, Any] = MappingProxyType({})


def _pg_fulltext_document(
    columns: Sequence[ColumnElement[Any]], language: str
) -> ColumnElement[Any]:
    """``to_tsvector('<language>'::regconfig, <columns joined by ' '>)``,
    every constant inline: the index expression and the query's must be the
    same text for the planner to use the index (a bound '' would not be)."""
    empty: ColumnElement[str] = literal_column("''")
    parts = [func.coalesce(sql_cast(c, Text), empty) for c in columns]
    document: ColumnElement[Any] = parts[0]
    for part in parts[1:]:
        document = document.op("||")(literal_column("' '")).op("||")(part)
    # ``language`` is checked by the form: a lowercase name.
    return func.to_tsvector(literal_column(f"'{language}'::regconfig"), document)


def _pg_text_path(path: tuple[JsonPathKey, ...]) -> ColumnElement[Any]:
    steps = postgresql.array([literal(str(step), String()) for step in path])
    return sql_cast(steps, postgresql.ARRAY(Text()))


def _pg_json_path(column: Any, path: tuple[JsonPathKey, ...]) -> ColumnElement[str]:
    # ``#>>`` (json and jsonb alike): the value at the path as text, NULL for
    # a missing path or JSON null. PostgreSQL reads a text step as an array
    # index or an object key alike; SQLite's ``[n]`` reads arrays only. So an
    # int step is guarded: the value it steps into must be an array, or the
    # answer is NULL on both.
    value = column.op("#>>", return_type=Text())(_pg_text_path(path))
    guards = [
        func.jsonb_typeof(
            sql_cast(
                column if at == 0 else column.op("#>")(_pg_text_path(path[:at])),
                postgresql.JSONB(),
            )
        )
        == literal_column("'array'")
        for at, step in enumerate(path)
        if isinstance(step, int)
    ]
    if not guards:
        return cast("ColumnElement[str]", value)
    return cast("ColumnElement[str]", case((and_(*guards), value)))


def _pg_jsonb(element: Any) -> Any:
    if isinstance(element.type, postgresql.JSONB):
        return element
    return sql_cast(element, postgresql.JSONB())


__all__ = [
    "POSTGRESQL",
    "SQLITE",
    "PostgresqlRowSecurity",
    "PostgresqlSql",
    "SQLITE_BEGIN_OPTION",
    "SQLITE_QUERY_ONLY_OPTION",
    "SQLITE_READ_BEGIN",
    "SQLITE_WRITE_BEGIN",
    "SqliteSql",
    "pg_connection_lost",
]


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
                "arrays",
                "fulltext_search",
                "json_path",
                "json_contains",
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

    # -- feature forms (arrays, fulltext_search, json_path, json_contains) --

    def array_type(self, item_type: TypeEngine[Any]) -> TypeEngine[Any]:
        return postgresql.ARRAY(item_type)

    def fulltext_index_options(self) -> dict[str, Any]:
        return {"postgresql_using": "gin"}

    def fulltext_document(
        self, columns: Sequence[ColumnElement[Any]], language: str
    ) -> ColumnElement[Any]:
        return _pg_fulltext_document(columns, language)

    def fulltext_match(
        self, document: ColumnElement[Any], query: ColumnElement[str], language: str
    ) -> ColumnElement[bool]:
        tsquery = func.websearch_to_tsquery(
            literal_column(f"'{language}'::regconfig"), query
        )
        return cast(
            "ColumnElement[bool]",
            document.op("@@", return_type=Boolean())(tsquery),
        )

    def json_path(
        self, column: Any, path: tuple[JsonPathKey, ...]
    ) -> ColumnElement[str]:
        return _pg_json_path(column, path)

    def json_contains(self, column: Any, value: Any) -> ColumnElement[bool]:
        # ``@>`` is jsonb's: a json column is compared as jsonb (a jsonb
        # column as itself, so its GIN index serves the query).
        return cast(
            "ColumnElement[bool]",
            _pg_jsonb(column).op("@>", return_type=Boolean())(_pg_jsonb(value)),
        )

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

    def wall_clock(self) -> str:
        # The time of the statement; now() is the transaction's start, so
        # every row of one transaction would tie.
        return "clock_timestamp()"

    def begin_statement(
        self, *, write: bool, read_only: bool, snapshot: bool
    ) -> str | None:
        check_unit_shape(write=write, read_only=read_only)
        return _PG_BEGINS[(write, read_only, snapshot)]

    def transaction_options(
        self, *, read_only: bool, snapshot: bool | None
    ) -> Mapping[str, Any]:
        return _PG_OPTIONS[(read_only, snapshot)]

    def row_security(self) -> RowSecurity:
        return _PG_ROW_SECURITY


#: Execution options the SQLite renderer's ``transaction_options`` set and
#: the aiosqlite adapter's ``begin`` hook reads
#: (:mod:`sandloom.core.drivers.dbapi_aiosqlite`): the unit's BEGIN text, and
#: whether the connection refuses writes (``PRAGMA query_only``) until it ends.
SQLITE_BEGIN_OPTION = "sandloom_sqlite_begin"
SQLITE_QUERY_ONLY_OPTION = "sandloom_sqlite_query_only"
#: A unit that may write takes the database's one write lock at BEGIN, so
#: writers are serial and every lock the kernel asks for is already held.
SQLITE_WRITE_BEGIN = "BEGIN IMMEDIATE"
#: A read unit: no lock at BEGIN; under WAL its first read fixes one snapshot
#: that later commits do not change.
SQLITE_READ_BEGIN = "BEGIN DEFERRED"

#: ``transaction_options`` per ``(read_only, snapshot)``. A read-only unit
#: begins deferred and refuses writes; a writable unit begins IMMEDIATE
#: whatever its snapshot (one writer at a time sees one database), so only an
#: explicit snapshot request is stated.
_SQLITE_OPTIONS = {
    (r, s): MappingProxyType(
        {SQLITE_BEGIN_OPTION: SQLITE_READ_BEGIN, SQLITE_QUERY_ONLY_OPTION: True}
        if r
        else ({SQLITE_BEGIN_OPTION: SQLITE_WRITE_BEGIN} if s else {})
    )
    for r in (False, True)
    for s in (None, False, True)
}


def _sqlite_error(error: BaseException) -> ErrorKind | None:
    """The kind of one sqlite3 error, from its extended result code name
    (Python 3.11+) or, failing that, its numeric extended code."""
    name = getattr(error, "sqlite_errorname", None)
    if not isinstance(name, str):
        code = getattr(error, "sqlite_errorcode", None)
        name = _SQLITE_CODES.get(code) if isinstance(code, int) else None
    if name is None:
        return None
    if name in ("SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"):
        return "unique_violation"
    if name == "SQLITE_BUSY_SNAPSHOT":
        # A read transaction tried to write after another writer committed:
        # its snapshot is stale, as PostgreSQL's 40001.
        return "serialization_failure"
    if name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
        return "lock_not_available"
    return None


#: sqlite3 extended result codes by number, for an error without a name.
_SQLITE_CODES = {
    5: "SQLITE_BUSY",
    6: "SQLITE_LOCKED",
    261: "SQLITE_BUSY_RECOVERY",
    517: "SQLITE_BUSY_SNAPSHOT",
    773: "SQLITE_BUSY_TIMEOUT",
    262: "SQLITE_LOCKED_SHAREDCACHE",
    1555: "SQLITE_CONSTRAINT_PRIMARYKEY",
    2067: "SQLITE_CONSTRAINT_UNIQUE",
}


def _sqlite_json_path(path: tuple[JsonPathKey, ...]) -> str:
    """SQLite's JSON path text: ``$."key"[0]`` (the form refuses a key with
    a double quote)."""
    return "$" + "".join(
        f"[{step}]" if isinstance(step, int) else f'."{step}"' for step in path
    )


class SqliteSql:
    """SQLite, for development and tests: ``COLLATE NOCASE``, ``WHERE`` indexes,
    ``ON CONFLICT``; no substring-search index and no named lock (its
    writers are serial instead).

    Writers are serial: a unit that may write begins ``BEGIN IMMEDIATE``
    (the aiosqlite adapter emits it, implicit transactions off), so a
    transaction lock is held by every such unit; a read-only unit begins
    deferred under WAL (one snapshot) with ``PRAGMA query_only`` on.
    """

    contract_version = 2
    name = "sqlite"
    #: ``RETURNING`` (3.35), ``ON CONFLICT`` (3.24) and the built-in JSON
    #: functions (3.38) are needed.
    minimum_server_version: tuple[int, ...] | None = (3, 38)
    #: The migration chains are PostgreSQL's DDL history: a SQLite file
    #: takes its schema from the models (SANDLOOM-B028).
    schema_from_models = True
    #: For development and tests (SANDLOOM-B029).
    development_only = True

    def features(self) -> frozenset[str]:
        # An in-memory database (one connection, no WAL) withholds
        # snapshot_reads and transaction_locks: the driver's instance
        # features (sandloom.core.drivers.sqlalchemy_database). JSON path
        # extraction is built in from 3.38, the minimum; no arrays, no
        # full-text index (FTS5 is a virtual table, not an index) and no
        # JSON containment.
        return frozenset(
            {
                "transaction_locks",
                "snapshot_reads",
                "readonly_transactions",
                "json_path",
            }
        )

    def classify_error(self, exc: BaseException) -> ErrorKind:
        for error in _causes(exc):
            kind = _sqlite_error(error)
            if kind is not None:
                return kind
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

    def json_path(
        self, column: Any, path: tuple[JsonPathKey, ...]
    ) -> ColumnElement[str]:
        # json_extract answers SQL values (a JSON true is 1): the text of
        # booleans is restated from json_type, the rest cast to text, so the
        # answer is PostgreSQL's ``#>>`` for strings, booleans, null and
        # 64-bit integers. Other numbers are read as SQLite REAL and printed
        # in its text (1.50 -> '1.5', 1e2 -> '100.0'): the form's contract
        # excludes them.
        where = literal(_sqlite_json_path(path), String())
        true: ColumnElement[str] = literal_column("'true'")
        false: ColumnElement[str] = literal_column("'false'")
        kind = func.json_type(column, where)
        return cast(
            "ColumnElement[str]",
            case(
                (kind == true, true),
                (kind == false, false),
                else_=sql_cast(func.json_extract(column, where), Text()),
            ),
        )

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
        # No named lock: a unit that may write holds the database's one
        # write lock from its BEGIN IMMEDIATE, which already excludes every
        # other writer, so the kernel's transaction_lock succeeds there and
        # refuses elsewhere (SANDLOOM-R015).
        check_lock_key(key)
        return None

    def unlock_statement(self, key: LockKey) -> Executable | None:
        check_lock_key(key)
        return None

    def for_update(
        self, stmt: Select[Any], *, skip_locked: bool, read: bool = False
    ) -> Select[Any]:
        # No row locks: writers are serial (BEGIN IMMEDIATE), and SQLite does
        # not declare skip_locked, so no queue relies on skipping.
        return stmt

    def relaxed_commit_clause(self) -> None:
        return None

    def wall_clock(self) -> str:
        # UTC to the millisecond, padded to the text SQLAlchemy's SQLite
        # DATETIME binds ('YYYY-MM-DD HH:MM:SS.ffffff'), so a stamped value
        # compares equal to itself read back and bound again ('now' is the
        # time of the statement's step). Ties within a millisecond are
        # ordered by the UUIDv7 id.
        return "(strftime('%Y-%m-%d %H:%M:%f', 'now') || '000')"

    def begin_statement(self, *, write: bool, read_only: bool, snapshot: bool) -> str:
        # Every unit not declared read-only begins IMMEDIATE: the lazy request
        # unit cannot know at BEGIN whether it will write, and a deferred
        # transaction that writes after another writer committed fails
        # (SQLITE_BUSY_SNAPSHOT). A read-only unit begins deferred: under WAL
        # it reads one snapshot, so a unit asking for a fresh snapshot per
        # statement gets the stronger answer.
        check_unit_shape(write=write, read_only=read_only)
        return SQLITE_READ_BEGIN if read_only else SQLITE_WRITE_BEGIN

    def transaction_options(
        self, *, read_only: bool, snapshot: bool | None
    ) -> Mapping[str, Any]:
        return _SQLITE_OPTIONS[(read_only, snapshot)]

    def row_security(self) -> None:
        # No row-level security: TenantScoped isolation is the ORM guard only
        # (application-enforced); tenant_rls is refused (SANDLOOM-B026).
        return None


POSTGRESQL = PostgresqlSql()
SQLITE = SqliteSql()
