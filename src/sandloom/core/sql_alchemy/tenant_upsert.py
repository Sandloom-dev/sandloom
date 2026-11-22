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
  column, so a row of another tenant is never the one that conflicts;
* ``DO UPDATE SET <column> = excluded.<column>`` for the listed columns, ``WHERE
  <tenant column> = <bound tenant>``, and nothing else: no caller expression;
* the statement runs on the unit's connection, marked for the cursor guard,
  which admits a marked statement only when it has exactly that shape and the
  bound tenant in every row (:func:`sandloom.core.tenant._tenant_upsert_admitted`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, Index, Table, UniqueConstraint, inspect
from sqlalchemy.orm import Mapper
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
    _seal_tenant_upsert,
    _tenant_column,
    _tenant_upsert_column,
    _tenant_upsert_where,
    _unseal_tenant_upsert,
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
    return mapper, table


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
    columns (no predicate, no expression)."""
    keys: list[tuple[Column[Any], ...]] = []
    if table.primary_key.columns:
        keys.append(tuple(table.primary_key.columns))
    keys.extend(
        tuple(constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    )
    for index in table.indexes:
        columns = tuple(index.expressions)
        if (
            index.unique
            and not _has_predicate(index)
            and all(isinstance(c, Column) and c.table is table for c in columns)
        ):
            keys.append(tuple(c for c in columns if isinstance(c, Column)))
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
                "key" + _candidates_hint(candidates, table)
            )
        wanted = {
            _column_of(mapper, table, key, "conflict_on").name for key in conflict_on
        } | {tenant.name}
        for key in candidates:
            if {column.name for column in key} == wanted:
                return key
        raise TenantUpsertError(
            f"conflict_on={list(conflict_on)!r} is not a unique key of {model} that "
            "includes the tenant column" + _candidates_hint(candidates, table)
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
            + _candidates_hint(candidates, table)
        )
    return candidates[0]


def _candidates_hint(candidates: list[tuple[Column[Any], ...]], table: Table) -> str:
    keys = "; ".join(", ".join(column.name for column in key) for key in candidates)
    return f" (candidates on {table.name}: {keys or 'none'})"


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
    tid = current_tenant_id()
    target = _conflict_target(mapper, table, tenant, conflict_on)
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
        for column in (
            *refreshed.values(),
            *(c for c in target if c.name != tenant.name),
        )
        if column.key not in named
    ]
    if missing:
        raise TenantUpsertError(
            "every row must carry the conflict key and the updated columns "
            f"(missing {missing}): an absent column would be written as its default"
        )
    for row in parameters:
        row[tenant.key] = tid
    statement = conflict_insert_for(session, table)
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=list(target),
        set_={column: excluded[column.key] for column in refreshed.values()},
        where=_tenant_upsert_where(tenant),
    )
    if _tenant_upsert_column(statement) is None:
        # a dialect renderer whose ON CONFLICT clause the cursor guard cannot read:
        # it would refuse the statement at the cursor, so refuse here, clearly
        raise UnsupportedSqlForm("tenant_upsert", dialect_name(session))
    # pending changes first: the upsert is ordered after them, as an ORM statement is
    await session.flush()
    connection = await session.connection(bind_arguments={"mapper": mapper})
    _seal_tenant_upsert(statement)
    try:
        await connection.execute(statement, parameters)
    finally:
        _unseal_tenant_upsert(statement)


__all__ = ["TenantUpsertError", "tenant_upsert"]
