"""Compiled plan forms and the shape helpers the planner compiler uses.

The SQL each declared operation compiles to (:class:`_Compiled`,
:class:`_CompiledList`), the list / join side resolution, key and order
helpers, the general-executor list builder and the cached
``BEGIN READ ONLY`` statement with its tenant GUC.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import (
    Integer,
    UniqueConstraint,
    and_,
    bindparam,
    inspect,
    select,
    tuple_,
)
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.orm import ColumnProperty
from sqlalchemy.sql import sqltypes

from sandloom.core.planner import (
    JoinRead,
    ListOperation,
    PlanningError,
    ReadOperation,
    like_prefix,
)
from sandloom.core.sql_alchemy.planner_safety import (
    unique_keys,
)
from sandloom.core.sql_alchemy.rls import TENANT_SETTING
from sandloom.core.tenant import TenantScoped

Checkpoint = Callable[[str], Awaitable[None]]


class LeaseError(RuntimeError):
    """The connection lease found (or left) an unsafe transaction state."""


@dataclass(frozen=True)
class _Compiled:
    entity: type
    #: GENERAL: ``session.get`` when the key is exactly the primary key.
    pk_get: bool
    stmt: Any  # GENERAL: select(entity).where(key == :param ...) otherwise
    #: OPTIMIZED: generated SQL ($n parameters: key columns, then tenant).
    sql: str | None
    #: OPTIMIZED: the statement needs the RLS GUC (a transaction) first.
    rls: bool
    #: JoinRead only ---------------------------------------------------------
    join: bool = False
    #: GENERAL: the statement selects plain columns (else (entity, joined)).
    columns: bool = True
    #: GENERAL + OPTIMIZED: constant filter values, in parameter order.
    consts: tuple[Any, ...] = ()
    #: GENERAL (entity mode): (side, attribute) per DTO field; side 0/1.
    getters: tuple[tuple[int, str], ...] = ()
    #: the statement carries a tenant parameter (some side is TenantScoped).
    tenant_param: bool = False


@dataclass(frozen=True)
class _ListForm:
    """One combination of present optional / prefix filters of a list."""

    stmt: Any
    stmt_after: Any
    sql: str | None
    sql_after: str | None


@dataclass(frozen=True)
class _CompiledList:
    """A ListOperation's compiled forms (see ``_compile_list``)."""

    #: GENERAL: first page / page after a cursor (ORM statements)
    stmt: Any
    stmt_after: Any
    #: GENERAL: the statement selects plain columns (else entities)
    columns: bool
    #: GENERAL (entity mode): (side, attribute) per DTO field; side 0/1
    getters: tuple[tuple[int, str], ...]
    #: GENERAL (entity mode): number of entities selected before order values
    n_entities: int
    #: constant filter values (entity ``where``, then ``join_where``)
    where_consts: tuple[Any, ...]
    join_consts: tuple[Any, ...]
    tenant_param: bool
    #: OPTIMIZED: generated SQL (first page / after a cursor), or None
    sql: str | None
    sql_after: str | None
    rls: bool
    #: optional / prefix filters (``ListOperation.optional`` then ``.prefix``):
    #: the leading ``n_required`` keys are always bound; ``prefix_at[i]``:
    #: optional key i is a prefix filter (bound as a literal LIKE pattern)
    n_required: int = 0
    prefix_at: tuple[bool, ...] = ()
    #: every key name, in ``op.keys`` order (GENERAL parameter names)
    names: tuple[str, ...] = ()
    #: builds the forms of one combination of present filters (bitmask)
    build: Any = field(default=None, repr=False, compare=False)
    variants: dict[int, _ListForm] = field(
        default_factory=dict, repr=False, compare=False
    )

    def bind(
        self, key: Sequence[Any]
    ) -> tuple[_CompiledList | _ListForm, Sequence[Any], Sequence[str]]:
        """The forms for ``key``'s present filters, the bound key values (the
        always-applied ones, then each PRESENT optional / prefix one, prefixes
        escaped) and their parameter names."""
        if not self.prefix_at:
            return self, key, self.names
        n = self.n_required
        mask = 0
        values = list(key[:n])
        names = list(self.names[:n])
        for i, is_prefix in enumerate(self.prefix_at):
            value = key[n + i]
            if value is None:
                continue
            mask |= 1 << i
            values.append(like_prefix(value) if is_prefix else value)
            names.append(self.names[n + i])
        if not mask:
            return self, values, names
        form = self.variants.get(mask)
        if form is None:
            form = self.variants[mask] = self.build(mask)
        return form, values, names

    def sql_args(
        self,
        key: Sequence[Any],
        tenant: Any,
        after: Sequence[Any] | None,
        limit: int,
    ) -> tuple[Any, ...]:
        """Positional parameters of the generated SQL, in ``$n`` order."""
        out: list[Any] = [*key, *self.where_consts, *self.join_consts]
        if self.tenant_param:
            out.append(tenant)
        if after is not None:
            out.extend(after)
        out.append(limit)
        return tuple(out)


_TENANT_PARAM = "_sandloom_tenant"


def begin_read_only_sql(tenant: uuid.UUID | None) -> str:
    """``BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY`` + the tenant GUC as ONE
    simple-protocol message (the isolation is explicit: part of the contract).

    The simple protocol has no parameters: the tenant is a literal, but only
    ever ``str(uuid.UUID(...))`` (hex digits and dashes) or ``''``. An exact
    ``uuid.UUID`` (the bound tenant) is already canonical: its statement is
    built once and kept in a small bounded cache; anything else is re-parsed.
    """
    if type(tenant) is uuid.UUID:
        sql = _BEGIN_SQL.get(tenant)
        if sql is None:
            if len(_BEGIN_SQL) >= _BEGIN_SQL_CACHE_SIZE:
                _BEGIN_SQL.pop(next(iter(_BEGIN_SQL)), None)
            sql = _BEGIN_SQL[tenant] = _begin_sql(str(tenant))
        return sql
    return _begin_sql(str(uuid.UUID(str(tenant))) if tenant is not None else "")


def _begin_sql(value: str) -> str:
    return (
        "BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY; "
        f"SELECT set_config('{TENANT_SETTING}', '{value}', true)"
    )


#: Bound of :data:`_BEGIN_SQL` (tenants seen by this process; oldest dropped).
_BEGIN_SQL_CACHE_SIZE = 4096
#: exact ``uuid.UUID`` tenant -> its read-only BEGIN statement.
_BEGIN_SQL: dict[uuid.UUID, str] = {}


def _mapper(op: ReadOperation[Any], entity: Any) -> Any:
    try:
        return inspect(entity)
    except NoInspectionAvailable:
        raise PlanningError(f"{op.name}: {entity!r} is not a mapped entity") from None


def _columns(mapper: Any) -> dict[str, Any]:
    return {p.key: p for p in mapper.attrs if isinstance(p, ColumnProperty)}


@dataclass(frozen=True)
class _ListSides:
    """The entity (``e``) and the optional joined (``j``) side of a list read."""

    ent: Any
    jn: Any
    m_e: Any
    cols_e: dict[str, Any]
    scoped_e: bool
    m_j: Any
    cols_j: dict[str, Any]
    scoped_j: bool


def _list_sides(op: ListOperation[Any]) -> _ListSides:
    ent: Any = op.entity
    jn: Any = op.join
    m_e = _mapper(op, ent)
    cols_e = _columns(m_e)
    scoped_e = issubclass(ent, TenantScoped)
    m_j: Any = None
    cols_j: dict[str, Any] = {}
    scoped_j = False
    if jn is not None:
        m_j = _mapper(op, jn)
        cols_j = _columns(m_j)
        scoped_j = issubclass(jn, TenantScoped)
    return _ListSides(ent, jn, m_e, cols_e, scoped_e, m_j, cols_j, scoped_j)


def _single_column(
    op: ReadOperation[Any], cols: dict[str, Any], name: str, what: str
) -> Any:
    prop = cols.get(name)
    if prop is None or len(prop.columns) != 1:
        raise PlanningError(f"{op.name}: {what} {name!r} is not a column")
    return prop


def _list_filters(
    op: ListOperation[Any], sides: _ListSides
) -> list[tuple[str, str, bool]]:
    """Validate the filters; the optional ones as ``(argument, column, is prefix)``."""
    ent, jn, cols_e = sides.ent, sides.jn, sides.cols_e
    for name in (*op.by, *op.where, *op.optional.values()):
        _single_column(op, cols_e, name, f"{ent.__name__} filter")
    for name in op.prefix.values():
        prop = _single_column(op, cols_e, name, f"{ent.__name__} prefix filter")
        if not isinstance(prop.columns[0].type, sqltypes.String):
            raise PlanningError(
                f"{op.name}: prefix filter {name!r} is not a string column"
            )
    # optional (equality / prefix) filters: (argument, column, is prefix)
    opt: list[tuple[str, str, bool]] = [
        *((a, c, False) for a, c in op.optional.items()),
        *((a, c, True) for a, c in op.prefix.items()),
    ]
    for name in (*op.join_by.values(), *op.join_where):
        _single_column(op, sides.cols_j, name, f"{getattr(jn, '__name__', '?')} filter")
    if (op.join_by or op.join_where or op.joined_fields) and jn is None:
        raise PlanningError(f"{op.name}: join_* given without join=")
    return opt


def _fk_to(prop: Any, pk: list[Any]) -> bool:
    return (
        prop is not None
        and len(prop.columns) == 1
        and len(pk) == 1
        and any(fk.column is pk[0] for fk in prop.columns[0].foreign_keys)
    )


def _list_order_keys(op: ListOperation[Any], sides: _ListSides) -> list[str]:
    """Ordering: declared, indexed NOT NULL columns + the primary key."""
    m_e = sides.m_e
    table = m_e.local_table
    indexed: set[Any] = set(m_e.primary_key)
    for cons in table.constraints:
        if isinstance(cons, UniqueConstraint):
            indexed |= set(cons.columns)
    for ix in table.indexes:
        indexed |= set(ix.columns)
    order_keys: list[str] = []
    for name in op.order_by:
        prop = _single_column(op, sides.cols_e, name, "order_by")
        if prop.columns[0].nullable:
            raise PlanningError(
                f"{op.name}: order_by {name!r} is nullable; keyset ordering "
                "requires NOT NULL columns"
            )
        if prop.columns[0] not in indexed:
            raise PlanningError(
                f"{op.name}: order_by {name!r} is not part of an index of "
                f"{table.name} (a keyset page must not sort the table)"
            )
        order_keys.append(name)
    for col in m_e.primary_key:
        pk_key = m_e.get_property_by_column(col).key
        if pk_key not in order_keys:
            order_keys.append(pk_key)
    return order_keys


def _list_getters(
    op: ListOperation[Any] | JoinRead[Any], fields: tuple[str, ...], sides: _ListSides
) -> list[tuple[int, str]]:
    """DTO fields as ``(side, attribute)`` (side 0: entity, 1: joined)."""
    ent, jn = sides.ent, sides.jn
    getters: list[tuple[int, str]] = []
    for f in fields:
        if f in op.joined_fields:
            attr = op.joined_fields[f]
            if not hasattr(jn, attr):
                raise PlanningError(f"{op.name}: {jn.__name__} has no {attr!r}")
            getters.append((1, attr))
        else:
            if not hasattr(ent, f):
                raise PlanningError(
                    f"{op.name}: result field {f!r} has no attribute on {ent.__name__}"
                )
            getters.append((0, f))
    return getters


def _list_order_index(
    getters: list[tuple[int, str]], plain: bool, order_keys: list[str], tail: int
) -> tuple[list[str], list[int]]:
    """``(extra_keys, order_index)`` of the order values.

    An order column the DTO already projects (a plain column of the entity) is
    read ONCE, at its DTO position; only the others are appended after the
    DTO's fields (``Plan.order_index``). Both executors select the same row
    shape, so a plan's cursor position is the same values whichever executor
    serves the page.
    """
    projected = (
        {attr: i for i, (side, attr) in enumerate(getters) if side == 0}
        if plain
        else {}
    )
    extra_keys = [k for k in order_keys if k not in projected]
    order_index: list[int] = []
    for k in order_keys:
        if k in projected:
            order_index.append(projected[k])
        else:
            order_index.append(tail)
            tail += 1
    return extra_keys, order_index


def _plain_getters(getters: list[tuple[int, str]], sides: _ListSides) -> bool:
    """Every DTO field is a plain one-column attribute."""
    cols_e, cols_j = sides.cols_e, sides.cols_j
    return all(
        (cols_e if side == 0 else cols_j).get(attr) is not None
        and len((cols_e if side == 0 else cols_j)[attr].columns) == 1
        for side, attr in getters
    )


def _join_sides(op: JoinRead[Any]) -> _ListSides:
    ent: Any = op.entity
    jn: Any = op.join
    m_ent, m_join = _mapper(op, ent), _mapper(op, jn)
    cols_e = _columns(m_ent)
    cols_j = _columns(m_join)
    scoped_e = issubclass(ent, TenantScoped)
    scoped_j = issubclass(jn, TenantScoped)
    return _ListSides(ent, jn, m_ent, cols_e, scoped_e, m_join, cols_j, scoped_j)


def _join_key_columns(op: JoinRead[Any], sides: _ListSides) -> None:
    for name in op.by:
        if name not in sides.cols_e:
            raise PlanningError(
                f"{op.name}: key {name!r} is not a column of {sides.ent.__name__}"
            )
    for name in (*op.join_by.values(), *op.join_where):
        if name not in sides.cols_j:
            raise PlanningError(
                f"{op.name}: {name!r} is not a column of {sides.jn.__name__}"
            )


def _join_via(op: JoinRead[Any], sides: _ListSides) -> tuple[Any, Any]:
    """``(via, pk_j)``: a DECLARED foreign key to the joined primary key."""
    ent, jn, m_join = sides.ent, sides.jn, sides.m_j
    via = sides.cols_e.get(op.via)
    if via is None or len(via.columns) != 1:
        raise PlanningError(f"{op.name}: via={op.via!r} is not a column attribute")
    if len(m_join.primary_key) != 1:
        raise PlanningError(f"{op.name}: {jn.__name__} needs a one-column primary key")
    pk_j = m_join.primary_key[0]
    if not any(fk.column is pk_j for fk in via.columns[0].foreign_keys):
        raise PlanningError(
            f"{op.name}: {ent.__name__}.{op.via} has no ForeignKey to "
            f"{jn.__name__}'s primary key (JoinRead never guesses a join)"
        )
    return via, pk_j


def _join_returns_one_row(op: JoinRead[Any], sides: _ListSides, via: Any) -> None:
    """At most ONE row on every executor.

    ``by`` covers a PK / unique key of the entity — ``via`` included when the
    joined side is itself pinned to at most one row (join_by + join_where cover
    a unique key of ``join``): then ``via`` equals that one row's primary key,
    a fixed value.
    """
    cols_e, cols_j = sides.cols_e, sides.cols_j
    by_cols = {cols_e[k].columns[0] for k in op.by}
    pinned_j = {cols_j[c].columns[0] for c in op.join_by.values()}
    pinned_j |= {cols_j[c].columns[0] for c in op.join_where}
    if any(u and u <= pinned_j for u in unique_keys(sides.m_j)):
        by_cols.add(via.columns[0])
    uniques = unique_keys(sides.m_e)
    if not any(u and u <= by_cols for u in uniques):
        raise PlanningError(
            f"{op.name}: by={op.by} is not a primary or unique key of "
            f"{sides.ent.__name__} (a JoinRead returns at most one row)"
        )


@dataclass(frozen=True)
class _GeneralList:
    """The GENERAL (SQLAlchemy) form of a list read, per optional-filter mask."""

    op: ListOperation[Any]
    sides: _ListSides
    opt: list[tuple[str, str, bool]]
    sel: Any
    cmp: Any
    ordering: list[Any]
    limit_param: Any

    def __call__(self, mask: int) -> tuple[Any, Any]:
        op, ent, jn = self.op, self.sides.ent, self.sides.jn
        preds: list[Any] = [getattr(ent, k) == bindparam(k) for k in op.by]
        preds += [
            getattr(ent, c) == bindparam(f"_w{i}") for i, c in enumerate(op.where)
        ]
        preds += [getattr(jn, c) == bindparam(a) for a, c in op.join_by.items()]
        preds += [
            getattr(jn, c) == bindparam(f"_c{i}") for i, c in enumerate(op.join_where)
        ]
        for i, (arg, col, is_prefix) in enumerate(self.opt):
            if mask & (1 << i):
                attr = getattr(ent, col)
                if not is_prefix:
                    preds.append(attr == bindparam(arg))
                elif op.prefix_ignore_case:
                    preds.append(attr.ilike(bindparam(arg)))
                else:
                    preds.append(attr.like(bindparam(arg)))
        if self.sides.scoped_e:
            preds.append(ent.tenant_id == bindparam(_TENANT_PARAM))
        if self.sides.scoped_j:
            preds.append(jn.tenant_id == bindparam(_TENANT_PARAM))
        base = self.sel.where(and_(*preds)) if preds else self.sel
        return (
            base.order_by(*self.ordering).limit(self.limit_param),
            base.where(self.cmp).order_by(*self.ordering).limit(self.limit_param),
        )


def _general_list(
    op: ListOperation[Any],
    sides: _ListSides,
    opt: list[tuple[str, str, bool]],
    getters: list[tuple[int, str]],
    plain: bool,
    order_keys: list[str],
    extra_keys: list[str],
    onclause: Any,
) -> tuple[_GeneralList, int]:
    """The GENERAL form builder and the number of ORM entities it loads."""
    ent, jn, cols_e = sides.ent, sides.jn, sides.cols_e
    order_cols = [getattr(ent, k) for k in order_keys]
    if plain:
        picked = [getattr(ent if side == 0 else jn, a) for side, a in getters]
        n_entities = 0
    else:  # ORM fallback: load the entities, read the DTO attributes
        picked = [ent] + ([jn] if jn is not None else [])
        n_entities = len(picked)
    sel = select(*picked, *[getattr(ent, k) for k in extra_keys]).select_from(ent)
    if jn is not None:
        sel = sel.join(jn, onclause)
    after_params = [
        bindparam(f"_k{i}", type_=cols_e[k].columns[0].type)
        for i, k in enumerate(order_keys)
    ]
    cmp = (
        tuple_(*order_cols) < tuple_(*after_params)
        if op.descending
        else tuple_(*order_cols) > tuple_(*after_params)
    )
    ordering = [c.desc() if op.descending else c.asc() for c in order_cols]
    limit_param = bindparam("_limit", type_=Integer())
    return _GeneralList(op, sides, opt, sel, cmp, ordering, limit_param), n_entities


def _list_referenced_columns(
    op: ListOperation[Any],
    sides: _ListSides,
    opt: list[tuple[str, str, bool]],
    getters: list[tuple[int, str]],
    order_keys: list[str],
) -> list[Any]:
    """Every column the optimized SQL reads (entity side, then joined side)."""
    cols_e, cols_j = sides.cols_e, sides.cols_j
    referenced = [*op.by, *op.where, *(c for _a, c, _p in opt), *order_keys]
    referenced_j = [*op.join_by.values(), *op.join_where]
    for side, attr in getters:
        (referenced if side == 0 else referenced_j).append(attr)
    for names, cols in ((referenced, cols_e), (referenced_j, cols_j)):
        if "tenant_id" in cols:
            names.append("tenant_id")
        if op.via in cols:
            names.append(op.via)
    return [
        cols[name].columns[0]
        for names, cols in ((referenced, cols_e), (referenced_j, cols_j))
        for name in names
    ]
