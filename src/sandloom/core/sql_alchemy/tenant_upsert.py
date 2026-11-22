"""``tenant_upsert``: a tenant-scoped ``INSERT ... ON CONFLICT DO UPDATE`` that the
helper builds from plain rows (ADR-0018, decision 6).

A conflict insert on a ``TenantScoped`` table is refused by the tenant guard
whatever its caller built: the guard cannot tell a harmless statement from one
that rewrites another tenant's row (a CTE that carries DML, raw SQL in a value
or in ``RETURNING``, a tenant key under a name the guard did not know, an
``ON CONFLICT`` on a key that does not include the tenant). So the caller does
not build one. The caller hands over rows of plain Python values and the names
of the columns to refresh, and this module builds the statement:

* the tenant column is never a row key: its value is the bound tenant, bound
  with the column's own type;
* the conflict target is a unique key of the table that includes the tenant
  column, so a row of another tenant is never the one that conflicts (every other
  key and every foreign key of the table acts as on a plain insert: see the
  multi-tenancy page);
* ``DO UPDATE SET <column> = excluded.<column>`` for the listed columns, ``WHERE
  <tenant column> = <bound tenant>``, and nothing else: no caller expression;
* the statement is built by :func:`canonical_tenant_upsert`, the one builder both
  this helper and the cursor guard use: the statement runs on the unit's
  connection, marked for the guard, which admits it only when its SQL equals the
  SQL of the statement the builder makes from the parts it read out of it, and
  the bound tenant is the tenant value of every row
  (:func:`sandloom.core.tenant._tenant_upsert_admitted`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, Index, Table, UniqueConstraint, inspect
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapper
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import ClauseElement

from sandloom.core import error_codes
from sandloom.core.sql_alchemy.portable import (
    UnsupportedSqlForm,
    conflict_insert_for,
    dialect_name,
    is_plain_index,
)
from sandloom.core.tenant import (
    TenantScoped,
    _plain_uuid,
    _seal_tenant_upsert,
    _tenant_column,
    _tenant_tables_read,
    _tenant_upsert_shape,
    _tenant_upsert_where,
    _unseal_tenant_upsert,
    has_plain_columns,
)
from sandloom.core.tenant_context import current_tenant_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: The attribute that holds a ``TenantScoped`` model's tenant (the mixin's).
_TENANT_ATTRIBUTE = "tenant_id"


class TenantUpsertError(ValueError):
    """``tenant_upsert`` refused what it was given (SANDLOOM-R018): a model, a
    row or a column list it cannot turn into a statement that stays inside the
    bound tenant. Nothing was sent to the database."""

    sandloom_code = "SANDLOOM-R018"

    def __init__(self, message: str) -> None:
        super().__init__(
            f"tenant_upsert: {message}. {error_codes.fix_line(self.sandloom_code)}"
        )


def _mapper_of(model: Any) -> tuple[Mapper[Any], Table]:
    """The mapper and table of a plain ``TenantScoped`` model: one table, no
    inheritance. A model of an inheritance hierarchy spans tables or shares one
    with a discriminator column, and the helper writes one table from plain
    values."""
    if not (isinstance(model, type) and issubclass(model, TenantScoped)):
        raise TenantUpsertError(f"{model!r} is not a TenantScoped model class")
    mapper = inspect(model, raiseerr=False)
    if not isinstance(mapper, Mapper):
        raise TenantUpsertError(f"{model.__name__} is not a mapped class")
    table = mapper.local_table
    if (
        not isinstance(table, Table)
        or mapper.inherits is not None
        or mapper.polymorphic_on is not None
        or len(mapper.tables) != 1
        or mapper.persist_selectable is not table
    ):
        raise TenantUpsertError(
            f"{model.__name__} is part of an inheritance hierarchy or is not mapped "
            "to one plain table; the helper writes one table from plain values and "
            "does not support joined or single-table inheritance"
        )
    if not has_plain_columns(table):
        raise TenantUpsertError(
            f"{table.name} has a column that is not exactly sqlalchemy.Column (a "
            "subclass renders its own SQL, which the tenant guard cannot vouch for "
            "in an upsert); declare its columns with mapped_column / Column"
        )
    replacing = _conflict_clauses(table)
    if replacing:
        raise TenantUpsertError(
            f"{table.name} declares a conflict resolution of its own "
            f"({', '.join(replacing)}): SQLite's REPLACE deletes the row that "
            "conflicts and inserts the new one, whichever tenant owns it. Remove the "
            "sqlite_on_conflict* option, or write the rows through the ORM"
        )
    return mapper, table


def _conflict_clauses(table: Table) -> list[str]:
    """The ``sqlite_on_conflict*`` options a column or a constraint of ``table``
    carries (``Column(..., sqlite_on_conflict_unique="REPLACE")``,
    ``UniqueConstraint(..., sqlite_on_conflict="REPLACE")``): SQLite resolves such
    a conflict before ``ON CONFLICT DO UPDATE`` sees it, by deleting the row of
    ANY tenant that holds the key."""
    owners: list[tuple[str, Any]] = [
        *((f"column {column.name}", column) for column in table.columns),
        *(
            (f"constraint {constraint.name or type(constraint).__name__}", constraint)
            for constraint in table.constraints
        ),
        *((f"index {index.name}", index) for index in table.indexes),
    ]
    return [
        f"{label}: {option}"
        for label, owner in owners
        for option in owner.dialect_kwargs
        if option.startswith("sqlite_on_conflict")
    ]


def _column_of(mapper: Mapper[Any], table: Table, key: Any, what: str) -> Column[Any]:
    """The column the model attribute ``key`` maps to, resolved through the mapper
    (``mapper.column_attrs``), never by the spelling of a name."""
    if not isinstance(key, str):
        raise TenantUpsertError(f"{what} {key!r} is not an attribute name")
    prop = mapper.column_attrs.get(key)
    if prop is None:
        raise TenantUpsertError(
            f"{what} {key!r} is not a column attribute of {mapper.class_.__name__} "
            "(use the attribute names of the model)"
        )
    column = prop.columns[0] if len(prop.columns) == 1 else None
    if not isinstance(column, Column) or column.table is not table:
        raise TenantUpsertError(
            f"{what} {key!r} is not a plain column of {table.name} (a SQL expression "
            "property cannot be written)"
        )
    return column


def _tenant_of(mapper: Mapper[Any], table: Table) -> Column[Any]:
    """The tenant column: the column of the ``tenant_id`` attribute, which must
    be the column the cursor guard names ``tenant_id`` (it finds it by name)."""
    column = _column_of(mapper, table, _TENANT_ATTRIBUTE, "the tenant attribute")
    if column is not _tenant_column(table):
        raise TenantUpsertError(
            f"the tenant_id attribute of {mapper.class_.__name__} maps to column "
            f"{column.name!r}; the helper needs the column to be named 'tenant_id', "
            "the one the tenant guard scopes"
        )
    return column


def _is_sql(value: Any) -> bool:
    """``value`` is a SQL expression, or an object SQLAlchemy would turn into one
    (``__clause_element__``: a model attribute, a hybrid)."""
    return isinstance(value, ClauseElement) or hasattr(
        type(value), "__clause_element__"
    )


def _has_predicate(index: Index) -> bool:
    """A partial index: ``ON CONFLICT`` infers it only with its predicate."""
    return not is_plain_index(index) or any(
        name.endswith("_where") for name in index.dialect_kwargs
    )


def _unique_keys(table: Table) -> list[tuple[Column[Any], ...]]:
    """The unique keys of ``table`` an ``ON CONFLICT`` target can name: its
    primary key, its unique constraints and its plain unique indexes over
    columns (no predicate, no expression). A ``DEFERRABLE`` constraint is left
    out: PostgreSQL cannot infer it as the arbiter of ``ON CONFLICT DO UPDATE``."""
    keys: list[tuple[Column[Any], ...]] = []
    if table.primary_key.columns and not table.primary_key.deferrable:
        keys.append(tuple(table.primary_key.columns))
    keys.extend(
        tuple(constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint) and not constraint.deferrable
    )
    for index in table.indexes:
        columns = tuple(index.expressions)
        if (
            index.unique
            and not _has_predicate(index)
            and all(isinstance(c, Column) and c.table is table for c in columns)
        ):
            keys.append(tuple(c for c in columns if isinstance(c, Column)))
    # ``table.constraints`` is a set: without an order the message and the
    # statement would change from one process to the next
    position = {column.name: place for place, column in enumerate(table.columns)}
    keys.sort(key=lambda key: tuple(position[column.name] for column in key))
    seen: set[frozenset[str]] = set()
    unique: list[tuple[Column[Any], ...]] = []
    for key in keys:
        names = frozenset(column.name for column in key)
        if names not in seen:
            seen.add(names)
            unique.append(key)
    return unique


def _conflict_target(
    mapper: Mapper[Any],
    table: Table,
    tenant: Column[Any],
    conflict_on: Sequence[str] | None,
) -> tuple[Column[Any], ...]:
    """The unique key the upsert conflicts on: one of the table's that includes
    the tenant column. ``conflict_on`` names it by attributes (the tenant column
    belongs to every such key, so it may be left out); without it the table must
    have exactly one."""
    candidates = [
        key for key in _unique_keys(table) if any(c.name == tenant.name for c in key)
    ]
    model = mapper.class_.__name__
    if conflict_on is not None:
        if isinstance(conflict_on, str) or not conflict_on:
            raise TenantUpsertError(
                "conflict_on must be a non-empty list of the attributes of a unique "
                "key" + _candidates_hint(candidates, mapper)
            )
        wanted = {
            _column_of(mapper, table, key, "conflict_on").name for key in conflict_on
        } | {tenant.name}
        for key in candidates:
            if {column.name for column in key} == wanted:
                return key
        raise TenantUpsertError(
            f"conflict_on={list(conflict_on)!r} is not a unique key of {model} that "
            "includes the tenant column" + _candidates_hint(candidates, mapper)
        )
    if not candidates:
        raise TenantUpsertError(
            f"{model} has no unique key that includes the tenant column: the upsert "
            "conflicts only on a key of the bound tenant (declare "
            "UniqueConstraint('tenant_id', ...) or make tenant_id part of the "
            "primary key)"
        )
    if len(candidates) > 1:
        raise TenantUpsertError(
            f"{model} has several unique keys that include the tenant column; "
            "name the one to conflict on with conflict_on=[...]"
            + _candidates_hint(candidates, mapper)
        )
    return candidates[0]


def _candidates_hint(
    candidates: list[tuple[Column[Any], ...]], mapper: Mapper[Any]
) -> str:
    """The candidate keys by MODEL ATTRIBUTE name (what ``conflict_on`` takes),
    not by the column names of the table."""
    attributes = {
        id(column): prop.key for prop in mapper.column_attrs for column in prop.columns
    }
    keys = "; ".join(
        ", ".join(attributes.get(id(column), column.name) for column in key)
        for key in candidates
    )
    return (
        f" (candidate keys of {mapper.class_.__name__}, by attribute: {keys or 'none'})"
    )


def _plain_row(
    row: Any,
    index: int,
    mapper: Mapper[Any],
    table: Table,
    tenant: Column[Any],
) -> dict[str, Any]:
    """The parameters of one row: the column KEY of each attribute, with its value
    (a plain value, never the tenant)."""
    if not isinstance(row, Mapping):
        raise TenantUpsertError(f"row {index} is not a mapping of column to value")
    parameters: dict[str, Any] = {}
    for key, value in row.items():
        column = _column_of(mapper, table, key, f"row {index} key")
        if column.name == tenant.name:
            raise TenantUpsertError(
                f"row {index} names the tenant column ({key!r}): the tenant of a row "
                "is the bound tenant, never a value of the row"
            )
        if column.key in parameters:
            raise TenantUpsertError(
                f"row {index} names the column {column.name!r} twice (two attributes)"
            )
        if _is_sql(value):
            raise TenantUpsertError(
                f"row {index} value of {key!r} is a SQL expression; rows carry plain "
                "Python values only (the helper binds them as parameters)"
            )
        parameters[column.key] = value
    return parameters


def canonical_tenant_upsert(
    dialect: Dialect,
    table: Table,
    target: Sequence[Column[Any]],
    update: Sequence[Column[Any]],
) -> Insert:
    """THE statement of a tenant upsert, from its parts: ``INSERT`` into ``table``,
    ``ON CONFLICT (target) DO UPDATE SET <c> = excluded.<c> for c in update WHERE
    tenant_id = <bound tenant>``, written by ``dialect``'s renderer.

    The helper builds the statement it runs with this function, and the cursor
    guard rebuilds the statement it is shown with the same function from the parts
    it reads out of it (:func:`sandloom.core.tenant._renders_canonically`): the
    guard admits only a statement whose SQL is this one's, so what it admits is
    what this function writes and nothing a caller shaped."""
    tenant = _tenant_column(table)
    if tenant is None:
        raise TenantUpsertError(f"{table.name} has no tenant_id column")
    statement = conflict_insert_for(dialect, table)
    excluded = statement.excluded
    return statement.on_conflict_do_update(
        index_elements=list(target),
        set_={column: excluded[column.key] for column in update},
        where=_tenant_upsert_where(tenant),
    )


def _refreshed_columns(
    mapper: Mapper[Any], table: Table, tenant: Column[Any], update: Sequence[str]
) -> tuple[Column[Any], ...]:
    """The columns ``update`` names: the table's own, never the tenant or a primary
    key column, each once."""
    if isinstance(update, str) or not update:
        raise TenantUpsertError(
            "update must be a non-empty list of the column attributes to refresh "
            "when the row exists"
        )
    primary = {column.name for column in table.primary_key.columns}
    refreshed: dict[str, Column[Any]] = {}
    for key in update:
        column = _column_of(mapper, table, key, "update")
        if column.name == tenant.name or column.name in primary:
            raise TenantUpsertError(
                f"update names {key!r}, the tenant column or a primary key column; "
                "neither is ever updated"
            )
        if column.name in refreshed:
            raise TenantUpsertError(f"update names {key!r} twice")
        refreshed[column.name] = column
    return tuple(refreshed.values())


def _refuse_what_the_guard_would(
    statement: Insert,
    dialect: Dialect,
    table: Table,
    parameters: list[dict[str, Any]],
) -> None:
    """Answer before ``session.flush()`` what the cursor guard would answer after
    it: a refusal then would come after the pending changes were sent."""
    if _tenant_upsert_shape(statement) is None:
        # a dialect renderer whose ON CONFLICT clause the cursor guard cannot read
        raise UnsupportedSqlForm("tenant_upsert", dialect_name(dialect))
    reads = _tenant_tables_read(
        statement,
        dialect,
        column_keys=sorted(parameters[0]),
        for_executemany=len(parameters) > 1,
    )
    if reads:
        raise TenantUpsertError(
            f"the statement would read the tenant table {', '.join(sorted(reads))} "
            "(a column default, onupdate or type bind_expression of "
            f"{table.name} that selects from one): the cursor guard refuses a "
            "statement that reads another tenant table. Give the column a value in "
            "every row, or drop the expression"
        )


async def tenant_upsert(
    model: Any,
    rows: Sequence[Mapping[str, Any]],
    *,
    update: Sequence[str],
    conflict_on: Sequence[str] | None = None,
    session: AsyncSession,
) -> None:
    """Insert ``rows`` of ``model`` for the bound tenant, updating ``update`` of the
    row that already exists. See :func:`sandloom.sdk.data.tenant_upsert`."""
    mapper, table = _mapper_of(model)
    tenant = _tenant_of(mapper, table)
    tid = _plain_uuid(current_tenant_id())
    target = _conflict_target(mapper, table, tenant, conflict_on)
    refreshed = _refreshed_columns(mapper, table, tenant, update)
    parameters = [
        _plain_row(row, i, mapper, table, tenant) for i, row in enumerate(rows)
    ]
    if not parameters:
        return
    keys = {frozenset(row) for row in parameters}
    if len(keys) != 1:
        raise TenantUpsertError("every row must name the same columns")
    (named,) = keys
    missing = [
        column.name
        for column in (*refreshed, *(c for c in target if c.name != tenant.name))
        if column.key not in named
    ]
    if missing:
        raise TenantUpsertError(
            "every row must carry the conflict key and the updated columns "
            f"(missing {missing}): an absent column would be written as its default"
        )
    for row in parameters:
        row[tenant.key] = tid
    # the database the statement runs on is the mapper's, the one the connection
    # below resolves: its dialect builds the statement and answers the checks
    dialect = session.get_bind(mapper=mapper).dialect
    statement = canonical_tenant_upsert(dialect, table, target, refreshed)
    _refuse_what_the_guard_would(statement, dialect, table, parameters)
    # pending changes first: the upsert is ordered after them, as an ORM statement is
    await session.flush()
    connection = await session.connection(bind_arguments={"mapper": mapper})
    _seal_tenant_upsert(statement)
    try:
        await connection.execute(statement, parameters)
    finally:
        _unseal_tenant_upsert(statement)


__all__ = ["TenantUpsertError", "canonical_tenant_upsert", "tenant_upsert"]
