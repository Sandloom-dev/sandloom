# ruff: noqa: F811 - the shared ``s5`` fixture is imported and requested by name
"""Execution planner: ``JoinRead`` (entity JOIN parent ON a declared FK).

On the S5 environment (non-superuser role, FORCEd RLS on BOTH tables, pool of
one, counting proxy), for both planner modes:

* the plan carries the tenant predicate on EVERY tenant-scoped table of the
  join (and RLS guards both), never a tenant value;
* both executors return the same declared DTO; the optimized one is ONE
  generated statement;
* not plannable -> the ORM general form (``Plan.reason`` says why);
* declarations that would guess (no FK, non-unique key) fail at planning;
* no tenant -> no I/O; inside a Unit of Work the join reads its own writes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import BigInteger, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from test_s5_secure_read import (  # noqa: F401 - fixtures
    ROLE,
    S5,
    A,
    B,
    admin,
    pg_proxy,
    s5,
)

from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements
from sandloom.core.orm import Base, TenantScoped, session_reads
from sandloom.core.planner import JoinRead, PlanningError, execute_one, plan_for
from sandloom.core.config.settings import settings
from sandloom.core.tenant import MissingTenantError, bind_tenant
from sandloom.core.unit_of_work import db_session

pytestmark = pytest.mark.asyncio

BOTH = pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)


class JParent(TenantScoped, Base):
    __tablename__ = "bench_join_parents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class JChild(TenantScoped, Base):
    __tablename__ = "bench_join_children"
    __table_args__ = (UniqueConstraint("parent_id", "code"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    parent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bench_join_parents.id"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # NOT a FK: a join on it must be refused
    other_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class RoleOut(BaseModel):
    role: str
    parent_name: str


#: the get_role shape: child by (parent, code) in an ACTIVE parent
role_in_active = JoinRead(
    entity=JChild,
    join=JParent,
    via="parent_id",
    by=("parent_id", "code"),
    join_where={"active": True},
    joined_fields={"parent_name": "name"},
    result=RoleOut,
    name="bench.child.role_in_active_parent",
)

DDL = [
    "DROP TABLE IF EXISTS bench_join_children CASCADE",
    "DROP TABLE IF EXISTS bench_join_parents CASCADE",
    """CREATE TABLE bench_join_parents (
        id BIGINT PRIMARY KEY, tenant_id UUID NOT NULL,
        name VARCHAR(64) NOT NULL, active BOOLEAN NOT NULL)""",
    """CREATE TABLE bench_join_children (
        id BIGINT PRIMARY KEY, tenant_id UUID NOT NULL,
        parent_id BIGINT NOT NULL REFERENCES bench_join_parents(id),
        code VARCHAR(32) NOT NULL, role VARCHAR(16) NOT NULL, other_id BIGINT,
        UNIQUE (parent_id, code))""",
    *enable_tenant_rls_statements("bench_join_parents"),
    *enable_tenant_rls_statements("bench_join_children"),
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON bench_join_parents, "
    f"bench_join_children TO {ROLE}",
    f"""INSERT INTO bench_join_parents VALUES
        (1, '{A}', 'pa', true), (2, '{A}', 'pa-off', false), (3, '{B}', 'pb', true)""",
    # 13: tenant A's child pointing at tenant B's parent (inconsistent data):
    # the predicate on the JOINED table must hide it from A
    f"""INSERT INTO bench_join_children VALUES
        (10, '{A}', 1, 'x', 'admin', NULL), (11, '{A}', 2, 'x', 'member', NULL),
        (12, '{B}', 3, 'x', 'owner', NULL), (13, '{A}', 3, 'y', 'rogue', NULL)""",
]


@pytest_asyncio.fixture
async def jn(s5: S5) -> AsyncIterator[S5]:
    for sql in DDL:
        await s5.admin.execute(sql)
    yield s5


async def _get(sandloom_core: Any, tenant: Any, **key: Any) -> Any:
    core = sandloom_core.runtime_view()
    with bind_tenant(tenant):
        return await execute_one(core, role_in_active, key)


@BOTH
async def test_plan_has_a_tenant_predicate_on_every_table(jn: S5, sandloom_core: Any) -> None:
    plan = plan_for(sandloom_core.runtime_view(), role_in_active)
    want = "optimized" if jn.variant == "planopt" else "general"
    assert plan.executor == want, plan.reason
    assert plan.tenant_scoped and plan.fields == ("role", "parent_name")
    assert plan.compiled.sql == (
        "SELECT e.role, j.name FROM bench_join_children AS e "
        "JOIN bench_join_parents AS j ON e.parent_id = j.id "
        "WHERE e.parent_id = $1 AND e.code = $2 AND j.active = $3 "
        "AND e.tenant_id = $4 AND j.tenant_id = $4"
    )
    general = str(plan.compiled.stmt)
    assert "bench_join_children.tenant_id = :_sandloom_tenant" in general
    assert "bench_join_parents.tenant_id = :_sandloom_tenant" in general
    assert str(A) not in repr(plan) and str(B) not in repr(plan)


@BOTH
async def test_rows_by_tenant_activity_and_joined_tenant(jn: S5, sandloom_core: Any) -> None:
    assert await _get(sandloom_core, A, parent_id=1, code="x") == RoleOut(
        role="admin", parent_name="pa"
    )
    assert await _get(sandloom_core, A, parent_id=2, code="x") is None  # inactive parent
    assert await _get(sandloom_core, A, parent_id=3, code="x") is None  # B's child
    assert await _get(sandloom_core, B, parent_id=1, code="x") is None  # A's child
    # A's child, B's parent: hidden by the predicate/RLS on the JOINED table
    assert await _get(sandloom_core, A, parent_id=3, code="y") is None
    assert await _get(sandloom_core, B, parent_id=3, code="x") == RoleOut(
        role="owner", parent_name="pb"
    )


@BOTH
async def test_optimized_is_one_statement(jn: S5, sandloom_core: Any) -> None:
    await _get(sandloom_core, A, parent_id=1, code="x")  # warm
    jn.proxy.reset()
    await _get(sandloom_core, A, parent_id=1, code="x")
    kinds = jn.kinds()
    if jn.variant == "planopt":
        assert kinds == ["BEGIN+SET_CONFIG", "SELECT", "COMMIT"], kinds
    else:  # general: asyncpg BEGIN READ ONLY, GUC, the ORM join, ROLLBACK
        # (BEGIN and the GUC are one message under DB_RLS_MERGED_BEGIN)
        assert kinds[0] in ("BEGIN", "BEGIN+SET_CONFIG"), kinds
        assert "SELECT" in kinds and kinds[-1] == "ROLLBACK", kinds


@BOTH
async def test_both_executors_return_the_same_dto(
    jn: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = []
    for optimized in (False, True):
        monkeypatch.setattr(settings, "planner_optimized_executor", optimized)
        out.append(
            [
                await _get(sandloom_core, t, parent_id=p, code=c)
                for t, p, c in ((A, 1, "x"), (A, 2, "x"), (A, 3, "y"), (B, 3, "x"))
            ]
        )
    assert out[0] == out[1]


class _Computed(BaseModel):
    role: str
    shout: str


@BOTH
async def test_not_plannable_falls_back_to_the_orm(
    jn: S5, sandloom_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        JChild, "shout", property(lambda o: o.role.upper()), raising=False
    )
    op = JoinRead(
        entity=JChild,
        join=JParent,
        via="parent_id",
        by=("parent_id", "code"),
        result=_Computed,
    )
    plan = plan_for(sandloom_core.runtime_view(), op)
    assert plan.executor == "general"
    if jn.variant == "planopt":
        assert "not a plain column" in plan.reason
    with bind_tenant(A):
        got = await execute_one(sandloom_core.runtime_view(), op, {"parent_id": 1, "code": "x"})
    assert got == _Computed(role="admin", shout="ADMIN")


@BOTH
async def test_guessing_declarations_fail_at_planning(jn: S5, sandloom_core: Any) -> None:
    core = sandloom_core.runtime_view()
    with pytest.raises(PlanningError, match="no ForeignKey"):
        plan_for(
            core,
            JoinRead(entity=JChild, join=JParent, via="other_id", by=("id",), result=RoleOut),
        )
    with pytest.raises(PlanningError, match="unique key"):
        plan_for(
            core,
            JoinRead(
                entity=JChild,
                join=JParent,
                via="parent_id",
                by=("code",),
                joined_fields={"parent_name": "name"},
                result=RoleOut,
            ),
        )
    with pytest.raises(PlanningError, match="not a column of JParent"):
        plan_for(
            core,
            JoinRead(
                entity=JChild,
                join=JParent,
                via="parent_id",
                by=("id",),
                join_where={"nope": 1},
                joined_fields={"parent_name": "name"},
                result=RoleOut,
            ),
        )
    with pytest.raises(PlanningError, match="tenant-scoped"):
        plan_for(
            core,
            JoinRead(
                entity=JChild,
                join=JParent,
                via="parent_id",
                by=("id",),
                tenant_scoped=False,
                joined_fields={"parent_name": "name"},
                result=RoleOut,
            ),
        )


@BOTH
async def test_no_tenant_no_io(jn: S5, sandloom_core: Any) -> None:
    await _get(sandloom_core, A, parent_id=1, code="x")
    jn.proxy.reset()
    with pytest.raises(MissingTenantError):
        await execute_one(sandloom_core.runtime_view(), role_in_active, {"parent_id": 1, "code": "x"})
    assert jn.kinds() == []


@BOTH
async def test_join_inside_a_unit_of_work_reads_its_own_writes(jn: S5) -> None:
    class _Undo(Exception):
        pass

    with bind_tenant(A), pytest.raises(_Undo):
        async with db_session() as db:
            db.add(JChild(id=20, parent_id=1, code="z", role="new"))
            got = await session_reads(db).one(role_in_active, parent_id=1, code="z")
            assert got == RoleOut(role="new", parent_name="pa")
            raise _Undo  # the UoW rolls back
    assert await jn.admin.fetchval("SELECT count(*) FROM bench_join_children") == 4
