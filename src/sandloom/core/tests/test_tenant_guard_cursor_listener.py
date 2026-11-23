"""Listeners that act at the cursor, after the guard read the statement (ADR-0018, decision 8).

The cursor guard reads the compiled statement of the execution context inside
``dialect.do_execute`` (and ``do_executemany``, ``do_execute_no_params``). Two kinds of
listener run before that call, so they are a seam and not a refusal:

* a ``before_cursor_execute`` listener registered with ``retval=True``, on the engine, on
  a connection or on the ``Engine`` class, hands the driver another SQL text or other
  parameters than the ones the guard checked;
* a dialect listener (``do_execute``, ``do_executemany``, ``do_execute_no_params``) that
  runs the cursor itself and returns ``True`` makes SQLAlchemy skip the dialect method,
  so the guard never runs for that statement.

For the tenant rule the reach is that of ``Connection.exec_driver_sql``: a statement the
guard never reads. A dialect listener also skips the other checks of the hook.

These tests pin the seam where it is, so that the documented limit (G-TENANT-25, "Not
covered") and the code cannot drift apart:

* a listener that only observes (no ``retval``) leaves the guard whole: it still refuses
  a Core read of a tenant table;
* a ``retval=True`` cursor listener that replaces the SQL text, or the parameters, reads
  the rows of another tenant under a bound tenant, on SQLite and on PostgreSQL, wherever
  the listener is registered;
* a dialect listener that runs the cursor reads, and writes, rows of another tenant;
* ``exec_driver_sql`` has the same reach, which is what the documents claim;
* the same rewrite one step earlier, ``before_execute`` with ``retval=True``, is a
  statement the guard compiles and refuses: the seam is the cursor and the dialect only.

If a test of the second to fourth group starts to fail because the guard now sees the
rewrite, the seam is closed: update G-TENANT-25, ADR-0018 decision 8 and the multi-tenancy
page, then turn the test into the refusal it has become.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Text, Uuid, event, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.expression import Executable

from sandloom.core.config.settings import settings
from sandloom.core.di import UnsafeTenantOperationError, bind_tenant, unscoped
from sandloom.core.orm import TenantScoped
from sandloom.core.sql_alchemy.write_admission import install_write_admission
from sandloom.core.testing import reload_test_env

#: ``before_cursor_execute`` listener: ``(conn, cursor, statement, parameters, context,
#: executemany) -> (statement, parameters)`` when it is registered with ``retval=True``.
_CursorListener = Callable[[Any, Any, str, Any, Any, bool], tuple[str, Any]]


class _Base(DeclarativeBase):
    pass


class _Note(TenantScoped, _Base):
    __tablename__ = "tgcl_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)


@dataclass
class _Env:
    engine: AsyncEngine
    maker: async_sessionmaker[AsyncSession]
    a: uuid.UUID
    b: uuid.UUID


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def env(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncGenerator[_Env, None]:
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'listener.db'}"
    else:
        url = settings.database_url
        if not url.startswith("postgresql"):
            pytest.fail(f"these tests need a PostgreSQL URL, got {url[:12]!r}")
    engine = create_async_engine(url)
    install_write_admission(engine)  # the cursor rule every kernel engine carries
    try:
        with unscoped():  # schema DDL names every table: system work (ADR-0018)
            async with engine.begin() as conn:
                await conn.run_sync(_Base.metadata.drop_all)
                await conn.run_sync(_Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
        async with maker() as session:
            with unscoped():
                session.add_all(
                    [
                        _Note(id=1, tenant_id=tenant_a, body="a-note"),
                        _Note(id=2, tenant_id=tenant_b, body="b-secret"),
                    ]
                )
                await session.commit()
        yield _Env(engine=engine, maker=maker, a=tenant_a, b=tenant_b)
    finally:
        try:
            with unscoped():
                async with engine.begin() as conn:
                    await conn.run_sync(_Base.metadata.drop_all)
        finally:
            await engine.dispose()


async def _bodies_read_as(env: _Env, tenant: uuid.UUID) -> list[str]:
    """What an ORM read of the notes returns with ``tenant`` bound."""
    async with env.maker() as session:
        with bind_tenant(tenant):
            result = await session.execute(select(_Note.body).order_by(_Note.id))
            return list(result.scalars())


#: A Core read of the table: it names no tenant criterion.
_READ_THE_TABLE = select(_Note.__table__.c.body).order_by(_Note.__table__.c.id)


async def _table_bodies_read_as(
    env: _Env, tenant: uuid.UUID, statement: Executable = _READ_THE_TABLE
) -> list[str]:
    """What a Core read of the table returns with ``tenant`` bound, run on the session's
    connection: the ORM guard refuses ``session.execute`` of it, so only the cursor rule
    stands between the statement and the rows."""
    async with env.maker() as session:
        with bind_tenant(tenant):
            connection = await session.connection()
            result = await connection.execute(statement)
            return list(result.scalars())


async def _all_bodies(env: _Env) -> list[str]:
    """Every note of every tenant, read as system work."""
    async with env.maker() as session:
        with unscoped():
            result = await session.execute(select(_Note.body).order_by(_Note.id))
            return list(result.scalars())


def _reads_the_notes(statement: str) -> bool:
    return statement.startswith("SELECT") and "FROM tgcl_notes" in statement


def _drop_the_filter(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> tuple[str, Any]:
    """Send the read of the notes without its ``WHERE`` clause and its parameter."""
    if _reads_the_notes(statement):
        return statement.partition("\nWHERE ")[0], ()
    return statement, parameters


def _bind_value(engine: AsyncEngine, tenant: uuid.UUID) -> Any:
    """``tenant`` as the driver receives it: a hex string on SQLite, a UUID on asyncpg."""
    process = Uuid().bind_processor(engine.dialect)
    return process(tenant) if process is not None else tenant


def _swap_the_tenant(
    engine: AsyncEngine, mine: uuid.UUID, theirs: uuid.UUID
) -> _CursorListener:
    """A listener that sends ``theirs`` where the guard checked ``mine``."""
    checked, sent = _bind_value(engine, mine), _bind_value(engine, theirs)

    def swap(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> tuple[str, Any]:
        if _reads_the_notes(statement):
            return statement, tuple(sent if p == checked else p for p in parameters)
        return statement, parameters

    return swap


@pytest.mark.asyncio
async def test_a_listener_that_only_observes_leaves_the_guard_whole(env: _Env) -> None:
    """Control: a listener without ``retval`` sees the statement, cannot change it, and the
    guard stays whole: the ORM read keeps the tenant filter, and the cursor rule still
    refuses a Core read of the table (the part that no ORM criterion can answer for)."""
    seen: list[str] = []

    def observe(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        seen.append(statement)

    sync_engine = env.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", observe)
    try:
        assert await _bodies_read_as(env, env.a) == ["a-note"]
        with pytest.raises(UnsafeTenantOperationError):
            await _table_bodies_read_as(env, env.a)
    finally:
        event.remove(sync_engine, "before_cursor_execute", observe)
    assert any(_reads_the_notes(s) and "tenant_id" in s for s in seen)


#: Where a ``before_cursor_execute`` listener is registered: SQLAlchemy runs the ones of
#: the engine, of the connection and of the ``Engine`` class alike.
_SITES = ("engine", "connection", "Engine class")


async def _bodies_read_with_a_cursor_listener(
    env: _Env, site: str, listener: _CursorListener
) -> list[str]:
    """The ORM read of the notes as tenant A, with ``listener`` registered (``retval=True``)
    on ``site`` for the length of the read."""
    async with env.maker() as session:
        with bind_tenant(env.a):
            connection = await session.connection()
            target: Any = {
                "engine": env.engine.sync_engine,
                "connection": connection.sync_connection,
                "Engine class": Engine,
            }[site]
            event.listen(target, "before_cursor_execute", listener, retval=True)
            try:
                result = await session.execute(select(_Note.body).order_by(_Note.id))
                return list(result.scalars())
            finally:
                event.remove(target, "before_cursor_execute", listener)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", _SITES)
async def test_a_retval_listener_that_replaces_the_sql_reaches_another_tenants_rows(
    env: _Env, site: str
) -> None:
    """The seam: the guard approved the filtered statement, the driver gets the other one,
    whether the listener is the engine's, the connection's or the ``Engine`` class's."""
    assert await _bodies_read_as(env, env.a) == ["a-note"]  # without the listener
    assert await _bodies_read_with_a_cursor_listener(env, site, _drop_the_filter) == [
        "a-note",
        "b-secret",
    ]
    assert await _bodies_read_as(env, env.a) == ["a-note"]  # and again without it


@pytest.mark.asyncio
async def test_a_retval_listener_that_replaces_the_parameters_reaches_another_tenants_rows(
    env: _Env,
) -> None:
    """The same seam through the values: the statement text is untouched, the tenant the
    driver binds is not the one the guard checked."""
    assert await _bodies_read_as(env, env.a) == ["a-note"]
    swap = _swap_the_tenant(env.engine, env.a, env.b)
    sync_engine = env.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", swap, retval=True)
    try:
        assert await _bodies_read_as(env, env.a) == ["b-secret"]
    finally:
        event.remove(sync_engine, "before_cursor_execute", swap)
    assert await _bodies_read_as(env, env.a) == ["a-note"]


@pytest.mark.asyncio
async def test_exec_driver_sql_has_the_same_reach_as_the_rewriting_listener(
    env: _Env,
) -> None:
    """What the documents claim for the tenant rule: the cursor listener adds nothing that
    ``exec_driver_sql`` does not already give, a string the guard has no compiled statement
    to read for."""
    async with env.maker() as session:
        with bind_tenant(env.a):
            connection = await session.connection()
            result = await connection.exec_driver_sql(
                "SELECT body FROM tgcl_notes ORDER BY id"
            )
            assert list(result.scalars()) == ["a-note", "b-secret"]


def _runs_the_cursor_itself(event_name: str) -> Callable[..., bool]:
    """A dialect listener for ``event_name`` that sends a statement on the notes table to
    the cursor itself and returns ``True``: SQLAlchemy then skips ``dialect.do_execute``
    (``do_executemany``, ``do_execute_no_params``), the method the guard wraps."""

    def do_execute(cursor: Any, statement: str, parameters: Any, context: Any) -> bool:
        if "tgcl_notes" not in statement:
            return False
        cursor.execute(statement, parameters)
        return True

    def do_executemany(
        cursor: Any, statement: str, parameters: Any, context: Any
    ) -> bool:
        if "tgcl_notes" not in statement:
            return False
        cursor.executemany(statement, parameters)
        return True

    def do_execute_no_params(cursor: Any, statement: str, context: Any) -> bool:
        if "tgcl_notes" not in statement:
            return False
        cursor.execute(statement)
        return True

    return {
        "do_execute": do_execute,
        "do_executemany": do_executemany,
        "do_execute_no_params": do_execute_no_params,
    }[event_name]


#: The Core read a dialect listener takes over, by the event that carries it.
#: ``no_parameters`` sends the statement through ``do_execute_no_params``.
_DIALECT_READS = {
    "do_execute": _READ_THE_TABLE,
    "do_execute_no_params": _READ_THE_TABLE.execution_options(no_parameters=True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", list(_DIALECT_READS))
async def test_a_dialect_listener_that_runs_the_cursor_reads_another_tenants_rows(
    env: _Env, event_name: str
) -> None:
    """The seam one step before the guard's hook: SQLAlchemy runs the dialect listeners
    first and skips ``dialect.do_execute`` when one returns ``True``, so the Core read that
    the cursor rule refuses reads every tenant's rows."""
    statement = _DIALECT_READS[event_name]
    with pytest.raises(UnsafeTenantOperationError):  # without the listener
        await _table_bodies_read_as(env, env.a, statement)
    listener = _runs_the_cursor_itself(event_name)
    dialect = env.engine.sync_engine
    event.listen(dialect, event_name, listener)
    try:
        assert await _table_bodies_read_as(env, env.a, statement) == [
            "a-note",
            "b-secret",
        ]
    finally:
        event.remove(dialect, event_name, listener)
    with pytest.raises(UnsafeTenantOperationError):  # and again without it
        await _table_bodies_read_as(env, env.a, statement)


async def _insert_notes_as(env: _Env, tenant: uuid.UUID, notes: list[_Note]) -> None:
    """A Core ``INSERT`` of several rows (one ``executemany``) with ``tenant`` bound."""
    rows = [
        {"id": note.id, "tenant_id": note.tenant_id, "body": note.body}
        for note in notes
    ]
    async with env.maker() as session:
        with bind_tenant(tenant):
            connection = await session.connection()
            await connection.execute(insert(_Note.__table__), rows)
            await session.commit()


@pytest.mark.asyncio
async def test_a_do_executemany_listener_that_runs_the_cursor_writes_another_tenants_rows(
    env: _Env,
) -> None:
    """The same seam for a write: tenant A inserts two rows that belong to tenant B."""
    planted = [
        _Note(id=3, tenant_id=env.b, body="planted-1"),
        _Note(id=4, tenant_id=env.b, body="planted-2"),
    ]
    with pytest.raises(UnsafeTenantOperationError):  # without the listener
        await _insert_notes_as(env, env.a, planted)
    assert await _all_bodies(env) == ["a-note", "b-secret"]
    listener = _runs_the_cursor_itself("do_executemany")
    dialect = env.engine.sync_engine
    event.listen(dialect, "do_executemany", listener)
    try:
        await _insert_notes_as(env, env.a, planted)
    finally:
        event.remove(dialect, "do_executemany", listener)
    assert await _all_bodies(env) == ["a-note", "b-secret", "planted-1", "planted-2"]


@pytest.mark.asyncio
async def test_a_before_execute_listener_that_replaces_the_statement_is_refused(
    env: _Env,
) -> None:
    """The same rewrite one step earlier is not a seam: ``before_execute`` replaces the
    clause before it is compiled, so the guard reads the replacement, which no ORM guard
    approved."""

    def replace(
        conn: Any,
        clauseelement: Any,
        multiparams: Any,
        params: Any,
        execution_options: Any,
    ) -> tuple[Any, Any, Any]:
        if "tgcl_notes" in str(clauseelement):
            return select(_Note.__table__.c.body), multiparams, params
        return clauseelement, multiparams, params

    sync_engine = env.engine.sync_engine
    event.listen(sync_engine, "before_execute", replace, retval=True)
    try:
        with pytest.raises(UnsafeTenantOperationError):
            await _bodies_read_as(env, env.a)
    finally:
        event.remove(sync_engine, "before_execute", replace)
    assert await _bodies_read_as(env, env.a) == ["a-note"]
