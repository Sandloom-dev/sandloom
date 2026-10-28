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

import uuid
from typing import Any

from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint, bindparam, event
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column, with_loader_criteria
from sqlalchemy.sql import visitors
from sqlalchemy.sql.selectable import Alias, SelectBase

from sandloom.core import error_codes
from sandloom.core.tenant_context import (
    MissingTenantError,
    TenantMismatchError,
    UnsafeTenantOperationError,
    _bypass,
    _current_tenant,
    bind_tenant,
    current_tenant_id,
    current_tenant_id_optional,
    unscoped,
)


class TenantScoped:
    """Mixin marking a model as tenant-owned (adds the indexed ``tenant_id``).

    Inheriting this both declares the column and opts the model into automatic
    tenant filtering. Declare models as ``class Foo(TenantScoped, Base)``.
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )

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
_reference_cache: dict[Any, bool] = {}
_unsafe_core_cache: dict[Any, bool] = {}
#: Counters (observability + the guard's micro-benchmark test).
guard_cache_stats: dict[str, int] = {"walks": 0, "hits": 0}


def _invalidate_guard_caches() -> None:
    global _tenant_tables_cache
    _tenant_tables_cache = None
    _reference_cache.clear()
    _unsafe_core_cache.clear()


def _tenant_tables() -> frozenset[Any]:
    """Tables of every mapped TenantScoped model (memoized subclass-tree walk)."""
    global _tenant_tables_cache
    cached = _tenant_tables_cache
    if cached is not None:
        return cached
    tables: set[Any] = set()
    stack: list[type] = list(TenantScoped.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        table = getattr(cls, "__table__", None)
        if table is not None:
            tables.add(table)
    result = frozenset(tables)
    _tenant_tables_cache = result
    return result


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


def _references_tenant_scoped(statement: Any) -> bool:
    """Memoized :func:`_walk_references_tenant_scoped` (see guard caches)."""
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

    try:
        cache_key = statement._generate_cache_key()
        key = cache_key.key if cache_key is not None else None
        if key is not None and key in _unsafe_core_cache:
            return _unsafe_core_cache[key]
    except Exception:  # noqa: BLE001 - an uncacheable statement is still checked
        key = None
    walk = _UnsafeCoreWalk(_tenant_tables(), Alias, SelectBase)

    try:
        verdict = walk.unsafe(statement, frozenset())
    except Exception:  # noqa: BLE001 - fail closed for opaque tenant SQL
        verdict = True
    if key is not None:
        if len(_unsafe_core_cache) >= _GUARD_CACHE_MAX:
            _unsafe_core_cache.clear()
        _unsafe_core_cache[key] = verdict
    return verdict


def _tenant_annotated(node: Any) -> bool:
    """``node`` carries an ORM annotation of a TenantScoped entity."""
    annotations = getattr(node, "_annotations", {})
    return any(
        _is_tenant_class(getattr(annotations.get(name), "class_", None))
        for name in ("parentmapper", "parententity")
    )


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


class _UnsafeCoreWalk:
    """One :func:`_unsafe_core_reference` walk (visited set per statement)."""

    __slots__ = ("alias", "select_base", "tables", "visited")

    def __init__(self, tables: Any, alias: type[Any], select_base: type[Any]) -> None:
        self.tables = tables
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
        if node in self.tables or self._raw_alias_of_tenant_table(node):
            return True
        table = getattr(node, "table", None)
        if table is not None and self.unsafe(table, scope):
            return True
        for join in getattr(node, "_setup_joins", ()):
            if any(part is not None and self.unsafe(part, scope) for part in join[:3]):
                return True
        children = getattr(node, "get_children", None)
        return (
            any(self.unsafe(child, scope) for child in children())
            if callable(children)
            else False
        )

    def _raw_alias_of_tenant_table(self, node: Any) -> bool:
        # An alias is not protected by the ORM predicate on its underlying
        # table: it can independently range across every tenant.
        raw: Any = node
        while isinstance(raw, self.alias):
            raw = raw.element
        return raw is not node and raw in self.tables


def _walk_references_tenant_scoped(statement: Any) -> bool:  # noqa: C901
    """True if ANY TenantScoped table/entity is reachable from ``statement``.

    Walks the whole clause tree (joins, ``select_from``, EXISTS / IN / scalar
    subqueries, correlated selects), ORM relationship joins (``_setup_joins``)
    and eager-loader option paths (``joinedload(Parent.children)`` adds the
    child table to the SAME SQL without a separate ORM execution).
    """

    guard_cache_stats["walks"] += 1
    tables = _tenant_tables()
    if not tables:
        return False

    def _hit(element: Any) -> bool:
        if element in tables:
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
        return False

    def _hit_shallow(part: Any) -> bool:
        prop = getattr(part, "property", None)
        if _is_tenant_class(getattr(getattr(prop, "mapper", None), "class_", None)):
            return True
        if _is_tenant_class(part) or part in tables:
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


_original_session_get: Any = None


def _install_identity_map_guard() -> None:
    """Make ``Session.get`` honour the bound tenant on identity-map hits.

    ``session.get(Model, pk)`` returns an object straight from the identity map
    WITHOUT emitting SQL (so ``do_orm_execute`` never runs) — e.g. an object
    loaded earlier in the same session under ``unscoped()``. For a TenantScoped
    result this re-checks ownership: no bound tenant → ``MissingTenantError``
    (fail closed, as the SQL path does); another tenant's object → ``None``
    (exactly what the auto-filtered SQL would return). ``get_one`` delegates to
    ``get`` and is covered too. ``unscoped()`` skips the check.
    """
    global _original_session_get
    if _original_session_get is not None:
        return
    original = Session.get
    _original_session_get = original

    def get(self: Session, entity: Any, ident: Any, **kwargs: Any) -> Any:
        instance = original(self, entity, ident, **kwargs)
        if instance is None or _bypass.get() or not isinstance(instance, TenantScoped):
            return instance
        tid = _current_tenant.get()
        if tid is None:
            raise _missing_tenant(
                f"Session.get({type(instance).__name__}, ...)", type(instance).__name__
            )
        if getattr(instance, "tenant_id", None) != tid:
            return None
        return instance

    get.__doc__ = original.__doc__
    Session.get = get  # type: ignore[method-assign]


_guard_installed = False


def _model_names(mappers: list[Any]) -> str:
    names = sorted({m.class_.__name__ for m in mappers})
    return ", ".join(names) or "a TenantScoped table reached through a join"


def _statement_kind(state: Any) -> str:
    for kind in ("select", "update", "delete"):
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
        type_=PG_UUID(as_uuid=True),
    )
    return with_loader_criteria(
        TenantScoped,
        lambda cls: cls.tenant_id == tenant,
        include_aliases=True,
    )


_tenant_criteria: Any = None


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
        # Column loads (deferred/expired attribute refresh of an already-loaded
        # row, by primary key) are scoped by the load that produced the row.
        # Relationship loads are NOT exempt: a selectinload/lazy load of a
        # TenantScoped collection is its own query and must be scoped too.
        if state.is_column_load:
            return
        if _bypass.get():
            return
        if not _tenant_tables():
            # Early exit (hot path): no TenantScoped model is mapped in this
            # process, so no statement can reach a tenant-owned table and there
            # is nothing to scope or refuse — skip the mapper scan, the cache-key
            # generation and the loader criteria entirely. Defining a
            # TenantScoped subclass invalidates the memoized table set.
            return
        tenant_mappers = [
            mapper
            for mapper in state.all_mappers
            if isinstance(mapper.class_, type)
            and issubclass(mapper.class_, TenantScoped)
        ]
        if (
            state.is_select or state.is_update or state.is_delete or state.is_insert
        ) and _unsafe_core_reference(state.statement):
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
        # Cover SELECT *and* ORM-enabled bulk UPDATE/DELETE: a bound request
        # issuing ``update(TenantOwned).values(...)`` must not touch other
        # tenants' rows. ``with_loader_criteria`` applies to the WHERE clause of
        # ORM UPDATE/DELETE just as it does to SELECT. Raw ``text()`` / Core
        # (non-ORM) statements cannot carry loader criteria — those remain the
        # caller's responsibility via ``scoped()`` / ``tenant_filter()``.
        if not (state.is_select or state.is_update or state.is_delete):
            return
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
        # Shared structure; the VALUE is read from the context per execution.
        state.statement = state.statement.options(_tenant_criteria)

    @event.listens_for(Session, "before_flush")
    def _enforce_tenant_writes(
        session: Session, _flush_context: Any, _instances: Any
    ) -> None:  # pragma: no cover - DB-exercised
        if _bypass.get():
            return
        tenant_objects = (
            obj
            for collection in (session.new, session.dirty, session.deleted)
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

    _install_identity_map_guard()
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
