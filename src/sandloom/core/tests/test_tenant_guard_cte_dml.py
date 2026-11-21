"""DML inside a CTE of a guarded statement, and ``unscoped()`` across tasks.

The tenant criteria the guard adds scope the SELECTs of a statement; they never
reach an INSERT/UPDATE/DELETE that rides along as a CTE. Under a bound tenant,
``insert(Other).add_cte(update(Item)...cte())`` would rewrite another tenant's
rows. Which tables a CTE reaches cannot be told reliably, so every DML CTE is
refused outside ``unscoped()``, whatever its target; SELECT-only CTEs stay
allowed and scoped. A task created inside ``unscoped()`` does not keep the
bypass once the block exits.

Real PostgreSQL (data-modifying CTEs do not exist on SQLite); the refusals
happen before the statement reaches the database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    Column,
    MetaData,
    Table,
    Text,
    column,
    delete,
    func,
    insert,
    select,
    table,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    foreign,
    joinedload,
    mapped_column,
    query_expression,
    relationship,
    with_expression,
    with_loader_criteria,
)
from sqlalchemy.sql.elements import quoted_name

from sandloom.core.consumers.callbacks import build_consumer_callback
from sandloom.core.di import (
    MissingTenantError,
    UnsafeTenantOperationError,
    bind_tenant,
    unscoped,
)
from sandloom.core.orm import TenantScoped
from sandloom.core.sql_alchemy.write_admission import install_write_admission
from sandloom.core.tenant_context import _bypass, bypass_active
from sandloom.core.testing import reload_test_env


class _Base(DeclarativeBase):
    pass


class _Other(_Base):
    """NOT tenant-scoped."""

    __tablename__ = "test_cte_dml_others"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    expr: Mapped[uuid.UUID | None] = query_expression()
    items: Mapped[list[_Item]] = relationship(
        lambda: _Item,
        primaryjoin=lambda: foreign(_Item.id) == _Other.id,
        viewonly=True,
    )


class _Item(TenantScoped, _Base):
    __tablename__ = "test_cte_dml_items"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


@dataclass
class _Env:
    engine: AsyncEngine
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
    # the cursor hook every kernel-built engine carries (the drivers install it)
    install_write_admission(engine)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
        await conn.run_sync(_Base.metadata.create_all)
    state = _Env(
        engine=engine,
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
    tenant_table = _Item.__table__
    return (
        update(tenant_table)
        .where(tenant_table.c.id == b_id)
        .values(name="pwned")
        .returning(tenant_table.c.id)
        .cte("hit")
    )


def _lightweight_update_cte(b_id: uuid.UUID) -> Any:
    """The tenant table named through ``table()``: not the mapped Table object."""
    items = table("test_cte_dml_items", column("id"), column("name"))
    return (
        update(items)
        .where(items.c.id == b_id)
        .values(name="pwned")
        .returning(items.c.id)
        .cte("hit")
    )


def _second_table_update_cte(b_id: uuid.UUID) -> Any:
    """A second ``Table`` object (another MetaData) for the tenant table."""
    items = Table(
        "test_cte_dml_items",
        MetaData(),
        Column("id", PG_UUID(as_uuid=True)),
        Column("name", Text),
    )
    return (
        update(items)
        .where(items.c.id == b_id)
        .values(name="pwned")
        .returning(items.c.id)
        .cte("hit")
    )


def _name_returning_cte(b_id: uuid.UUID) -> Any:
    return (
        update(_Item)
        .where(_Item.id == b_id)
        .values(name="pwned")
        .returning(_Item.name)
        .cte("hit2")
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


def _quote_false_update_cte(items: Any, b_id: uuid.UUID) -> Any:
    return (
        update(items)
        .where(items.c.id == b_id)
        .values(name="pwned")
        .returning(items.c.id)
        .cte("hit")
    )


def _items_columns() -> tuple[Column[Any], Column[Any]]:
    return Column("id", PG_UUID(as_uuid=True)), Column("name", Text)


def _lightweight_items() -> Any:
    return table("test_cte_dml_items", column("id"), column("name"))


def _sink_copying_from_tenant_cte(b_id: uuid.UUID) -> Any:
    """DML on a NON-tenant table that reaches the tenant table only through
    the columns of its ``values()`` and ``where()`` (an implicit FROM)."""
    sink = table("test_cte_dml_others", column("id"), column("name"))
    items = _lightweight_items()
    return (
        update(sink)
        .values(name=items.c.name)
        .where(sink.c.id == items.c.id)
        .where(items.c.id == b_id)
        .returning(sink.c.id)
        .cte("hit")
    )


def _insert_with_default_reading_tenant_cte(b_id: uuid.UUID) -> Any:
    """INSERT on a NON-tenant table whose column default is a SQL expression
    reading the tenant table: the statement tree never names that table."""
    items = Table("test_cte_dml_items", MetaData(), *_items_columns())
    sink = Table(
        "test_cte_dml_others",
        MetaData(),
        Column("id", PG_UUID(as_uuid=True)),
        Column(
            "name",
            Text,
            default=select(items.c.name).where(items.c.id == b_id).scalar_subquery(),
        ),
    )
    return insert(sink).values(id=uuid.uuid4()).returning(sink.c.id).cte("hit")


# name -> builder(env). Every shape carries DML in a CTE; most on a TenantScoped
# table, the rest on a table that is not (refused all the same).
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
    # C1-C7: places the statement-tree walk never reached (red-team of #338)
    "C1 with_loader_criteria option": lambda e: select(_Other).options(
        with_loader_criteria(
            _Other,
            _Other.id.in_(select(_tenant_update_cte(e.b_id).c.id)),
        )
    ),
    "C2 with_expression option": lambda e: select(_Other).options(
        with_expression(
            _Other.expr,
            select(_tenant_update_cte(e.b_id).c.id).scalar_subquery(),
        )
    ),
    "C3 relationship and_() in a loader option": lambda e: select(_Other).options(
        joinedload(
            _Other.items.and_(_Item.id.in_(select(_tenant_update_cte(e.b_id).c.id)))
        )
    ),
    "C4 multi-row VALUES": lambda e: insert(_Other).values(
        [
            {
                "id": uuid.uuid4(),
                "name": select(_name_returning_cte(e.b_id).c.name).scalar_subquery(),
            }
        ]
    ),
    "C5 DML on a lightweight table()": lambda e: select(_Other).add_cte(
        _lightweight_update_cte(e.b_id)
    ),
    "C6 DML on a second Table object": lambda e: select(_Other).add_cte(
        _second_table_update_cte(e.b_id)
    ),
    "C7 a function statement": lambda e: func.coalesce(
        select(_tenant_update_cte(e.b_id).c.id).scalar_subquery()
    ),
    "aliased CTE": lambda e: select(_tenant_update_cte(e.b_id).alias("h").c.id),
    "non-tenant DML CTE reading a tenant table through multi-row VALUES": (
        lambda e: select(_Other).add_cte(
            insert(_Other)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "name": select(_Item.name)
                        .where(_Item.id == e.b_id)
                        .scalar_subquery(),
                    }
                ]
            )
            .returning(_Other.id)
            .cte("copy")
        )
    ),
    "DML CTE on a non-tenant table": lambda e: select(_Other).add_cte(
        update(_Other)
        .where(_Other.id == e.other_id)
        .values(name="renamed")
        .returning(_Other.id)
        .cte("touch")
    ),
    # names that match no (schema, name) pair yet render the tenant table
    "quote=False upper-case table() name": lambda e: select(_Other).add_cte(
        _quote_false_update_cte(
            table(
                quoted_name("TEST_CTE_DML_ITEMS", quote=False),
                column("id"),
                column("name"),
            ),
            e.b_id,
        )
    ),
    "quote=False Table object": lambda e: select(_Other).add_cte(
        _quote_false_update_cte(
            Table(
                "TEST_CTE_DML_ITEMS",
                MetaData(),
                *_items_columns(),
                quote=False,
            ),
            e.b_id,
        )
    ),
    "quote=False dotted table() name": lambda e: select(_Other).add_cte(
        _quote_false_update_cte(
            table(
                quoted_name("public.test_cte_dml_items", quote=False),
                column("id"),
                column("name"),
            ),
            e.b_id,
        )
    ),
    "non-tenant UPDATE CTE copying through values() and where() columns": (
        lambda e: select(_Other).add_cte(_sink_copying_from_tenant_cte(e.b_id))
    ),
    "non-tenant INSERT CTE with a SQL-expression column default": lambda e: select(
        _Other
    ).add_cte(_insert_with_default_reading_tenant_cte(e.b_id)),
}


_REFUSED_FIRST_BY_CORE_GUARD = {
    "insert.add_cte (Core table target)",
    "insert.add_cte (insert CTE)",
}

_REFUSED_FIRST_BY_FUNCTION_GUARD = {"C7 a function statement"}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_REFUSED))
async def test_a_dml_cte_is_refused_under_a_bound_tenant(env: _Env, shape: str) -> None:
    statement = _REFUSED[shape](env)
    # an INSERT whose own text names a tenant table is refused earlier by the
    # Core-statement guard, and a bare function statement by the guard on every
    # non-text executable; every other shape reaches the compiled-CTE check
    if shape in _REFUSED_FIRST_BY_CORE_GUARD:
        message = "Core statements"
    elif shape in _REFUSED_FIRST_BY_FUNCTION_GUARD:
        message = "reaches a TenantScoped table"
    else:
        message = "CTE"
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            with pytest.raises(UnsafeTenantOperationError, match=message):
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
async def test_dml_cte_on_a_non_tenant_table_is_refused(env: _Env) -> None:
    """No table matching: the target does not matter, only that it is DML."""
    cte = (
        update(_Other)
        .where(_Other.id == env.other_id)
        .values(name="renamed")
        .returning(_Other.id)
        .cte("touch")
    )
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            with pytest.raises(UnsafeTenantOperationError, match="CTE"):
                await session.execute(
                    insert(_Other).values(id=uuid.uuid4(), name="x").add_cte(cte)
                )
    async with env.maker() as session:
        row = (
            await session.execute(select(_Other.name).where(_Other.id == env.other_id))
        ).scalar_one()
    assert row == "other"


@pytest.mark.asyncio
async def test_a_schema_translate_map_cannot_hide_the_tenant_table(
    env: _Env,
) -> None:
    """The per-execution map renames the table in the SQL sent; the statement
    names a schema that matches nothing, the compiled CTE is still DML."""
    items = table("test_cte_dml_items", column("id"), column("name"), schema="hidden")
    cte = (
        update(items)
        .where(items.c.id == env.b_id)
        .values(name="pwned")
        .returning(items.c.id)
        .cte("hit")
    )
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            with pytest.raises(UnsafeTenantOperationError, match="CTE"):
                await session.execute(
                    select(_Other).add_cte(cte),
                    execution_options={"schema_translate_map": {"hidden": "public"}},
                )
    assert await _names(env) == {env.a_id: "a-row", env.b_id: "b-row"}


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


def _connection_statement(env: _Env) -> Any:
    """Core statements: nothing on this path reaches ``do_orm_execute``."""
    return (
        insert(_Other.__table__)
        .values(id=uuid.uuid4(), name="x")
        .add_cte(_second_table_update_cte(env.b_id))
    )


@pytest.mark.asyncio
async def test_session_connection_execute_refuses_a_dml_cte(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            connection = await session.connection()
            with pytest.raises(UnsafeTenantOperationError, match="CTE"):
                await connection.execute(_connection_statement(env))
    assert await _names(env) == {env.a_id: "a-row", env.b_id: "b-row"}


@pytest.mark.asyncio
async def test_engine_connection_execute_refuses_a_dml_cte(env: _Env) -> None:
    async with env.engine.connect() as connection:
        with bind_tenant(env.tenant_a):
            with pytest.raises(UnsafeTenantOperationError, match="CTE"):
                await connection.execute(_connection_statement(env))
    assert await _names(env) == {env.a_id: "a-row", env.b_id: "b-row"}


@pytest.mark.asyncio
async def test_connection_execute_with_no_tenant_bound_fails_closed(
    env: _Env,
) -> None:
    async with env.maker() as session:
        connection = await session.connection()
        with pytest.raises(MissingTenantError):
            await connection.execute(_connection_statement(env))
    assert await _names(env) == {env.a_id: "a-row", env.b_id: "b-row"}


@pytest.mark.asyncio
async def test_connection_execute_inside_unscoped_is_allowed(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.tenant_a), unscoped(reason="system maintenance"):
            connection = await session.connection()
            await connection.execute(_connection_statement(env))
            await session.commit()
    assert (await _names(env))[env.b_id] == "pwned"


@pytest.mark.asyncio
async def test_a_nested_dml_cte_never_runs_against_a_tenant_table(env: _Env) -> None:
    """A ``nesting=True`` CTE leaves ``compiled.ctes`` once rendered, so the
    compiled check cannot see it; PostgreSQL itself refuses a data-modifying
    statement below the top level, and that is what stops it here."""
    nested = (
        update(_Item)
        .where(_Item.id == env.b_id)
        .values(name="pwned")
        .returning(_Item.id)
        .cte("hit", nesting=True)
    )
    statement = select(_Other).where(_Other.id.in_(select(nested.c.id)))
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            with pytest.raises(DBAPIError, match="top level"):
                await session.execute(statement)
    assert await _names(env) == {env.a_id: "a-row", env.b_id: "b-row"}


@pytest.mark.asyncio
async def test_a_task_created_in_an_inner_block_keeps_the_bypass_while_outer_is_open() -> (
    None
):
    inner_closed = asyncio.Event()
    outer_closed = asyncio.Event()
    started = asyncio.Event()
    seen: dict[str, bool] = {}

    async def child() -> None:
        started.set()
        await inner_closed.wait()
        seen["outer_open"] = bypass_active()
        await outer_closed.wait()
        seen["all_closed"] = bypass_active()

    with unscoped():
        with unscoped():
            task = asyncio.create_task(child())
            await started.wait()
        inner_closed.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    outer_closed.set()
    await task
    assert seen == {"outer_open": True, "all_closed": False}


def test_a_stray_bool_in_the_bypass_variable_does_not_crash_the_guard() -> None:
    token = _bypass.set(True)  # type: ignore[arg-type]
    try:
        assert bypass_active() is False
    finally:
        _bypass.reset(token)


@pytest.mark.asyncio
async def test_a_consumer_delivery_runs_guarded_when_its_task_started_unscoped() -> (
    None
):
    seen: list[bool] = []

    @asynccontextmanager
    async def opener(_database: str) -> AsyncGenerator[Any, None]:
        yield None

    async def handler(_session: Any, _envelope: Any) -> None:
        seen.append(bypass_active())

    callback = build_consumer_callback(
        name="rt338-delivery",
        database="main",
        handler=handler,
        durable=False,
        opener=opener,
        metric_processed="unused_processed",
        metric_failed="unused_failed",
        labels={"consumer": "rt338-delivery"},
    )
    with unscoped():
        assert bypass_active() is True
        await callback({"id": "e1", "payload": {}})
        assert bypass_active() is True  # the delivery did not leak its reset
    assert seen == [False]
