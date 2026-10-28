"""Proof obligations for bypassing SQLAlchemy during a declared read.

Unknown types, mappings and application policies use the general executor.
This is deliberately conservative: a faster result with different semantics
is not an optimization. Policy checks also run for already prepared plans.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Column, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import ColumnProperty, Session
from sqlalchemy.sql import sqltypes

_NATIVE_TYPES = (
    sqltypes.Integer,
    sqltypes.String,
    sqltypes.Boolean,
    sqltypes.Date,
    sqltypes.DateTime,
    sqltypes.Time,
    sqltypes.LargeBinary,
    sqltypes.Uuid,
)
#: Exact dialect types that are native as RESULT columns only. PostgreSQL's
#: ``citext`` has no SQLAlchemy result processing and asyncpg decodes it to the
#: same ``str`` (text I/O) on both executors. As a key, filter or order column
#: its comparison semantics depend on the parameter's inferred type (``citext``
#: vs a cast ``varchar``/``text`` is case-sensitive), so those stay general.
_RESULT_ONLY_TYPES: tuple[type, ...] = (postgresql.CITEXT,)
_ENGINE_EVENTS = (
    "before_execute",
    "after_execute",
    "before_cursor_execute",
    "after_cursor_execute",
    "engine_connect",
    "begin",
    "commit",
    "rollback",
)
_SESSION_EVENTS = (
    "loaded_as_persistent",
    "after_begin",
    "after_transaction_create",
    "after_transaction_end",
    "after_rollback",
    "after_soft_rollback",
)


#: One closed, never-used ``Session`` whose dispatch is read per planned read.
_PROBE: Session | None = None


def _session_probe() -> Session:
    """The process's listener probe, built once (``Session()`` costs ~4 µs).

    It is never a cache of the verdict: the listeners are re-read on every
    call. A listener added to (or removed from) the ``Session`` class after
    the probe was built is seen through it — SQLAlchemy applies class-level
    listeners to every instance, existing ones included; pinned by
    ``test_planner_policy_probe.py`` so an upgrade that stopped doing so
    fails the suite instead of skipping a new listener.
    """
    global _PROBE
    probe = _PROBE
    if probe is None:
        probe = Session()
        probe.close()
        _PROBE = probe
    return probe


def policy_reason(engine: Any, mappers: Sequence[Any]) -> str | None:
    """Never skip user-installed row filters, load hooks or SQL policies.

    The only recognized ORM execute listener is the kernel's tenant guard,
    whose predicate and GUC the optimized executor implements explicitly.
    A rename / new event shape falls back rather than guessing compatibility.
    """
    try:
        if engine.sync_engine.get_execution_options().get("schema_translate_map"):
            return "schema translation requires the general executor"
        probe = _session_probe()
        for listener in probe.dispatch.do_orm_execute:
            if not (
                getattr(listener, "__module__", "") == "sandloom.core.tenant"
                and getattr(listener, "__qualname__", "")
                == "install_tenant_guard.<locals>._apply_tenant_criteria"
            ):
                return "custom ORM execute listener requires the general executor"
        if any(tuple(getattr(probe.dispatch, name)) for name in _SESSION_EVENTS):
            return "custom ORM session listener requires the general executor"
        dispatch = engine.sync_engine.dispatch
        if any(tuple(getattr(dispatch, name)) for name in _ENGINE_EVENTS):
            return "custom engine listener requires the general executor"
        for mapper in mappers:
            if tuple(mapper.class_manager.dispatch.load) or tuple(
                mapper.class_manager.dispatch.refresh
            ):
                return "custom ORM load listener requires the general executor"
    except (AttributeError, TypeError):
        return "cannot verify ORM listeners; using the general executor"
    return None


def optimized_reason(  # noqa: C901
    engine: Any,
    mappers: Sequence[Any],
    columns: Sequence[Any],
    *,
    parameters: Sequence[Any] = (),
    constants: Sequence[tuple[Any, Any]] = (),
) -> str | None:
    reason = policy_reason(engine, mappers)
    if reason:
        return reason
    for mapper in mappers:
        if (
            mapper.inherits is not None
            or len(mapper.tables) != 1
            or mapper.polymorphic_on is not None
            or len(mapper.polymorphic_map) > 1
        ):
            return "entity has inheritance or polymorphic mapping"
        if any(
            prop.lazy in ("joined", "subquery", "selectin")
            for prop in mapper.relationships
        ):
            return "ORM eager-loading policy requires the general executor"
        if any(
            prop.deferred
            for prop in mapper.column_attrs
            if any(
                column is selected for column in prop.columns for selected in columns
            )
        ):
            return "ORM deferred column policy requires the general executor"
        if any(
            prop.comparator_factory is not ColumnProperty.Comparator
            for prop in mapper.column_attrs
        ):
            return "custom column comparator requires the general executor"
    bound = {id(column) for column in parameters}
    bound.update(id(column) for column, _ in constants)
    for column in columns:
        if not isinstance(column, Column):
            return "an expression is not a plain column"
        kind = column.type
        if type(kind) in _RESULT_ONLY_TYPES and not kind._variant_mapping:
            if id(column) in bound:
                return (
                    f"column {column.name!r}: a {type(kind).__name__} key, filter "
                    "or order column requires SQLAlchemy bind processing"
                )
            continue
        if (
            type(kind).__module__ != "sqlalchemy.sql.sqltypes"
            or not isinstance(kind, _NATIVE_TYPES)
            or isinstance(kind, sqltypes.Enum)
            or (
                isinstance(kind, sqltypes.Uuid)
                and (not kind.as_uuid or not kind.native_uuid)
            )
            or bool(kind._variant_mapping)
        ):
            return f"column {column.name!r} has a type requiring SQLAlchemy processing"
    # Boolean is native on results, but SQLAlchemy normalizes integer 0/1
    # inputs that raw asyncpg rejects. Keep dynamic Boolean binds on the ORM
    # path; static, exact bool/None values need no conversion.
    if any(isinstance(column.type, sqltypes.Boolean) for column in parameters):
        return "dynamic Boolean parameters require SQLAlchemy bind processing"
    if any(
        isinstance(column.type, sqltypes.Boolean)
        and value is not None
        and type(value) is not bool
        for column, value in constants
    ):
        return "Boolean constants require SQLAlchemy bind processing"
    return None


def unique_keys(mapper: Any) -> list[set[Any]]:
    """Unconditional keys only: a partial index proves nothing outside its WHERE."""
    table = mapper.local_table
    keys = [set(mapper.primary_key)]
    keys.extend(
        set(c.columns) for c in table.constraints if isinstance(c, UniqueConstraint)
    )
    keys.extend(
        set(ix.columns)
        for ix in table.indexes
        if ix.unique and ix.dialect_options["postgresql"].get("where") is None
    )
    return keys


def covering_indexes(mapper: Any) -> list[list[Any]]:
    table = mapper.local_table
    candidates = [list(mapper.primary_key)]
    candidates.extend(
        list(c.columns) for c in table.constraints if isinstance(c, UniqueConstraint)
    )
    candidates.extend(
        list(ix.columns)
        for ix in table.indexes
        if ix.dialect_options["postgresql"].get("where") is None
        and ix.dialect_options["postgresql"].get("using") in (None, False, "btree")
    )
    return candidates
