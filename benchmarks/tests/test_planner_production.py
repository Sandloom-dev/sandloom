# ruff: noqa: F811 - fixtures are imported and requested by name
"""Public planner semantics with codecs, late policies and ambiguous reads."""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest
from pydantic import BaseModel
from sqlalchemy import Index, String, event, text
from sqlalchemy.exc import DBAPIError, InvalidRequestError, MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.types import TypeDecorator
from test_s5_secure_read import S5, A, admin, pg_proxy, s5  # noqa: F401

from sandloom.core.planner import (
    ExecutionContext,
    ListOperation,
    ReadOperation,
    Reads,
    execute_one,
    plan_for,
)
from sandloom.core.sql_alchemy.planner_adapter import SQLAlchemyExecutionAdapter
from sandloom.core.sql_alchemy.planner_adapter.units import _Lease
from sandloom.core.tenant import bind_tenant
from benchsuite.bench.api import ItemOut
from benchsuite.bench.operations import get_tenant_item
from benchsuite.bench.tenant_models import BenchTenantItem

pytestmark = pytest.mark.asyncio
BOTH = pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
OPT = pytest.mark.parametrize("s5", ["planopt"], indirect=True)


class _Base(DeclarativeBase):
    pass


class _Codec(TypeDecorator[str]):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        return None if value is None else "encoded:" + value

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        return None if value is None else value.removeprefix("encoded:")


class _Typed(_Base):
    __tablename__ = "planner_production_typed"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(_Codec())


class _Out(BaseModel):
    id: int
    code: str


class _Flag(_Base):
    __tablename__ = "planner_production_flag"
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool]
    __table_args__ = (Index("ix_planner_flag_enabled_id", "enabled", "id"),)


class _FlagOut(BaseModel):
    id: int
    enabled: bool


@BOTH
async def test_boolean_integer_inputs_keep_orm_coercion(s5: S5, sandloom_core: Any):
    await s5.admin.execute(
        "CREATE TABLE planner_production_flag (id integer PRIMARY KEY, enabled boolean NOT NULL);"
        "CREATE INDEX ON planner_production_flag (enabled,id);"
        "INSERT INTO planner_production_flag VALUES (1,true),(2,false);"
        "GRANT SELECT ON planner_production_flag TO " + s5.engine.url.username
    )
    try:
        core = sandloom_core.runtime_view()
        op = ReadOperation(_Flag, _FlagOut, by=("id", "enabled"))
        for number, enabled in ((1, 1), (2, 0)):
            assert await execute_one(core, op, {"id": number, "enabled": enabled}) == (
                _FlagOut(id=number, enabled=bool(enabled))
            )
        listing = ListOperation(_Flag, _FlagOut, by=("enabled",), order_by=("id",))
        assert (await Reads(core).page(listing, enabled=1, limit=10)).items == [
            _FlagOut(id=1, enabled=True)
        ]
        constant = ListOperation(
            _Flag, _FlagOut, where={"enabled": 1}, order_by=("id",)
        )
        assert (await Reads(core).page(constant, limit=10)).items == [
            _FlagOut(id=1, enabled=True)
        ]
    finally:
        await s5.admin.execute("DROP TABLE planner_production_flag")


class _Deferred(_Base):
    __tablename__ = "planner_production_deferred"
    id: Mapped[int] = mapped_column(primary_key=True)
    secret: Mapped[str] = mapped_column(deferred=True, deferred_raiseload=True)


class _SecretOut(BaseModel):
    id: int
    secret: str


@BOTH
async def test_deferred_raiseload_is_preserved(s5: S5, sandloom_core: Any):
    await s5.admin.execute(
        "CREATE TABLE planner_production_deferred (id integer PRIMARY KEY, secret varchar NOT NULL);"
        "INSERT INTO planner_production_deferred VALUES (1,'hidden');"
        "GRANT SELECT ON planner_production_deferred TO " + s5.engine.url.username
    )
    try:
        with pytest.raises(InvalidRequestError, match="raiseload"):
            await execute_one(
                sandloom_core.runtime_view(), ReadOperation(_Deferred, _SecretOut), {"id": 1}
            )
    finally:
        await s5.admin.execute("DROP TABLE planner_production_deferred")


class _Polymorphic(_Base):
    __tablename__ = "planner_production_polymorphic"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "root"}


class _IdOut(BaseModel):
    id: int


@BOTH
async def test_root_discriminator_is_preserved_without_subclasses(
    s5: S5, sandloom_core: Any
):
    await s5.admin.execute(
        "CREATE TABLE planner_production_polymorphic (id integer PRIMARY KEY, kind varchar NOT NULL);"
        "INSERT INTO planner_production_polymorphic VALUES (1,'unknown');"
        "GRANT SELECT ON planner_production_polymorphic TO " + s5.engine.url.username
    )
    try:
        with pytest.raises(AssertionError, match="polymorphic_identity"):
            await execute_one(
                sandloom_core.runtime_view(), ReadOperation(_Polymorphic, _IdOut), {"id": 1}
            )
    finally:
        await s5.admin.execute("DROP TABLE planner_production_polymorphic")


@BOTH
async def test_bind_and_result_codecs_are_preserved(s5: S5, sandloom_core: Any):
    await s5.admin.execute(
        "CREATE TABLE planner_production_typed (id integer PRIMARY KEY, code varchar NOT NULL);"
        "INSERT INTO planner_production_typed VALUES (1,'encoded:abc');"
        "GRANT SELECT ON planner_production_typed TO " + s5.engine.url.username
    )
    try:
        core = sandloom_core.runtime_view()
        by_id = ReadOperation(_Typed, _Out)
        by_code = ReadOperation(_Typed, _Out, by=("id", "code"))
        listing = ListOperation(_Typed, _Out, where={"code": "abc"}, order_by=("id",))
        assert await execute_one(core, by_id, {"id": 1}) == _Out(id=1, code="abc")
        assert await execute_one(core, by_code, {"id": 1, "code": "abc"}) == _Out(
            id=1, code="abc"
        )
        page = await Reads(core).page(listing, limit=10)
        assert page.items == [_Out(id=1, code="abc")]
        assert plan_for(core, by_id).executor == "general"
    finally:
        await s5.admin.execute("DROP TABLE planner_production_typed")


@BOTH
async def test_duplicate_matches_raise_on_both_executors(s5: S5, sandloom_core: Any):
    await s5.admin.execute("UPDATE bench_tenant_items SET name='titem-1' WHERE id=2")
    try:
        op = ReadOperation(BenchTenantItem, ItemOut, by=("name",))
        with bind_tenant(A), pytest.raises(MultipleResultsFound):
            await execute_one(sandloom_core.runtime_view(), op, {"name": "titem-1"})
    finally:
        await s5.admin.execute(
            "UPDATE bench_tenant_items SET name='titem-2' WHERE id=2"
        )


@OPT
@pytest.mark.parametrize("where", ["session", "engine"])
async def test_late_row_filter_also_protects_a_prepared_plan(
    s5: S5, sandloom_core: Any, where
):
    core = sandloom_core.runtime_view()
    assert plan_for(core, get_tenant_item).executor == "optimized"

    def orm_filter(state):
        if state.is_select and any(
            m.class_ is BenchTenantItem for m in state.all_mappers
        ):
            state.statement = state.statement.where(BenchTenantItem.id != 1)

    def sql_filter(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "bench_tenant_items" in statement:
            statement = statement.replace("WHERE ", "WHERE false AND ", 1)
        return statement, parameters

    target, name, fn = (
        (Session, "do_orm_execute", orm_filter)
        if where == "session"
        else (s5.engine.sync_engine, "before_cursor_execute", sql_filter)
    )
    event.listen(target, name, fn, **({"retval": True} if where == "engine" else {}))
    try:
        with bind_tenant(A):
            assert await execute_one(core, get_tenant_item, {"id": 1}) is None
        assert plan_for(core, get_tenant_item).executor == "general"
    finally:
        event.remove(target, name, fn)


@OPT
async def test_mapper_load_processing_is_preserved(s5: S5, sandloom_core: Any):
    def loaded(target, context):
        target.name = "load-hook"

    event.listen(BenchTenantItem, "load", loaded)
    try:
        with bind_tenant(A):
            item = await execute_one(
                sandloom_core.runtime_view(), get_tenant_item, {"id": 1}
            )
        assert item.name == "load-hook"
    finally:
        event.remove(BenchTenantItem, "load", loaded)


class _View(_Base):
    __tablename__ = "planner_production_view"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)


class _ViewOut(BaseModel):
    id: int
    name: str


@BOTH
async def test_read_only_is_enforced_even_without_tenant_rls(s5: S5):
    await s5.admin.execute(
        "CREATE TABLE planner_production_effect (value integer);"
        "CREATE FUNCTION planner_production_value() RETURNS text LANGUAGE plpgsql AS $$ "
        "BEGIN INSERT INTO planner_production_effect VALUES (1); RETURN 'value'; END $$;"
        "CREATE VIEW planner_production_view AS SELECT 1 AS id,planner_production_value() AS name;"
        "GRANT SELECT ON planner_production_view TO " + s5.engine.url.username + ";"
        "GRANT INSERT ON planner_production_effect TO " + s5.engine.url.username
    )
    engine = create_async_engine(s5.engine.url)
    try:
        adapter = SQLAlchemyExecutionAdapter(engine)
        plan = adapter.compile(
            ReadOperation(_View, _ViewOut), optimized=s5.variant == "planopt"
        )
        with pytest.raises((asyncpg.ReadOnlySQLTransactionError, DBAPIError)):
            await adapter.fetch_one(
                plan, ExecutionContext("default", None, False), (1,)
            )
        assert (
            await s5.admin.fetchval("SELECT count(*) FROM planner_production_effect")
            == 0
        )
    finally:
        await engine.dispose()
        await s5.admin.execute(
            "DROP VIEW planner_production_view;DROP FUNCTION planner_production_value();DROP TABLE planner_production_effect"
        )


@OPT
async def test_cancel_during_normal_lease_return_invalidates(
    s5: S5, sandloom_core: Any, monkeypatch
):
    adapter = sandloom_core.database("default").execution_adapter()
    lease = adapter.lease()
    connection, raw = await lease.__aenter__()
    closing = asyncio.Event()
    original = AsyncConnection.close
    entered = False

    async def close(conn):
        nonlocal entered
        if conn is connection and not entered:
            entered = True
            closing.set()
            await asyncio.Event().wait()
        await original(conn)

    monkeypatch.setattr(AsyncConnection, "close", close)
    try:
        task = asyncio.create_task(lease.__aexit__(None, None, None))
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert raw.is_closed(), "cancelled return did not terminate its connection"
        assert s5.engine.pool.checkedout() == 0
    finally:
        monkeypatch.setattr(AsyncConnection, "close", original)
        await lease._abort()


@OPT
async def test_read_unit_setup_failure_releases_its_lease(
    s5: S5, sandloom_core: Any, monkeypatch
):
    adapter = sandloom_core.database("default").execution_adapter()
    unit = adapter.read_unit()

    def fail(*args):
        raise RuntimeError("read unit setup failed")

    monkeypatch.setattr(adapter, "_read_only", fail)
    # Force the ORM setup seam, independently of the global rollout flag.
    plan = adapter.compile(get_tenant_item, optimized=False)
    try:
        with pytest.raises(RuntimeError, match="read unit setup failed"):
            await unit.fetch(plan, ExecutionContext("default", A, True), (1,))
        assert s5.engine.pool.checkedout() == 0
        await unit.close()
        assert s5.engine.pool.checkedout() == 0
    finally:
        await unit._abort()


@OPT
async def test_failed_checkout_cannot_terminate_a_previously_returned_connection(
    s5: S5, sandloom_core: Any, monkeypatch
):
    adapter = sandloom_core.database("default").execution_adapter()
    unit = adapter.read_unit()
    ctx = ExecutionContext("default", A, True)
    optimized = adapter.compile(get_tenant_item, optimized=True)
    general = adapter.compile(get_tenant_item, optimized=False)
    await unit.fetch(optimized, ctx, (1,))
    await unit.close()
    # The only pooled connection now belongs to a different caller.
    async with s5.engine.connect() as peer:

        async def unavailable(self):
            raise RuntimeError("checkout unavailable")

        monkeypatch.setattr(_Lease, "__aenter__", unavailable)
        with pytest.raises(RuntimeError, match="checkout unavailable"):
            await unit.fetch(general, ctx, (1,))
        with bind_tenant(A):
            assert (
                await peer.scalar(
                    text("SELECT 1").execution_options(sandloom_non_tenant=True)
                )
                == 1
            )
