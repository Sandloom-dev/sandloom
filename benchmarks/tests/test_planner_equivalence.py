# ruff: noqa: F811 - the shared ``s5`` / ``jn`` fixtures are requested by name
"""Switching executors never changes business semantics.

Runs EVERY declared operation alive in the test session
(``sandloom.core.planner.declared_operations()``: the bench module's, the tenancy
module's and the ones the planner suites declare) on the GENERAL and on the
OPTIMIZED executor, for keys taken from the operation's own table (own tenant,
another tenant, no tenant, a missing key), through both entry points
(``execute_one``: one transaction per call; ``read_scope``: the scope's read
unit), and compares:

* results (the declared DTO, or ``None``),
* errors (exception type and message),
* side effects: no outbox / audit row, nothing written to the entity table,
  the pooled connection clean afterwards (no transaction, no tenant GUC,
  nothing checked out).

Environment: the S5 one (FORCEd RLS, NOBYPASSRLS role, pool of one).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import inspect
from test_planner_executors import _clean
from test_planner_join import jn  # noqa: F401 - fixture (join tables + rows)
from test_s5_secure_read import (  # noqa: F401 - fixtures
    S5,
    A,
    B,
    admin,
    pg_proxy,
    s5,
)

from sandloom.core.planner import (
    ListOperation,
    PlanningError,
    ReadOperation,
    Reads,
    declared_operations,
    execute_one,
    plan_for,
    read_scope,
)
from sandloom.core.config.settings import settings
from sandloom.core.tenant import TenantScoped, bind_tenant

pytestmark = pytest.mark.asyncio

ORG_ACTIVE = uuid.UUID("0e000000-0000-4000-8000-0000000000a1")
ORG_OFF = uuid.UUID("0e000000-0000-4000-8000-0000000000a2")
USER = uuid.UUID("0e000000-0000-4000-8000-0000000000b1")


async def _seed_tenancy(s5: S5) -> None:
    """Tenancy rows for ``member_role_in_active_org`` (an active and an inactive
    organization, one member in each)."""
    await s5.admin.execute(
        "INSERT INTO tenancy_organizations (id, name, slug, owner_id, is_active) "
        "VALUES ($1, 'on', 'eq-on', $3, true), ($2, 'off', 'eq-off', $3, false) "
        "ON CONFLICT DO NOTHING",
        ORG_ACTIVE,
        ORG_OFF,
        USER,
    )
    await s5.admin.execute(
        "INSERT INTO tenancy_members (id, organization_id, user_id, role) VALUES "
        "(gen_random_uuid(), $1, $3, 'owner'), (gen_random_uuid(), $2, $3, 'admin') "
        "ON CONFLICT DO NOTHING",
        ORG_ACTIVE,
        ORG_OFF,
        USER,
    )


def _table(entity: Any) -> Any:
    return inspect(entity).local_table


def _col(entity: Any, attr: str) -> str:
    return inspect(entity).attrs[attr].columns[0].name


async def _keys(s5: S5, op: ReadOperation[Any]) -> list[tuple[dict[str, Any], Any]]:
    """(key, owning tenant or None) for up to 4 rows, plus a missing key."""
    ent = op.entity
    cols = [f"e.{_col(ent, k)}" for k in op.by]
    src = f"{_table(ent).name} AS e"
    tenant_expr = "e.tenant_id" if issubclass(ent, TenantScoped) else "NULL::uuid"
    jn_ = getattr(op, "join", None)
    if jn_ is not None:
        if op.via in inspect(ent).attrs:  # many-to-one: FK on the entity
            pk = inspect(jn_).primary_key[0].name
            on = f"e.{_col(ent, op.via)} = j.{pk}"
        else:  # one-to-many (ListOperation): FK on the joined entity
            pk = inspect(ent).primary_key[0].name
            on = f"j.{_col(jn_, op.via)} = e.{pk}"
        src += f" LEFT JOIN {_table(jn_).name} AS j ON {on}"
        cols += [f"j.{_col(jn_, c)}" for c in op.join_by.values()]
        if tenant_expr == "NULL::uuid" and issubclass(jn_, TenantScoped):
            tenant_expr = "j.tenant_id"
    exists = await s5.admin.fetchval("SELECT to_regclass($1)", _table(ent).name)
    if exists is None:
        return []
    if not cols:  # a list with no filter arguments: all rows of a tenant
        return [({}, A if tenant_expr != "NULL::uuid" else None)]
    rows = await s5.admin.fetch(
        f"SELECT {', '.join(cols)}, {tenant_expr} FROM {src} ORDER BY {cols[0]} LIMIT 4"
    )
    out = [(dict(zip(op.keys, r[:-1], strict=True)), r[-1]) for r in rows]
    if out:  # a key that matches nothing: the first key, first value replaced
        key, tenant = dict(out[0][0]), out[0][1]
        first = op.keys[0]
        value = key[first]
        key[first] = (
            uuid.uuid4()
            if isinstance(value, uuid.UUID)
            else (-1 if isinstance(value, int) else "no-such-key")
        )
        out.append((key, tenant))
    return out


async def _side_effects(s5: S5, op: ReadOperation[Any]) -> tuple[int, ...]:
    """Counts that a read must never change."""
    table = _table(op.entity).name
    counts = []
    for sql in (
        "SELECT count(*) FROM outbox_messages",
        "SELECT count(*) FROM audit_log",
        f"SELECT count(*) FROM {table}",
    ):
        counts.append(int(await s5.admin.fetchval(sql)))
    return tuple(counts)


async def _outcome(
    s5: S5,
    sandloom_core: Any,
    op: ReadOperation[Any],
    key: dict[str, Any],
    tenant: Any,
    *,
    optimized: bool,
    scoped: bool,
) -> tuple[str, Any]:
    settings.planner_optimized_executor = optimized
    core = sandloom_core.runtime_view()
    before = await _side_effects(s5, op)

    async def run() -> Any:
        if isinstance(op, ListOperation):  # the first pages, cursor-chained
            out: list[Any] = []
            async with read_scope(core, "equivalence") as reads:
                cursor = None
                for _ in range(3):
                    if not scoped:  # a fresh scope (transaction) per page
                        reads = Reads(core)
                    page = await reads.page(op, limit=5, cursor=cursor, **key)
                    out.append(page.items)
                    cursor = page.next_cursor
                    if cursor is None:
                        break
            return out
        if scoped:
            async with read_scope(core, "equivalence") as reads:
                return await reads.one(op, **key)
        return await execute_one(core, op, key)

    try:
        if tenant is None:
            result: Any = ("ok", await run())
        else:
            with bind_tenant(tenant):
                result = ("ok", await run())
    except Exception as exc:  # noqa: BLE001 - the error IS the outcome compared
        result = ("error", (type(exc).__name__, str(exc)))
    assert await _side_effects(s5, op) == before, f"{op.name}: a read changed data"
    await _clean(s5)  # no transaction, no GUC, nothing checked out
    return result


@pytest.mark.parametrize("s5", ["plan"], indirect=True)
async def test_every_declared_operation_is_executor_independent(
    s5: S5, jn: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "planner_optimized_executor", False)
    await _seed_tenancy(s5)
    core = sandloom_core.runtime_view()
    covered: dict[str, int] = {}
    for op in declared_operations():
        # an operation that cannot be planned is refused identically by both
        plans = []
        for optimized in (False, True):
            settings.planner_optimized_executor = optimized
            try:
                plans.append(plan_for(core, op).executor)
            except PlanningError as exc:
                plans.append(f"PlanningError: {exc}")
        if plans[0].startswith("PlanningError"):
            assert plans[0] == plans[1], op.name
            continue
        keys = await _keys(s5, op)
        if not keys:
            continue  # its table does not exist in this database
        cases = 0
        for key, owner in keys:
            tenants = {owner, A, B, None} if owner is not None else {None, A}
            for tenant in tenants:
                for scoped in (False, True):
                    general = await _outcome(
                        s5, sandloom_core, op, key, tenant, optimized=False, scoped=scoped
                    )
                    optimized = await _outcome(
                        s5, sandloom_core, op, key, tenant, optimized=True, scoped=scoped
                    )
                    assert general == optimized, (op.name, key, tenant, scoped)
                    cases += 1
        covered[op.name] = cases
    # the operations this suite must cover (the rest is whatever else is alive)
    for name in (
        "bench.tenant_item.get",
        "bench.child.role_in_active_parent",
        "tenancy.member.role_in_active_org",
        "bench.tenant_items.list",
        "tenancy.members.of_org",
        "tenancy.orgs.active_of_user",
    ):
        assert covered.get(name, 0) > 0, (name, covered)
