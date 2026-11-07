"""Database-portable building blocks for module tables and writes.

A business module never imports ``sqlalchemy.dialects`` (SANDLOOM-C028), and
neither does this module: it names no database. What a dialect offers, a
module asks for here, and the **SQL dialect renderer** of the database decides
how it is written. A renderer belongs to a database driver: the built-in
PostgreSQL and SQLite renderers live in ``sandloom.core.drivers.sql_dialects``,
and every renderer, built-in or a driver package's, is found by its SQLAlchemy
dialect name in the ``sandloom.sql_dialects`` entry-point group. Models and
migrations need the renderers before any driver is configured, which is why
they are found by name rather than handed over by the driver bootstrap.

No form falls back silently. A database without a renderer, or whose renderer
lacks a form, refuses with :class:`UnsupportedSqlForm` (SANDLOOM-R015) when
the statement or the DDL is compiled, and startup refuses a configured
database without a renderer, served by a driver that does not declare its
dialect (SANDLOOM-B024), or with a partial index its renderer cannot restrict
(SANDLOOM-R015, :func:`unrendered_partial_indexes`; the migration environment
checks the same). A migration keeps the options its renderers wrote when it was
generated: it is one database's history, so run it on that database.

- :class:`CaseInsensitiveText`: text compared without regard to case.
- :func:`partial_index`: an index over the rows a condition selects.
- :func:`search_index`: an index that serves substring search
  (``ILIKE '%term%'``), created where the database has one.
- :func:`conflict_insert`: ``INSERT ... ON CONFLICT DO NOTHING / DO UPDATE``.
- :func:`greatest`: the largest of its arguments.
- :class:`WallClock`: the database's clock at the statement (a timestamp's
  default), and :class:`UtcDateTime`, a time that reads back in UTC.
- :func:`transaction_lock`: a lock held until the transaction ends, built
  on the renderer's :meth:`SqlDialect.lock_statement` like the kernel's own
  locks (:func:`lock_statement_for`).

**Feature forms.** Some forms need a ``database`` feature the database may
not have (:data:`~sandloom.core.infra.capabilities.DATABASE_FORM_FEATURES`):

- :class:`Array` (``arrays``): a column of a list of one item type;
- :func:`fulltext_index` and :func:`matches` (``fulltext_search``): a
  full-text search over text columns and the index that serves it;
- :func:`json_path` (``json_path``) and :func:`json_contains`
  (``json_contains``): a scalar inside a JSON column, and containment;
- :func:`transaction_lock` (``transaction_locks``);
- :func:`aggregate_list` (``list_aggregates``): the values of a group as one
  Python list. Every built-in renderer declares it.

A renderer that declares one of these features writes its forms (optional
members of contract 2, :data:`FEATURE_FORM_MEMBERS`); one that does not
answers nothing and the form refuses (SANDLOOM-R015). A module that uses a
form declares the feature (SANDLOOM-C030), and a table built with one is
refused at startup on an instance without it (SANDLOOM-B033,
:func:`table_feature_uses`).

On PostgreSQL each renders exactly the SQL the standard modules wrote by hand
before, so moving a module onto them changes no schema and needs no migration.
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from importlib import metadata
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol, cast

from sqlalchemy import (
    JSON,
    ColumnElement,
    DateTime,
    Index,
    MetaData,
    Select,
    String,
    Table,
    Text,
    TextClause,
    bindparam,
    text,
)
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.sql import coercions, operators, roles
from sqlalchemy.sql import functions as sql_functions
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.compiler import StrSQLCompiler
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    ClauseElement,
    ClauseList,
    ColumnClause,
    Grouping,
    Null,
    Tuple,
)
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.selectable import ScalarSelect
from sqlalchemy.sql.type_api import to_instance
from sqlalchemy.sql.visitors import InternalTraversal
from sqlalchemy.types import (
    ARRAY,
    Boolean,
    LargeBinary,
    NullType,
    TypeDecorator,
    TypeEngine,
)

from sandloom.core.infra.capabilities import (
    DATABASE_FORM_FEATURES,
    DATABASE_RENDERER_FEATURES,
)

if TYPE_CHECKING:
    from typing import Self

    from sqlalchemy.orm import QueryableAttribute
    from sqlalchemy.sql.base import ReadOnlyColumnCollection
    from sqlalchemy.sql.compiler import SQLCompiler
    from sqlalchemy.sql.elements import KeyedColumnElement
    from sqlalchemy.sql.selectable import FromClause

__all__ = [
    "Array",
    "CaseInsensitiveText",
    "FEATURE_FORM_MEMBERS",
    "FeatureUse",
    "JsonPathKey",
    "ConflictInsert",
    "ErrorKind",
    "IncompatibleSqlDialect",
    "LockKey",
    "LockScope",
    "LockStatement",
    "RENDERER_FEATURES",
    "ROW_SECURITY_MEMBERS",
    "RowSecurity",
    "SQL_DIALECTS_GROUP",
    "SQL_DIALECT_CONTRACT",
    "ARRAY_INSERT_TYPES",
    "ArrayInsert",
    "SqlDialect",
    "TENANT_SETTING",
    "UnsupportedServerVersion",
    "UnsupportedSqlForm",
    "UtcDateTime",
    "NaiveDateTimeError",
    "WallClock",
    "begin_statement_for",
    "check_lock_key",
    "check_server_version",
    "check_unit_shape",
    "array_insert_sql_for",
    "classify_error",
    "conflict_insert",
    "conflict_insert_for",
    "dialect_name",
    "development_only",
    "dialect_of_url",
    "for_update_for",
    "greatest",
    "greatest_for",
    "has_sql_dialect",
    "is_error_kind",
    "is_partial_index",
    "is_plain_index",
    "id_in_for",
    "aggregate_list",
    "fulltext_index",
    "json_contains",
    "json_path",
    "matches",
    "table_feature_uses",
    "lock_statement_for",
    "optional_lock_statement_for",
    "partial_index",
    "register_sql_dialect",
    "relaxed_commit_clause_for",
    "renderer_features",
    "row_security_for",
    "row_security_of_url",
    "aware_times",
    "schema_from_models",
    "serial_writers",
    "search_index",
    "skips_locked_rows",
    "sql_dialect",
    "transaction_lock",
    "hold_transaction_lock",
    "WRITE_LOCK_HELD",
    "WRITE_LOCK_TASK",
    "TAKE_WRITE_LOCK",
    "transaction_options_for",
    "unlock_statement_for",
    "unrendered_dialect",
    "unrendered_partial_indexes",
]

#: The entry-point group a database driver package names its renderer in:
#: ``<sqlalchemy dialect name> = "<module>:<renderer object>"``.
SQL_DIALECTS_GROUP = "sandloom.sql_dialects"

#: ``Index.info`` keys the forms leave on their indexes (read generically by
#: the migration environment and the DDL check, with no import of this module).
PARTIAL_WHERE = "sandloom.partial_where"
RENDERED_ON = "sandloom.rendered_on"
#: The form that made an index whose options come from the renderer's
#: ``<form>_options`` (``"fulltext_index"``, ``"search_index"``); a partial
#: index is known by its condition.
INDEX_FORM = "sandloom.index_form"


def is_partial_index(index: Index) -> bool:
    """Whether ``index`` covers only the rows a condition selects
    (:func:`partial_index`), read from the form, not from a database's options."""
    return PARTIAL_WHERE in index.info


def is_plain_index(index: Index) -> bool:
    """Whether ``index`` is an ordinary ordered index over every row: not
    partial and not made by a form whose renderer chooses another access
    method (:func:`search_index`, :func:`fulltext_index`). The planner's
    unique-key and covering-index decisions read this on every database."""
    return PARTIAL_WHERE not in index.info and INDEX_FORM not in index.info


class UnsupportedSqlForm(NotImplementedError):
    """The database a module runs on has no way to write a portable form."""

    code = "SANDLOOM-R015"

    def __init__(
        self,
        form: str,
        dialect: str,
        *,
        renderer: bool = True,
        feature: str | None = None,
    ) -> None:
        if renderer and feature is not None:
            why = (
                f"it needs the database feature {feature!r}, which the "
                f"{dialect!r} database's SQL dialect renderer does not declare "
                f"[{self.code}]. Run the module on a database that has it, and "
                f'declare CapabilityRequirement("database", '
                f'features=frozenset({{"{feature}"}})) so startup refuses the '
                "others (SANDLOOM-B033)"
            )
        elif renderer:
            why = (
                f"the {dialect!r} database's SQL dialect renderer does not "
                f"write it [{self.code}]"
            )
        else:
            why = (
                f"the {dialect!r} database has no SQL dialect renderer "
                f"[{self.code}]. A database driver package registers one in "
                f"the {SQL_DIALECTS_GROUP!r} entry-point group; the built-in "
                "ones (PostgreSQL, SQLite) come with the installed sandloom "
                "package"
            )
        super().__init__(f"{form}: {why}")
        self.form = form
        self.dialect = dialect
        self.feature = feature


if TYPE_CHECKING:

    class ConflictInsert(Insert):
        """An ``INSERT`` that can say what to do when the row already exists.

        The typing view of the database's own insert: ``conflict_insert``
        returns the renderer's statement, which has these methods.
        """

        @property
        def excluded(self) -> ReadOnlyColumnCollection[str, KeyedColumnElement[Any]]:
            """The row the insert proposed, for ``set_=`` and ``where=``."""

        def on_conflict_do_nothing(
            self, index_elements: Any = None, index_where: Any = None
        ) -> Self:
            """Skip the row when it conflicts on ``index_elements``."""

        def on_conflict_do_update(
            self,
            index_elements: Any = None,
            index_where: Any = None,
            set_: Any = None,
            where: Any = None,
        ) -> Self:
            """Update the existing row when it conflicts on ``index_elements``."""

else:

    class ConflictInsert(Insert):
        """For annotations only: the shape :func:`conflict_insert` returns
        (``.on_conflict_do_nothing(index_elements=, index_where=)``,
        ``.on_conflict_do_update(index_elements=, index_where=, set_=,
        where=)``, ``.excluded``). The statement itself is the database's own
        insert, not an instance of this class.
        """


#: The contract major this kernel speaks. A renderer of another major is
#: refused when it loads (SANDLOOM-B025); a renderer without
#: ``contract_version`` is version 1.
SQL_DIALECT_CONTRACT = 2

#: The ``database`` capability features a renderer may declare
#: (design/infra-agnostic/spec.md, section 2), owned by the capability spec.
#: A renderer declaring anything else is refused when it loads (B025).
RENDERER_FEATURES = DATABASE_RENDERER_FEATURES

#: What a database error means to the kernel, whatever the database said.
ErrorKind = Literal[
    "unique_violation",
    "serialization_failure",
    "deadlock",
    "lock_not_available",
    "connection_lost",
    # The transaction already failed and refuses every statement until it
    # ends (PostgreSQL's 25P02); a unit shared by several reads is rerun alone.
    "transaction_aborted",
    "other",
]


#: The column types :meth:`SqlDialect.array_insert_sql` is asked for.
ARRAY_INSERT_TYPES = frozenset({"uuid", "varchar", "json", "timestamp", "integer"})

#: ``(table, bind prefix, ((column, type from ARRAY_INSERT_TYPES), ...))``.
ArrayInsert = tuple[str, str, tuple[tuple[str, str], ...]]

#: A named lock's key: a signed 64-bit integer; a pair of a signed 32-bit
#: namespace and a signed 32-bit integer or a string; or a string. A string
#: is hashed by the database itself, so every process (of any release) maps
#: it to the same lock.
LockKey = int | str | tuple[int, int] | tuple[int, str]

#: How long a lock is held: until the transaction ends, or until the session
#: (the connection) unlocks it with :meth:`SqlDialect.unlock_statement`.
LockScope = Literal["transaction", "session"]


class LockStatement(NamedTuple):
    """A lock (or unlock) statement and the parameters that carry its key.

    Run it as ``execute(lock.statement, lock.parameters)``. A renderer builds
    the statement once per lock function and key shape and binds the key at
    execution, so taking a lock per request builds nothing (its compiled
    form stays cached).
    """

    statement: Executable
    parameters: Mapping[str, Any]


_INT64 = (-(1 << 63), (1 << 63) - 1)
_INT32 = (-(1 << 31), (1 << 31) - 1)


def _is_int(value: Any, bounds: tuple[int, int]) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and bounds[0] <= value <= bounds[1]
    )


def check_lock_key(key: Any) -> None:
    """Refuse a value that is not a :data:`LockKey` (``TypeError``)."""
    if _is_int(key, _INT64) or isinstance(key, str):
        return
    if (
        isinstance(key, tuple)
        and len(key) == 2
        and _is_int(key[0], _INT32)
        and (_is_int(key[1], _INT32) or isinstance(key[1], str))
    ):
        return
    raise TypeError(
        f"a lock key is a signed 64-bit int, a string, or a pair of a signed "
        f"32-bit namespace and a signed 32-bit int or a string; got {key!r}"
    )


def check_unit_shape(*, write: bool, read_only: bool) -> None:
    """Refuse a unit that both may write and is read-only (``ValueError``):
    :meth:`SqlDialect.begin_statement` and
    :meth:`SqlDialect.transaction_options` are never asked for one."""
    if write and read_only:
        raise ValueError("a unit that may write cannot be read-only")


#: The transaction-local setting that carries the bound tenant to the
#: database: the driver binds it at each transaction's first statement, and
#: the tenant policy of :class:`RowSecurity` compares each row with it.
TENANT_SETTING = "sandloom.tenant_id"


class RowSecurity(Protocol):
    """The SQL of database-enforced tenant isolation (contract version 2).

    A renderer whose database confines each row to the tenant its
    transaction binds returns one from :meth:`SqlDialect.row_security`.
    It writes text only: the driver runs it, binds the tenant at each
    transaction and verifies the roles at startup. ``table``, ``column``,
    ``schema`` and ``system_role`` are identifiers it checks and quotes
    (``ValueError`` for anything else).
    """

    def policy_name(self, table: str) -> str:
        """The name of ``table``'s tenant policy (what
        :meth:`table_policies` reports for a covered table)."""
        ...

    def enable_statements(
        self,
        table: str,
        *,
        column: str,
        schema: str | None,
        system_access: bool,
        system_role: str,
    ) -> list[str]:
        """The DDL that confines ``table`` to the bound tenant (re-runnable):
        rows whose ``column`` equals :data:`TENANT_SETTING`, none for an
        unset or malformed setting, and no bypass for the table's owner.
        ``system_access=True`` also lets ``system_role`` reach every tenant's
        rows of this table; without it that access is removed."""
        ...

    def disable_statements(self, table: str, *, schema: str | None) -> list[str]:
        """The DDL that reverts :meth:`enable_statements` on ``table``."""
        ...

    def bind_statement(self, placeholder: str) -> str:
        """A statement that sets :data:`TENANT_SETTING` until the transaction
        ends to the one bound parameter ``placeholder`` (the DBAPI's mark,
        such as ``$1``)."""
        ...

    def begin_and_bind(self, begin: str, value: str) -> str:
        """``begin`` (the transaction's ``BEGIN`` text) and the binding of
        ``value`` as ONE message, the value inlined: it is only ever a
        canonical UUID (hex digits and dashes) or ``''``."""
        ...

    def role_attributes(self) -> Executable:
        """Selects one row: the current role, whether it is a superuser, and
        whether it bypasses row security."""
        ...

    def privileged_memberships(self) -> Executable:
        """Selects the superuser or bypassing roles the current role can
        assume."""
        ...

    def owned_secured_tables(self) -> Executable:
        """Selects the row-secured tables the current role owns (an owner
        can switch the security off)."""
        ...

    def unconditional_policies(self) -> Executable:
        """Selects the policies without a condition that cover the current
        role (the system role's)."""
        ...

    def secured_tables(self) -> Executable:
        """Selects ``(table, enabled, forced)`` for each table named in the
        bound list ``names``."""
        ...

    def table_policies(self) -> Executable:
        """Selects ``(table, policy)`` for each policy on a table named in
        the bound list ``names``."""
        ...


class SqlDialect(Protocol):
    """What a database's SQL dialect renderer provides (contract version 2).

    Required: ``contract_version``, ``name``, ``minimum_server_version``,
    :meth:`features` and :meth:`classify_error`. A form method returns
    ``None`` for a form its database cannot write.

    Optional members write the feature forms (:data:`FEATURE_FORM_MEMBERS`).

    Optional facts, read with their default when absent
    (:func:`schema_from_models`, :func:`development_only`,
    :func:`serial_writers`, :func:`aware_times`):

    * ``schema_from_models: bool`` (default ``False``): the database takes
      its schema from the models (``metadata.create_all`` at startup), not
      from the migration chains; ``sandloom migrate`` refuses it and a file
      whose tables lag the models refuses to start (SANDLOOM-B028);
    * ``development_only: bool`` (default ``False``): the database serves
      development and tests; in production it needs the accepted risk
      ``development_database_in_prod`` and the strict profile refuses it
      (SANDLOOM-B029);
    * ``serial_writers: bool`` (default ``False``): the database has one
      writer at a time, so a second write unit of the same task would wait
      for its own caller; a nested unit of the same task on the same
      instance runs as a SAVEPOINT on the caller's connection instead and
      commits with the caller (``docs/infrastructure/database.md``);
    * ``aware_times: bool`` (default ``False``): ``DateTime(timezone=True)``
      keeps the instant of an aware time bound in any offset and its DBAPI
      returns aware values, so :class:`UtcDateTime` converts nothing either
      way (it still refuses a naive time). Without it the type binds the
      time in UTC and attaches UTC to what it reads.
    """

    #: The contract major the renderer implements (2).
    contract_version: int
    #: The SQLAlchemy dialect name it renders for.
    name: str
    #: The oldest server version it supports, or ``None`` (SANDLOOM-B030).
    minimum_server_version: tuple[int, ...] | None

    def features(self) -> frozenset[str]:
        """The ``database`` features this database can give, from
        :data:`RENDERER_FEATURES`. They depend on the dialect only, never on
        the server, so the registry's build-free preflight stays exact."""
        ...

    def classify_error(self, exc: BaseException) -> ErrorKind:
        """What ``exc`` (a SQLAlchemy ``DBAPIError`` or the driver's own
        exception) means; ``"other"`` when it is none of the kinds."""
        ...

    def case_insensitive_text(self, length: int | None) -> TypeEngine[Any] | None:
        """The column type for :class:`CaseInsensitiveText`."""
        ...

    def partial_index_options(self, where: Any) -> dict[str, Any] | None:
        """``Index`` keyword arguments that restrict it to ``where``."""
        ...

    def search_index_options(self) -> dict[str, Any] | None:
        """``Index`` keyword arguments of a substring-search index, or ``None``
        when the database has no such index."""
        ...

    def search_operand(self, column: str, case_insensitive: bool) -> str | None:
        """The SQL of one column of a substring-search index."""
        ...

    def conflict_insert(self, table: Any) -> ConflictInsert | None:
        """An insert with ``on_conflict_do_nothing`` / ``on_conflict_do_update``."""
        ...

    def greatest(self, *exprs: Any) -> ColumnElement[Any] | None:
        """The largest of ``exprs`` (PostgreSQL ``greatest``, SQLite ``max``)."""
        ...

    def id_in(
        self, column: Any, ids: Sequence[Any], *, key: str
    ) -> ColumnElement[bool] | None:
        """``column`` is one of ``ids``, bound as the parameter ``key`` so a
        prebuilt statement takes another list at execution (``{key: [...]}``)."""
        ...

    def array_insert_sql(self, inserts: Sequence[ArrayInsert]) -> str | None:
        """One SQL statement that inserts into each table of ``inserts`` the
        rows bound as one array per column (``:<prefix>_<column>``), or
        ``None`` when the database binds no arrays (the kernel then writes
        one multi-row ``INSERT`` per table). The kernel's bulk outbox and
        audit write; column types are :data:`ARRAY_INSERT_TYPES`."""
        ...

    def lock_statement(
        self, key: LockKey, *, scope: LockScope, wait: bool
    ) -> LockStatement | None:
        """The statement that takes the named lock ``key`` (check it with
        :func:`check_lock_key`) and its parameters, or ``None`` when the
        database has no such lock. Build the statement once per key shape and
        put the key in :attr:`LockStatement.parameters`: the kernel takes a
        lock per request.

        ``scope="transaction"``: held until the transaction ends;
        ``"session"``: until :meth:`unlock_statement` runs on the same
        connection. ``wait=True`` waits for the lock; ``wait=False`` never
        waits and selects one boolean, whether it locked. The kernel runs it
        on sync connections (migrations) and async sessions alike.
        """
        ...

    def unlock_statement(self, key: LockKey) -> LockStatement | None:
        """The statement that releases the session lock ``key`` (selecting
        one boolean, whether it was held) and its parameters; ``None``
        exactly when
        :meth:`lock_statement` has no session lock."""
        ...

    def for_update(
        self, stmt: Select[Any], *, skip_locked: bool, read: bool = False
    ) -> Select[Any]:
        """``stmt`` locking the rows it reads until the transaction ends:
        exclusively, or shared with ``read=True``. ``skip_locked=True`` skips
        rows another transaction holds; only a renderer that declares the
        ``skip_locked`` feature honours it, any other takes the lock waiting
        (or none at all, when its writers are serial)."""
        ...

    def relaxed_commit_clause(self) -> ColumnElement[bool] | None:
        """A ``WHERE`` term, always true, that lets the running transaction
        commit without waiting for its durable flush (the outbox's
        mark-delivered, whose loss only means a redelivery); ``None`` when the
        database has no such setting."""
        ...

    def wall_clock(self) -> str:
        """The SQL of "now, at the call": the database's clock when the
        statement runs, not when its transaction began, so rows one
        transaction writes get ordered times. It is a column's server default
        and an ORM update's value (:class:`WallClock`), and it reads back as a
        timezone-aware UTC time through :class:`UtcDateTime`."""
        ...

    def begin_statement(
        self, *, write: bool, read_only: bool, snapshot: bool
    ) -> str | None:
        """The text that begins a unit, or ``None`` for the database's plain
        ``BEGIN``. ``write``: the unit may write; ``read_only``: the database
        should refuse its writes (only a renderer that declares the
        ``readonly_transactions`` feature does); ``snapshot``: every statement
        of the unit sees one snapshot (only with the ``snapshot_reads``
        feature; the kernel refuses such a unit elsewhere, SANDLOOM-B027),
        otherwise each statement sees what was committed before it started.
        ``write`` and ``read_only`` together are refused
        (:func:`check_unit_shape`). The kernel asks once per combination and
        sends the text itself, where it begins a transaction by hand (the
        planner's optimized executor)."""
        ...

    def transaction_options(
        self, *, read_only: bool, snapshot: bool | None
    ) -> Mapping[str, Any]:
        """The SQLAlchemy execution options that make SQLAlchemy begin the
        unit :meth:`begin_statement` describes (empty: none). ``snapshot=None``
        keeps the database's default isolation; ``False`` asks for a fresh
        snapshot per statement and ``True`` for one snapshot, explicitly. An
        ``isolation_level`` equal to the connection's default changes nothing,
        and the kernel may leave it out. Where SQLAlchemy (or its DBAPI)
        begins the transaction, as in the request and read units, these
        options are the only way to shape it. Asked once per combination."""
        ...

    def row_security(self) -> RowSecurity | None:
        """The SQL of database-enforced tenant isolation, or ``None`` when
        the database has none: there ``TenantScoped`` isolation is the ORM
        guard only (application-enforced), and ``tenant_rls`` and the system
        channel are refused at startup (SANDLOOM-B026)."""
        ...


_lock = threading.RLock()
_registered: dict[str, SqlDialect] = {}
#: ``loaded``: the entry points were read (a dict, so the double-check under
#: the lock reads the current value).
_state = {"loaded": False}
#: Every index the forms made, so a renderer registered later still gives
#: them its options (a driver package may load after the models).
_indexes: weakref.WeakSet[Index] = weakref.WeakSet()


def _apply_options(index: Index, renderers: dict[str, SqlDialect]) -> None:
    where = index.info.get(PARTIAL_WHERE)
    rendered = index.info.setdefault(RENDERED_ON, set())
    form = index.info.get(INDEX_FORM)
    for name, renderer in renderers.items():
        if name in rendered:
            continue
        if where is not None:
            options = renderer.partial_index_options(where)
        elif form is not None:
            member = getattr(renderer, f"{form}_options", None)
            options = None if member is None else member()
        else:
            options = renderer.search_index_options()
        if options is not None:
            index.dialect_kwargs.update(options)
            rendered.add(name)


class IncompatibleSqlDialect(RuntimeError):
    """A renderer this kernel cannot use (SANDLOOM-B025)."""


#: What a contract-2 renderer must have: its facts and every form (a form may
#: answer ``None``, but it must be there).
REQUIRED_MEMBERS = (
    "name",
    "minimum_server_version",
    "features",
    "classify_error",
    "case_insensitive_text",
    "partial_index_options",
    "search_index_options",
    "search_operand",
    "conflict_insert",
    "greatest",
    "id_in",
    "array_insert_sql",
    "lock_statement",
    "unlock_statement",
    "for_update",
    "relaxed_commit_clause",
    "wall_clock",
    "begin_statement",
    "transaction_options",
    "row_security",
)

#: The renderer members that write each feature form (optional members of
#: contract 2: a renderer without the feature may lack them, and its forms
#: refuse). A renderer that declares the feature must have every one
#: (SANDLOOM-B025):
#:
#: * ``array_type(item_type) -> TypeEngine | None``: the column type of
#:   :class:`Array`;
#: * ``fulltext_index_options() -> dict | None``: ``Index`` keyword arguments
#:   of a :func:`fulltext_index`;
#: * ``fulltext_document(columns, language) -> ColumnElement | None`` (a
#:   ``language`` of :data:`FULLTEXT_LANGUAGES`, mapped to the database's own
#:   configuration by the renderer): the
#:   searchable document of ``columns`` (an index expression and the left side
#:   of :func:`matches`; constants inline, never bound, so the query's
#:   expression is the index's);
#: * ``fulltext_match(document, query, language) -> ColumnElement[bool] |
#:   None``: ``document`` matches the search ``query`` (a bound text);
#: * ``json_path(column, path) -> ColumnElement[str] | None``: the scalar at
#:   ``path`` (a tuple of keys and indexes) as text, ``NULL`` when missing or
#:   JSON ``null``;
#: * ``json_contains(column, value) -> ColumnElement[bool] | None``: the JSON
#:   in ``column`` contains ``value`` (a bound JSON document);
#: * ``aggregate_list(value) -> ColumnElement | None``: the aggregate of
#:   :func:`aggregate_list` over ``value``;
#: * ``aggregate_list_type(item_type) -> TypeEngine | None``: the type the
#:   aggregate's result is read through, answering a Python list of items
#:   read as ``item_type`` (``None`` for no rows is read as ``[]``).
FEATURE_FORM_MEMBERS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "arrays": ("array_type",),
        "fulltext_search": (
            "fulltext_index_options",
            "fulltext_document",
            "fulltext_match",
        ),
        "json_path": ("json_path",),
        "json_contains": ("json_contains",),
        "list_aggregates": ("aggregate_list", "aggregate_list_type"),
    }
)

#: What a :class:`RowSecurity` must have.
ROW_SECURITY_MEMBERS = (
    "policy_name",
    "enable_statements",
    "disable_statements",
    "bind_statement",
    "begin_and_bind",
    "role_attributes",
    "privileged_memberships",
    "owned_secured_tables",
    "unconditional_policies",
    "secured_tables",
    "table_policies",
)


def _check_renderer(name: str, renderer: Any, *, source: str) -> None:
    """Refuse a renderer of another contract major, one missing a member of
    the contract, or one that declares a feature outside
    :data:`RENDERER_FEATURES` (SANDLOOM-B025)."""
    version = getattr(renderer, "contract_version", 1)
    if version != SQL_DIALECT_CONTRACT:
        raise IncompatibleSqlDialect(
            f"the SQL dialect renderer {name!r} ({source}) implements contract "
            f"version {version}; this Sandloom needs {SQL_DIALECT_CONTRACT} "
            "[SANDLOOM-B025]: upgrade the driver package"
        )
    missing = [member for member in REQUIRED_MEMBERS if not hasattr(renderer, member)]
    if missing:
        raise IncompatibleSqlDialect(
            f"the SQL dialect renderer {name!r} ({source}) lacks {missing} of "
            f"contract version {SQL_DIALECT_CONTRACT} [SANDLOOM-B025]: a form "
            "the database cannot write answers None, but it must be there"
        )
    security = renderer.row_security()
    lacking = [m for m in ROW_SECURITY_MEMBERS if not hasattr(security, m)]
    if security is not None and lacking:
        raise IncompatibleSqlDialect(
            f"the SQL dialect renderer {name!r} ({source}) answers row_security() "
            f"with an object that lacks {lacking} of contract version "
            f"{SQL_DIALECT_CONTRACT} [SANDLOOM-B025]: return None when the "
            "database has no row security"
        )
    prefix = [m for m in PREFIX_MEMBERS if callable(getattr(renderer, m, None))]
    if prefix and len(prefix) != len(PREFIX_MEMBERS):
        raise IncompatibleSqlDialect(
            f"the SQL dialect renderer {name!r} ({source}) writes {prefix} "
            f"without the rest of {list(PREFIX_MEMBERS)} [SANDLOOM-B025]: the "
            "pattern and the match that reads it come together"
        )
    if CONCURRENT_WRITERS in set(renderer.features()):
        raise IncompatibleSqlDialect(
            f"the SQL dialect renderer {name!r} ({source}) declares "
            f"{CONCURRENT_WRITERS!r} [SANDLOOM-B025]: it is derived from the "
            "serial_writers fact (a renderer without it has the feature); "
            "drop it from features()"
        )
    unknown = set(renderer.features()) - RENDERER_FEATURES
    if unknown:
        raise IncompatibleSqlDialect(
            f"the SQL dialect renderer {name!r} ({source}) declares features "
            f"{sorted(unknown)} outside the database capability's renderer "
            f"features {sorted(RENDERER_FEATURES)} [SANDLOOM-B025]"
        )
    unwritten = sorted(
        f"{feature}: {member}"
        for feature in set(renderer.features()) & set(FEATURE_FORM_MEMBERS)
        for member in FEATURE_FORM_MEMBERS[feature]
        if not callable(getattr(renderer, member, None))
    )
    if unwritten:
        raise IncompatibleSqlDialect(
            f"the SQL dialect renderer {name!r} ({source}) declares features "
            f"whose forms it does not write: {unwritten} [SANDLOOM-B025]: a "
            "renderer declares a feature form's feature only with its members"
        )


#: Optional members of contract 2, both or neither: a literal prefix filter
#: (the read planner's ``prefix=``). Without them: ``LIKE``/``ILIKE`` with a
#: backslash ``ESCAPE`` over a backslash-escaped pattern, which a database
#: whose ``LIKE`` ignores case (SQLite) answers case-insensitively.
PREFIX_MEMBERS = ("prefix_pattern", "prefix_match")


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def prefix_pattern(dialect: str, prefix: str, *, ignore_case: bool) -> str:
    """The value to bind for a literal ``prefix`` filter on ``dialect``,
    read by :func:`prefix_match`."""
    member = getattr(sql_dialect(dialect), "prefix_pattern", None)
    if callable(member):
        return str(member(prefix, ignore_case=ignore_case))
    return _like_prefix(prefix)


def prefix_match(
    dialect: str, column: Any, pattern: Any, *, ignore_case: bool
) -> ColumnElement[bool]:
    """``column`` starts with the prefix bound as ``pattern``
    (:func:`prefix_pattern`); case-sensitive unless ``ignore_case``."""
    member = getattr(sql_dialect(dialect), "prefix_match", None)
    if callable(member):
        return cast(
            "ColumnElement[bool]", member(column, pattern, ignore_case=ignore_case)
        )
    if ignore_case:
        return cast("ColumnElement[bool]", column.ilike(pattern, escape="\\"))
    return cast("ColumnElement[bool]", column.like(pattern, escape="\\"))


#: Derived, never declared: a renderer without ``serial_writers`` has it.
CONCURRENT_WRITERS = "concurrent_writers"


def renderer_features(dialect: str) -> frozenset[str]:
    """The features the renderer of ``dialect`` declares (empty without one),
    plus ``concurrent_writers`` unless it states ``serial_writers``: write
    transactions overlap, each holding its own row locks (what a race test
    needs). Derived from the fact, so the two never disagree; a renderer
    that lists it itself is not believed."""
    renderer = sql_dialect(dialect)
    if renderer is None:
        return frozenset()
    declared = frozenset(renderer.features()) & RENDERER_FEATURES
    declared -= {CONCURRENT_WRITERS}
    if not getattr(renderer, "serial_writers", False):
        declared |= {CONCURRENT_WRITERS}
    return declared


def schema_from_models(dialect: str) -> bool:
    """The renderer of ``dialect`` says its database takes its schema from
    the models, not the migration chains (SANDLOOM-B028). ``False`` without
    a renderer."""
    return bool(getattr(sql_dialect(dialect), "schema_from_models", False))


def development_only(dialect: str) -> bool:
    """The renderer of ``dialect`` says its database serves development and
    tests only (SANDLOOM-B029). ``False`` without a renderer."""
    return bool(getattr(sql_dialect(dialect), "development_only", False))


def serial_writers(dialect: str) -> bool:
    """The renderer of ``dialect`` says its database has one writer at a
    time: a nested write unit of the same task joins its caller's
    transaction as a SAVEPOINT. ``False`` without a renderer."""
    return bool(getattr(sql_dialect(dialect), "serial_writers", False))


def aware_times(dialect: str) -> bool:
    """The renderer of ``dialect`` says ``DateTime(timezone=True)`` keeps
    the instant and its DBAPI reads it back aware (UTC on asyncpg, the
    session's ``TimeZone`` on psycopg): :class:`UtcDateTime` converts nothing
    there. ``False`` without a renderer."""
    return bool(getattr(sql_dialect(dialect), "aware_times", False))


def skips_locked_rows(bind: Any) -> bool:
    """Whether the database of ``bind`` has ``skip_locked``: concurrent
    workers split a queue with ``FOR UPDATE SKIP LOCKED``. Without it the
    kernel's queues (the outbox relay, the saga sweep) claim a row by
    compare-and-set on its own lease column instead."""
    return "skip_locked" in renderer_features(dialect_name(bind))


def classify_error(dialect: str, exc: BaseException) -> ErrorKind:
    """What ``exc`` means on ``dialect``; ``"other"`` without a renderer."""
    renderer = sql_dialect(dialect)
    return "other" if renderer is None else renderer.classify_error(exc)


def _classified(renderer: SqlDialect, exc: BaseException) -> ErrorKind:
    """``renderer.classify_error(exc)``; ``"other"`` when the renderer itself
    raises, so it never masks the error being classified."""
    try:
        return renderer.classify_error(exc)
    except Exception:
        logging.getLogger("sandloom.sql_dialects").warning(
            "the %s renderer's classify_error raised; the error is classified as other",
            getattr(renderer, "name", "?"),
            exc_info=True,
        )
        return "other"


def is_error_kind(exc: BaseException, kind: ErrorKind) -> bool:
    """True when a registered renderer classifies ``exc``, or an error it
    wraps (``orig`` / ``__cause__`` / ``__context__``), as ``kind``.

    For a caller that holds an error and no bind (the database is not known
    there); an error path only, never per statement.
    """
    renderers = tuple(_renderers().values())
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseException) and any(
            _classified(renderer, current) == kind for renderer in renderers
        ):
            return True
        stack.extend(
            (
                getattr(current, "orig", None),
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            )
        )
    return False


class UnsupportedServerVersion(RuntimeError):
    """The server is older than its renderer supports (SANDLOOM-B030)."""


def check_server_version(dialect: Dialect) -> None:
    """Refuse a server below its renderer's minimum (SANDLOOM-B030). Called
    on the engine's first connection, when the version is known."""
    renderer = sql_dialect(dialect.name)
    minimum = None if renderer is None else renderer.minimum_server_version
    found = getattr(dialect, "server_version_info", None)
    if minimum is None or found is None:
        return
    if tuple(found[: len(minimum)]) < minimum:
        shown = ".".join(str(part) for part in found)
        needed = ".".join(str(part) for part in minimum)
        raise UnsupportedServerVersion(
            f"the {dialect.name} server is version {shown}; Sandloom needs "
            f"{needed} or newer [SANDLOOM-B030]"
        )


def register_sql_dialect(name: str, renderer: SqlDialect) -> None:
    """Register ``renderer`` for the SQLAlchemy dialect ``name``.

    A driver package normally names its renderer in the entry-point group
    instead; this is for tests and for a renderer built at run time.
    """
    _check_renderer(name, renderer, source="register_sql_dialect")
    with _lock:
        _registered[name] = renderer
        for index in list(_indexes):
            # A renderer registered again replaces the earlier one's options.
            index.info.get(RENDERED_ON, set()).discard(name)
            _apply_options(index, {name: renderer})


#: Set while this thread loads the entry points: a renderer module that uses a
#: form on import sees the renderers registered so far instead of recursing.
_loading = threading.local()


def _load_entry_points() -> None:
    if _state["loaded"] or getattr(_loading, "active", False):
        return
    _loading.active = True
    try:
        _load_entry_points_once()
    finally:
        _loading.active = False


def _load_entry_points_once() -> None:
    # Load outside the lock's critical section: a renderer module may import
    # models (which call the forms) or register itself, and must not wait on
    # a lock its own import holds. The RLock only makes publishing atomic.
    found = [
        (entry.name, entry)
        for entry in metadata.entry_points().select(group=SQL_DIALECTS_GROUP)
    ]
    loaded: dict[str, SqlDialect] = {}
    for name, entry in found:
        try:
            renderer = entry.load()
        except Exception as exc:
            raise RuntimeError(
                f"the SQL dialect renderer {name!r} ({entry.value}) in "
                f"{SQL_DIALECTS_GROUP!r} failed to load: {exc}"
            ) from exc
        _check_renderer(name, renderer, source=entry.value)
        loaded[name] = renderer
    with _lock:
        if _state["loaded"]:
            return
        for name, renderer in loaded.items():
            _registered.setdefault(name, renderer)
        for index in list(_indexes):
            _apply_options(index, _registered)
        _state["loaded"] = True


def _renderers() -> dict[str, SqlDialect]:
    _load_entry_points()
    with _lock:
        return dict(_registered)


def sql_dialect(name: str) -> SqlDialect | None:
    """The renderer for the SQLAlchemy dialect ``name``, if one is registered."""
    if _state["loaded"]:
        # The hot paths (claims, outbox writes) ask once per statement: a
        # dict read, no lock and no copy, once the entry points are read.
        return _registered.get(name)
    return _renderers().get(name)


def dialect_name(bind: Any) -> str:
    """The SQLAlchemy dialect name of ``bind``: a name, a ``Dialect``, a
    connection or engine (sync or async), or a session (sync or async)."""
    if isinstance(bind, str):
        return bind
    # A session (the hot callers pass one): its own bind is what
    # ``get_bind()`` answers without a mapper. Read on the sync session,
    # whose ``bind`` and the engine's ``dialect`` are plain attributes (the
    # async session's are proxies).
    session = getattr(bind, "sync_session", None)
    if session is None and isinstance(bind, Session):
        session = bind
    if session is not None:
        engine = session.bind
        dialect = (engine if engine is not None else session.get_bind()).dialect
    elif isinstance(bind, Dialect):
        dialect = bind
    else:
        # a connection or an engine (sync or async); else anything with
        # ``get_bind()``
        dialect = getattr(bind, "dialect", None)
        if dialect is None:
            dialect = bind.get_bind().dialect
    name: str = dialect.name
    return name


def has_sql_dialect(name: str) -> bool:
    """Whether the SQLAlchemy dialect ``name`` has a renderer."""
    return sql_dialect(name) is not None


def dialect_of_url(url: str) -> str:
    """The SQLAlchemy dialect name a database URL names (``postgresql``)."""
    return make_url(url).get_backend_name()


def unrendered_partial_indexes(
    metadata: MetaData, dialects: Iterable[str]
) -> list[str]:
    """``table.index (dialect)`` for each :func:`partial_index` that has no
    condition on one of ``dialects``: created there, it would index every row.
    Startup and the migration environment refuse on any (SANDLOOM-R015)."""
    wanted = set(dialects)
    found: list[str] = []
    for table in metadata.tables.values():
        for index in table.indexes:
            if PARTIAL_WHERE not in index.info:
                continue
            missing = wanted - set(index.info.get(RENDERED_ON, ()))
            found.extend(f"{table.name}.{index.name} ({d})" for d in sorted(missing))
    return found


def unrendered_dialect(url: str) -> str | None:
    """The dialect name of the database ``url`` when it has no renderer
    (the startup check, SANDLOOM-B024), else ``None``."""
    dialect = dialect_of_url(url)
    return None if has_sql_dialect(dialect) else dialect


def _require(dialect: str, form: str) -> SqlDialect:
    renderer = sql_dialect(dialect)
    if renderer is None:
        raise UnsupportedSqlForm(form, dialect, renderer=False)
    return renderer


class CaseInsensitiveText(TypeDecorator[str]):
    """Text that compares, sorts and enforces uniqueness without regard to case.

    The stored value keeps the case it was written with: ``Ann@Example.com``
    reads back as written and is equal to ``ann@example.com``.
    ``CaseInsensitiveText(80)`` caps the length where the database stores one.
    How far case folding reaches is the database's: PostgreSQL's ``citext``
    folds every letter, SQLite's ``NOCASE`` only ASCII letters.
    """

    impl = String
    cache_ok = True

    def __init__(self, length: int | None = None) -> None:
        super().__init__(length)
        self.length = length

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """The database's own type for this column, from its renderer."""
        form = _require(dialect.name, "CaseInsensitiveText")
        type_ = form.case_insensitive_text(self.length)
        if type_ is None:
            raise UnsupportedSqlForm("CaseInsensitiveText", dialect.name)
        return dialect.type_descriptor(type_)


class NaiveDateTimeError(ValueError):
    """A naive ``datetime`` bound to a :class:`UtcDateTime` column: its
    instant is not known (PostgreSQL would read it in the process's local
    time zone, SQLite as UTC), so it is refused rather than guessed."""

    def __init__(self, value: datetime) -> None:
        super().__init__(
            f"a naive datetime ({value.isoformat()}) bound to a UtcDateTime "
            "column: pass an aware one, e.g. datetime.now(UTC) or "
            "value.replace(tzinfo=UTC)"
        )


def _refuse_naive(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise NaiveDateTimeError(value)
    return value


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware time: always read back aware, in UTC on asyncpg and
    SQLite.

    The column is ``DateTime(timezone=True)`` (``TIMESTAMP WITH TIME ZONE`` on
    PostgreSQL, which keeps the instant and returns an aware value: in UTC
    on asyncpg, in the session's ``TimeZone`` on psycopg). There
    (the renderer's ``aware_times``) the type converts nothing. A database
    that keeps no offset (SQLite stores the text of the time) hands back a
    naive value: there this type writes an aware time in UTC and attaches UTC
    to what it reads, so a module compares and orders the same aware values on
    every database. An expression typed ``UtcDateTime`` that PostgreSQL
    answers as ``timestamp without time zone`` (``... AT TIME ZONE 'UTC'``)
    reads back naive there. Bind aware times: a naive one has no known instant (on
    PostgreSQL asyncpg would read it in the process's local time zone, on
    SQLite it would mean UTC), so it is refused with
    :class:`NaiveDateTimeError`, in a value and in a comparison alike. The
    planner's raw executor, which skips bind processing, takes no caller's
    value of this type: such a key or filter runs on the general executor.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    # SQLAlchemy memoises both processors per dialect: the fact is read once.
    def bind_processor(self, dialect: Dialect) -> Any:
        """Where the database keeps the instant (``aware_times``), only the
        refusal of a naive time; elsewhere :meth:`process_bind_param`."""
        if not aware_times(dialect.name):
            return super().bind_processor(dialect)
        impl = self.impl_instance.bind_processor(dialect)
        if impl is None:
            return _refuse_naive
        return lambda value: impl(_refuse_naive(value))

    def result_processor(self, dialect: Dialect, coltype: Any) -> Any:
        """Where the DBAPI reads aware values (``aware_times``), the
        column's own processor (none on asyncpg); elsewhere
        :meth:`process_result_value`."""
        if not aware_times(dialect.name):
            return super().result_processor(dialect, coltype)
        return self.impl_instance.result_processor(dialect, coltype)

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """An aware value as the same instant in UTC; a naive one refused."""
        # The same instant in UTC: SQLite stores the text of the time and
        # drops the offset.
        if value is None:
            return value
        if value.tzinfo is None:
            raise NaiveDateTimeError(value)
        return value.astimezone(UTC)

    def coerce_compared_value(self, op: Any, value: Any) -> Any:
        """A time compared with the column is bound as this type (an aware
        time is required); anything else (``column - timedelta(...)``) as
        ``DateTime`` would bind it (an interval)."""
        if isinstance(value, datetime):
            return self
        return self.impl_instance.coerce_compared_value(op, value)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """A loaded value with UTC attached when the database kept no offset."""
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class WallClock(FunctionElement[datetime]):
    """The database's clock when the statement runs ("now, at the call").

    A column's ``server_default=WallClock()`` or ``onupdate=WallClock()``,
    and "now" in a statement: ``.values(revoked_at=WallClock())``,
    ``.where(Model.expires_at > WallClock())``. Each database's renderer
    writes it (:meth:`SqlDialect.wall_clock`): PostgreSQL
    ``clock_timestamp()``, SQLite the current UTC time to the millisecond in
    the text a bound ``UtcDateTime`` has. Unlike a transaction's start time
    (``now()``), rows one transaction writes get ordered times; order ties by
    a UUIDv7 id. A module writes it instead of ``func.now()``, which SQLite
    answers in whole seconds (SANDLOOM-C031).
    """

    type = UtcDateTime()
    name = "wall_clock"
    inherit_cache = True


@compiles(WallClock)
def _compile_wall_clock(_element: WallClock, compiler: SQLCompiler, **_kw: Any) -> str:
    if isinstance(compiler, StrSQLCompiler):
        # ``str(statement)`` only: SQLAlchemy's stringifier names no database,
        # so neither does this. Compiling for a dialect without a renderer,
        # SQLAlchemy's bare ``DefaultDialect()`` ('default') included, refuses
        # below, so no DDL or executed statement ever carries this text.
        return "wall_clock()"
    dialect = compiler.dialect.name
    sql = _require(dialect, "WallClock").wall_clock()
    if not sql:
        raise UnsupportedSqlForm("WallClock", dialect)
    return sql


def _condition(where: str | ColumnElement[bool] | TextClause) -> Any:
    return text(where) if isinstance(where, str) else where


def _track(index: Index) -> Index:
    with _lock:
        _indexes.add(index)
    _apply_options(index, _renderers())
    return index


def partial_index(
    name: str,
    *columns: str | ColumnElement[Any],
    where: str | ColumnElement[bool] | TextClause,
    unique: bool = False,
) -> Index:
    """An index over the rows ``where`` selects, for ``__table_args__``.

    ``unique=True`` makes the columns unique among those rows only, such as
    one pending invitation per email::

        partial_index("uq_inv", "email", where="accepted_at IS NULL", unique=True)

    Each database's renderer writes the condition its own way. A database
    whose renderer cannot is refused at startup and by the migration
    environment rather than given an index over every row.
    """
    index = Index(name, *columns, unique=unique)
    index.info[PARTIAL_WHERE] = _condition(where)
    return _track(index)


class _SearchOperand(ColumnClause[Any]):
    """One column of a :func:`search_index`, written by the database's renderer."""

    # DDL only: never part of a cached statement.
    inherit_cache = False

    def __init__(self, column: str, case_insensitive: bool) -> None:
        super().__init__(column)
        self.case_insensitive = case_insensitive


@compiles(_SearchOperand)
def _compile_search_operand(
    element: _SearchOperand, compiler: SQLCompiler, **_kw: Any
) -> str:
    dialect = compiler.dialect.name
    sql = _require(dialect, "search_index").search_operand(
        element.name, element.case_insensitive
    )
    if sql is None:
        raise UnsupportedSqlForm("search_index", dialect)
    return sql


def _creates_search_index(*_args: Any, dialect: Dialect, **_kw: Any) -> bool:
    renderer = sql_dialect(dialect.name)
    return renderer is not None and renderer.search_index_options() is not None


def search_index(
    name: str,
    *columns: str,
    case_insensitive: Iterable[str] = (),
) -> Index:
    """An index that serves substring search (``column.ilike('%term%')``).

    Name each :class:`CaseInsensitiveText` column in ``case_insensitive``; a
    search on it compares ``cast(column, Text)`` to use the index. The index
    is created only on a database whose renderer has one (PostgreSQL: a
    ``pg_trgm`` GIN index, the extension the generated deploy files create);
    elsewhere the search runs unindexed.
    """
    folded = frozenset(case_insensitive)
    unknown = folded.difference(columns)
    if unknown:
        raise ValueError(
            f"search_index {name!r}: case_insensitive names columns it does "
            f"not index: {sorted(unknown)}"
        )
    operands = [_SearchOperand(column, column in folded) for column in columns]
    index = Index(name, *operands).ddl_if(callable_=_creates_search_index)
    index.info[INDEX_FORM] = "search_index"
    return _track(index)


def conflict_insert(table: Any, *, session: AsyncSession) -> ConflictInsert:
    """``INSERT`` with ``.on_conflict_do_nothing()`` / ``.on_conflict_do_update()``.

    Takes a model class or a table and the session the statement runs on, and
    returns the insert of that session's database, with ``.values()``,
    ``.returning()`` and ``.excluded``. A database without ``ON CONFLICT``
    refuses with :class:`UnsupportedSqlForm`.
    """
    return conflict_insert_for(session, table)


def conflict_insert_for(bind: Any, table: Any) -> ConflictInsert:
    """:func:`conflict_insert` where only a dialect, a dialect name or a
    connection is at hand (``bind``, read by :func:`dialect_name`)."""
    dialect = dialect_name(bind)
    renderer = _registered.get(dialect) if _state["loaded"] else sql_dialect(dialect)
    if renderer is None:
        raise UnsupportedSqlForm("conflict_insert", dialect, renderer=False)
    statement = renderer.conflict_insert(table)
    if statement is None:
        raise UnsupportedSqlForm("conflict_insert", dialect)
    return statement


def greatest_for(bind: Any, *exprs: Any) -> ColumnElement[Any]:
    """The largest of ``exprs`` on the database of ``bind`` (see
    :func:`dialect_name`); ``NULL`` arguments follow the database's rule."""
    dialect = dialect_name(bind)
    element = _require(dialect, "greatest").greatest(*exprs)
    if element is None:
        raise UnsupportedSqlForm("greatest", dialect)
    return element


def greatest(*exprs: Any, session: AsyncSession) -> ColumnElement[Any]:
    """The largest of ``exprs``, written for ``session``'s database::

        set_={"seen": greatest(Item.seen, stmt.excluded.seen, session=s)}

    Give it non-``NULL`` arguments (``coalesce`` them): databases disagree on
    what the largest of a ``NULL`` is.
    """
    return greatest_for(session, *exprs)


def id_in_for(
    bind: Any, column: Any, ids: Sequence[Any], *, key: str = "ids"
) -> ColumnElement[bool]:
    """``column`` is one of ``ids`` on the database of ``bind``, bound as
    the parameter ``key`` (a prebuilt statement takes ``{key: [...]}`` at
    execution). PostgreSQL binds one array (``= ANY(:key)``), so the
    statement is the same for any number of ids."""
    dialect = dialect_name(bind)
    element = _require(dialect, "id_in").id_in(column, ids, key=key)
    if element is None:
        raise UnsupportedSqlForm("id_in", dialect)
    return element


def array_insert_sql_for(bind: Any, inserts: Sequence[ArrayInsert]) -> str | None:
    """The renderer's :meth:`SqlDialect.array_insert_sql` for the database
    of ``bind``; ``None`` without one (the caller writes row by row)."""
    renderer = sql_dialect(dialect_name(bind))
    return None if renderer is None else renderer.array_insert_sql(inserts)


def optional_lock_statement_for(
    bind: Any, key: LockKey, *, scope: LockScope, wait: bool
) -> LockStatement | None:
    """The renderer's :meth:`SqlDialect.lock_statement` for the database of
    ``bind`` (see :func:`dialect_name`: a sync connection or an async
    session alike); ``None`` without a renderer or without such a lock, for
    a caller that has a correct answer unlocked. Run it as
    ``execute(lock.statement, lock.parameters)``."""
    dialect = dialect_name(bind)
    renderer = _registered.get(dialect) if _state["loaded"] else sql_dialect(dialect)
    if renderer is None:
        check_lock_key(key)
        return None
    return renderer.lock_statement(key, scope=scope, wait=wait)


def lock_statement_for(
    bind: Any, key: LockKey, *, scope: LockScope, wait: bool
) -> LockStatement:
    """:func:`optional_lock_statement_for` that refuses a database without
    such a lock (:class:`UnsupportedSqlForm`, SANDLOOM-R015) rather than run
    the section unlocked. The caller executes it; ``wait=False`` selects
    whether it locked."""
    dialect = dialect_name(bind)
    statement = _require(dialect, "lock_statement").lock_statement(
        key, scope=scope, wait=wait
    )
    if statement is None:
        raise UnsupportedSqlForm(f"lock_statement(scope={scope!r})", dialect)
    return statement


def unlock_statement_for(bind: Any, key: LockKey) -> LockStatement:
    """The statement that releases the session lock ``key`` taken with
    :func:`lock_statement_for` on the same connection."""
    dialect = dialect_name(bind)
    statement = _require(dialect, "unlock_statement").unlock_statement(key)
    if statement is None:
        raise UnsupportedSqlForm("unlock_statement", dialect)
    return statement


def for_update_for(
    bind: Any, stmt: Select[Any], *, skip_locked: bool, read: bool = False
) -> Select[Any]:
    """The renderer's :meth:`SqlDialect.for_update` of ``stmt`` for the
    database of ``bind``; refuses a database without a renderer."""
    dialect = dialect_name(bind)
    renderer = _registered.get(dialect) if _state["loaded"] else sql_dialect(dialect)
    if renderer is None:
        raise UnsupportedSqlForm("for_update", dialect, renderer=False)
    return renderer.for_update(stmt, skip_locked=skip_locked, read=read)


def row_security_for(bind: Any) -> RowSecurity | None:
    """The renderer's :meth:`SqlDialect.row_security` for the database of
    ``bind`` (see :func:`dialect_name`); ``None`` without a renderer or
    without row security. Ask once per engine, not per statement."""
    renderer = sql_dialect(dialect_name(bind))
    return None if renderer is None else renderer.row_security()


def row_security_of_url(url: str) -> RowSecurity | None:
    """:func:`row_security_for` the database a URL names (build-free)."""
    return row_security_for(dialect_of_url(url))


_NO_OPTIONS: Mapping[str, Any] = MappingProxyType({})


def begin_statement_for(
    bind: Any, *, write: bool, read_only: bool, snapshot: bool
) -> str | None:
    """The renderer's :meth:`SqlDialect.begin_statement` for the database of
    ``bind``; ``None`` (the plain ``BEGIN``) without a renderer. Ask once per
    engine and combination, not per unit."""
    check_unit_shape(write=write, read_only=read_only)
    renderer = sql_dialect(dialect_name(bind))
    if renderer is None:
        return None
    return renderer.begin_statement(write=write, read_only=read_only, snapshot=snapshot)


def transaction_options_for(
    bind: Any, *, read_only: bool, snapshot: bool | None
) -> Mapping[str, Any]:
    """The renderer's :meth:`SqlDialect.transaction_options` for the database
    of ``bind``, read-only; empty without a renderer. Ask once per engine and
    combination, not per unit."""
    renderer = sql_dialect(dialect_name(bind))
    if renderer is None:
        return _NO_OPTIONS
    return MappingProxyType(
        dict(renderer.transaction_options(read_only=read_only, snapshot=snapshot))
    )


def relaxed_commit_clause_for(bind: Any) -> ColumnElement[bool] | None:
    """The renderer's :meth:`SqlDialect.relaxed_commit_clause` for the
    database of ``bind``; ``None`` without a renderer or without one."""
    renderer = sql_dialect(dialect_name(bind))
    return None if renderer is None else renderer.relaxed_commit_clause()


#: Connection ``info`` key the driver sets while the running transaction
#: holds the database's only write lock (a database whose writers are serial:
#: SQLite's ``BEGIN IMMEDIATE``), and clears when it ends. There every
#: transaction lock is already held, so a renderer answers
#: :meth:`SqlDialect.lock_statement` with ``None`` and the kernel's
#: transaction locks succeed only inside such a transaction.
WRITE_LOCK_HELD = "sandloom.write_lock_held"
#: Connection ``info`` key beside :data:`WRITE_LOCK_HELD`: a weak reference to
#: the asyncio task whose transaction took the lock (a driver of a database
#: whose writers are serial sets it; kernel use: nested units).
WRITE_LOCK_TASK = "sandloom.write_lock_task"
#: Connection ``info`` key: a transaction that has not taken the write lock
#: yet but may (one begun at its first write): a sync callable of the
#: connection that takes it now (kernel use: :func:`hold_transaction_lock`).
TAKE_WRITE_LOCK = "sandloom.take_write_lock"


async def hold_transaction_lock(
    session: AsyncSession, key: LockKey, *, form: str = "transaction_lock"
) -> None:
    """Hold the lock ``key`` in ``session``'s transaction until it ends.

    The renderer's ``lock_statement(scope="transaction", wait=True)`` where
    the database has named locks. Where it has none but its writers are
    serial (the ``transaction_locks`` feature without a statement), the
    transaction must already hold the write lock (:data:`WRITE_LOCK_HELD`;
    the session begins one if it has not yet). Anything else refuses
    (:class:`UnsupportedSqlForm`, SANDLOOM-R015) rather than run the section
    unlocked.
    """
    dialect = dialect_name(session)
    renderer = _require(dialect, form)
    lock = renderer.lock_statement(key, scope="transaction", wait=True)
    if lock is not None:
        await session.execute(lock.statement, lock.parameters)
        return
    if "transaction_locks" in renderer_features(dialect):
        connection = await session.connection()
        info = connection.info
        if info.get(WRITE_LOCK_HELD):
            return
        take = info.get(TAKE_WRITE_LOCK)
        if take is not None:
            await connection.run_sync(take)
            if info.get(WRITE_LOCK_HELD):
                return
        raise UnsupportedSqlForm(
            f"{form} outside a unit that may write (a read-only unit holds no "
            "write lock)",
            dialect,
        )
    raise UnsupportedSqlForm(form, dialect)


async def transaction_lock(key: int, *, session: AsyncSession) -> None:
    """Hold the lock ``key`` until ``session``'s transaction ends.

    Two transactions that take the same key run the code after it one at a
    time; the lock is released by the commit or the rollback, never by hand.
    ``key`` is a signed 64-bit integer the module chooses and keeps stable. A
    database without such a lock refuses rather than run the section unlocked;
    on SQLite, whose writers are serial, every unit that may write already
    holds it and a read-only unit is refused.
    """
    await hold_transaction_lock(session, key)


# --------------------------------------------------------------------------- #
# Feature forms: each needs a ``database`` feature (DATABASE_FORM_FEATURES)
# --------------------------------------------------------------------------- #


#: SQLAlchemy's traversal spec of a clause: what its cache key and copy see.
_Internals = list[tuple[str, InternalTraversal]]


_FULLTEXTDOCUMENT_INTERNALS: _Internals = [
    ("columns", InternalTraversal.dp_clauseelement_tuple),
    ("language", InternalTraversal.dp_string),
]

_FULLTEXTMATCH_INTERNALS: _Internals = [
    ("document", InternalTraversal.dp_clauseelement),
    ("query", InternalTraversal.dp_clauseelement),
    ("language", InternalTraversal.dp_string),
]

_JSONPATH_INTERNALS: _Internals = [
    ("column", InternalTraversal.dp_clauseelement),
    # The path is part of the statement's cache key: the renderer binds
    # its steps when it writes the statement.
    ("path", InternalTraversal.dp_plain_obj),
]

_JSONCONTAINS_INTERNALS: _Internals = [
    ("column", InternalTraversal.dp_clauseelement),
    ("value", InternalTraversal.dp_clauseelement),
]


def _feature_member(dialect: str, form: str, member: str) -> Any:
    """The renderer's ``member`` writing ``form`` on ``dialect``; refuses a
    database without a renderer, or whose renderer lacks the member (naming
    the form's feature when it has one)."""
    renderer = _require(dialect, form)
    found = getattr(renderer, member, None)
    if found is None:
        raise UnsupportedSqlForm(
            form, dialect, feature=DATABASE_FORM_FEATURES.get(form)
        )
    return found


def _written(element: Any, form: str, dialect: str) -> Any:
    if element is None:
        raise UnsupportedSqlForm(
            form, dialect, feature=DATABASE_FORM_FEATURES.get(form)
        )
    return element


class Array(TypeDecorator[list[Any]]):
    """A column holding a list of ``item_type`` values (feature ``arrays``).

    ``tags: Mapped[list[str]] = mapped_column(Array(String(40)))``. The
    database stores it as its own array type (PostgreSQL ``VARCHAR(40)[]``);
    a database without arrays refuses the column when its DDL or a statement
    is compiled (SANDLOOM-R015), and startup refuses a module whose table has
    one on such an instance (SANDLOOM-B033). Find a row by an item with
    ``sqlalchemy.any_``: ``select(T).where(value == any_(T.tags))``.
    """

    impl = ARRAY
    cache_ok = True

    def __init__(self, item_type: TypeEngine[Any] | type[TypeEngine[Any]]) -> None:
        item = to_instance(item_type)
        if isinstance(item, (Array, ARRAY)):
            raise TypeError("Array(item_type): an item type is not itself an array")
        super().__init__(item)
        self.item_type = item

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """The database's own array type, from its renderer."""
        type_ = _feature_member(dialect.name, "Array", "array_type")(self.item_type)
        return dialect.type_descriptor(_written(type_, "Array", dialect.name))

    @property
    def python_type(self) -> type[list[Any]]:
        """``list``: a value of the column is a list of items."""
        return list


#: The languages a full-text form takes, by their English name: stemming and
#: stop words of that language, or ``"simple"`` for neither. A renderer maps
#: each to its database's own configuration (PostgreSQL: the text search
#: configuration of the same name).
FULLTEXT_LANGUAGES: frozenset[str] = frozenset(
    {
        "simple",
        "arabic",
        "armenian",
        "basque",
        "catalan",
        "danish",
        "dutch",
        "english",
        "finnish",
        "french",
        "german",
        "greek",
        "hindi",
        "hungarian",
        "indonesian",
        "irish",
        "italian",
        "lithuanian",
        "nepali",
        "norwegian",
        "portuguese",
        "romanian",
        "russian",
        "serbian",
        "spanish",
        "swedish",
        "tamil",
        "turkish",
        "yiddish",
    }
)


def _check_language(language: str) -> str:
    if not isinstance(language, str) or language not in FULLTEXT_LANGUAGES:
        raise ValueError(
            f"a full-text search language is a language's English name such as "
            f"'english', 'german' or 'russian', or 'simple' for no stemming; got "
            f"{language!r} (one of: {', '.join(sorted(FULLTEXT_LANGUAGES))})"
        )
    return language


def _columns(column: Any) -> tuple[ColumnElement[Any], ...]:
    # ``column``: a column, or a list / tuple of them (see ``matches``).
    items = column if isinstance(column, (list, tuple)) else (column,)
    if not items:
        raise ValueError("matches() needs at least one column")
    return tuple(coercions.expect(roles.ExpressionElementRole, c) for c in items)


class _FulltextDocument(ColumnElement[Any]):
    """The searchable document of some text columns, written by the renderer
    (an index expression, and the left side of :func:`matches`)."""

    __visit_name__ = "sandloom_fulltext_document"
    inherit_cache = True
    _traverse_internals = _FULLTEXTDOCUMENT_INTERNALS

    def __init__(self, columns: Sequence[ColumnElement[Any]], language: str) -> None:
        self.columns = tuple(columns)
        self.language = language


@compiles(_FulltextDocument)
def _compile_fulltext_document(
    element: _FulltextDocument, compiler: SQLCompiler, **kw: Any
) -> str:
    dialect = compiler.dialect.name
    member = _feature_member(dialect, "fulltext_index", "fulltext_document")
    document = member(element.columns, element.language)
    return str(compiler.process(_written(document, "fulltext_index", dialect), **kw))


class _FulltextMatch(ColumnElement[bool]):
    """``document`` matches a search query, written by the renderer."""

    __visit_name__ = "sandloom_fulltext_match"
    inherit_cache = True
    type = Boolean()
    _traverse_internals = _FULLTEXTMATCH_INTERNALS

    def __init__(
        self, document: _FulltextDocument, query: ColumnElement[str], language: str
    ) -> None:
        self.document = document
        self.query = query
        self.language = language


@compiles(_FulltextMatch)
def _compile_fulltext_match(
    element: _FulltextMatch, compiler: SQLCompiler, **kw: Any
) -> str:
    dialect = compiler.dialect.name
    member = _feature_member(dialect, "matches", "fulltext_match")
    condition = member(element.document, element.query, element.language)
    return str(compiler.process(_written(condition, "matches", dialect), **kw))


def fulltext_index(name: str, *columns: str, language: str = "english") -> Index:
    """An index that serves :func:`matches` over ``columns`` (feature
    ``fulltext_search``), for ``__table_args__``::

        fulltext_index("ix_articles_text", "title", "body")

    ``language`` is the language of the text by its English name
    (``"english"``, ``"german"``, ``"russian"``, ...: its stemming and stop
    words; ``"simple"`` for neither), one of :data:`FULLTEXT_LANGUAGES`; each
    database's renderer maps it to its own configuration. :func:`matches` over the same
    columns, in the same order and language, uses the index. PostgreSQL: a GIN
    index over ``to_tsvector``. A database without full-text search refuses
    the index when its DDL is compiled, and startup refuses the module on
    such an instance (SANDLOOM-B033).
    """
    if not columns:
        raise ValueError(f"fulltext_index {name!r}: name at least one column")
    document = _FulltextDocument(
        [ColumnClause(column) for column in columns], _check_language(language)
    )
    index = Index(name, document)
    index.info[INDEX_FORM] = "fulltext_index"
    return _track(index)


def matches(
    column: ColumnElement[Any]
    | QueryableAttribute[Any]
    | Sequence[ColumnElement[Any] | QueryableAttribute[Any]],
    query: str | ColumnElement[str],
    *,
    language: str = "english",
) -> ColumnElement[bool]:
    """``column`` (or a list of columns) matches the search ``query``
    (feature ``fulltext_search``)::

        select(Article).where(matches([Article.title, Article.body], "cat -dog"))

    ``query`` is what a user types: words, ``"quoted phrases"``, ``or`` and
    ``-excluded`` words (PostgreSQL ``websearch_to_tsquery``). Name the
    columns of a :func:`fulltext_index`, in its order, and its language to
    use it: with another language the database scans every row. A database
    without full-text search refuses the statement when it is compiled
    (SANDLOOM-R015).
    """
    lang = _check_language(language)
    bound: ColumnElement[str] = (
        bindparam(None, query, type_=Text())
        if isinstance(query, str)
        else coercions.expect(roles.ExpressionElementRole, query)
    )
    return _FulltextMatch(_FulltextDocument(_columns(column), lang), bound, lang)


#: One step of a :func:`json_path`: an object key or an array index.
JsonPathKey = str | int


#: The operators whose other side a json_path text checks: a text compared
#: with a number is an error on PostgreSQL and a different answer on SQLite
#: (any TEXT sorts after any number there), so it is refused.
_JSON_PATH_COMPARISONS = frozenset(
    {
        operators.eq,
        operators.ne,
        operators.lt,
        operators.le,
        operators.gt,
        operators.ge,
        operators.between_op,
        operators.not_between_op,
        operators.in_op,
        operators.not_in_op,
        operators.is_distinct_from,
        operators.is_not_distinct_from,
    }
)


def _is_json_path_text(element: Any) -> bool:
    return isinstance(getattr(element, "type", None), _JsonPathText)


def _is_text_type(kind: Any) -> bool:
    """A string type (``String``, ``Text``, a decorator over one); never
    ``NullType``: an untyped ``literal_column("1")`` may be a number."""
    if kind is None or isinstance(kind, NullType):
        return False
    try:
        return issubclass(kind._type_affinity, String)
    except TypeError:  # an affinity that is not a class
        return False


def _json_path_operand_problem(other: Any) -> str | None:
    """Why ``other`` cannot be compared with a json_path text, or None."""
    if other is None or isinstance(other, str):
        return None
    if isinstance(other, (list, tuple, set, frozenset)):
        return next(
            (p for p in map(_json_path_operand_problem, other) if p is not None), None
        )
    if hasattr(other, "__clause_element__"):
        other = other.__clause_element__()
    if not isinstance(other, ClauseElement):
        return f"the {type(other).__name__} {other!r}"
    return _json_path_clause_problem(other)


def _json_path_clause_problem(other: ClauseElement) -> str | None:
    """Why the SQL expression ``other`` cannot be compared with a json_path
    text, or None: a subquery's columns, a grouping's element, a list's
    items are read; ``NULL`` passes; anything else must be text."""
    columns = getattr(other, "selected_columns", None)
    if columns is not None:
        return next(
            (p for p in map(_json_path_clause_problem, columns) if p is not None),
            None,
        )
    if isinstance(other, Null):
        return None
    if isinstance(other, (Grouping, ScalarSelect)):
        return _json_path_clause_problem(cast("ClauseElement", other.element))
    if isinstance(other, (ClauseList, Tuple)):
        return next(
            (p for p in map(_json_path_clause_problem, other.clauses) if p is not None),
            None,
        )
    if isinstance(other, BindParameter) and isinstance(other.type, NullType):
        return None  # typed as the json_path's text by the comparison
    kind = getattr(other, "type", None)
    if _is_text_type(kind):
        return None
    return f"an expression of type {kind!r}"


def _refuse_json_path_operand(other: Any) -> None:
    problem = _json_path_operand_problem(other)
    if problem is not None:
        _refuse_json_path_operand_problem(problem)


class _JsonPathText(TypeDecorator[str]):
    """:func:`json_path`'s type: text that refuses to be compared with a
    number (risk fix 3). The scalar's JSON text is the same on every
    database; a number compared with it is not. Refused when the expression
    is built (this comparator), when the statement compiles (a json_path on
    the right of a comparison, ``coalesce`` with a number) and when a value
    is bound (a non-``str`` given to a parameter compared with it)."""

    impl = Text
    cache_ok = True

    class Comparator(TypeDecorator.Comparator[str]):
        def operate(self, op: Any, *other: Any, **kwargs: Any) -> Any:
            if op in _JSON_PATH_COMPARISONS:
                for operand in other:
                    _refuse_json_path_operand(operand)
            return super().operate(op, *other, **kwargs)

        def reverse_operate(self, op: Any, other: Any, **kwargs: Any) -> Any:
            if op in _JSON_PATH_COMPARISONS:
                _refuse_json_path_operand(other)
            return super().reverse_operate(op, other, **kwargs)

    comparator_factory = Comparator

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        # A parameter compared with a json_path (``== bindparam("x")``) is
        # typed as it: a number bound later fails the same on every database.
        if value is None or isinstance(value, str):
            return value
        _refuse_json_path_operand(value)
        return None  # unreachable: a non-str value is refused


#: The compile dispatch ``BinaryExpression`` had when this module was
#: imported: SQLAlchemy's generated one, or a ``sqlalchemy.ext.compiler``
#: dispatcher an application registered first (``@compiles(BinaryExpression,
#: "<dialect>")``). The refusing dispatch runs its check, then hands over to
#: it, so such a hook keeps working; with none, it calls ``visit_binary``
#: itself (no extra frame). A hook registered later wraps the refusing
#: dispatch as its default, or joins the earlier dispatcher (which it calls).
_BINARY_DISPATCH_BEFORE = BinaryExpression.__dict__["_compiler_dispatch"]
_BINARY_HOOKED = _BINARY_DISPATCH_BEFORE is not BinaryExpression.__dict__.get(
    "_original_compiler_dispatch"
)
#: ``coalesce``'s own dispatch when this module was imported (an application's
#: ``@compiles(coalesce)``), else ``None``: the refusing dispatch then hands
#: over to its parent's dispatch, looked up at each compile (a ``coalesce`` is
#: rare; a hook on ``Function`` registered later still applies).
_COALESCE_DISPATCH_BEFORE = sql_functions.coalesce.__dict__.get("_compiler_dispatch")
_COALESCE_PARENT: Any = sql_functions.coalesce.__mro__[1]
_COALESCE_PARENT_GENERATED = _COALESCE_PARENT._original_compiler_dispatch


def _binary_dispatch(element: BinaryExpression[Any], visitor: Any, **kw: Any) -> str:
    """``BinaryExpression``'s compile dispatch: refuse a json_path compared
    with a number, then compile as before (SQLAlchemy's ``visit_binary``, or
    the dispatch installed before this module, see
    :data:`_BINARY_DISPATCH_BEFORE`).

    The comparator refuses a number on the right of a json_path when the
    expression is built; this sees the other order (``Model.id ==
    json_path(...)``: the left side's comparator built it, and no hook of
    the json_path's type is consulted there) and anything typed after the
    fact. It replaces the generated dispatch (one frame, as before), so an
    expression without a json_path pays one ``frozenset`` membership and,
    for a comparison, two type checks: no ``sqlalchemy.ext.compiler``
    dispatch per binary (measured: ``XCmp``, STANDS.md section D)."""
    if element.operator in _JSON_PATH_COMPARISONS:
        try:
            left = isinstance(element.left.type, _JsonPathText)
            right = isinstance(element.right.type, _JsonPathText)
        except AttributeError:  # an operand without a type (a clause list)
            left = isinstance(getattr(element.left, "type", None), _JsonPathText)
            right = isinstance(getattr(element.right, "type", None), _JsonPathText)
        if right and not left:
            _refuse_json_path_clause(element.left)
        elif left and not right:
            _refuse_json_path_clause(element.right)
    # (no typing.cast here: it is a call per binary expression)
    if _BINARY_HOOKED:
        return _BINARY_DISPATCH_BEFORE(element, visitor, **kw)  # type: ignore[no-any-return]
    try:
        visit = visitor.visit_binary
    except AttributeError:
        return _BINARY_DISPATCH_BEFORE(element, visitor, **kw)  # type: ignore[no-any-return]
    return visit(element, **kw)  # type: ignore[no-any-return]


def _coalesce_dispatch(
    element: sql_functions.coalesce[Any], visitor: Any, **kw: Any
) -> str:
    """``coalesce``'s compile dispatch: ``coalesce(json_path(...), 0)`` is
    text or a number, by row on SQLite, an error on PostgreSQL. Then the
    dispatch it had before (see :data:`_COALESCE_DISPATCH_BEFORE`)."""
    arguments = element.clauses.clauses
    for argument in arguments:
        if isinstance(getattr(argument, "type", None), _JsonPathText):
            for other in arguments:
                if not _is_json_path_text(other):
                    _refuse_json_path_clause(other)
            break
    if _COALESCE_DISPATCH_BEFORE is not None:
        return _COALESCE_DISPATCH_BEFORE(element, visitor, **kw)  # type: ignore[no-any-return]
    parent = _COALESCE_PARENT._compiler_dispatch
    if parent is _COALESCE_PARENT_GENERATED:  # no hook on a parent: visit directly
        try:
            visit = visitor.visit_function
        except AttributeError:
            pass
        else:
            return visit(element, **kw)  # type: ignore[no-any-return]
    return parent(element, visitor, **kw)  # type: ignore[no-any-return]


BinaryExpression._compiler_dispatch = _binary_dispatch  # type: ignore[method-assign,assignment]
sql_functions.coalesce._compiler_dispatch = _coalesce_dispatch  # type: ignore[method-assign,assignment]


def _refuse_json_path_clause(other: ClauseElement) -> None:
    problem = _json_path_clause_problem(other)
    if problem is not None:
        _refuse_json_path_operand_problem(problem)


def _refuse_json_path_operand_problem(problem: str) -> None:
    raise TypeError(
        f"json_path() reads text; it is compared with {problem}. Compare "
        "it with a str (a number as stored: '1.50', '1e2'), or read the "
        "value and compare it in Python: PostgreSQL refuses text against "
        "a number and SQLite answers differently"
    )


class _JsonPath(ColumnElement[str]):
    """The scalar at ``path`` in a JSON column, as text (renderer-written)."""

    __visit_name__ = "sandloom_json_path"
    inherit_cache = True
    type = _JsonPathText()
    _traverse_internals = _JSONPATH_INTERNALS

    def __init__(
        self, column: ColumnElement[Any], path: tuple[JsonPathKey, ...]
    ) -> None:
        self.column = column
        self.path = path


@compiles(_JsonPath)
def _compile_json_path(element: _JsonPath, compiler: SQLCompiler, **kw: Any) -> str:
    dialect = compiler.dialect.name
    member = _feature_member(dialect, "json_path", "json_path")
    value = _written(member(element.column, element.path), "json_path", dialect)
    return str(compiler.process(value, **kw))


def json_path(
    column: ColumnElement[Any] | QueryableAttribute[Any], *path: JsonPathKey
) -> ColumnElement[str]:
    """The scalar at ``path`` inside a JSON column, as text (feature
    ``json_path``)::

        select(Order).where(json_path(Order.meta, "shipping", "country") == "FR")

    Each step is an object key (``str``) or an array index (``int``, from 0;
    an index step on an object reads ``NULL``, a negative one is refused).
    The value is the scalar's JSON text as stored, the same on every
    database: a string reads as itself (unquoted), a boolean ``'true'`` /
    ``'false'``, a number as written (``1.50`` reads ``'1.50'``, ``1e2``
    ``'1e2'``, a 30-digit integer all its digits); ``NULL`` when the path is
    missing or holds JSON ``null``. Objects and arrays read differently on
    each database (whitespace). Compare the value with a ``str``: a number
    on either side (``== 1.5``, ``Model.id == json_path(...)``, an integer
    subquery, ``coalesce(..., 0)``) is refused when the query is built or
    compiled, and a non-``str`` bound to a parameter compared with it when
    the statement runs (``TypeError``), since PostgreSQL refuses text
    against a number and SQLite answers otherwise. Do not cast it to a
    number in SQL either (SQLite reads ``CAST('1e2' AS INTEGER)`` as 1 and
    ``CAST('abc' AS NUMERIC)`` as 0, PostgreSQL raises): compare numbers in
    Python. Ordering (``<``, ``ORDER BY``, ``GROUP BY``, ``DISTINCT``)
    uses the database's collation: SQLite's is byte order, PostgreSQL's
    usually linguistic (``'a' < 'B'``), even for ASCII; use
    ``.collate("C")`` on PostgreSQL when byte order matters, or compare in
    Python. ``column`` is a ``sqlalchemy.JSON`` column (or one the
    database stores JSON in). The steps are bound parameters, so an index on
    a ``json_path`` expression would not serve the query.
    """
    if not path:
        raise ValueError("json_path() needs at least one key or index")
    for step in path:
        if isinstance(step, bool) or not isinstance(step, (str, int)):
            raise TypeError(
                f"a json_path step is a str key or an int index; got {step!r}"
            )
        if isinstance(step, int) and step < 0:
            raise ValueError(
                f"a json_path index counts from 0; got {step} (databases "
                "disagree on counting from the end)"
            )
        if isinstance(step, str) and ('"' in step or not step):
            raise ValueError(
                f"a json_path key is a non-empty name without '\"'; got {step!r}"
            )
    return _JsonPath(coercions.expect(roles.ExpressionElementRole, column), tuple(path))


class _JsonContains(ColumnElement[bool]):
    """The JSON in ``column`` contains ``value`` (renderer-written)."""

    __visit_name__ = "sandloom_json_contains"
    inherit_cache = True
    type = Boolean()
    _traverse_internals = _JSONCONTAINS_INTERNALS

    def __init__(self, column: ColumnElement[Any], value: ColumnElement[Any]) -> None:
        self.column = column
        self.value = value


@compiles(_JsonContains)
def _compile_json_contains(
    element: _JsonContains, compiler: SQLCompiler, **kw: Any
) -> str:
    dialect = compiler.dialect.name
    member = _feature_member(dialect, "json_contains", "json_contains")
    condition = member(element.column, element.value)
    return str(compiler.process(_written(condition, "json_contains", dialect), **kw))


def json_contains(
    column: ColumnElement[Any] | QueryableAttribute[Any], value: Any
) -> ColumnElement[bool]:
    """The JSON in ``column`` contains ``value`` (feature ``json_contains``)::

        select(Order).where(json_contains(Order.meta, {"tags": ["gift"]}))

    An object contains another when it has each of its keys with a contained
    value; an array contains each item of another array; a scalar only
    itself (PostgreSQL ``jsonb @>``). ``value`` is bound as JSON. A database
    without JSON containment refuses the statement when it is compiled
    (SANDLOOM-R015).
    """
    bound = bindparam(None, value, type_=JSON())
    return _JsonContains(coercions.expect(roles.ExpressionElementRole, column), bound)


# --------------------------------------------------------------------------- #
# aggregate_list (feature ``list_aggregates``; every built-in renderer has it)
# --------------------------------------------------------------------------- #

_LISTAGGREGATE_INTERNALS: _Internals = [
    ("value", InternalTraversal.dp_clauseelement),
]


class _AggregatedList(TypeDecorator[list[Any]]):
    """The result of :func:`aggregate_list`: a Python list of ``item_type``
    values, read through the type the renderer names for the aggregate
    (``aggregate_list_type``); no rows read ``[]``."""

    impl = JSON
    cache_ok = True

    def __init__(self, item_type: TypeEngine[Any]) -> None:
        super().__init__()
        self.item_type = item_type

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        member = _feature_member(dialect.name, "aggregate_list", "aggregate_list_type")
        type_ = _written(member(self.item_type), "aggregate_list", dialect.name)
        return dialect.type_descriptor(type_)

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        raise TypeError("aggregate_list() is a result, never a bound value")

    def process_result_value(self, value: Any, dialect: Dialect) -> list[Any]:
        return [] if value is None else list(value)

    @property
    def python_type(self) -> type[list[Any]]:
        """``list``."""
        return list


class _ListAggregate(ColumnElement[list[Any]]):
    """The values of an expression over a group, as one list (renderer-written)."""

    __visit_name__ = "sandloom_list_aggregate"
    inherit_cache = True
    _traverse_internals = _LISTAGGREGATE_INTERNALS

    def __init__(self, value: ColumnElement[Any]) -> None:
        self.value = value
        self.type = _AggregatedList(value.type)

    @property
    def _from_objects(self) -> list[FromClause]:
        # The tables the value reads: ``select(aggregate_list(T.c.x))`` reads
        # FROM T, and a scalar subquery of it correlates as its value does.
        return self.value._from_objects


@compiles(_ListAggregate)
def _compile_list_aggregate(
    element: _ListAggregate, compiler: SQLCompiler, **kw: Any
) -> str:
    dialect = compiler.dialect.name
    member = _feature_member(dialect, "aggregate_list", "aggregate_list")
    value = _written(member(element.value), "aggregate_list", dialect)
    return str(compiler.process(value, **kw))


def aggregate_list(
    value: ColumnElement[Any] | QueryableAttribute[Any],
) -> ColumnElement[list[Any]]:
    """The values of ``value`` over the rows of a group, as one Python list
    (an aggregate, like ``count``)::

        select(Tag.post_id, aggregate_list(Tag.name)).group_by(Tag.post_id)

    Each item reads back as ``value``'s type does: a ``Uuid`` as a
    ``UUID``, a ``Boolean`` as a ``bool``. A ``NULL`` value is a ``None``
    item, and no rows read ``[]``. The order of the items is unspecified, as in
    SQL: sort them in Python. A scalar subquery of it returns a list per
    outer row in the same statement::

        roles = select(aggregate_list(Role.code)).where(
            UserRole.role_id == Role.id, UserRole.user_id == User.id
        )
        select(User, roles.scalar_subquery())

    It needs the database feature ``list_aggregates``, which every built-in
    database has (PostgreSQL ``array_agg``, SQLite ``json_group_array`` read
    back to the same list): declare it like any feature form
    (SANDLOOM-C030), so startup refuses a database whose renderer lacks it
    (SANDLOOM-B033). ``value`` is a scalar: a JSON, array or binary value is
    refused.
    """
    element = coercions.expect(roles.ExpressionElementRole, value)
    if isinstance(element.type, (JSON, ARRAY, Array, _AggregatedList, LargeBinary)):
        raise TypeError(
            "aggregate_list() collects scalar values; got a "
            f"{type(element.type).__name__} value"
        )
    return _ListAggregate(element)


#: ``(form, feature, where)``: a feature form a table is built with
#: (:func:`table_feature_uses`); ``where`` is ``table.column`` or
#: ``table.index``.
FeatureUse = tuple[str, str, str]


def table_feature_uses(table: Table) -> list[FeatureUse]:
    """The feature forms ``table`` is built with: :class:`Array` columns and
    :func:`fulltext_index` indexes. Startup refuses a module whose tables
    use one on an instance without its feature (SANDLOOM-B033)."""
    uses: list[FeatureUse] = [
        ("Array", DATABASE_FORM_FEATURES["Array"], f"{table.name}.{column.name}")
        for column in table.columns
        if isinstance(column.type, Array)
    ]
    uses += [
        (form, DATABASE_FORM_FEATURES[form], f"{table.name}.{index.name}")
        for index in sorted(table.indexes, key=lambda i: str(i.name))
        if isinstance(form := index.info.get(INDEX_FORM), str)
        and form in DATABASE_FORM_FEATURES
    ]
    return uses
