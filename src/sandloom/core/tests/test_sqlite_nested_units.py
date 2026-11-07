"""Nested write units on a database whose writers are serial (SQLite).

Spec ``design/infra-agnostic/spec.md``, "Slice 8c, as built", option (a): a
local contract call, a saga step's marker unit or a child ``open_uow()``
inside a unit of the same task, on the same instance, runs as a SAVEPOINT on
the caller's connection. It commits with the caller: an outer rollback undoes
its rows, its outbox rows and its command-log claim, and its post-commit
stages (outbox dispatch, ``on_commit``) run only after the outer COMMIT. Its
own failure rolls back to the savepoint, and the caller continues. PostgreSQL
(no ``serial_writers``) still opens a fresh, independent unit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import Column, Integer, MetaData, Table, event, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import sandloom.core.runtime as _runtime
from sandloom.core import app_factory
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.drivers.dbapi_aiosqlite import NestedWriterRefused
from sandloom.core.drivers.schema_reset import schema_reset_for
from sandloom.core.facade import CoreFacade
from sandloom.core.guarantee_lattice import (
    COMMITS_WITH_CALLER,
    Mechanism,
    effect_label,
    nested_unit_label,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.module_api import contract_method
from sandloom.core.sql_alchemy.models.contract_command import ContractCommandRecord
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.nesting import _write_connection, pin_test_host
from sandloom.core.sql_alchemy.portable import WRITE_LOCK_HELD, serial_writers
from sandloom.core.testing import reload_test_env
from sandloom.core.testing.database import assert_disposable_test_database
from sandloom.core.testing.ownership import claim_database
from sandloom.core.testing.workers import worker_env_values
from sandloom.core.unit_of_work import JoinedUnitRolledBack, db_session, open_uow
from sandloom.core.unit_of_work.dispatch import drain_post_commit
from sandloom.core.unit_of_work.effects import publish_event
from sandloom.core.unit_of_work.hooks import on_commit
from sandloom.core.unit_of_work.request import (
    WriteAdmission,
    db,
    fresh_admission_scope,
)

_ITEMS = MetaData()
ITEMS = Table("nested_items", _ITEMS, Column("id", Integer, primary_key=True))
KERNEL_TABLES: list[Table] = [OutboxMessage.__table__, ContractCommandRecord.__table__]  # type: ignore[list-item]
CHANNEL = "nested.item.added.v1"


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


#: What the providers' on_commit callbacks did, in order.
RAN: list[str] = []


class Items:
    """A provider whose command writes a row, publishes and queues a callback."""

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def add(cls, item: int, operation_key: str) -> int:
        await db().execute(insert(ITEMS).values(id=item))
        await publish_event(CHANNEL, {"item": item})
        await on_commit(lambda: RAN.append(f"on_commit {item}"))
        return item

    @classmethod
    @contract_method
    async def count(cls) -> int:
        return int(
            (await db().execute(text("SELECT count(*) FROM nested_items"))).scalar_one()
        )

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def fail(cls, item: int, operation_key: str) -> int:
        await db().execute(insert(ITEMS).values(id=item))
        raise LookupError("the provider failed")


async def _build(url: str) -> Any:
    provider = driver.create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="nested",
            driver="sqlalchemy",
            options={"url": url},
        )
    )
    async with provider.engine.begin() as conn:
        await conn.run_sync(
            lambda c: OutboxMessage.metadata.create_all(c, tables=KERNEL_TABLES)
        )
        await conn.run_sync(_ITEMS.create_all)
    return provider


@pytest_asyncio.fixture
async def core(tmp_path: Path) -> AsyncGenerator[CoreFacade, None]:
    provider = await _build(f"sqlite+aiosqlite:///{tmp_path / 'nested.db'}")
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": provider  # type: ignore[method-assign]
    facade.register_service("nested.items.v1", Items)
    facade.event_bus._schema_registry.register(
        CHANNEL, "item_added", {"type": "object"}
    )
    RAN.clear()
    _runtime.set_active_core(facade)
    try:
        yield facade
    finally:
        try:
            # The dispatch of the test's events marks rows on this provider:
            # finish it before the provider and the loop close (risk fix 1).
            await drain_post_commit(timeout=5)
        finally:
            _runtime.clear_active_core()
            await provider.aclose()


@pytest.fixture
def statements(core: CoreFacade) -> Iterator[list[str]]:
    engine: AsyncEngine = core.database().engine
    seen: list[str] = []

    def _seen(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _seen)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", _seen)


async def _rows(core: CoreFacade) -> dict[str, list[Any]]:
    async with db_session() as session:
        items = (await session.execute(select(ITEMS.c.id))).scalars().all()
        outbox = (await session.execute(select(OutboxMessage.channel))).scalars().all()
        claims = (
            (await session.execute(select(ContractCommandRecord.dedupe_key)))
            .scalars()
            .all()
        )
    return {"items": sorted(items), "outbox": list(outbox), "claims": list(claims)}


async def _write_lock(core: CoreFacade) -> None:
    """The outer unit takes the write lock (BEGIN IMMEDIATE at its first
    statement) and writes a row of its own."""
    await db().execute(insert(ITEMS).values(id=1))


def test_sqlite_declares_serial_writers_and_postgresql_does_not() -> None:
    assert serial_writers("sqlite") is True
    assert serial_writers("postgresql") is False


@pytest.mark.asyncio
async def test_a_contract_command_in_a_unit_commits_with_the_outer(
    core: CoreFacade, statements: list[str]
) -> None:
    items = core.get_service("nested.items.v1")
    async with db_session():
        await _write_lock(core)
        assert await items.add(2, operation_key="k-2") == 2
        # Nothing claims durability before the outer COMMIT.
        assert RAN == []
    assert RAN == ["on_commit 2"]
    assert any(s.startswith("SAVEPOINT") for s in statements)
    assert any(s.startswith("RELEASE SAVEPOINT") for s in statements)
    rows = await _rows(core)
    assert rows["items"] == [1, 2]
    assert rows["outbox"] == [CHANNEL]
    assert len(rows["claims"]) == 1


@pytest.mark.filterwarnings("ignore::sandloom.core.unit_of_work.JoinedUnitRolledBack")
@pytest.mark.asyncio
async def test_an_outer_rollback_undoes_the_inner_unit(core: CoreFacade) -> None:
    items = core.get_service("nested.items.v1")
    with pytest.raises(RuntimeError, match="outer failed"):
        async with db_session():
            await _write_lock(core)
            await items.add(2, operation_key="k-2")
            raise RuntimeError("outer failed")
    assert RAN == []  # never dispatched, never called back
    assert await _rows(core) == {"items": [], "outbox": [], "claims": []}
    # The claim went with the caller: the command runs afresh on a retry.
    async with db_session():
        await _write_lock(core)
        await items.add(2, operation_key="k-2")
    assert (await _rows(core))["items"] == [1, 2]
    assert RAN == ["on_commit 2"]


@pytest.mark.asyncio
async def test_an_inner_failure_rolls_back_only_the_inner_unit(
    core: CoreFacade,
) -> None:
    items = core.get_service("nested.items.v1")
    async with db_session():
        await _write_lock(core)
        with pytest.raises(LookupError):
            await items.fail(3, operation_key="k-3")
        await db().execute(insert(ITEMS).values(id=4))  # the caller continues
    rows = await _rows(core)
    assert rows["items"] == [1, 4]
    assert rows["claims"] == []


@pytest.mark.filterwarnings("ignore::sandloom.core.unit_of_work.JoinedUnitRolledBack")
@pytest.mark.asyncio
async def test_a_savepoint_rollback_around_the_call_drops_its_effects(
    core: CoreFacade,
) -> None:
    items = core.get_service("nested.items.v1")
    async with db_session():
        await _write_lock(core)
        session = db()
        nested = await session.begin_nested()
        await items.add(2, operation_key="k-2")
        await nested.rollback()
    assert RAN == []
    assert await _rows(core) == {"items": [1], "outbox": [], "claims": []}


@pytest.mark.filterwarnings("ignore::sandloom.core.unit_of_work.JoinedUnitRolledBack")
@pytest.mark.asyncio
async def test_a_child_unit_runs_nested_and_commits_with_the_outer(
    core: CoreFacade, statements: list[str]
) -> None:
    """A child ``open_uow()`` (``db_session``, as a saga step's marker unit
    opens it inside the saga's unit) on the same instance."""
    ran: list[str] = []
    async with db_session():
        await _write_lock(core)
        async with db_session() as child:
            await child.execute(insert(ITEMS).values(id=5))
            await on_commit(lambda: ran.append("child"))
        assert ran == []
        with pytest.raises(LookupError):
            async with db_session() as failing:
                await failing.execute(insert(ITEMS).values(id=6))
                raise LookupError("child failed")
    assert ran == ["child"]
    assert sum(s.startswith("SAVEPOINT") for s in statements) == 2
    assert (await _rows(core))["items"] == [1, 5]
    with pytest.raises(RuntimeError):
        async with db_session():
            await db().execute(insert(ITEMS).values(id=10))
            async with db_session() as child:
                await child.execute(insert(ITEMS).values(id=7))
            raise RuntimeError("outer failed")
    assert (await _rows(core))["items"] == [1, 5]


@pytest.mark.asyncio
async def test_a_unit_before_the_outer_wrote_commits_on_its_own(
    core: CoreFacade, statements: list[str]
) -> None:
    """The outer holds no write lock yet (nothing reached the database): the
    inner unit has its own transaction and commits alone, as on PostgreSQL."""
    items = core.get_service("nested.items.v1")
    with pytest.raises(RuntimeError):
        async with db_session():
            await items.add(2, operation_key="k-2")
            assert RAN == ["on_commit 2"]
            raise RuntimeError("outer failed")
    assert not any(s.startswith("SAVEPOINT") for s in statements)
    assert (await _rows(core))["items"] == [2]


@pytest.mark.asyncio
async def test_a_unit_outside_the_task_s_open_units_is_still_refused(
    core: CoreFacade,
) -> None:
    """A write lock the task holds outside a unit of this context (a bare
    session) cannot be nested into: R016 still refuses at once."""
    provider = core.database()
    async with provider.session_factory() as bare, bare.begin():
        await bare.execute(text("SELECT 1"))
        with pytest.raises(NestedWriterRefused, match="SANDLOOM-R016"):
            async with open_uow(core) as session:
                await session.execute(insert(ITEMS).values(id=9))


@pytest.mark.asyncio
async def test_another_instance_keeps_its_own_transaction(
    core: CoreFacade, tmp_path: Path, statements: list[str]
) -> None:
    other = await _build(f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    try:
        with pytest.raises(RuntimeError):
            async with db_session():
                await _write_lock(core)
                async with open_uow(core, factory=other.uow_factory()) as session:
                    await session.execute(insert(ITEMS).values(id=8))
                raise RuntimeError("outer failed")
        assert not any(s.startswith("SAVEPOINT") for s in statements)
        async with other.engine.connect() as conn:
            ids = (await conn.execute(select(ITEMS.c.id))).scalars().all()
        assert list(ids) == [8]  # committed on its own file
    finally:
        await other.aclose()


@pytest.mark.asyncio
async def test_a_read_contract_in_a_writing_unit_reads_committed_rows_only(
    core: CoreFacade,
) -> None:
    """A read contract keeps a read-only transaction of its own (as on
    PostgreSQL): it never nests, so it never sees the caller's uncommitted
    writes."""
    items = core.get_service("nested.items.v1")
    async with db_session():
        await _write_lock(core)
        assert await items.count() == 0
    async with db_session():
        assert await items.count() == 1


@pytest.mark.filterwarnings("ignore::sandloom.core.unit_of_work.JoinedUnitRolledBack")
@pytest.mark.asyncio
async def test_units_of_concurrent_tasks_nest_one_task_at_a_time(
    core: CoreFacade,
) -> None:
    """Two tasks nesting in one pinned host whose lock a finished task took
    (an async fixture): the second waits for the first's unit, so the
    first's rollback to its savepoint never undoes the second's commit."""
    provider = core.database()
    maker = async_sessionmaker(
        schema_reset_for(provider.engine).unit_engine(provider.engine),
        expire_on_commit=False,
    )
    async with maker() as host:
        with pin_test_host(host):
            await asyncio.create_task(host.execute(insert(ITEMS).values(id=1)))

            async def child(item: int, delay: float) -> None:
                async with db_session() as session:
                    await session.execute(insert(ITEMS).values(id=item))
                    await asyncio.sleep(delay)
                    if item == 2:
                        raise LookupError("the first child fails")

            results = await asyncio.gather(
                child(2, 0.05), child(3, 0.01), return_exceptions=True
            )
            assert isinstance(results[0], LookupError) and results[1] is None
            ids = (await host.execute(select(ITEMS.c.id))).scalars().all()
            assert sorted(ids) == [1, 3]
        await host.rollback()


def test_two_in_memory_engines_are_two_databases() -> None:
    """The same in-memory URL on two engines names two databases: only the
    engine itself says "the same database" there."""
    first = SimpleNamespace(url=make_url("sqlite+aiosqlite://"))
    second = SimpleNamespace(url=make_url("sqlite+aiosqlite://"))
    conn = SimpleNamespace(engine=first, closed=False, info={WRITE_LOCK_HELD: True})
    session = SimpleNamespace(
        get_transaction=lambda: SimpleNamespace(_connections={"bind": (conn,)})
    )
    assert _write_connection(session, first) is conn
    assert _write_connection(session, second) is None
    files = SimpleNamespace(url=make_url("sqlite+aiosqlite:////srv/a.db"))
    conn.engine = files
    other = SimpleNamespace(url=make_url("sqlite+aiosqlite:////srv/a.db"))
    assert _write_connection(session, other) is conn  # one file, two engines


# -- PostgreSQL: unchanged -----------------------------------------------------


@pytest_asyncio.fixture
async def pg_core() -> AsyncGenerator[tuple[CoreFacade, Table], None]:
    url = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")[
        "DATABASE_URL"
    ]
    if not url.startswith("postgresql"):
        pytest.skip("the test database is not PostgreSQL (a SQLite test run)")
    assert_disposable_test_database(url)
    claim_database(url)
    provider = driver.create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="nested_pg",
            driver="sqlalchemy",
            options={"url": url},
        )
    )
    table = Table(
        f"nested_items_{uuid.uuid4().hex[:12]}",
        MetaData(),
        Column("id", Integer, primary_key=True),
    )
    async with provider.engine.begin() as conn:
        await conn.run_sync(table.metadata.create_all)
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": provider  # type: ignore[method-assign]
    _runtime.set_active_core(facade)
    try:
        yield facade, table
    finally:
        try:
            await drain_post_commit(timeout=5)
        finally:
            _runtime.clear_active_core()
            try:
                async with provider.engine.begin() as conn:
                    await conn.run_sync(table.metadata.drop_all)
            finally:
                await provider.aclose()


@pytest.mark.asyncio
async def test_postgresql_still_opens_a_fresh_independent_unit(
    pg_core: tuple[CoreFacade, Table],
) -> None:
    """No ``serial_writers``: a child unit inside a writing unit takes its
    own connection and commits on its own; no SAVEPOINT is emitted."""
    core, table = pg_core
    engine = core.database().engine
    seen: list[str] = []

    def _seen(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _seen)
    ran: list[str] = []
    try:
        with pytest.raises(RuntimeError):
            async with db_session() as outer:
                await outer.execute(insert(table).values(id=1))
                async with db_session() as child:
                    assert child is not outer
                    await child.execute(insert(table).values(id=2))
                    await on_commit(lambda: ran.append("child"))
                assert ran == ["child"]  # ran at the child's own COMMIT
                raise RuntimeError("outer failed")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _seen)
    assert not any("SAVEPOINT" in s.upper() for s in seen)
    async with engine.connect() as conn:
        ids = (await conn.execute(select(table.c.id))).scalars().all()
    assert list(ids) == [2]  # the child committed; the outer rolled back


# -- the label -----------------------------------------------------------------


class _Infra:
    """The registry surface :func:`nested_units_join_caller` reads."""

    def __init__(self, instances: dict[str, dict[str, Any]]) -> None:
        self._instances = instances

    def instances(self, capability: str) -> list[str]:
        return sorted(self._instances)

    def sql_dialects(self, capability: str, name: str) -> Any:
        return ("sqlalchemy", frozenset({"postgresql", "sqlite"}))

    def instance_config(self, capability: str, name: str) -> Any:
        return SimpleNamespace(options=self._instances[name])


def test_the_label_is_computed_from_serial_writers() -> None:
    assert nested_unit_label(Mechanism()) is None
    assert nested_unit_label(Mechanism(serial_writers=True)) == COMMITS_WITH_CALLER
    command = effect_label("command", Mechanism(serial_writers=True))
    assert COMMITS_WITH_CALLER in (command.deviation() or "")
    assert COMMITS_WITH_CALLER not in (effect_label("command").deviation() or "")


def test_startup_warns_once_per_serial_writer_instance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    infra = _Infra(
        {
            "default": {"url": "postgresql+asyncpg://h/db"},
            "lite": {"url": "sqlite+aiosqlite:///lite.db"},
        }
    )
    assert app_factory.nested_units_join_caller(infra) == [  # type: ignore[arg-type]
        ("lite", COMMITS_WITH_CALLER)
    ]
    log = logging.getLogger("sandloom.app")
    with caplog.at_level(logging.WARNING, logger="sandloom.app"):
        app_factory.warn_nested_units_join_caller(infra, log)  # type: ignore[arg-type]
    records = [r for r in caplog.records if r.name == "sandloom.app"]
    assert len(records) == 1
    assert "database/lite" in records[0].getMessage()
    assert COMMITS_WITH_CALLER in records[0].getMessage()


# -- saga steps on the SQLite test harness -------------------------------------

_REPO = Path(__file__).resolve().parents[4]
#: Saga protocol tests whose steps (the transition unit, the step-started
#: marker unit, the provider's command unit) nest on SQLite.
_SAGA_TESTS = (
    "src/sandloom/core/tests/test_sagas_protocol.py::"
    "test_happy_path_runs_every_step_once_with_derived_keys",
    "src/sandloom/core/tests/test_sagas_protocol.py::"
    "test_duplicate_deliveries_never_run_a_step_twice",
    "src/sandloom/core/tests/test_sagas_recovery.py::"
    "test_refused_before_the_pivot_compensates_the_applied_steps",
    # the harness beside the client fixture: no mark waits for db's lock
    "src/sandloom/core/tests/test_saga_harness_with_client.py::"
    "test_a_saga_run_between_requests_waits_for_no_lock",
)


@pytest.mark.subprocess_probe
def test_saga_steps_run_on_the_sqlite_test_harness(tmp_path: Path) -> None:
    """The real saga protocol (``saga_harness``) on a SQLite test database:
    each step's units nest in the transition's unit and commit with it."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'saga_test.db'}"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")},
        "SANDLOOM_TEST_DATABASE_URL": url,
        "DATABASE_URL": url,
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:xdist",
            "-p",
            "no:randomly",
            *_SAGA_TESTS,
        ],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert f"{len(_SAGA_TESTS)} passed" in proc.stdout, proc.stdout[-2000:]


@pytest.mark.asyncio
async def test_a_read_scope_unit_holds_no_write_lock_on_sqlite(
    core: CoreFacade,
) -> None:
    """A request's contract read scope keeps its shared unit open across
    local read calls: on SQLite it is read-only (no write lock, so the
    request's own write units go on); on PostgreSQL the contract factory, as
    before."""
    provider = core.database()
    factory = provider.contract_read_uow_factory()
    assert factory is not None
    async with factory() as reader:
        assert (await reader.session.execute(select(ITEMS.c.id))).all() == []
        connection = await reader.session.connection()
        assert not connection.info.get(WRITE_LOCK_HELD)
        async with provider.uow_factory()() as writer:  # not refused, not waiting
            await writer.session.execute(insert(ITEMS).values(id=1))


@pytest.mark.asyncio
async def test_a_read_only_unit_is_no_writer_of_the_admission_scope(
    core: CoreFacade,
) -> None:
    """A read-only unit's ``PRAGMA query_only`` is the adapter's own
    statement: in a write-admission scope (a contract command's) it is no
    write, so the scope's one writer is still the command's unit."""
    provider = core.database()
    reader = provider.contract_read_uow_factory()
    assert reader is not None
    with fresh_admission_scope(WriteAdmission()):
        async with reader() as unit:
            await unit.session.execute(select(ITEMS.c.id))
        async with provider.uow_factory()() as writer:
            await writer.session.execute(insert(ITEMS).values(id=1))


@pytest.mark.asyncio
async def test_postgresql_read_scope_keeps_the_contract_factory(
    pg_core: tuple[CoreFacade, Table],
) -> None:
    assert pg_core[0].database().contract_read_uow_factory() is None


@pytest.mark.asyncio
async def test_a_caller_rolling_back_a_joined_unit_warns(core: CoreFacade) -> None:
    """On PostgreSQL the command would stay committed: relying on SQLite's
    rollback of it is reported (G-UOW-09), and the harness fails on it."""
    items = core.get_service("nested.items.v1")
    with (
        pytest.warns(JoinedUnitRolledBack, match="G-UOW-09"),
        pytest.raises(RuntimeError, match="outer failed"),
    ):
        async with db_session():
            await _write_lock(core)
            await items.add(8, operation_key="k-8")
            raise RuntimeError("outer failed")
