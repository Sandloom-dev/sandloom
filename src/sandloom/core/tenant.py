"""Multi-tenancy isolation — a sanctioned, fail-closed tenant scope.

Manually adding ``where(Model.tenant_id == ...)`` to every query is the classic
multi-tenant footgun: forget it once and you leak data across tenants. This
module gives modules a paved path instead:

* a request-scoped **tenant context** (a contextvar bound by the ``TenantScope``
  dependency — :mod:`sandloom.core.http.tenant_scope`, re-exported by
  ``sandloom.core.di`` — or ``bind_tenant`` for workers/scripts);
* **explicit, fail-closed helpers** — ``scoped(select(Model), Model)`` /
  ``tenant_filter(Model)`` raise if no tenant is bound, so an un-scoped read can
  never silently run;
* **automatic enforcement** — any ORM read or bulk mutation against a model
  that inherits the ``TenantScoped`` mixin is rejected without a bound tenant,
  otherwise auto-filtered to that tenant via a SQLAlchemy ``do_orm_execute``
  hook (defense in depth), with an explicit ``unscoped()`` escape hatch for
  admin/system queries.

Models opt in by inheriting :class:`TenantScoped` (which also declares the
indexed ``tenant_id`` column). Nothing here filters models that do NOT opt in.
Tenant-owned operations always require a bound tenant; migrations and deliberate
cross-tenant system work use the explicit ``unscoped()`` context.
"""

from __future__ import annotations

import functools
import re
import types
import uuid
import weakref
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import (
    Column,
    PrimaryKeyConstraint,
    UniqueConstraint,
    Uuid,
    bindparam,
    event,
    inspect,
)
from sqlalchemy.orm import (
    ColumnProperty,
    Mapped,
    Mapper,
    RelationshipProperty,
    Session,
    SessionTransaction,
    mapped_column,
    with_loader_criteria,
)
from sqlalchemy.orm.exc import UnmappedColumnError
from sqlalchemy.orm.strategy_options import Load
from sqlalchemy.orm.util import LoaderCriteriaOption
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.base import DialectKWArgs, Executable
from sqlalchemy.sql.compiler import Compiled, SQLCompiler
from sqlalchemy.sql.dml import Delete, Insert, Update, ValuesBase
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    ClauseElement,
    ColumnClause,
    ColumnElement,
    TextClause,
)
from sqlalchemy.sql.lambdas import LambdaElement
from sqlalchemy.sql.schema import ColumnElementColumnDefault, Table
from sqlalchemy.sql.selectable import (
    Alias,
    Join,
    SelectBase,
    TableClause,
    Values,
)

from sandloom.core import error_codes
from sandloom.core.tenant_context import (
    MissingTenantError,
    TenantMismatchError,
    UnsafeTenantOperationError,
    _current_tenant,
    bind_tenant,
    bypass_active,
    current_tenant_id,
    current_tenant_id_optional,
    unscoped,
)


class TenantScoped:
    """Mixin marking a model as tenant-owned (adds the indexed ``tenant_id``).

    Inheriting this both declares the column and opts the model into automatic
    tenant filtering. Declare models as ``class Foo(TenantScoped, Base)``.
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)  # maps the class: __table__ exists
        table = cls.__dict__.get("__table__")
        if table is not None:
            _drop_redundant_tenant_index(table)
        # A new tenant-owned model changes the TenantScoped table set: drop the
        # guard's memoized table set and per-statement verdicts.
        _invalidate_guard_caches()


def _drop_redundant_tenant_index(table: Any) -> None:
    """Drop the mixin's plain ``tenant_id`` index when a unique key (primary
    key, unique constraint or unique index) of the table leads with
    ``tenant_id``: that key already serves every lookup the plain index would,
    so the index is pure write cost (and fails the redundant-index check,
    SANDLOOM-T012). Per-tenant natural keys stay tenant-first."""
    column = table.c.get("tenant_id")
    if column is None:
        return
    leading = [
        list(constraint.columns)[:1]
        for constraint in table.constraints
        if isinstance(constraint, (UniqueConstraint, PrimaryKeyConstraint))
    ] + [list(index.columns)[:1] for index in table.indexes if index.unique]
    if not any(first and first[0] is column for first in leading):
        return
    for index in list(table.indexes):
        if (
            not index.unique
            and list(index.columns) == [column]
            and getattr(index, "_column_flag", False)
        ):
            table.indexes.discard(index)
            column.index = None


def tenant_filter(model: Any) -> Any:
    """A ``model.tenant_id == <current tenant>`` clause (raises if unbound)."""
    return model.tenant_id == current_tenant_id()


def scoped(stmt: Any, model: Any) -> Any:
    """Add a fail-closed tenant filter to ``stmt`` for ``model``.

    ``scoped(select(Task), Task)`` is the explicit, hard-to-misuse read path:
    forgetting it is visible, and it raises rather than returning unscoped rows
    when no tenant is bound.
    """
    return stmt.where(tenant_filter(model))


def _is_tenant_class(cls: Any) -> bool:
    return isinstance(cls, type) and issubclass(cls, TenantScoped)


# --------------------------------------------------------------------------- #
# Guard caches (hot path: every ORM execute with NO tenant bound)
# --------------------------------------------------------------------------- #
# An unbound ORM statement must be walked to prove it touches no TenantScoped
# table (~150us for a 3-table select with subqueries — comparable to a DB round
# trip). The verdict depends only on the statement's STRUCTURE and on the set of
# TenantScoped tables, so it is memoized per SQLAlchemy structural cache key
# (``_generate_cache_key().key`` — bind VALUES excluded; the key is memoized on
# the statement and reused by the compiled-SQL cache, so computing it here adds
# ~nothing). Both caches are dropped whenever a TenantScoped subclass is
# defined. The verdict cache is size-bounded (cleared wholesale on overflow).
_GUARD_CACHE_MAX = 2048
_tenant_tables_cache: frozenset[Any] | None = None
_tenant_names_cache: frozenset[str] | None = None
_reach_names_cache: frozenset[str] | None = None
_reference_cache: dict[Any, bool] = {}
_unsafe_core_cache: dict[Any, bool] = {}
#: Every constructed mapper of a TenantScoped class, imperatively mapped ones
#: included (a ``__table__`` attribute is declarative-only).
_tenant_mappers: weakref.WeakSet[Mapper[Any]] = weakref.WeakSet()
#: Counters (observability + the guard's micro-benchmark test).
guard_cache_stats: dict[str, int] = {"walks": 0, "hits": 0}


def _invalidate_guard_caches() -> None:
    global _tenant_tables_cache, _tenant_names_cache, _reach_names_cache
    _tenant_tables_cache = None
    _tenant_names_cache = None
    _reach_names_cache = None
    _reference_cache.clear()
    _unsafe_core_cache.clear()
    _mapper_sql_cache.clear()
    _mapper_names_cache.clear()


@event.listens_for(Mapper, "after_mapper_constructed")
def _note_tenant_mapper(mapper: Mapper[Any], class_: type) -> None:
    """A new TenantScoped mapper (declarative or imperative) changes the table
    set the guard matches by: drop the memoized sets and verdicts."""
    if _is_tenant_class(class_):
        _tenant_mappers.add(mapper)
        _invalidate_guard_caches()


def _tenant_tables() -> frozenset[Any]:
    """Tables of every mapped TenantScoped model: ``mapper.tables`` of each
    TenantScoped mapper, so a joined or imperatively mapped model counts too."""
    global _tenant_tables_cache
    cached = _tenant_tables_cache
    if cached is not None:
        return cached
    result = frozenset(
        table for mapper in tuple(_tenant_mappers) for table in mapper.tables
    )
    _tenant_tables_cache = result
    return result


def _tenant_names() -> frozenset[str]:
    """The lower-cased NAMES of the tenant tables.

    The guard never trusts a ``Table`` object's identity: a lightweight
    ``table("x")``, a second ``Table("x", MetaData())`` and ``to_metadata`` all
    name the same physical table. The schema is not part of the match (a
    ``schema_translate_map`` rewrites it at render time): a same-named table of
    another schema is refused too, the safe direction.
    """
    global _tenant_names_cache
    cached = _tenant_names_cache
    if cached is not None:
        return cached
    result = frozenset(str(table.name).lower() for table in _tenant_tables())
    _tenant_names_cache = result
    return result


#: Stands in a fragment's identifiers for a name no scan can read (a PostgreSQL
#: ``U&"..."`` identifier spells its characters as escapes).
_UNREADABLE = 'u&"'


def _reach_names() -> frozenset[str]:
    """What reaching a tenant table means at the cursor: the tenant table names
    and the :data:`_UNREADABLE` marker; empty while no TenantScoped model is
    mapped (nothing to guard: the cursor rule costs one set check)."""
    global _reach_names_cache
    cached = _reach_names_cache
    if cached is not None:
        return cached
    names = _tenant_names()
    result = names | {_UNREADABLE} if names else names
    _reach_names_cache = result
    return result


def _is_tenant_table(node: Any, names: frozenset[str]) -> bool:
    """``node`` is a table (any object, any metadata) named like a tenant table."""
    return isinstance(node, TableClause) and str(node.name).lower() in names


def _path_hits_tenant(path: Any) -> bool:
    try:
        elements = list(path)
    except TypeError:
        return False
    for element in elements:
        if _is_tenant_class(getattr(element, "class_", None)):
            return True
        mapper = getattr(element, "mapper", None)
        if _is_tenant_class(getattr(mapper, "class_", None)):
            return True
    return False


def _insert_select_without_defaults(statement: Any) -> bool:
    """``insert(...).from_select(..., include_defaults=False)``: it renders no
    client-side column default."""
    return (
        isinstance(statement, Insert)
        and statement.select is not None
        and not statement.include_insert_from_select_defaults
    )


def _verdict_cacheable(statement: Any) -> bool:
    """A verdict may be memoized under the statement's structural cache key only
    when that key fixes the SQL the statement renders.

    A lambda whose closure variables are not tracked
    (``track_closure_variables=False``, ``enable_tracking=False``) keeps one
    cache key for different SQL: the second statement would reuse the verdict of
    the first (a "safe" one could then bless a raw tenant subquery). Such a
    statement is walked on every execution. So is ``INSERT ... SELECT`` with
    ``include_defaults=False``: the flag decides which column defaults render, yet
    SQLAlchemy leaves it out of the cache key, so one key would stand for both
    variants and a "safe" verdict of one could bless the other.
    """
    if _insert_select_without_defaults(statement):
        return False
    if isinstance(statement, LambdaElement) and _untracked(statement.opts):
        return False
    if isinstance(statement, Executable):
        for option in statement._with_options:
            if (
                isinstance(option, LoaderCriteriaOption)
                and option.deferred_where_criteria
            ):
                lam = option.where_criteria
                if isinstance(lam, LambdaElement) and _untracked(lam.opts):
                    return False
    return True


def _untracked(opts: Any) -> bool:
    return not (opts.enable_tracking and opts.track_closure_variables)


def _references_tenant_scoped(statement: Any) -> bool:
    """Memoized :func:`_walk_references_tenant_scoped` (see guard caches)."""
    if not _verdict_cacheable(statement):
        return _walk_references_tenant_scoped(statement)
    try:
        cache_key = statement._generate_cache_key()
    except Exception:  # noqa: BLE001 - uncacheable: just walk
        cache_key = None
    if cache_key is None:
        return _walk_references_tenant_scoped(statement)
    key = cache_key.key
    try:
        verdict = _reference_cache.get(key)
    except TypeError:  # unhashable structural key: never cache, just walk
        return _walk_references_tenant_scoped(statement)
    if verdict is not None:
        guard_cache_stats["hits"] += 1
        return verdict
    verdict = _walk_references_tenant_scoped(statement)
    if len(_reference_cache) >= _GUARD_CACHE_MAX:
        _reference_cache.clear()
    _reference_cache[key] = verdict
    return verdict


def _unsafe_core_reference(statement: Any) -> bool:
    """Reject raw tenant sources while preserving ORM aliases and subqueries.

    SQLAlchemy also exposes an implicit *raw* FROM table for a selected ORM
    column. Each SELECT therefore carries its own set of ORM-protected sources;
    a raw alias of that table is a different source and remains unsafe. Nested
    SELECTs establish a fresh scope so an ORM source in a subquery cannot bless
    a raw tenant table in the outer query.
    """

    key = None
    if _verdict_cacheable(statement):
        try:
            cache_key = statement._generate_cache_key()
            key = cache_key.key if cache_key is not None else None
            if key is not None and key in _unsafe_core_cache:
                return _unsafe_core_cache[key]
        except Exception:  # noqa: BLE001 - an uncacheable statement is still checked
            key = None
    walk = _UnsafeCoreWalk(_reach_names(), Alias, SelectBase)

    try:
        verdict = walk.unsafe(statement, frozenset())
    except Exception:  # noqa: BLE001 - fail closed for opaque tenant SQL
        verdict = True
    if key is not None:
        if len(_unsafe_core_cache) >= _GUARD_CACHE_MAX:
            _unsafe_core_cache.clear()
        _unsafe_core_cache[key] = verdict
    return verdict


def check_compiled_statement(compiled: SQLCompiler) -> None:
    """Refuse a COMPILED statement that carries a data-modifying CTE.

    The loader criteria of the ``do_orm_execute`` guard scope the SELECTs of a
    statement; they never reach an INSERT/UPDATE/DELETE that rides along as a
    CTE, so ``insert(Other).add_cte(update(Item)...)`` would rewrite another
    tenant's rows. Which tables such a CTE reaches cannot be told reliably from
    the statement (quote-free names, ``schema_translate_map``, tables only
    named through columns, SQL-expression defaults), so no table is matched:
    a CTE whose element is not a SELECT is DML and is refused unless inside
    ``unscoped()``, whatever its target.

    The check runs on what is about to be sent, at the cursor boundary
    (``write_admission.install_write_admission``, which every kernel-built
    engine gets): ``compiled.ctes`` holds every CTE the SQL renders, wherever
    the statement tree hung it (options, multi-row ``VALUES``, any table
    object) and whatever ran it (a session, ``session.connection().execute``,
    an engine). The caller reads ``compiled.ctes`` first: a statement without a
    CTE costs one attribute read.

    A ``nesting=True`` CTE below the top level leaves ``compiled.ctes`` once
    rendered; PostgreSQL itself refuses data-modifying statements there.
    """
    if bypass_active():
        return
    for cte in compiled.ctes or ():
        if isinstance(cte.element, SelectBase):
            continue
        if _current_tenant.get() is None:
            raise MissingTenantError(
                "a data-modifying CTE ran with no tenant bound. "
                + error_codes.fix_line("SANDLOOM-R001")
            )
        raise UnsafeTenantOperationError(
            "a CTE holding INSERT/UPDATE/DELETE cannot be tenant-scoped; use "
            "ORM statements outside the CTE or explicit unscoped() for "
            "trusted system access"
        )


#: What the ``do_orm_execute`` guard decided on, by STATEMENT: a private weak map
#: from the statement object the guard checked to the kind of approval. At the
#: cursor a statement that reaches a tenant table is admitted only when
#: ``context.invoked_statement`` is in it (or as a flush write, below). Nothing
#: is written to the statement or its execution options, so no execution
#: inherits, reads or replays an approval it was not given: a statement a later
#: listener swapped in, a follow-up loader execution, a copy of the options.
_APPROVED: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
#: Approval kinds. ``_CRITERION``: the guard attached its tenant criterion, so
#: the statement must carry the criterion's bound tenant at the cursor.
#: ``_TRUSTED``: a column load of a stamped instance, or the caller's own
#: ``text()`` (their responsibility: ``scoped()`` / the ``unsafe-tenant-sql``
#: check / RLS).
_CRITERION = 1
_TRUSTED = 2
#: ``InstanceState.info`` key: the ``tenant_id`` the row carried when the
#: instance was READ from the database, or when a flush that inserted it
#: succeeded. A forged, merged-without-load or unpickled instance carries none;
#: an insert loses it when its transaction ends without committing.
_STAMP = "sandloom.tenant_id"
#: Per session TRANSACTION (the root or a savepoint, whichever was innermost when
#: the flush ran): the instances it inserted and stamped, with the tenant of the
#: stamp, that no commit has made permanent yet. Releasing a savepoint moves its
#: records to the parent; a root commit drops them (the rows exist); a
#: transaction that ENDS still holding records ended without committing (a
#: rollback, a ``close()`` with a savepoint open, an exception), so the stamps go.
_pending_inserts: weakref.WeakKeyDictionary[
    SessionTransaction, dict[Any, uuid.UUID]
] = weakref.WeakKeyDictionary()


class _FlushWindow:
    """What ONE ``Session._flush`` may write at the cursor: the rows the mapper
    events verified during it, and nothing else.

    * ``tables``: the ``Table`` OBJECTS of the mappers whose INSERT was verified
      (a statement over another ``Table`` object of the same name is none of them);
    * ``rows``: ``(Table, primary key)`` of every row a verified UPDATE or DELETE
      may write: the table object, and the key the events read in that table's
      own primary-key order (never a parameter);
    * ``inserted``: the instances the flush inserts, with the tenant verified
      for each. They are stamped when the flush succeeds, never before.
    """

    __slots__ = ("inserted", "rows", "tables")

    def __init__(self) -> None:
        self.tables: set[Table] = set()
        self.rows: set[tuple[Table, tuple[Any, ...]]] = set()
        self.inserted: dict[Any, uuid.UUID] = {}


#: The running flush (``None``: no flush is running).
_flush_window: ContextVar[_FlushWindow | None] = ContextVar(
    "sandloom_flush_window", default=None
)


def _approve(statement: Any, kind: int) -> None:
    # a statement that cannot be weakly referenced is never approved: the cursor
    # refuses it (every SQLAlchemy statement can be referenced)
    with suppress(TypeError):
        _APPROVED[statement] = kind


def _approval_of(statement: Any) -> int | None:
    try:
        return _APPROVED.get(statement)
    except TypeError:  # ``None`` or a statement that cannot be referenced
        return None


class ReachRecorder(SQLCompiler):
    """A statement compiler that records the table NAMES a statement renders.

    The cursor guard (:func:`check_tenant_reach`) asks what a statement
    reaches, not how it was built: the names are read where the SQL is
    rendered, so a lightweight ``table()``, a second ``Table`` of the same name,
    a mapper-level ``column_property``, a ``secondary``, a rendered column
    default, a ``text()`` / ``literal_column()`` fragment and a custom
    ``@compiles`` element are all seen alike. ``reads`` holds every table the
    SQL selects from or names in a fragment, ``targets`` the table a DML writes.
    Compile-time only: SQLAlchemy caches the compiled object, so an execution
    costs one set check. Installed on a dialect by :func:`install_reach_recorder`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # the statement is rendered inside ``super().__init__``
        self.reads: set[str] = set()
        self.targets: set[str] = set()
        super().__init__(*args, **kwargs)

    def process(self, obj: visitors.Visitable, **kwargs: Any) -> str:
        rendered = super().process(obj, **kwargs)
        # ``sqlalchemy.ext.compiler.compiles`` sets this class attribute on an
        # element that carries its own renderer (SQLAlchemy declares no type for
        # it). Its function returns SQL no ``visit_*`` hook saw: read the
        # fragment it rendered, and only that one (the statement's own target
        # table is not a read of it), as an opaque fragment.
        if hasattr(obj, "_compiler_dispatcher"):
            self.reads |= _identifiers(rendered)
        return rendered

    def visit_table(self, table: TableClause, **kw: Any) -> str:  # type: ignore[override]
        (self.targets if kw.get("iscrud") else self.reads).add(str(table.name).lower())
        return str(super().visit_table(table, **kw))  # type: ignore[no-untyped-call]

    def visit_insert(self, insert_stmt: Insert, **kw: Any) -> str:  # type: ignore[override]
        self.targets.add(str(insert_stmt.table.name).lower())
        return str(super().visit_insert(insert_stmt, **kw))  # type: ignore[no-untyped-call]

    def visit_textclause(self, textclause: TextClause, **kw: Any) -> str:  # type: ignore[override]
        self.reads |= _identifiers(textclause.text)
        return str(super().visit_textclause(textclause, **kw))  # type: ignore[no-untyped-call]

    def visit_column(self, column: ColumnClause[Any], **kw: Any) -> str:  # type: ignore[override]
        if column.is_literal:
            self.reads |= _identifiers(str(column.name))
        return super().visit_column(column, **kw)

    @functools.cached_property
    def persistence_shaped(self) -> bool:
        """The statement is a plain Core ``Insert`` / ``Update`` / ``Delete`` of
        the class the persistence layer builds: no dialect subclass
        (``ON CONFLICT``, ``ON DUPLICATE KEY``), no prefix (``OR REPLACE``,
        ``IGNORE``), no post-values clause, no ``INSERT ... SELECT``. Anything
        else is not a flush's own write, whatever it targets. Memoized with the
        compiled statement."""
        statement = self.statement
        if (
            not isinstance(statement, (Insert, Update, Delete))
            or type(statement) not in (Insert, Update, Delete)
            or statement._prefixes
        ):
            return False
        return not (
            isinstance(statement, Insert)
            and (
                statement._post_values_clause is not None
                or statement.select is not None
            )
        )

    @functools.cached_property
    def row_pins(self) -> tuple[str, ...] | None:
        """The parameter keys that pin the primary key of the row an UPDATE /
        DELETE writes, in primary-key order; ``None`` when the statement does not
        pin every primary-key column of its table with ``column = :param``
        (read from the top-level AND terms of its WHERE: a term can only narrow
        the rows). A pin carries the column's OWN type object, as the
        persistence layer builds it (``bindparam(key, type_=column.type)``): a
        parameter of another type (a ``TypeDecorator`` that rewrites the value
        on its way to the database) is no pin of the verified key. Memoized with
        the compiled statement."""
        statement = self.statement
        if not isinstance(statement, (Update, Delete)):
            return None
        table = statement.table
        if not isinstance(table, Table):
            return None
        binds: dict[str, str] = {}
        for criterion in statement._where_criteria:
            for term in _and_terms(criterion):
                if (
                    isinstance(term, BinaryExpression)
                    and term.operator is operators.eq
                    and isinstance(term.left, ColumnClause)
                    and term.left.table is table
                    and isinstance(term.right, BindParameter)
                    and term.right.type is term.left.type
                ):
                    if term.left.name in binds:
                        return None  # the same column pinned twice: not a row key
                    binds[term.left.name] = term.right.key
        pins = tuple(binds.get(column.name, "") for column in table.primary_key)
        return pins if pins and all(pins) else None

    @functools.cached_property
    def tenant_upsert_column(self) -> Column[Any] | None:
        """The tenant column of the table the statement upserts when it has
        exactly the shape ``tenant_upsert`` builds (:func:`_tenant_upsert_column`),
        else ``None``. Memoized with the compiled statement."""
        return _tenant_upsert_column(self.statement)


def _and_terms(clause: Any) -> Iterator[Any]:
    """The terms of a WHERE criterion that are ANDed at its top level."""
    if isinstance(clause, BooleanClauseList) and clause.operator is operators.and_:
        for term in clause.clauses:
            yield from _and_terms(term)
    else:
        yield clause


_recorder_classes: dict[type, type] = {}


def install_reach_recorder(dialect: Any) -> None:
    """Make ``dialect`` compile with :class:`ReachRecorder` (idempotent)."""
    base = dialect.statement_compiler
    if issubclass(base, ReachRecorder):
        return
    recorder = _recorder_classes.get(base)
    if recorder is None:
        recorder = types.new_class(f"Reach{base.__name__}", (ReachRecorder, base))
        _recorder_classes[base] = recorder
    dialect.statement_compiler = recorder


def _params_of(context: Any) -> list[dict[str, Any]]:
    return context.compiled_parameters or []


def _value_keys(values: Any) -> set[str]:
    """The column names a ``.values()`` mapping sets (a key is a name or a column)."""
    return {
        key.key if isinstance(key, ColumnClause) else key
        for key in values
        if isinstance(key, (str, ColumnClause))
    }


#: The column that carries a row's tenant (``TenantScoped.tenant_id``).
_TENANT_COLUMN = "tenant_id"


def _tenant_column(table: Table) -> Column[Any] | None:
    """The ``tenant_id`` column of ``table``, found by NAME: a column's key (the
    name of its parameter) is no proof of which column it is."""
    return next((column for column in table.c if column.name == _TENANT_COLUMN), None)


def _supplied_keys(statement: ValuesBase) -> list[Any] | None:
    """The keys the statement's own ``.values()`` / ``.ordered_values()`` assign
    (``ordered_values()`` keeps its pairs in ``_values`` since SQLAlchemy 2.1);
    ``None`` when a row is positional (it assigns every column of the table by
    position). An ``INSERT ... SELECT`` never gets here: it is not
    ``persistence_shaped``."""
    keys: list[Any] = list(statement._values or ())
    for batch in statement._multi_values:
        for row in batch:
            if not isinstance(row, Mapping):
                return None
            keys.extend(row)
    return keys


def _supplies_tenant_id(statement: ValuesBase) -> bool:
    """The statement's own values name (or may name) the ``tenant_id`` column,
    resolved by column NAME, not by the key a value is passed under. The
    persistence layer never does: it passes a column's value as an execution
    parameter, and ``tenant_id`` only ever as the bound tenant. A caller-built
    statement that sets the column itself (a literal, an expression, a key that
    spells another column's name) is refused whatever its parameters say.
    Fails closed for a key it cannot resolve and for a positional row."""
    keys = _supplied_keys(statement)
    if keys is None:
        return True
    table = statement.table
    for key in keys:
        if isinstance(key, ColumnClause):
            name = str(key.name)
        elif isinstance(key, str):
            column = table.c.get(key)
            name = key if column is None else str(column.name)
        else:
            return True
        if name == _TENANT_COLUMN:
            return True
    return False


def _columns_set_by_every_row(statement: ValuesBase) -> set[str]:
    """The columns EVERY row of ``statement.values(...)`` sets itself: their
    SQL-expression defaults are never rendered (``ordered_values()`` pairs live in
    ``_values``). An ``INSERT ... SELECT`` sets the columns of its target list.
    Nothing is known when the values arrive with the execution (``insert(Model)``
    with a parameter list) or as positional rows, so the set is empty then and
    every default counts."""
    if isinstance(statement, Insert) and statement.select is not None:
        return _value_keys(statement._select_names or ())
    rows: list[Mapping[Any, Any] | Sequence[Any]] = []
    if statement._values:
        rows.append(statement._values)
    rows.extend(row for batch in statement._multi_values for row in batch)
    if not rows or not all(isinstance(row, Mapping) for row in rows):
        return set()
    return set.intersection(*(_value_keys(row) for row in rows))


def _flush_write_admitted(
    context: Any,
    compiled: ReachRecorder,
    names: frozenset[str],
    reach: frozenset[str],
) -> bool:
    """A flush write of rows the mapper events verified: an ALLOWLIST, every
    part of the statement must be accounted for.

    * a flush is running, a tenant is bound, and the statement is the plain Core
      INSERT / UPDATE / DELETE the persistence layer builds
      (:attr:`ReachRecorder.persistence_shaped`), not one a column default or an
      event listener wrote on the flush connection;
    * nothing it renders READS a tenant table (a default or ``onupdate``
      subquery would) or names one no scan can read;
    * it is written over a ``Table`` OBJECT of a mapper whose event verified a row
      of this flush (a second ``Table`` of the same name, with other columns or
      another primary key, is none of them);
    * an INSERT writes a table a verified insert of this flush writes and, where
      that table has a ``tenant_id`` column, carries it EXPLICITLY, equal to the
      bound tenant, in every parameter set (a missing or computed value is no
      value);
    * an UPDATE / DELETE pins its rows by primary key (``pk = :param``, the
      column's own type) and every parameter set names a ``(table, primary key)``
      the events verified in this flush; an UPDATE never sets another tenant's
      ``tenant_id``;
    * an INSERT / UPDATE never carries values of its own that name the
      ``tenant_id`` column (:func:`_supplies_tenant_id`): the persistence layer
      sets it through the parameters the checks above read.
    """
    window = _flush_window.get()
    tid = _current_tenant.get()
    if (
        window is None
        or tid is None
        or not compiled.persistence_shaped
        or not reach.isdisjoint(compiled.reads)
    ):
        return False
    params = _params_of(context)
    statement = compiled.statement
    if not params or not isinstance(statement, (Insert, Update, Delete)):
        return False
    table = statement.table
    if isinstance(statement, Insert):
        if (
            not isinstance(table, Table)
            or table not in window.tables
            or not (compiled.targets & names)
            <= {str(verified.name).lower() for verified in window.tables}
            or _supplies_tenant_id(statement)
        ):
            return False
        tenant = _tenant_column(table)
        return tenant is None or all(row.get(tenant.key) == tid for row in params)
    pins = compiled.row_pins
    if (
        pins is None
        or not isinstance(table, Table)  # row_pins is only set for a plain Table
        or (isinstance(statement, Update) and _supplies_tenant_id(statement))
    ):
        return False
    tenant = _tenant_column(table)
    return all(
        all(key in row for key in pins)
        and (table, tuple(row[key] for key in pins)) in window.rows
        and (
            not isinstance(statement, Update)
            or tenant is None
            or row.get(tenant.key, tid) == tid
        )
        for row in params
    )


#: The statements :func:`sandloom.core.sql_alchemy.tenant_upsert.tenant_upsert`
#: built and is about to run, by identity: the helper's private marker, set just
#: before it executes and dropped right after. The marker alone admits nothing:
#: a marked statement is also checked for the exact shape the helper builds
#: (:func:`_tenant_upsert_column`) and for the bound tenant in every parameter set
#: (:func:`_tenant_upsert_admitted`). A conflict insert without the marker stays
#: refused like any other statement that names a tenant table.
_UPSERTS: weakref.WeakSet[Any] = weakref.WeakSet()
#: Bound-parameter name of the tenant VALUE in the ``WHERE`` of the upsert's
#: ``DO UPDATE`` (a callable bind: it reads the bound tenant at execution).
TENANT_UPSERT_PARAM = "sandloom_tenant_upsert__"
#: What ``ON CONFLICT`` names the row the insert proposed.
_EXCLUDED = "excluded"


def _seal_tenant_upsert(statement: Insert) -> None:
    _UPSERTS.add(statement)


def _unseal_tenant_upsert(statement: Insert) -> None:
    _UPSERTS.discard(statement)


def _tenant_upsert_where(tenant: Column[Any]) -> ColumnElement[bool]:
    """The ``WHERE`` of an upsert's ``DO UPDATE``: only a row of the bound tenant
    is ever updated. The parameter carries the tenant column's OWN type object,
    so a ``TypeDecorator`` on the column rewrites it as it rewrites the stored
    value."""
    return tenant == bindparam(
        TENANT_UPSERT_PARAM, callable_=_tenant_criteria_value, type_=tenant.type
    )


@runtime_checkable
class _OnConflictUpdate(Protocol):
    """The ``DO UPDATE`` clause of ``Insert.on_conflict_do_update``. PostgreSQL and
    SQLite build one class each with these attributes; the kernel imports no
    dialect, so the clause is read through this view."""

    inferred_target_elements: Sequence[Any] | None
    update_values_to_set: Mapping[Any, Any]
    update_whereclause: Any


def _unset(value: Any) -> bool:
    """``None``, ``False`` or an empty sequence or mapping: a slot nothing fills."""
    return (
        value is None
        or value is False
        or (isinstance(value, (Sequence, Mapping)) and not value)
    )


def _empty_beyond(node: ClauseElement, used: frozenset[str]) -> bool:
    """Every slot SQLAlchemy lists for ``node`` (``_traverse_internals``: what its
    SQL and its cache key read) outside ``used`` is unset. The shape check reads
    SQLAlchemy's own list of slots, not a list of ours, so a slot a later
    SQLAlchemy adds fails closed until it is read here."""
    for name, _kind in type(node)._traverse_internals:
        if name in used:
            continue
        # a statement's dialect options fill on read: ask for the ones it was given
        value = (
            node.dialect_kwargs
            if name == "dialect_options" and isinstance(node, DialectKWArgs)
            else getattr(node, name)
        )
        if not _unset(value):
            return False
    return True


def _is_excluded_column(value: Any, column: Column[Any], table: Table) -> bool:
    """``value`` is ``excluded.<column>``: the column of the ``excluded`` alias of
    ``table`` itself that carries ``column``'s name."""
    owner = value.table if isinstance(value, ColumnClause) else None
    return (
        isinstance(owner, Alias)
        and owner.element is table
        and owner.name == _EXCLUDED
        and value.name == column.name
    )


def _tenant_upsert_column(statement: Any) -> Column[Any] | None:
    """The tenant column of the table ``statement`` upserts when it is EXACTLY the
    statement ``tenant_upsert`` builds, else ``None``:

    * an ``INSERT`` over a ``Table`` with a ``tenant_id`` column whose every slot
      but the table and its ``ON CONFLICT DO UPDATE`` clause is unset: no
      ``values()``, ``RETURNING``, prefix, hint, CTE, ``INSERT ... SELECT``;
    * a conflict target of columns of the table that includes ``tenant_id``
      (another tenant's row can never be the arbiter);
    * ``SET`` writes only ``excluded.<column>`` into a column of the table that
      is neither ``tenant_id`` nor part of the primary key;
    * ``WHERE <tenant_id> = :sandloom_tenant_upsert__`` is the one condition, the
      parameter typed as the column itself;
    * no index predicate, no named constraint.
    """
    if not isinstance(statement, Insert):
        return None
    table = statement.table
    post = statement._post_values_clause
    # read through the protocol: a dialect's clause class is not a type the kernel knows
    clause: object = post
    tenant = _tenant_column(table) if isinstance(table, Table) else None
    if (
        tenant is None
        or not isinstance(table, Table)
        or post is None
        or not isinstance(clause, _OnConflictUpdate)
        or not _empty_beyond(statement, frozenset({"table", "_post_values_clause"}))
        or not _empty_beyond(
            post,
            frozenset(
                {
                    "inferred_target_elements",
                    "update_values_to_set",
                    "update_whereclause",
                }
            ),
        )
    ):
        return None
    target = clause.inferred_target_elements or ()
    primary = {column.name for column in table.primary_key.columns}
    where = clause.update_whereclause
    if (
        not target
        or not clause.update_values_to_set
        or any(not isinstance(c, Column) or c.table is not table for c in target)
        or all(c.name != tenant.name for c in target)
        or not (
            isinstance(where, BinaryExpression)
            and where.operator is operators.eq
            and where.left is tenant
            and isinstance(where.right, BindParameter)
            and where.right.key == TENANT_UPSERT_PARAM
            and where.right.type is tenant.type
        )
    ):
        return None
    for column, value in clause.update_values_to_set.items():
        if (
            not isinstance(column, Column)
            or column.table is not table
            or column.name == tenant.name
            or column.name in primary
            or not _is_excluded_column(value, column, table)
        ):
            return None
    return tenant


def _tenant_upsert_admitted(
    context: Any, compiled: ReachRecorder, reach: frozenset[str]
) -> bool:
    """The one conflict insert the cursor admits on a tenant table: the statement
    ``tenant_upsert`` marked (:data:`_UPSERTS`) AND built to the shape
    :func:`_tenant_upsert_column` reads, a tenant bound, nothing it renders reading
    a tenant table, the upserted table its only target, and the bound tenant as
    the value of the tenant column in every parameter set and of the ``WHERE``
    parameter of the update. A marked statement that differs in any of them is
    refused like any unapproved one."""
    tid = _current_tenant.get()
    tenant = compiled.tenant_upsert_column
    if tid is None or tenant is None or context.invoked_statement not in _UPSERTS:
        return False
    params = _params_of(context)
    return (
        bool(params)
        and reach.isdisjoint(compiled.reads)
        and compiled.targets == {str(tenant.table.name).lower()}
        and all(
            row.get(tenant.key) == tid and row.get(TENANT_UPSERT_PARAM) == tid
            for row in params
        )
    )


def _shown(hits: Iterable[str]) -> str:
    return ", ".join(
        'an identifier no scan can read (U&"...")' if hit == _UNREADABLE else hit
        for hit in sorted(hits)
    )


def check_tenant_reach(context: Any, compiled: Compiled) -> None:
    """Refuse, at the cursor, a statement that reaches a tenant table unscoped.

    The ``do_orm_execute`` guard sees what a caller hands the session; SQL also
    reaches the cursor from places it never sees: ``session.connection()`` and
    ``ctx.connection`` Core calls, the persistence layer's own statements (a
    flush, the legacy ``bulk_*`` paths), SQL the mapper renders on its own
    (``column_property``, ``secondary``), column defaults. So the rule sits
    where every statement ends up, on the rendered SQL: outside ``unscoped()``
    a statement that names a tenant table (:func:`_tenant_names`) is admitted
    only when

    * the ORM guard approved THIS statement (:data:`_APPROVED`, matched against
      ``context.invoked_statement``): it attached the tenant criterion, verified
      a column load, or the caller sent its own ``text()`` (their
      responsibility: ``scoped()`` / the ``unsafe-tenant-sql`` check / RLS); or
    * it is a flush write of rows the mapper events verified
      (:func:`_flush_write_admitted`); or
    * it is the upsert ``tenant_upsert`` marked and built, for the bound tenant
      (:func:`_tenant_upsert_admitted`): the one conflict insert admitted on a
      tenant table.

    A criterion approval also needs the criterion's bound parameter to hold the
    bound tenant: a caller-supplied value never wins, a missing one is refused.
    A statement the recorder did not compile (a DDL element, a compiled object
    from another dialect) is no ``ReachRecorder``: its rendered SQL is read
    whole (:func:`_check_unrecorded`). A statement that names no tenant table
    costs two set checks.
    """
    reach = _reach_names()
    if not reach:
        return
    if not isinstance(compiled, ReachRecorder):
        _check_unrecorded(compiled, reach)
        return
    if reach.isdisjoint(compiled.reads) and reach.isdisjoint(compiled.targets):
        return
    if bypass_active():
        return
    approval = _approval_of(context.invoked_statement)
    if approval == _CRITERION:
        _check_criterion(context)
        return
    if approval == _TRUSTED:
        return
    if _tenant_upsert_admitted(context, compiled, reach):
        return
    names = _tenant_names()
    if _flush_write_admitted(context, compiled, names, reach):
        return
    shown = _shown(reach & (compiled.reads | compiled.targets))
    if _current_tenant.get() is None:
        raise _missing_tenant("a statement", shown)
    raise UnsafeTenantOperationError(
        f"a statement that reaches TenantScoped table {shown} ran outside the tenant "
        "guard (the ORM execute guard did not approve this statement: Core on "
        "session.connection(), a column default or a mapper-level SQL expression "
        "reading the table, a statement through a connection, a statement a later "
        "do_orm_execute listener replaced). Use ORM statements over the entity, or "
        "explicit unscoped() for trusted system access"
    )


def _check_unrecorded(compiled: Compiled, reach: frozenset[str]) -> None:
    """A compiled statement that is no :class:`ReachRecorder` reports no names:
    a DDL element (``DDL("UPDATE ...")``), a compiled object from a dialect
    without the recorder, any compiler a driver or an extension swaps in. Fail
    closed: its rendered SQL is read whole, and it is refused outside
    ``unscoped()`` when that names a tenant table."""
    hits = reach & _identifiers(compiled.string)
    if not hits or bypass_active():
        return
    shown = _shown(hits)
    if _current_tenant.get() is None:
        raise _missing_tenant("a statement compiled outside the guard", shown)
    raise UnsafeTenantOperationError(
        f"a {type(compiled).__name__} statement that reaches TenantScoped table "
        f"{shown} (a DDL element, a compiled object from another dialect) cannot "
        "be tenant-scoped: it is neither an ORM statement nor a statement the "
        "guard's compiler read. Use ORM statements over the entity, or explicit "
        "unscoped() for trusted system access (schema DDL included)"
    )


def _check_criterion(context: Any) -> None:
    """A criterion approval holds only while the criterion's bound parameter is
    in the statement and holds the BOUND tenant: the bind's callable read it for
    this execution, a parameter of the caller cannot replace it, and a statement
    that lost the parameter after the guard looked at it is no longer scoped."""
    tid = _current_tenant.get()
    params = _params_of(context)
    if (
        tid is None
        or not params
        or any(row.get(TENANT_CRITERIA_PARAM) != tid for row in params)
    ):
        raise UnsafeTenantOperationError(
            "the tenant criterion of a guarded statement is missing or carries a "
            "value other than the bound tenant (parameters of an execution cannot "
            "override it)"
        )


def _source_owned(source: Any, names: frozenset[str]) -> bool:
    """``source`` is a table of the entity, a plain alias of one, or a join of
    them: the sources the loader criteria of the guard really reach."""
    while isinstance(source, Alias):
        source = source.element
    if isinstance(source, Join):
        return _source_owned(source.left, names) and _source_owned(source.right, names)
    return isinstance(source, TableClause) and str(source.name).lower() in names


def _tenant_annotated(node: Any) -> bool:
    """``node`` is the OWN source of a TenantScoped entity: carries an ORM
    annotation of one AND is that mapper's table or a plain alias of it.

    The annotation alone proves nothing: ``aliased(Item, <any selectable>)``
    annotates an arbitrary subquery as the entity, and the criterion then lands
    on whatever ``tenant_id`` column the caller picked. Such a node is raw.
    """
    annotations = getattr(node, "_annotations", None)
    if not annotations:
        return False
    for name in ("parentmapper", "parententity"):
        entity = annotations.get(name)
        if _is_tenant_class(getattr(entity, "class_", None)):
            mapper = getattr(entity, "mapper", entity)
            names = frozenset(str(table.name).lower() for table in mapper.tables)
            return _source_owned(getattr(node, "table", node), names)
    return False


def _orm_sources(select: Any, select_base: type[Any]) -> frozenset[Any]:
    """The ORM-protected sources of ONE select (nested selects excluded)."""
    sources: set[Any] = set()
    seen: set[int] = set()
    for node in (
        *getattr(select, "_raw_columns", ()),
        *getattr(select, "_from_obj", ()),
    ):
        _collect_orm_sources(node, select_base, sources, seen)
    for join in getattr(select, "_setup_joins", ()):
        for part in join[:3]:
            if part is not None:
                _collect_orm_sources(part, select_base, sources, seen)
    return frozenset(sources)


def _collect_orm_sources(
    node: Any, select_base: type[Any], sources: set[Any], seen: set[int]
) -> None:
    if id(node) in seen or isinstance(node, select_base):
        return
    seen.add(id(node))
    if _tenant_annotated(node):
        table = getattr(node, "table", None)
        sources.add(table if table is not None else node)
        return
    children = getattr(node, "get_children", None)
    if callable(children):
        for child in children():
            _collect_orm_sources(child, select_base, sources, seen)


def _as_clause(value: Any) -> ClauseElement | None:
    """``value`` as a SQL expression, by SQLAlchemy's ``__clause_element__``
    protocol (a mapped attribute resolves to its annotated column)."""
    if isinstance(value, ClauseElement):
        return value
    resolve = getattr(value, "__clause_element__", None)
    clause = resolve() if callable(resolve) else None
    return clause if isinstance(clause, ClauseElement) else None


def _option_expressions(option: Any) -> Iterator[ClauseElement]:
    """The SQL expressions ONE loader option carries inside itself.

    ``with_loader_criteria`` holds a criterion (possibly a lambda, resolved
    against EVERY mapper it applies to: a lambda may branch on the class and
    hide raw SQL behind the second mapper), ``with_expression`` holds its
    expression in ``local_opts`` and ``rel.and_(...)`` holds
    ``_extra_criteria``: none of them is a child of the statement, yet each is
    compiled into its SQL.
    """
    if option is _tenant_criteria:
        return  # the guard's own criterion: it only names the tenant column
    if isinstance(option, LoaderCriteriaOption):
        for mapper in option._all_mappers():
            yield option._resolve_where_criteria(mapper)
    elif isinstance(option, Load):
        for element in option.context:
            for value in (*element._extra_criteria, *element.local_opts.values()):
                clause = _as_clause(value)
                if clause is not None:
                    yield clause


def _table_default_expressions(
    table: Any, *, onupdate: bool, set_by_statement: set[str]
) -> Iterator[ClauseElement]:
    """The SQL-expression defaults a table's INSERT (``default``) or UPDATE
    (``onupdate``) renders for the columns the statement does not set itself
    (``set_by_statement``: the columns every row of its ``.values()`` sets)."""
    if not isinstance(table, Table):
        return
    for column in table.c:
        default = column.onupdate if onupdate else column.default
        if isinstance(default, ColumnElementColumnDefault) and (
            column.key not in set_by_statement
        ):
            yield default.arg


def _row_expressions(batches: Iterable[Iterable[Any]]) -> Iterator[ClauseElement]:
    """The SQL expressions among the values of ``VALUES`` row batches."""
    for rows in batches:
        for row in rows:
            for value in row.values() if isinstance(row, dict) else row:
                clause = _as_clause(value)
                if clause is not None:
                    yield clause


def _hidden_expressions(node: Any) -> Iterator[ClauseElement]:
    """SQL a node renders that ``get_children()`` does not yield: loader-option
    expressions, the rows of a multi-row ``VALUES`` and the SQL-expression
    defaults of an INSERT/UPDATE target. A guard that stops at ``get_children``
    reads past each of them."""
    if isinstance(node, Executable):
        for option in node._with_options:
            yield from _option_expressions(option)
    if isinstance(node, ValuesBase):
        yield from _row_expressions(node._multi_values)
    if isinstance(node, Values):
        # ``values(...).data([...])``: the rows are not children of the node
        yield from _row_expressions(node._data)
    if isinstance(node, (Insert, Update)) and not _insert_select_without_defaults(node):
        yield from _table_default_expressions(
            node.table,
            onupdate=isinstance(node, Update),
            set_by_statement=_columns_set_by_every_row(node),
        )


#: Every bare word of a SQL fragment, read from the RAW text: no quoting rule is
#: applied, so nothing a dialect's quoting hides (dollar quotes, ``E'..'``
#: strings with escaped quotes, comments, a word inside a literal) is skipped.
#: A superset by design: a literal that spells a tenant table's name counts.
_BARE_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
#: A quoted identifier (double quote, backtick, bracket), read whole too: a
#: table named ``"my items"`` is one name, not two words.
_QUOTED_IDENTIFIER = re.compile(r'"((?:[^"]|"")*)"|`((?:[^`]|``)*)`|\[([^\]]*)\]')
#: A PostgreSQL unicode identifier, ``U&"\0070gq_items"``: its characters are
#: escapes, so no scan reads the name it spells.
_UNICODE_IDENTIFIER = re.compile(r'u&"', re.IGNORECASE)


def _identifiers(sql: str) -> frozenset[str]:
    """The lower-cased words a SQL fragment names (see :data:`_BARE_WORD`), plus
    the :data:`_UNREADABLE` marker when it holds a ``U&"`` identifier (treated as
    reaching every tenant table)."""
    found = {word.lower() for word in _BARE_WORD.findall(sql)}
    for match in _QUOTED_IDENTIFIER.finditer(sql):
        name = match.group(1) or match.group(2) or match.group(3)
        if name:
            found.add(name.replace('""', '"').lower())
    if _UNICODE_IDENTIFIER.search(sql):
        found.add(_UNREADABLE)
    return frozenset(found)


def _opaque_text_reaches(node: Any, names: frozenset[str]) -> bool:
    """A ``text()`` or ``literal_column()`` fragment INSIDE a statement that
    names a tenant table: it carries SQL no clause walk can read."""
    if isinstance(node, TextClause):
        return not names.isdisjoint(_identifiers(node.text))
    if isinstance(node, ColumnClause) and node.is_literal:
        return not names.isdisjoint(_identifiers(str(node.name)))
    return False


class _UnsafeCoreWalk:
    """One :func:`_unsafe_core_reference` walk (visited set per statement)."""

    __slots__ = ("alias", "names", "select_base", "visited")

    def __init__(
        self, names: frozenset[str], alias: type[Any], select_base: type[Any]
    ) -> None:
        self.names = names
        self.alias = alias
        self.select_base = select_base
        self.visited: set[tuple[int, frozenset[Any]]] = set()

    def unsafe(self, node: Any, scope: frozenset[Any]) -> bool:
        if isinstance(node, self.select_base):
            scope = _orm_sources(node, self.select_base)
        identity = (id(node), scope)
        if identity in self.visited:
            return False
        self.visited.add(identity)
        if _tenant_annotated(node) or node in scope:
            return False
        if (
            _is_tenant_table(node, self.names)
            or self._raw_alias_of_tenant_table(node)
            or _opaque_text_reaches(node, self.names)
        ):
            return True
        table = getattr(node, "table", None)
        if table is not None and self.unsafe(table, scope):
            return True
        for join in getattr(node, "_setup_joins", ()):
            if any(part is not None and self.unsafe(part, scope) for part in join[:3]):
                return True
        children = getattr(node, "get_children", None)
        if callable(children) and any(
            self.unsafe(child, scope) for child in children()
        ):
            return True
        return any(self.unsafe(child, scope) for child in _hidden_expressions(node))

    def _raw_alias_of_tenant_table(self, node: Any) -> bool:
        # An alias is not protected by the ORM predicate on its underlying
        # table: it can independently range across every tenant.
        raw: Any = node
        while isinstance(raw, self.alias):
            raw = raw.element
        return raw is not node and _is_tenant_table(raw, self.names)


_mapper_sql_cache: weakref.WeakKeyDictionary[Mapper[Any], bool] = (
    weakref.WeakKeyDictionary()
)
_mapper_names_cache: weakref.WeakKeyDictionary[Mapper[Any], frozenset[str]] = (
    weakref.WeakKeyDictionary()
)


def _mapper_table_names(mapper: Mapper[Any]) -> frozenset[str]:
    """The lower-cased names of the tables ``mapper`` writes (memoized)."""
    cached = _mapper_names_cache.get(mapper)
    if cached is None:
        cached = frozenset(str(table.name).lower() for table in mapper.tables)
        _mapper_names_cache[mapper] = cached
    return cached


_mapper_layout_cache: weakref.WeakKeyDictionary[
    Mapper[Any], tuple[tuple[Table, tuple[int, ...]], ...]
] = weakref.WeakKeyDictionary()


def _mapper_row_layout(
    mapper: Mapper[Any],
) -> tuple[tuple[Table, tuple[int, ...]], ...]:
    """For each ``Table`` ``mapper`` writes, where the columns of that table's
    primary key sit in the mapper's identity key (memoized).

    An identity key follows the MAPPER's ``primary_key`` order, the WHERE of a
    statement the TABLE's: ``Mapper(primary_key=[b, a])`` over a table keyed
    ``(a, b)`` reverses them. A table with a key column the mapper does not map
    to one of its key properties is left out, so no statement over it is ever
    admitted (it fails closed)."""
    layout = _mapper_layout_cache.get(mapper)
    if layout is None:
        key_props = [mapper.get_property_by_column(col) for col in mapper.primary_key]
        placed: list[tuple[Table, tuple[int, ...]]] = []
        for table in mapper.tables:
            if not isinstance(table, Table):
                continue
            try:
                positions = [
                    next(
                        (
                            i
                            for i, key in enumerate(key_props)
                            if key is mapper.get_property_by_column(column)
                        ),
                        -1,
                    )
                    for column in table.primary_key.columns
                ]
            except UnmappedColumnError:
                continue
            if -1 not in positions:
                placed.append((table, tuple(positions)))
        layout = tuple(placed)
        _mapper_layout_cache[mapper] = layout
    return layout


def _row_keys(
    mapper: Mapper[Any], identity: Sequence[Any]
) -> Iterator[tuple[Table, tuple[Any, ...]]]:
    """``(table, primary key)`` of the row ``identity`` names in each table of
    ``mapper``, the key in that table's own primary-key order."""
    for table, positions in _mapper_row_layout(mapper):
        yield table, tuple(identity[i] for i in positions)


def _mapper_sql_reaches_raw_tenant(mapper: Mapper[Any]) -> bool:
    """The SQL the MAPPER adds to every statement over it reads a tenant table
    as raw Core (memoized per mapper).

    A ``column_property`` / ``deferred`` / ``query_expression`` expression and a
    relationship's ``secondary`` are compiled with the entity yet are not part
    of any statement the caller wrote, so the statement walk never sees them. A
    subquery over a raw tenant table there is not scoped by the guard's loader
    criteria. The mapper's own tables stay safe (a computed column reads its own
    row); a nested select starts a fresh scope.
    """
    verdict = _mapper_sql_cache.get(mapper)
    if verdict is None:
        own = frozenset(mapper.tables)
        walk = _UnsafeCoreWalk(_reach_names(), Alias, SelectBase)
        verdict = False
        for prop in mapper.iterate_properties:
            parts: list[ClauseElement]
            if isinstance(prop, ColumnProperty):
                parts = [col for col in prop.columns if not isinstance(col, Column)]
            elif isinstance(prop, RelationshipProperty) and prop.secondary is not None:
                parts = [prop.secondary]
            else:
                continue
            if any(walk.unsafe(part, own) for part in parts):
                verdict = True
                break
        _mapper_sql_cache[mapper] = verdict
    return verdict


def _walk_references_tenant_scoped(statement: Any) -> bool:  # noqa: C901
    """True if ANY TenantScoped table/entity is reachable from ``statement``.

    Walks the whole clause tree (joins, ``select_from``, EXISTS / IN / scalar
    subqueries, correlated selects), ORM relationship joins (``_setup_joins``)
    and eager-loader option paths (``joinedload(Parent.children)`` adds the
    child table to the SAME SQL without a separate ORM execution). A table is a
    tenant table by NAME, whatever its ``Table`` object; a ``text()`` /
    ``literal_column()`` fragment inside the statement counts when it names one.
    """

    guard_cache_stats["walks"] += 1
    names = _reach_names()
    if not names:
        return False

    def _hit(element: Any) -> bool:
        if _is_tenant_table(element, names) or _opaque_text_reaches(element, names):
            return True
        annotations = getattr(element, "_annotations", None) or {}
        parent = annotations.get("parententity")
        if _is_tenant_class(getattr(parent, "class_", None)):
            return True
        prop = getattr(element, "property", None)
        if _is_tenant_class(getattr(getattr(prop, "mapper", None), "class_", None)):
            return True
        for join in getattr(element, "_setup_joins", ()) or ():
            for part in join[:3]:
                if part is not None and _hit_shallow(part):
                    return True
        for option in getattr(element, "_with_options", ()) or ():
            for load in getattr(option, "context", ()) or ():
                if _path_hits_tenant(getattr(load, "path", ())):
                    return True
        return any(_hit_shallow(hidden) for hidden in _hidden_expressions(element))

    def _hit_shallow(part: Any) -> bool:
        prop = getattr(part, "property", None)
        if _is_tenant_class(getattr(getattr(prop, "mapper", None), "class_", None)):
            return True
        if _is_tenant_class(part) or _is_tenant_table(part, names):
            return True
        return (
            any(_hit(sub) for sub in visitors.iterate(part))
            if hasattr(part, "get_children")
            else False
        )

    try:
        return any(_hit(element) for element in visitors.iterate(statement))
    except Exception:  # noqa: BLE001 - never let introspection open a hole
        # If the statement cannot be introspected, fail closed: it is only
        # reached with NO tenant bound, where unscoped() is the explicit path.
        return True


def _mutation_keys(state: Any) -> set[str]:
    """Column names a bulk UPDATE would SET (all statement + param forms)."""
    stmt = state.statement
    keys: list[Any] = list((getattr(stmt, "_values", None) or {}).keys())
    keys.extend(k for k, _ in (getattr(stmt, "_ordered_values", None) or ()))
    for row in getattr(stmt, "_multi_values", None) or ():
        for values in row:
            if isinstance(values, dict):
                keys.extend(values.keys())
    params = state.parameters
    rows = params if isinstance(params, (list, tuple)) else [params]
    for row in rows:
        if isinstance(row, dict):
            keys.extend(row.keys())
    names: set[str] = set()
    for key in keys:
        if isinstance(key, str):
            names.add(key)
        else:
            for attr in ("key", "name"):
                value = getattr(key, attr, None)
                if isinstance(value, str):
                    names.add(value)
    return names


def _reject_unsafe_bulk_mutation(state: Any) -> None:
    """Refuse ORM bulk UPDATE/DELETE forms that bypass tenant ownership.

    * any form that SETs ``tenant_id`` (``.values()``, ``.ordered_values()``,
      or the per-row parameters of a bulk-by-primary-key UPDATE);
    * ORM bulk UPDATE/DELETE *by primary key* (``session.execute(update(M),
      [{"id": ..., ...}])``): it is an executemany keyed on the PK alone, so
      loader criteria cannot scope it — another tenant's row would be written
      by id. Use a criteria UPDATE (``update(M).where(...).values(...)``,
      auto-scoped) or explicit ``unscoped()`` system code.
    """
    if state.is_update and "tenant_id" in _mutation_keys(state):
        raise UnsafeTenantOperationError(
            "bulk UPDATE cannot change TenantScoped.tenant_id"
        )
    params = state.parameters
    if isinstance(params, (list, tuple)) and len(params) > 0:
        raise UnsafeTenantOperationError(
            "ORM bulk UPDATE/DELETE by primary key of TenantScoped models "
            "cannot be tenant-scoped; use a criteria update(...).where(...) or "
            "explicit unscoped() for trusted system code"
        )


_original_identity_lookup: Any = None
_original_flush: Any = None


def _stamp_of(instance: Any) -> uuid.UUID | None:
    """The ``tenant_id`` ``instance`` was read from the database with (or
    inserted by a verified flush); ``None`` for any other instance."""
    stamp: uuid.UUID | None = inspect(instance).info.get(_STAMP)
    return stamp


def _install_identity_map_guard() -> None:
    """Make every identity-map hit honour the bound tenant.

    ``Session.get``, ``Query.get``, ``get_one`` and a many-to-one lazy load all
    ask ``Session._identity_lookup`` first and return what it finds WITHOUT
    emitting SQL (so ``do_orm_execute`` never runs) — e.g. an object loaded
    earlier in the same session under ``unscoped()``. For a TenantScoped hit
    this re-checks ownership: no bound tenant → ``MissingTenantError`` (fail
    closed, as the SQL path does); an instance whose database ``tenant_id`` is
    not the bound tenant (another tenant's, or a forged one with no such stamp)
    → ``None``, so the caller falls through to the scoped SQL load, which returns
    exactly what the auto-filtered query returns. ``unscoped()`` skips the check.
    """
    global _original_identity_lookup
    if _original_identity_lookup is not None:
        return
    original = Session._identity_lookup
    _original_identity_lookup = original

    def _identity_lookup(self: Session, mapper: Any, *args: Any, **kwargs: Any) -> Any:
        instance = original(self, mapper, *args, **kwargs)
        if not isinstance(instance, TenantScoped) or bypass_active():
            return instance
        tid = _current_tenant.get()
        if tid is None:
            raise _missing_tenant(
                f"an identity lookup of {type(instance).__name__}",
                type(instance).__name__,
            )
        return instance if _stamp_of(instance) == tid else None

    _identity_lookup.__doc__ = original.__doc__
    Session._identity_lookup = _identity_lookup  # type: ignore[method-assign]


def _install_flush_window() -> None:
    """Open a flush window around ``Session._flush``: the mapper events record
    in it which rows the flush may write (:func:`check_tenant_reach`). The
    instances it inserted are stamped when the flush SUCCEEDED: one that fails
    (and rolls back) stamps nothing. A stamp lasts while the row can exist: the
    session remembers the instances of its open root transaction
    per transaction (:data:`_pending_inserts`) until the root commits."""
    global _original_flush
    if _original_flush is not None:
        return
    original = Session._flush
    _original_flush = original

    def _flush(self: Session, objects: Any = None) -> None:
        window = _FlushWindow()
        token = _flush_window.set(window)
        try:
            original(self, objects)
        finally:
            _flush_window.reset(token)
        if window.inserted:
            owner = _innermost_transaction(self)
            if owner is not None:
                # no open transaction (it cannot happen after a flush): no stamp
                _pending_inserts.setdefault(owner, {}).update(window.inserted)
                for state, tenant in window.inserted.items():
                    state.info[_STAMP] = tenant

    _flush.__doc__ = original.__doc__
    Session._flush = _flush  # type: ignore[method-assign]


_original_legacy_bulk: dict[str, Any] = {}


def _tenant_mapper_names(mappers: Iterable[Mapper[Any]]) -> list[str]:
    """Class names among ``mappers`` (and their subclasses) that map a
    TenantScoped model or a table NAMED like one, sorted."""
    tenant_names = _tenant_names()
    names: set[str] = set()
    for root in mappers:
        for mapper in root.self_and_descendants:
            if _is_tenant_class(mapper.class_) or not tenant_names.isdisjoint(
                _mapper_table_names(mapper)
            ):
                names.add(mapper.class_.__name__)
    return sorted(names)


def _refuse_legacy_bulk(api: str, mappers: Iterable[Mapper[Any]]) -> None:
    """Refuse a legacy ``Session.bulk_*`` call that reaches a TenantScoped
    model, outside ``unscoped()``."""
    if bypass_active():
        return
    names = _tenant_mapper_names(mappers)
    if names:
        raise UnsafeTenantOperationError(
            f"Session.{api} on TenantScoped model {', '.join(names)} writes rows "
            "without the tenant guard (it fires neither the ORM execute nor the "
            "flush hook, so it can rewrite or plant another tenant's rows); use "
            "ORM instances (session.add) so tenant ownership is enforced, or "
            "explicit unscoped() for trusted system imports"
        )


def _mapper_of(target: Any) -> Mapper[Any] | None:
    """The mapper of a class, mapper or mapped instance (``None``: unmapped,
    which SQLAlchemy's own call then refuses)."""
    inspected = inspect(target, raiseerr=False)
    return inspected.mapper if inspected is not None else None


def _install_legacy_bulk_guard() -> None:
    """Refuse ``Session.bulk_save_objects`` / ``bulk_insert_mappings`` /
    ``bulk_update_mappings`` on TenantScoped models outside ``unscoped()``.

    The legacy bulk APIs emit their INSERT/UPDATE straight from the persistence
    layer: ``do_orm_execute`` never runs (no statement is executed through
    ``Session.execute``) and neither does ``before_flush`` (nothing is flushed),
    so no loader criterion and no ``tenant_id`` ownership check reaches them. A
    bulk update by primary key rewrites another tenant's row; a bulk insert
    plants one with any ``tenant_id``. Like ``Session.get`` above, they are
    wrapped; the refusal needs no bound tenant (there is no scoped form of them).
    """
    if _original_legacy_bulk:
        return

    def bulk_save_objects(
        self: Session, objects: Iterable[object], *args: Any, **kwargs: Any
    ) -> None:
        batch = list(objects)  # inspected here, consumed by the original below
        mappers = (_mapper_of(obj) for obj in batch)
        _refuse_legacy_bulk(
            "bulk_save_objects", [mapper for mapper in mappers if mapper is not None]
        )
        _original_legacy_bulk["bulk_save_objects"](self, batch, *args, **kwargs)

    def bulk_insert_mappings(
        self: Session, mapper: Any, mappings: Any, *args: Any, **kwargs: Any
    ) -> None:
        resolved = _mapper_of(mapper)
        _refuse_legacy_bulk(
            "bulk_insert_mappings", [resolved] if resolved is not None else []
        )
        _original_legacy_bulk["bulk_insert_mappings"](
            self, mapper, mappings, *args, **kwargs
        )

    def bulk_update_mappings(
        self: Session, mapper: Any, mappings: Any, *args: Any, **kwargs: Any
    ) -> None:
        resolved = _mapper_of(mapper)
        _refuse_legacy_bulk(
            "bulk_update_mappings", [resolved] if resolved is not None else []
        )
        _original_legacy_bulk["bulk_update_mappings"](
            self, mapper, mappings, *args, **kwargs
        )

    _original_legacy_bulk.update(
        bulk_save_objects=Session.bulk_save_objects,
        bulk_insert_mappings=Session.bulk_insert_mappings,
        bulk_update_mappings=Session.bulk_update_mappings,
    )
    bulk_save_objects.__doc__ = Session.bulk_save_objects.__doc__
    bulk_insert_mappings.__doc__ = Session.bulk_insert_mappings.__doc__
    bulk_update_mappings.__doc__ = Session.bulk_update_mappings.__doc__
    Session.bulk_save_objects = bulk_save_objects  # type: ignore[method-assign]
    Session.bulk_insert_mappings = bulk_insert_mappings  # type: ignore[method-assign]
    Session.bulk_update_mappings = bulk_update_mappings  # type: ignore[method-assign]


_guard_installed = False


def _model_names(mappers: list[Any]) -> str:
    names = sorted({m.class_.__name__ for m in mappers})
    return ", ".join(names) or "a TenantScoped table reached through a join"


def _statement_kind(state: Any) -> str:
    for kind in ("select", "update", "delete", "insert"):
        if getattr(state, f"is_{kind}", False):
            return kind.upper()
    return "statement"


def _missing_tenant(operation: str, models: str) -> MissingTenantError:
    """The fail-closed refusal, naming the model (SANDLOOM-R001)."""
    return MissingTenantError(
        f"{operation} on TenantScoped model {models} ran with no tenant bound. "
        + error_codes.fix_line("SANDLOOM-R001")
    )


#: Bound-parameter name of the tenant VALUE in the guard's criterion.
TENANT_CRITERIA_PARAM = "sandloom_tenant_id__"


def _tenant_criteria_value() -> uuid.UUID:
    """The tenant bound NOW — called by SQLAlchemy when it builds the
    statement's parameters for an execution (never cached in the statement or
    the compiled cache). Fails closed if the context lost its tenant."""
    tid = _current_tenant.get()
    if tid is None:
        raise MissingTenantError(
            "tenant-scoped ORM operation has no bound tenant at execution time"
        )
    return tid


def _build_tenant_criteria() -> Any:
    """The guard's loader criterion, built ONCE: only its STRUCTURE is shared.

    ``tenant_id = :sandloom_tenant_id__`` where the parameter's value is a callable
    reading the current tenant context at execution time. Building
    ``with_loader_criteria`` per statement (lambda analysis, coercion, a fresh
    closure value) cost ~2/3 of the tenant layer on the S5 ladder.
    """
    tenant = bindparam(
        TENANT_CRITERIA_PARAM,
        callable_=_tenant_criteria_value,
        type_=Uuid(),
    )
    return with_loader_criteria(
        TenantScoped,
        lambda cls: cls.tenant_id == tenant,
        include_aliases=True,
    )


_tenant_criteria: Any = None


def _params_override_tenant(parameters: Any) -> bool:
    """The caller's parameters name the guard's tenant bind: SQLAlchemy lets a
    passed parameter win over the bind's callable, so it would pick the tenant."""
    if not parameters:
        return False
    rows = parameters if isinstance(parameters, (list, tuple)) else (parameters,)
    return any(
        isinstance(row, dict)
        and any(str(key).startswith(TENANT_CRITERIA_PARAM) for key in row)
        for row in rows
    )


def _verify_column_load(state: Any) -> None:
    """A column load / refresh of a TenantScoped instance carries no criterion
    (it is keyed on the primary key of an instance already loaded), so the
    instance must be one whose ``tenant_id`` was read from the database (the
    stamp), and the bound tenant's. A forged detached instance, a
    ``merge(load=False)`` result or an unpickled one has no stamp. A row the
    RUNNING flush inserts is not stamped yet (that waits for the flush to
    succeed): its verified tenant stands in for the stamp, so a refresh of
    server-side defaults right after the INSERT is admitted."""
    refreshed = state.load_options._refresh_state
    if refreshed is None or not issubclass(refreshed.class_, TenantScoped):
        return
    stamp = refreshed.info.get(_STAMP)
    if stamp is None:
        window = _flush_window.get()
        stamp = window.inserted.get(refreshed) if window is not None else None
    tid = _current_tenant.get()
    if stamp is None or (tid is not None and stamp != tid):
        raise UnsafeTenantOperationError(
            f"a column load or refresh of {refreshed.class_.__name__} whose tenant_id "
            "was not read from the database under the bound tenant (a forged, "
            "merged-without-load or other tenant's instance); load it through a "
            "scoped query, or use explicit unscoped() for trusted system access"
        )
    _approve(state.statement, _TRUSTED)


def _stamp_loaded(state: Any, context: Any) -> None:
    # ``merge(load=False)`` fires ``load`` with no context: nothing was read
    if context is not None:
        state.info[_STAMP] = state.dict.get("tenant_id")


def _stamp_refreshed(state: Any, context: Any, attrs: Any) -> None:
    if context is not None and (attrs is None or "tenant_id" in attrs):
        state.info[_STAMP] = state.dict.get("tenant_id")


def _verified_flush_row(operation: str) -> Any:
    """The mapper event that re-verifies every row of a flush, whatever
    ``before_flush`` listener ran before it (a late one can add or alter rows
    after the guard's own check): a TenantScoped row must carry the bound tenant,
    an UPDATE / DELETE only a row whose ``tenant_id`` was read from the database
    (a forged identity would otherwise write another tenant's row by primary
    key). It also fills the flush window with what the flush may write: the
    tables of a verified INSERT, the ``(table, primary key)`` of a verified UPDATE
    / DELETE (:func:`_flush_write_admitted`). An inserted row is stamped only
    when the whole flush succeeded (the ``Session._flush`` wrapper)."""

    def handler(mapper: Mapper[Any], _connection: Any, state: Any) -> None:
        names = _tenant_names()
        if not names or bypass_active():
            return
        table_names = _mapper_table_names(mapper)
        if not issubclass(mapper.class_, TenantScoped):
            if not names.isdisjoint(table_names):
                raise UnsafeTenantOperationError(
                    f"{mapper.class_.__name__} is not TenantScoped but maps a table "
                    "named like a TenantScoped one; a flush cannot tenant-scope its "
                    "rows. Map the model with TenantScoped, or flush inside "
                    "explicit unscoped() for trusted system access"
                )
            return
        tid = current_tenant_id()
        instance = state.obj()
        if operation == "insert" and instance.tenant_id is None:
            instance.tenant_id = tid
        if instance.tenant_id != tid:
            raise TenantMismatchError(
                f"{mapper.class_.__name__}.tenant_id does not match the bound tenant"
            )
        if operation != "insert" and state.info.get(_STAMP) != tid:
            raise UnsafeTenantOperationError(
                f"{operation.upper()} of a {mapper.class_.__name__} whose tenant_id "
                "was not read from the database under the bound tenant (a forged or "
                "merged-without-load instance writes by primary key, so another "
                "tenant's row could be rewritten); load it through a scoped query"
            )
        window = _flush_window.get()
        if window is None:
            return
        if operation == "insert":
            window.tables.update(
                table for table in mapper.tables if isinstance(table, Table)
            )
            window.inserted[state] = tid
        else:
            # the committed primary key a WHERE pins, per table in its own key order
            window.rows.update(_row_keys(mapper, state.key[1]))

    return handler


def _record_inserted_row(mapper: Mapper[Any], _connection: Any, state: Any) -> None:
    """``after_insert``: the primary key of a row the flush just inserted joins
    the window, so a later statement of the same flush (a ``post_update``
    UPDATE) may pin it. Only a row ``before_insert`` verified is recorded."""
    window = _flush_window.get()
    if window is None or state not in window.inserted:
        return
    window.rows.update(_row_keys(mapper, mapper.primary_key_from_instance(state.obj())))


def _innermost_transaction(session: Session) -> SessionTransaction | None:
    """The savepoint the session is in, else its root transaction (``None``: no
    transaction is open). This is the owner of a flush's insert records."""
    return session.get_nested_transaction() or session.get_transaction()


def _release_inserted_rows(session: Session) -> None:
    """``after_commit``: a SAVEPOINT was released or the ROOT transaction
    committed (the event fires for both, while the committing transaction is
    still the innermost one). A released savepoint hands its insert records to
    its parent: the rows exist only if the parent commits. The root's commit
    makes its rows permanent, so their stamps stay and the records are dropped."""
    committing = _innermost_transaction(session)
    if committing is None:
        return
    records = _pending_inserts.pop(committing, None)
    parent = committing.parent
    if records and committing.nested and parent is not None:
        _pending_inserts.setdefault(parent, {}).update(records)


def _discard_insert_records(transaction: SessionTransaction) -> None:
    """Drop the insert records of ``transaction`` and the stamps they made. A
    record clears a stamp only while it still names the tenant the record made."""
    for state, tenant in _pending_inserts.pop(transaction, {}).items():
        if state.info.get(_STAMP) == tenant:
            state.info.pop(_STAMP, None)


def _forget_uncommitted_inserts(session: Session, transaction: Any) -> None:
    """``after_transaction_end``: a transaction (root, savepoint or the flush's
    own) ended. A record it still owns was never committed: a rollback undid the
    row, and ``session.close()`` rolls back an open root AND an open savepoint
    WITHOUT ``after_soft_rollback``. A stamp that outlived the row would let the
    held instance (``make_transient_to_detached``, or re-attached after another
    tenant took its primary key) write or read that tenant's row."""
    _discard_insert_records(transaction)


def _forget_rolled_back_inserts(session: Session) -> None:
    """``after_rollback``: the database transaction of the innermost boundary (a
    savepoint, else the root) was rolled back. A FAILED flush does this while
    SQLAlchemy ends only the flush's own subtransaction: the boundary stays open
    (inactive) and ``after_transaction_end`` does not name it, yet the rows it
    inserted earlier are gone. Their records and stamps go now, fail-closed; the
    records of the transactions that enclose a rolled-back savepoint stay."""
    boundary = _innermost_transaction(session)
    if boundary is not None:
        _discard_insert_records(boundary)


def install_tenant_guard() -> None:  # noqa: C901
    """Install the automatic tenant-criteria ORM hook (idempotent).

    Registered once at import; also exposed so a composition root can ensure it.
    Tenant-scoped ORM operations fail closed unless a tenant is bound or the
    caller explicitly enters ``unscoped()``.
    """
    global _guard_installed, _tenant_criteria
    if _guard_installed:
        return
    _tenant_criteria = _build_tenant_criteria()

    @event.listens_for(Session, "do_orm_execute")
    def _apply_tenant_criteria(state: Any) -> None:  # pragma: no cover - DB-exercised  # noqa: C901
        if bypass_active():
            return
        if not _tenant_tables():
            # Early exit (hot path): no TenantScoped model is mapped in this
            # process, so no statement can reach a tenant-owned table and there
            # is nothing to scope or refuse — skip the mapper scan, the cache-key
            # generation and the loader criteria entirely. Defining a
            # TenantScoped subclass invalidates the memoized table set.
            return
        if _params_override_tenant(state.parameters):
            raise UnsafeTenantOperationError(
                f"the execution parameters name {TENANT_CRITERIA_PARAM!r}, the "
                "guard's own tenant bind: a caller-supplied value would pick the "
                "tenant. Pass only your statement's own parameters"
            )
        # Column loads (deferred/expired attribute refresh of an already-loaded
        # row, by primary key) carry no criterion: they are admitted for an
        # instance whose tenant_id was read from the database (its stamp), not
        # for a forged one. Relationship loads are NOT exempt: a selectinload/
        # lazy load of a TenantScoped collection is its own query and must be
        # scoped too.
        if state.is_column_load:
            _verify_column_load(state)
            return
        tenant_mappers = [
            mapper
            for mapper in state.all_mappers
            if isinstance(mapper.class_, type)
            and issubclass(mapper.class_, TenantScoped)
        ]
        # The four statement kinds ORM compiles (and the loader criteria below
        # reach). Anything else that is not ``text()`` still carries SQL: a bare
        # ``func.f(select(...))`` sets none of the four flags.
        scopable = (
            state.is_select or state.is_update or state.is_delete or state.is_insert
        )
        is_text = isinstance(state.statement, TextClause)
        if (scopable or not is_text) and _unsafe_core_reference(state.statement):
            if not state.is_insert and _current_tenant.get() is None:
                raise _missing_tenant("a Core statement", _model_names(tenant_mappers))
            raise UnsafeTenantOperationError(
                "Core statements on TenantScoped tables cannot be automatically "
                "scoped; use the ORM entity or explicit unscoped() for trusted "
                "system access"
            )
        if state.is_insert and tenant_mappers:
            raise UnsafeTenantOperationError(
                "bulk INSERT of TenantScoped models is forbidden; use ORM "
                "instances so tenant ownership is enforced, or explicit "
                "unscoped() for trusted system imports"
            )
        if not scopable:
            # A function, DDL or other executable: ORM compiles no select for
            # it, so the loader criteria below never reach a subquery it holds.
            # Whatever TenantScoped table it reaches cannot be scoped. Raw
            # ``text()`` is the caller's responsibility (``scoped()`` /
            # ``tenant_filter()``, the ``unsafe-tenant-sql`` check).
            if is_text:
                _approve(state.statement, _TRUSTED)  # the caller's own SQL
            elif _references_tenant_scoped(state.statement):
                if _current_tenant.get() is None:
                    raise _missing_tenant(
                        "a " + type(state.statement).__name__ + " statement",
                        _model_names(tenant_mappers),
                    )
                raise UnsafeTenantOperationError(
                    f"a {type(state.statement).__name__} statement that reaches a "
                    "TenantScoped table cannot be automatically scoped; use an ORM "
                    "select, or explicit unscoped() for trusted system access"
                )
            return
        # Cover SELECT, INSERT *and* ORM-enabled bulk UPDATE/DELETE: a bound
        # request issuing ``update(TenantOwned).values(...)`` must not touch
        # other tenants' rows, and ``insert(Other).values(x=select(Owned.x))``
        # must not copy them. ``with_loader_criteria`` applies to the WHERE
        # clause of ORM UPDATE/DELETE and to the subqueries of an INSERT just as
        # it does to SELECT.
        if (state.is_update or state.is_delete) and tenant_mappers:
            _reject_unsafe_bulk_mutation(state)
        tid = _current_tenant.get()
        if tid is None:
            # Fail closed on ANY TenantScoped table reachable from the statement
            # — top-level entity, join target (incl. relationship joins),
            # select_from, EXISTS / IN / scalar subqueries, eager-load options
            # — not only top-level entities.
            if tenant_mappers or _references_tenant_scoped(state.statement):
                raise _missing_tenant(
                    "an ORM " + _statement_kind(state), _model_names(tenant_mappers)
                )
            return
        if not tenant_mappers and not _references_tenant_scoped(state.statement):
            # Bound tenant, but the statement provably touches no TenantScoped
            # table (memoized structural verdict — the same walk that decides
            # fail-closed above): the loader criteria would be a no-op, so do
            # not pay for building it and re-keying the statement. A later
            # lazy/selectin load of a tenant-owned relationship is its own
            # ORM execution and is scoped there.
            return
        if any(_mapper_sql_reaches_raw_tenant(mapper) for mapper in state.all_mappers):
            raise UnsafeTenantOperationError(
                "the SQL a mapper adds on its own (a column_property / deferred / "
                "query_expression expression, a relationship secondary) reads a "
                "TenantScoped table as raw Core, which the guard's criterion does "
                "not scope; read it through the ORM entity, or use explicit "
                "unscoped() for trusted system access"
            )
        # Shared structure; the VALUE is read from the context per execution.
        state.statement = state.statement.options(_tenant_criteria)
        _approve(state.statement, _CRITERION)

    @event.listens_for(Session, "before_flush")
    def _enforce_tenant_writes(
        session: Session, _flush_context: Any, _instances: Any
    ) -> None:  # pragma: no cover - DB-exercised
        if bypass_active():
            return
        new, dirty = session.new, session.dirty
        tenant_objects = (
            obj
            for collection in (new, dirty, session.deleted)
            for obj in collection
            if isinstance(obj, TenantScoped)
        )
        for obj in tenant_objects:
            tid = current_tenant_id()
            object_tid = getattr(obj, "tenant_id", None)
            if obj in session.new and object_tid is None:
                obj.tenant_id = tid
            elif object_tid != tid:
                raise TenantMismatchError(
                    f"{type(obj).__name__}.tenant_id does not match the bound tenant"
                )

    # The stamp: which instances had their tenant_id READ from the database.
    event.listen(TenantScoped, "load", _stamp_loaded, propagate=True, raw=True)
    event.listen(TenantScoped, "refresh", _stamp_refreshed, propagate=True, raw=True)
    # An insert's stamp waits for its flush to succeed (the ``_flush`` wrapper)
    # and goes with any end of the transaction that inserted the row that is no
    # commit of the root (per-transaction records: ``_pending_inserts``).
    event.listen(Session, "after_commit", _release_inserted_rows)
    event.listen(Session, "after_transaction_end", _forget_uncommitted_inserts)
    event.listen(Session, "after_rollback", _forget_rolled_back_inserts)
    # Per-row re-verification inside the flush (every mapper: a non-TenantScoped
    # one over a tenant table is refused here), after any later before_flush.
    for operation in ("insert", "update", "delete"):
        event.listen(
            Mapper, f"before_{operation}", _verified_flush_row(operation), raw=True
        )
    event.listen(Mapper, "after_insert", _record_inserted_row, raw=True)
    _install_identity_map_guard()
    _install_flush_window()
    _install_legacy_bulk_guard()
    _guard_installed = True


# Register the automatic guard as soon as this module is imported (e.g. via
# sandloom.core.orm or sandloom.core.di re-exports), so any tenant-scoped query is
# covered.
install_tenant_guard()


__all__ = [
    "TenantScoped",
    "MissingTenantError",
    "TenantMismatchError",
    "UnsafeTenantOperationError",
    "current_tenant_id",
    "current_tenant_id_optional",
    "bind_tenant",
    "unscoped",
    "tenant_filter",
    "scoped",
    "install_tenant_guard",
]
