"""The compile half of :class:`SQLAlchemyExecutionAdapter`.

Turns a declared read operation (``ListOperation`` / ``JoinRead`` / ...) into
a :class:`~sandloom.core.planner.Plan` for this database: the general-executor
statement and, where the safety policy allows, the optimized single-statement
form. Plans are cached per operation and executor.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from sqlalchemy import (
    and_,
    bindparam,
    inspect,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import ColumnProperty

from sandloom.core.planner import (
    JoinRead,
    ListOperation,
    Plan,
    PlanningError,
    ReadOperation,
)
from sandloom.core.sql_alchemy.planner_adapter.forms import (
    _TENANT_PARAM,
    Checkpoint,
    _Compiled,
    _CompiledList,
    _fk_to,
    _general_list,
    _join_key_columns,
    _join_returns_one_row,
    _join_sides,
    _join_via,
    _list_filters,
    _list_getters,
    _list_order_index,
    _list_order_keys,
    _list_referenced_columns,
    _list_sides,
    _ListForm,
    _ListSides,
    _mapper,
    _plain_getters,
)
from sandloom.core.sql_alchemy.planner_safety import (
    covering_indexes,
    optimized_reason,
    policy_reason,
    unique_keys,
)
from sandloom.core.sql_alchemy.tenant_rls import is_tenant_rls_installed
from sandloom.core.tenant import TenantScoped


class _PlannerCompiler:
    """The compile half of :class:`SQLAlchemyExecutionAdapter` (module doc)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.plans: dict[tuple[int, bool], Plan[Any]] = {}
        #: TEST SEAM: awaited at every executor phase boundary when set.
        self.checkpoint: Checkpoint | None = None
        self._postgresql = engine.sync_engine.dialect.name == "postgresql"
        #: the server default isolation is READ COMMITTED (None = not known yet)
        self._rc_default: bool | None = None

    # ------------------------------------------------------------- planning
    def guard_plan(self, plan: Plan[Any]) -> Plan[Any]:
        """A policy installed after startup must also protect a cached plan."""
        if plan.executor != "optimized":
            return plan
        entities = [plan.operation.entity]
        joined = getattr(plan.operation, "join", None)
        if joined is not None:
            entities.append(joined)
        reason = policy_reason(self.engine, [inspect(entity) for entity in entities])
        if reason:
            logging.getLogger("sandloom.core.planner").warning(
                "%s: falling back to general executor: %s", plan.operation.name, reason
            )
            return replace(plan, executor="general", reason=reason)
        return plan

    def compile(self, op: ReadOperation[Any], *, optimized: bool) -> Plan[Any]:  # noqa: C901
        if isinstance(op, ListOperation):
            return self._compile_list(op, optimized=optimized)
        if isinstance(op, JoinRead):
            return self._compile_join(op, optimized=optimized)
        mapper = _mapper(op, op.entity)
        is_tenant = isinstance(op.entity, type) and issubclass(op.entity, TenantScoped)
        if op.tenant_scoped is not None and op.tenant_scoped != is_tenant:
            raise PlanningError(
                f"{op.name}: declared tenant_scoped={op.tenant_scoped} but "
                f"{op.entity.__name__} is {'' if is_tenant else 'not '}TenantScoped"
            )
        fields = tuple(getattr(op.result, "model_fields", {}))
        if not fields:
            raise PlanningError(
                f"{op.name}: result {op.result!r} is not a pydantic model"
            )
        columns = {p.key: p for p in mapper.attrs if isinstance(p, ColumnProperty)}
        for name in op.by:
            if name not in columns:
                raise PlanningError(
                    f"{op.name}: key {name!r} is not a column attribute"
                )
        for name in fields:
            if not hasattr(op.entity, name):
                raise PlanningError(
                    f"{op.name}: result field {name!r} has no attribute on "
                    f"{op.entity.__name__}"
                )
        pk_keys = tuple(
            mapper.get_property_by_column(c).key for c in mapper.primary_key
        )
        pk_get = op.by == pk_keys
        stmt = None
        if not pk_get:
            ent: Any = op.entity
            stmt = select(ent).where(
                and_(*[getattr(ent, k) == bindparam(k) for k in op.by])
            )
        rls = is_tenant_rls_installed(self.engine)
        sql, reason = self._optimized_sql(op, mapper, columns, fields, is_tenant)
        if not optimized:
            executor, reason = "general", "optimized executor disabled"
        elif sql is None:
            executor = "general"
        else:
            executor, reason = "optimized", "single-table equality read"
        return Plan(
            operation=op,
            executor=executor,  # type: ignore[arg-type]
            reason=reason,
            tenant_scoped=is_tenant,
            fields=fields,
            compiled=_Compiled(op.entity, pk_get, stmt, sql, rls),
        )

    # ------------------------------------------------------- ListOperation
    def _compile_list(self, op: ListOperation[Any], *, optimized: bool) -> Plan[Any]:
        """One declaration, validated top-down (first violation raises)."""
        sides = _list_sides(op)
        is_tenant = sides.scoped_e or sides.scoped_j
        if op.tenant_scoped is not None and op.tenant_scoped != is_tenant:
            raise PlanningError(
                f"{op.name}: declared tenant_scoped={op.tenant_scoped} but the "
                f"read is {'' if is_tenant else 'not '}tenant-scoped"
            )
        fields = tuple(getattr(op.result, "model_fields", {}))
        if not fields:
            raise PlanningError(
                f"{op.name}: result {op.result!r} is not a pydantic model"
            )
        opt = _list_filters(op, sides)
        onclause, on_sql = self._list_join(op, sides)
        order_keys = _list_order_keys(op, sides)
        index_note = self._list_index(op, sides.m_e, sides.cols_e, sides.scoped_e)
        getters = _list_getters(op, fields, sides)
        plain = _plain_getters(getters, sides)
        extra_keys, order_index = _list_order_index(
            getters, plain, order_keys, len(fields)
        )
        general, n_entities = _general_list(
            op, sides, opt, getters, plain, order_keys, extra_keys, onclause
        )
        stmt, stmt_after = general(0)
        sql, sql_after, reason = self._list_optimized(
            op, sides, opt, getters, plain, order_keys, extra_keys, on_sql, is_tenant
        )

        if not optimized:
            executor, reason = "general", "optimized executor disabled"
        elif sql is None:
            executor = "general"
        else:
            executor = "optimized"
        if index_note:
            reason = f"{reason}; {index_note}"
        sql_ok = sql is not None
        list_sql = self._list_sql
        m_e, m_j, cols_e, cols_j = sides.m_e, sides.m_j, sides.cols_e, sides.cols_j
        scoped_e, scoped_j = sides.scoped_e, sides.scoped_j

        def build(mask: int) -> _ListForm:
            """The forms of one combination of present optional filters."""
            g, g_after = general(mask)
            if not sql_ok:
                return _ListForm(g, g_after, None, None)
            present = [(c, p) for i, (_a, c, p) in enumerate(opt) if mask & (1 << i)]
            o, o_after = list_sql(
                op,
                m_e,
                m_j,
                cols_e,
                cols_j,
                getters,
                order_keys,
                extra_keys,
                on_sql,
                scoped_e,
                scoped_j,
                is_tenant,
                present,
            )
            return _ListForm(g, g_after, o, o_after)

        return Plan(
            operation=op,
            executor=executor,  # type: ignore[arg-type]
            reason=reason,
            tenant_scoped=is_tenant,
            fields=fields,
            compiled=_CompiledList(
                stmt=stmt,
                stmt_after=stmt_after,
                columns=plain,
                getters=tuple(getters),
                n_entities=n_entities,
                where_consts=tuple(op.where.values()),
                join_consts=tuple(op.join_where.values()),
                tenant_param=is_tenant,
                sql=sql,
                sql_after=sql_after,
                rls=is_tenant_rls_installed(self.engine),
                n_required=op.n_required,
                prefix_at=tuple(p for _a, _c, p in opt),
                names=op.keys,
                build=build,
            ),
            order_width=len(extra_keys),
            order_index=tuple(order_index),
        )

    def _list_join(self, op: ListOperation[Any], sides: _ListSides) -> tuple[Any, str]:
        """``(onclause, on_sql)``: a DECLARED foreign key, either direction."""
        onclause: Any = None
        on_sql = ""
        q = self.engine.sync_engine.dialect.identifier_preparer
        ent, jn, m_e, m_j = sides.ent, sides.jn, sides.m_e, sides.m_j
        if jn is None:
            return onclause, on_sql
        cols_j = sides.cols_j
        pk_e, pk_j = list(m_e.primary_key), list(m_j.primary_key)
        via_e: Any = sides.cols_e.get(op.via)
        via_j: Any = cols_j.get(op.via)
        if _fk_to(via_e, pk_j):
            key_j = m_j.get_property_by_column(pk_j[0]).key
            onclause = getattr(ent, op.via) == getattr(jn, key_j)
            on_sql = f"e.{q.quote(via_e.columns[0].name)} = j.{q.quote(pk_j[0].name)}"
        elif _fk_to(via_j, pk_e):
            key_e = m_e.get_property_by_column(pk_e[0]).key
            onclause = getattr(jn, op.via) == getattr(ent, key_e)
            on_sql = f"j.{q.quote(via_j.columns[0].name)} = e.{q.quote(pk_e[0].name)}"
            # one-to-many: at most ONE joined row per entity row
            pinned = {via_j.columns[0]}
            pinned |= {cols_j[c].columns[0] for c in op.join_by.values()}
            pinned |= {cols_j[c].columns[0] for c in op.join_where}
            uniques = unique_keys(m_j)
            if not any(u and u <= pinned for u in uniques):
                raise PlanningError(
                    f"{op.name}: {jn.__name__}.{op.via} + join_by/join_where "
                    "do not cover a unique key: the join could repeat rows"
                )
        else:
            raise PlanningError(
                f"{op.name}: via={op.via!r} is no declared ForeignKey between "
                f"{ent.__name__} and {jn.__name__} (no guessed joins)"
            )
        return onclause, on_sql

    def _list_optimized(
        self,
        op: ListOperation[Any],
        sides: _ListSides,
        opt: list[tuple[str, str, bool]],
        getters: list[tuple[int, str]],
        plain: bool,
        order_keys: list[str],
        extra_keys: list[str],
        on_sql: str,
        is_tenant: bool,
    ) -> tuple[Any, Any, str]:
        """``(sql, sql_after, reason)`` of the OPTIMIZED form (``None`` sql: none)."""
        m_e, m_j, cols_e, cols_j = sides.m_e, sides.m_j, sides.cols_e, sides.cols_j
        sql = sql_after = None
        reason = "keyset list, equality filters"
        dialect = self.engine.sync_engine.dialect
        if dialect.name != "postgresql" or dialect.driver != "asyncpg":
            reason = f"no optimized executor for {dialect.name}+{dialect.driver}"
        elif any(
            m is not None and (m.inherits is not None or len(m.tables) != 1)
            for m in (m_e, m_j)
        ):
            reason = "entity is not mapped to exactly one table"
        elif not plain:
            reason = "a result field is not a plain column attribute"
        elif any(
            getattr((cols_e if side == 0 else cols_j)[a].columns[0], "table", None)
            is not (m_e if side == 0 else m_j).local_table
            for side, a in getters
        ):
            reason = "a result field is an expression, not a column"
        else:
            safety = optimized_reason(
                self.engine,
                [m for m in (m_e, m_j) if m is not None],
                _list_referenced_columns(op, sides, opt, getters, order_keys),
                parameters=[
                    cols_e[k].columns[0]
                    for k in (*op.by, *(c for _a, c, _p in opt), *order_keys)
                ]
                + [cols_j[k].columns[0] for k in op.join_by.values()],
                constants=[(cols_e[k].columns[0], v) for k, v in op.where.items()]
                + [(cols_j[k].columns[0], v) for k, v in op.join_where.items()],
            )
            if safety:
                reason = safety
            else:
                sql, sql_after = self._list_sql(
                    op,
                    m_e,
                    m_j,
                    cols_e,
                    cols_j,
                    getters,
                    order_keys,
                    extra_keys,
                    on_sql,
                    sides.scoped_e,
                    sides.scoped_j,
                    is_tenant,
                )
                if opt:
                    reason = "keyset list, equality / optional / prefix filters"
        return sql, sql_after, reason

    def _list_sql(  # noqa: C901
        self,
        op: ListOperation[Any],
        m_e: Any,
        m_j: Any,
        cols_e: dict[str, Any],
        cols_j: dict[str, Any],
        getters: list[tuple[int, str]],
        order_keys: list[str],
        extra_keys: list[str],
        on_sql: str,
        scoped_e: bool,
        scoped_j: bool,
        is_tenant: bool,
        present: Sequence[tuple[str, bool]] = (),
    ) -> tuple[str, str]:
        """The generated SQL; ``present`` = the optional / prefix filters bound
        in this statement as ``(column, is prefix)``, in key order."""
        table = m_e.local_table
        jn = op.join
        q = self.engine.sync_engine.dialect.identifier_preparer

        def ref(alias: str, prop: Any) -> str:
            return f"{alias}.{q.quote(prop.columns[0].name)}"

        select_list = [
            ref("e" if side == 0 else "j", (cols_e if side == 0 else cols_j)[a])
            for side, a in getters
        ] + [ref("e", cols_e[k]) for k in extra_keys]
        n = 0
        conds = []
        for k in op.by:
            n += 1
            conds.append(f"{ref('e', cols_e[k])} = ${n}")
        for c in op.join_by.values():
            n += 1
            conds.append(f"{ref('j', cols_j[c])} = ${n}")
        like = "ILIKE" if op.prefix_ignore_case else "LIKE"
        for c, is_prefix in present:
            n += 1
            # a prefix argument is bound already escaped (``like_prefix``):
            # PostgreSQL's default LIKE escape character is the backslash
            conds.append(f"{ref('e', cols_e[c])} {like if is_prefix else '='} ${n}")
        for c in op.where:
            n += 1
            conds.append(f"{ref('e', cols_e[c])} = ${n}")
        for c in op.join_where:
            n += 1
            conds.append(f"{ref('j', cols_j[c])} = ${n}")
        if is_tenant:
            n += 1
            if scoped_e:
                conds.append(f"{ref('e', cols_e['tenant_id'])} = ${n}")
            if scoped_j:
                conds.append(f"{ref('j', cols_j['tenant_id'])} = ${n}")
        src = f"{q.format_table(table)} AS e"
        if jn is not None:
            src += f" JOIN {q.format_table(m_j.local_table)} AS j ON {on_sql}"
        order_refs = [ref("e", cols_e[k]) for k in order_keys]
        direction = "DESC" if op.descending else "ASC"
        order_sql = ", ".join(f"{r} {direction}" for r in order_refs)
        head = f"SELECT {', '.join(select_list)} FROM {src}"  # noqa: S608 - dialect-quoted identifiers, values bound

        def build(extra: list[str], limit_n: int) -> str:
            where = " AND ".join(conds + extra)
            return (
                f"{head}{' WHERE ' + where if where else ''} "
                f"ORDER BY {order_sql} LIMIT ${limit_n}"
            )

        sql = build([], n + 1)
        marks = [f"${n + 1 + i}" for i in range(len(order_keys))]
        op_sym = "<" if op.descending else ">"
        sql_after = build(
            [f"({', '.join(order_refs)}) {op_sym} ({', '.join(marks)})"],
            n + 1 + len(order_keys),
        )
        return sql, sql_after

    @staticmethod
    def _list_index(
        op: ListOperation[Any], m_e: Any, cols_e: dict[str, Any], scoped: bool
    ) -> str:
        """The covering-index rule of a ListOperation ("" when covered).

        Covered: a btree index (or the primary key / a unique constraint)
        whose leading columns are ALL the equality filter arguments (``by``,
        any order; constant filters and ``tenant_id`` may be interleaved),
        immediately followed by the declared ``order_by`` columns in order.
        All order columns share one direction, which a btree scans forwards
        or backwards — so ``descending`` is covered by the same index.
        """
        table = m_e.local_table
        eq = {cols_e[k].columns[0] for k in op.by}
        optional = {cols_e[c].columns[0] for c in op.where}
        if scoped and "tenant_id" in cols_e:
            optional.add(cols_e["tenant_id"].columns[0])
        order = [cols_e[k].columns[0] for k in op.order_by]
        candidates = covering_indexes(m_e)
        for cols in candidates:
            i, seen = 0, set()
            while i < len(cols) and cols[i] in eq | optional:
                seen.add(cols[i])
                i += 1
            if eq <= seen and cols[i : i + len(order)] == order:
                return ""
        wanted = ", ".join([*(c.name for c in eq), *(c.name for c in order)])
        if op.require_index:
            raise PlanningError(
                f"{op.name}: no index of {table.name} has the leading columns "
                f"({wanted}) — the equality filters then the order; a page would "
                "sort every matching row. Add the index, or declare "
                "require_index=False for a small bounded table"
            )
        note = f"WARNING no covering index ({wanted}): pages sort"
        logging.getLogger("sandloom.core.planner").warning("%s: %s", op.name, note)
        return note

    # ------------------------------------------------------------ JoinRead
    def _compile_join(self, op: JoinRead[Any], *, optimized: bool) -> Plan[Any]:
        sides = _join_sides(op)
        ent, jn, m_join = sides.ent, sides.jn, sides.m_j
        is_tenant = sides.scoped_e or sides.scoped_j
        if op.tenant_scoped is not None and op.tenant_scoped != is_tenant:
            raise PlanningError(
                f"{op.name}: declared tenant_scoped={op.tenant_scoped} but the "
                f"join is {'' if is_tenant else 'not '}tenant-scoped"
            )
        fields = tuple(getattr(op.result, "model_fields", {}))
        if not fields:
            raise PlanningError(
                f"{op.name}: result {op.result!r} is not a pydantic model"
            )
        _join_key_columns(op, sides)
        via, pk_j = _join_via(op, sides)
        _join_returns_one_row(op, sides, via)
        getters = _list_getters(op, fields, sides)
        plain = _plain_getters(getters, sides)
        # ---- GENERAL form: the ORM statement (explicit tenant predicates)
        preds: list[Any] = [getattr(ent, k) == bindparam(k) for k in op.by]
        preds += [getattr(jn, c) == bindparam(a) for a, c in op.join_by.items()]
        preds += [
            getattr(jn, c) == bindparam(f"_c{i}") for i, c in enumerate(op.join_where)
        ]
        if sides.scoped_e:
            preds.append(ent.tenant_id == bindparam(_TENANT_PARAM))
        if sides.scoped_j:
            preds.append(jn.tenant_id == bindparam(_TENANT_PARAM))
        onclause = getattr(ent, op.via) == getattr(
            jn, m_join.get_property_by_column(pk_j).key
        )
        if plain:
            sel: Any = select(
                *[getattr(ent if side == 0 else jn, attr) for side, attr in getters]
            )
        else:  # ORM fallback: load both entities, read the DTO attributes
            sel = select(ent, jn)
        stmt = sel.select_from(ent).join(jn, onclause).where(and_(*preds))
        consts = tuple(op.join_where.values())
        # ---- OPTIMIZED form: generated SQL, or why not
        sql, reason = self._join_sql(
            op,
            sides.m_e,
            m_join,
            sides.cols_e,
            sides.cols_j,
            getters,
            plain,
            sides.scoped_e,
            sides.scoped_j,
            pk_j,
        )
        if not optimized:
            executor, reason = "general", "optimized executor disabled"
        elif sql is None:
            executor = "general"
        else:
            executor, reason = "optimized", "FK join, equality keys"
        return Plan(
            operation=op,
            executor=executor,  # type: ignore[arg-type]
            reason=reason,
            tenant_scoped=is_tenant,
            fields=fields,
            compiled=_Compiled(
                ent,
                False,
                stmt,
                sql,
                is_tenant_rls_installed(self.engine),
                join=True,
                columns=plain,
                consts=consts,
                getters=tuple(getters),
                tenant_param=is_tenant,
            ),
        )

    def _join_sql(  # noqa: C901
        self,
        op: JoinRead[Any],
        m_ent: Any,
        m_join: Any,
        cols_e: dict[str, Any],
        cols_j: dict[str, Any],
        getters: list[tuple[int, str]],
        plain: bool,
        scoped_e: bool,
        scoped_j: bool,
        pk_j: Any,
    ) -> tuple[str | None, str]:
        dialect = self.engine.sync_engine.dialect
        if dialect.name != "postgresql" or dialect.driver != "asyncpg":
            return None, f"no optimized executor for {dialect.name}+{dialect.driver}"
        for m in (m_ent, m_join):
            if m.inherits is not None or len(m.tables) != 1:
                return None, "entity is not mapped to exactly one table"
        if not plain:
            return None, "a result field is not a plain column attribute"
        t_e, t_j = m_ent.local_table, m_join.local_table
        for side, attr in getters:
            col = (cols_e if side == 0 else cols_j)[attr].columns[0]
            if getattr(col, "table", None) is not (t_e if side == 0 else t_j):
                return None, f"{attr!r} is an expression, not a column"
        attrs_e = [*op.by, op.via, *(a for side, a in getters if side == 0)]
        attrs_j = [
            *op.join_by.values(),
            *op.join_where,
            *(a for side, a in getters if side == 1),
        ]
        columns = [pk_j]
        for mapper, cols, names in (
            (m_ent, cols_e, attrs_e),
            (m_join, cols_j, attrs_j),
        ):
            if issubclass(mapper.class_, TenantScoped):
                names.append("tenant_id")
            columns.extend(cols[name].columns[0] for name in names)
        safety = optimized_reason(
            self.engine,
            [m_ent, m_join],
            columns,
            parameters=[cols_e[k].columns[0] for k in op.by]
            + [cols_j[k].columns[0] for k in op.join_by.values()],
            constants=[(cols_j[k].columns[0], v) for k, v in op.join_where.items()],
        )
        if safety:
            return None, safety
        q = dialect.identifier_preparer

        def ref(alias: str, prop: Any) -> str:
            return f"{alias}.{q.quote(prop.columns[0].name)}"

        select_list = ", ".join(
            ref("e" if side == 0 else "j", (cols_e if side == 0 else cols_j)[attr])
            for side, attr in getters
        )
        n = 0
        preds = []
        for k in op.by:
            n += 1
            preds.append(f"{ref('e', cols_e[k])} = ${n}")
        for c in op.join_by.values():
            n += 1
            preds.append(f"{ref('j', cols_j[c])} = ${n}")
        for c in op.join_where:
            n += 1
            preds.append(f"{ref('j', cols_j[c])} = ${n}")
        if scoped_e or scoped_j:
            n += 1
            if scoped_e:
                preds.append(f"{ref('e', cols_e['tenant_id'])} = ${n}")
            if scoped_j:
                preds.append(f"{ref('j', cols_j['tenant_id'])} = ${n}")
        sql = (
            f"SELECT {select_list} FROM {q.format_table(t_e)} AS e "  # noqa: S608 - dialect-quoted identifiers, values bound
            f"JOIN {q.format_table(t_j)} AS j ON {ref('e', cols_e[op.via])} = "
            f"j.{q.quote(pk_j.name)} WHERE {' AND '.join(preds)}"
        )
        return sql, "FK join, equality keys"

    def _optimized_sql(
        self,
        op: ReadOperation[Any],
        mapper: Any,
        columns: dict[str, Any],
        fields: tuple[str, ...],
        is_tenant: bool,
    ) -> tuple[str | None, str]:
        """The generated statement, or ``(None, why not)``: never guessed."""
        dialect = self.engine.sync_engine.dialect
        if dialect.name != "postgresql" or dialect.driver != "asyncpg":
            return None, f"no optimized executor for {dialect.name}+{dialect.driver}"
        if mapper.inherits is not None or len(mapper.tables) != 1:
            return None, "entity is not mapped to exactly one table"
        table = mapper.local_table
        names = list(fields) + list(op.by) + (["tenant_id"] if is_tenant else [])
        cols: dict[str, Any] = {}
        for name in names:
            prop = columns.get(name)
            if prop is None or len(prop.columns) != 1:
                return None, f"{name!r} is not a plain column attribute"
            col = prop.columns[0]
            if getattr(col, "table", None) is not table:
                return None, f"{name!r} is an expression, not a column of {table.name}"
            cols[name] = col
        safety = optimized_reason(
            self.engine,
            [mapper],
            list(cols.values()),
            parameters=[cols[k] for k in op.by],
        )
        if safety:
            return None, safety
        bound = {cols[k] for k in op.by}
        if is_tenant:
            bound.add(cols["tenant_id"])
        if not any(key and key <= bound for key in unique_keys(mapper)):
            return None, "equality key is not unique; ORM cardinality check required"
        q = dialect.identifier_preparer
        target = q.format_table(table)
        select_list = ", ".join(q.quote(cols[f].name) for f in fields)
        preds = [f"{q.quote(cols[k].name)} = ${i}" for i, k in enumerate(op.by, 1)]
        if is_tenant:
            preds.append(f"{q.quote(cols['tenant_id'].name)} = ${len(op.by) + 1}")
        sql = f"SELECT {select_list} FROM {target} WHERE {' AND '.join(preds)}"  # noqa: S608 - dialect-quoted identifiers, values bound
        return sql, "single-table equality read"
