"""DML inside a CTE of a guarded statement, and ``unscoped()`` across tasks.

The tenant criteria the guard adds scope the SELECTs of a statement; they never
reach an INSERT/UPDATE/DELETE that rides along as a CTE. Under a bound tenant,
``insert(Other).add_cte(update(Item)...cte())`` would rewrite another tenant's
rows. Every shape of such a CTE is refused unless it sits inside ``unscoped()``
or provably touches no TenantScoped table; SELECT-only CTEs stay allowed and
scoped. A task created inside ``unscoped()`` does not keep the bypass once the
block exits.

Real PostgreSQL (data-modifying CTEs do not exist on SQLite); the refusals
happen before the statement reaches the database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Text, delete, insert, select, union, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core.di import (
    MissingTenantError,
    UnsafeTenantOperationError,
    bind_tenant,
    unscoped,
)
from sandloom.core.orm import TenantScoped
from sandloom.core.tenant_context import bypass_active
from sandloom.core.testing import reload_test_env


class _Base(DeclarativeBase):
    pass


class _Other(_Base):
    """NOT tenant-scoped."""

    __tablename__ = "test_cte_dml_others"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class _Item(TenantScoped, _Base):
    __tablename__ = "test_cte_dml_items"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


@dataclass
class _Env:
    maker: async_sessionmaker[AsyncSession]
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    a_id: uuid.UUID
    b_id: uuid.UUID
    other_id: uuid.UUID


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def env() -> AsyncGenerator[_Env, None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.fail("DATABASE_URL not set: the guard test needs a PostgreSQL URL")
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
        await conn.run_sync(_Base.metadata.create_all)
    state = _Env(
        maker=async_sessionmaker(engine, expire_on_commit=False),
        tenant_a=uuid.uuid4(),
        tenant_b=uuid.uuid4(),
        a_id=uuid.uuid4(),
        b_id=uuid.uuid4(),
        other_id=uuid.uuid4(),
    )
    async with state.maker() as session:
        with unscoped():
            session.add_all(
                [
                    _Item(id=state.a_id, tenant_id=state.tenant_a, name="a-row"),
                    _Item(id=state.b_id, tenant_id=state.tenant_b, name="b-row"),
                    _Other(id=state.other_id, name="other"),
                ]
            )
            await session.commit()
    try:
        yield state
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


async def _names(env: _Env) -> dict[uuid.UUID, str]:
    async with env.maker() as session:
        with unscoped():
            rows = (await session.execute(select(_Item.id, _Item.name))).all()
        return {row.id: row.name for row in rows}


def _tenant_update_cte(b_id: uuid.UUID) -> Any:
    return (
        update(_Item)
        .where(_Item.id == b_id)
        .values(name="pwned")
        .returning(_Item.id)
        .cte("hit")
    )


def _core_tenant_update_cte(b_id: uuid.UUID) -> Any:
    table = _Item.__table__
    return (
        update(table)
        .where(table.c.id == b_id)
        .values(name="pwned")
        .returning(table.c.id)
        .cte("hit")
    )


def _tenant_delete_cte(b_id: uuid.UUID) -> Any:
    return delete(_Item).where(_Item.id == b_id).returning(_Item.id).cte("hit")


def _tenant_insert_cte() -> Any:
    return (
        insert(_Item.__table__)
        .values(id=uuid.uuid4(), tenant_id=uuid.uuid4(), name="planted")
        .returning(_Item.__table__.c.id)
        .cte("hit")
    )


def _other_update_reading_tenant_cte(b_id: uuid.UUID) -> Any:
    """DML on a NON-tenant table whose subquery copies tenant data."""
    return (
        update(_Other)
        .values(name=select(_Item.name).where(_Item.id == b_id).scalar_subquery())
        .returning(_Other.id)
        .cte("hit")
    )


# name -> builder(env). Every shape carries DML on a TenantScoped table in a CTE.
_REFUSED: dict[str, Callable[[_Env], Any]] = {
    "insert.add_cte (ORM target)": lambda e: (
        insert(_Other)
        .values(id=uuid.uuid4(), name="x")
        .add_cte(_tenant_update_cte(e.b_id))
    ),
    "insert.add_cte (Core table target)": lambda e: (
        insert(_Other.__table__)
        .values(id=uuid.uuid4(), name="x")
        .add_cte(_core_tenant_update_cte(e.b_id))
    ),
    "insert.add_cte (delete CTE)": lambda e: (
        insert(_Other)
        .values(id=uuid.uuid4(), name="x")
        .add_cte(_tenant_delete_cte(e.b_id))
    ),
    "insert.add_cte (insert CTE)": lambda e: (
        insert(_Other).values(id=uuid.uuid4(), name="x").add_cte(_tenant_insert_cte())
    ),
    "select.add_cte": lambda e: select(_Other).add_cte(_tenant_update_cte(e.b_id)),
    "update.add_cte": lambda e: (
        update(_Other).values(name="y").add_cte(_tenant_delete_cte(e.b_id))
    ),
    "delete.add_cte": lambda e: (
        delete(_Other)
        .where(_Other.id == e.other_id)
        .add_cte(_tenant_update_cte(e.b_id))
    ),
    "select from the CTE": lambda e: select(_tenant_update_cte(e.b_id).c.id),
    "CTE in a WHERE subquery": lambda e: select(_Other).where(
        _Other.id.in_(select(_tenant_update_cte(e.b_id).c.id))
    ),
    "CTE inside a CTE": lambda e: select(_Other).add_cte(
        select(_tenant_update_cte(e.b_id).c.id).cte("outer")
    ),
    "CTE on a subquery": lambda e: select(_Other).where(
        _Other.id.in_(
            select(_Other.id).add_cte(_tenant_update_cte(e.b_id)).scalar_subquery()
        )
    ),
    "CTE on one branch of a union": lambda e: union(
        select(_Other.id).add_cte(_tenant_update_cte(e.b_id)),
        select(_Other.id),
    ),
    "DML CTE nested in a DML CTE": lambda e: select(_Other).add_cte(
        update(_Other)
        .values(name="z")
        .add_cte(_tenant_update_cte(e.b_id))
        .returning(_Other.id)
        .cte("wrap")
    ),
    "tenant model SELECT carrying a DML CTE": lambda e: select(_Item).add_cte(
        _tenant_update_cte(e.b_id)
    ),
    "insert from_select over the CTE": lambda e: insert(_Other).from_select(
        ["id"], select(_tenant_update_cte(e.b_id).c.id)
    ),
    "non-tenant DML CTE that reads a tenant table": lambda e: select(_Other).add_cte(
        _other_update_reading_tenant_cte(e.b_id)
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_REFUSED))
async def test_dml_cte_on_a_tenant_table_is_refused_under_a_bound_tenant(
    env: _Env, shape: str
) -> None:
    statement = _REFUSED[shape](env)
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            with pytest.raises(UnsafeTenantOperationError, match="CTE"):
                await session.execute(statement)
    assert await _names(env) == {env.a_id: "a-row", env.b_id: "b-row"}


@pytest.mark.asyncio
async def test_dml_cte_on_a_tenant_table_with_no_tenant_bound_fails_closed(
    env: _Env,
) -> None:
    statement = insert(_Other).values(id=uuid.uuid4(), name="x")
    statement = statement.add_cte(_tenant_update_cte(env.b_id))
    async with env.maker() as session:
        with pytest.raises(MissingTenantError):
            await session.execute(statement)
    assert await _names(env) == {env.a_id: "a-row", env.b_id: "b-row"}


@pytest.mark.asyncio
async def test_dml_cte_inside_unscoped_is_allowed(env: _Env) -> None:
    statement = insert(_Other).values(id=uuid.uuid4(), name="x")
    statement = statement.add_cte(_tenant_update_cte(env.b_id))
    async with env.maker() as session:
        with bind_tenant(env.tenant_a), unscoped(reason="system maintenance"):
            await session.execute(statement)
            await session.commit()
    assert (await _names(env))[env.b_id] == "pwned"


@pytest.mark.asyncio
async def test_dml_cte_on_a_non_tenant_table_stays_allowed(env: _Env) -> None:
    cte = (
        update(_Other)
        .where(_Other.id == env.other_id)
        .values(name="renamed")
        .returning(_Other.id)
        .cte("touch")
    )
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            await session.execute(
                insert(_Other).values(id=uuid.uuid4(), name="x").add_cte(cte)
            )
            await session.commit()
    async with env.maker() as session:
        row = (
            await session.execute(select(_Other.name).where(_Other.id == env.other_id))
        ).scalar_one()
    assert row == "renamed"


@pytest.mark.asyncio
async def test_a_select_only_cte_on_a_tenant_table_stays_allowed_and_scoped(
    env: _Env,
) -> None:
    visible = select(_Item.id, _Item.name).cte("visible")
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            rows = (await session.execute(select(visible.c.id))).scalars().all()
    assert rows == [env.a_id]
    # the same statement structure, with and without a DML CTE, keeps its own
    # (memoized) verdict: the cached allow never admits the DML form
    plain = select(_Other)
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            await session.execute(plain)
            with pytest.raises(UnsafeTenantOperationError, match="CTE"):
                await session.execute(
                    plain.add_cte(_tenant_update_cte(env.b_id)),
                )


@pytest.mark.asyncio
async def test_a_task_created_inside_unscoped_does_not_keep_the_bypass(
    env: _Env,
) -> None:
    started = asyncio.Event()
    exited = asyncio.Event()
    seen: dict[str, Any] = {}

    async def child() -> None:
        seen["inside"] = bypass_active()
        started.set()
        await exited.wait()
        seen["after"] = bypass_active()
        async with env.maker() as session:
            try:
                await session.execute(select(_Item))
            except MissingTenantError as error:
                seen["error"] = error

    with unscoped():
        task = asyncio.create_task(child())
        await started.wait()
    exited.set()
    await task
    assert seen["inside"] is True
    assert seen["after"] is False
    assert isinstance(seen["error"], MissingTenantError)


@pytest.mark.asyncio
async def test_a_task_created_and_finished_inside_unscoped_keeps_the_bypass(
    env: _Env,
) -> None:
    async def child() -> int:
        async with env.maker() as session:
            return len((await session.execute(select(_Item))).scalars().all())

    with unscoped():
        counts = await asyncio.gather(child(), child())
    assert counts == [2, 2]
    assert bypass_active() is False
