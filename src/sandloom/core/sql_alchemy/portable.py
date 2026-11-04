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
- :func:`transaction_lock`: a lock held until the transaction ends.

On PostgreSQL each renders exactly the SQL the standard modules wrote by hand
before, so moving a module onto them changes no schema and needs no migration.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Iterable
from importlib import metadata
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import ColumnElement, Index, MetaData, String, TextClause, text
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import ColumnClause
from sqlalchemy.types import TypeDecorator, TypeEngine

if TYPE_CHECKING:
    from typing import Self

    from sqlalchemy.sql.base import ReadOnlyColumnCollection
    from sqlalchemy.sql.compiler import SQLCompiler
    from sqlalchemy.sql.elements import KeyedColumnElement

__all__ = [
    "SQL_DIALECTS_GROUP",
    "CaseInsensitiveText",
    "ConflictInsert",
    "SqlDialect",
    "UnsupportedSqlForm",
    "conflict_insert",
    "dialect_of_url",
    "has_sql_dialect",
    "partial_index",
    "register_sql_dialect",
    "search_index",
    "sql_dialect",
    "transaction_lock",
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


class SqlDialect(Protocol):
    """What a database's SQL dialect renderer provides.

    A method returns ``None`` for a form its database cannot write.
    """

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

    async def transaction_lock(self, session: AsyncSession, key: int) -> bool:
        """Hold ``key`` until the session's transaction ends; ``False`` when
        the database has no such lock."""
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


def register_sql_dialect(name: str, renderer: SqlDialect) -> None:
    """Register ``renderer`` for the SQLAlchemy dialect ``name``.

    A driver package normally names its renderer in the entry-point group
    instead; this is for tests and for a renderer built at run time.
    """
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
            loaded[name] = entry.load()
        except Exception as exc:
            raise RuntimeError(
                f"the SQL dialect renderer {name!r} ({entry.value}) in "
                f"{SQL_DIALECTS_GROUP!r} failed to load: {exc}"
            ) from exc
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
    return _renderers().get(name)


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
    dialect = session.get_bind().dialect.name
    statement = _require(dialect, "conflict_insert").conflict_insert(table)
    if statement is None:
        raise UnsupportedSqlForm("conflict_insert", dialect)
    return statement


async def transaction_lock(key: int, *, session: AsyncSession) -> None:
    """Hold the lock ``key`` until ``session``'s transaction ends.

    Two transactions that take the same key run the code after it one at a
    time; the lock is released by the commit or the rollback, never by hand.
    ``key`` is a signed 64-bit integer the module chooses and keeps stable. A
    database without such a lock refuses rather than run the section unlocked.
    """
    dialect = session.get_bind().dialect.name
    if not await _require(dialect, "transaction_lock").transaction_lock(session, key):
        raise UnsupportedSqlForm("transaction_lock", dialect)
