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
- :func:`transaction_lock`: a lock held until the transaction ends, built
  on the renderer's :meth:`SqlDialect.lock_statement` like the kernel's own
  locks (:func:`lock_statement_for`).

On PostgreSQL each renders exactly the SQL the standard modules wrote by hand
before, so moving a module onto them changes no schema and needs no migration.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Iterable, Sequence
from importlib import metadata
from typing import TYPE_CHECKING, Any, Literal, Protocol

from sqlalchemy import (
    ColumnElement,
    Index,
    MetaData,
    Select,
    String,
    TextClause,
    text,
)
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import ColumnClause
from sqlalchemy.types import TypeDecorator, TypeEngine

from sandloom.core.infra.capabilities import DATABASE_RENDERER_FEATURES

if TYPE_CHECKING:
    from typing import Self

    from sqlalchemy.sql.base import ReadOnlyColumnCollection
    from sqlalchemy.sql.compiler import SQLCompiler
    from sqlalchemy.sql.elements import KeyedColumnElement

__all__ = [
    "CaseInsensitiveText",
    "ConflictInsert",
    "ErrorKind",
    "IncompatibleSqlDialect",
    "LockKey",
    "LockScope",
    "RENDERER_FEATURES",
    "SQL_DIALECTS_GROUP",
    "SQL_DIALECT_CONTRACT",
    "ARRAY_INSERT_TYPES",
    "ArrayInsert",
    "SqlDialect",
    "UnsupportedServerVersion",
    "UnsupportedSqlForm",
    "check_lock_key",
    "check_server_version",
    "array_insert_sql_for",
    "classify_error",
    "conflict_insert",
    "conflict_insert_for",
    "dialect_name",
    "dialect_of_url",
    "for_update_for",
    "greatest",
    "greatest_for",
    "has_sql_dialect",
    "id_in_for",
    "lock_statement_for",
    "optional_lock_statement_for",
    "partial_index",
    "register_sql_dialect",
    "relaxed_commit_clause_for",
    "renderer_features",
    "search_index",
    "skips_locked_rows",
    "sql_dialect",
    "transaction_lock",
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


class UnsupportedSqlForm(NotImplementedError):
    """The database a module runs on has no way to write a portable form."""

    code = "SANDLOOM-R015"

    def __init__(self, form: str, dialect: str, *, renderer: bool = True) -> None:
        if renderer:
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


class SqlDialect(Protocol):
    """What a database's SQL dialect renderer provides (contract version 2).

    Required: ``contract_version``, ``name``, ``minimum_server_version``,
    :meth:`features` and :meth:`classify_error`. A form method returns
    ``None`` for a form its database cannot write.
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
    ) -> Executable | None:
        """The statement that takes the named lock ``key`` (check it with
        :func:`check_lock_key`), or ``None`` when the database has no such lock.

        ``scope="transaction"``: held until the transaction ends;
        ``"session"``: until :meth:`unlock_statement` runs on the same
        connection. ``wait=True`` waits for the lock; ``wait=False`` never
        waits and selects one boolean, whether it locked. The kernel runs it
        on sync connections (migrations) and async sessions alike.
        """
        ...

    def unlock_statement(self, key: LockKey) -> Executable | None:
        """The statement that releases the session lock ``key`` (selecting
        one boolean, whether it was held); ``None`` exactly when
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
    for name, renderer in renderers.items():
        if name in rendered:
            continue
        if where is not None:
            options = renderer.partial_index_options(where)
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
    unknown = set(renderer.features()) - RENDERER_FEATURES
    if unknown:
        raise IncompatibleSqlDialect(
            f"the SQL dialect renderer {name!r} ({source}) declares features "
            f"{sorted(unknown)} outside the database capability's renderer "
            f"features {sorted(RENDERER_FEATURES)} [SANDLOOM-B025]"
        )


def renderer_features(dialect: str) -> frozenset[str]:
    """The features the renderer of ``dialect`` declares (empty without one)."""
    renderer = sql_dialect(dialect)
    if renderer is None:
        return frozenset()
    return frozenset(renderer.features()) & RENDERER_FEATURES


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
    if isinstance(bind, Dialect):
        return str(bind.name)
    dialect = getattr(bind, "dialect", None)
    if dialect is not None:
        return str(dialect.name)
    return str(bind.get_bind().dialect.name)


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
    statement = _require(dialect, "conflict_insert").conflict_insert(table)
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
) -> Executable | None:
    """The renderer's :meth:`SqlDialect.lock_statement` for the database of
    ``bind`` (see :func:`dialect_name`: a sync connection or an async
    session alike); ``None`` without a renderer or without such a lock, for
    a caller that has a correct answer unlocked."""
    renderer = sql_dialect(dialect_name(bind))
    if renderer is None:
        check_lock_key(key)
        return None
    return renderer.lock_statement(key, scope=scope, wait=wait)


def lock_statement_for(
    bind: Any, key: LockKey, *, scope: LockScope, wait: bool
) -> Executable:
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


def unlock_statement_for(bind: Any, key: LockKey) -> Executable:
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
    return _require(dialect_name(bind), "for_update").for_update(
        stmt, skip_locked=skip_locked, read=read
    )


def relaxed_commit_clause_for(bind: Any) -> ColumnElement[bool] | None:
    """The renderer's :meth:`SqlDialect.relaxed_commit_clause` for the
    database of ``bind``; ``None`` without a renderer or without one."""
    renderer = sql_dialect(dialect_name(bind))
    return None if renderer is None else renderer.relaxed_commit_clause()


async def transaction_lock(key: int, *, session: AsyncSession) -> None:
    """Hold the lock ``key`` until ``session``'s transaction ends.

    Two transactions that take the same key run the code after it one at a
    time; the lock is released by the commit or the rollback, never by hand.
    ``key`` is a signed 64-bit integer the module chooses and keeps stable. A
    database without such a lock refuses rather than run the section unlocked.
    """
    dialect = dialect_name(session)
    statement = _require(dialect, "transaction_lock").lock_statement(
        key, scope="transaction", wait=True
    )
    if statement is None:
        raise UnsupportedSqlForm("transaction_lock", dialect)
    await session.execute(statement)
