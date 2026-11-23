"""An engine ``before_cursor_execute`` listener with ``retval=True`` (ADR-0018, decision 8).

The cursor guard reads the compiled statement of the execution context. SQLAlchemy runs
the ``before_cursor_execute`` listeners after that statement is compiled and before
``dialect.do_execute``, where the guard stands, so a listener registered with
``retval=True`` can hand the driver another SQL text or other parameters than the ones
the guard checked. That is a seam, not a refusal, and the reach is the one of
``Connection.exec_driver_sql``: a string the guard never reads.

These tests pin the seam where it is, so that the documented limit (G-TENANT-25, "Not
covered") and the code cannot drift apart:

* a listener that only observes (no ``retval``) leaves the guard whole;
* a ``retval=True`` listener that replaces the SQL text, or the parameters, reads the
  rows of another tenant under a bound tenant, on SQLite and on PostgreSQL;
* ``exec_driver_sql`` has the same reach, which is what the documents claim;
* the same rewrite one step earlier, ``before_execute`` with ``retval=True``, is a
  statement the guard compiles and refuses: the seam is the cursor listener only.

If a test of the second group starts to fail because the guard now sees the rewrite, the
seam is closed: update G-TENANT-25, ADR-0018 decision 8 and the multi-tenancy page, then
turn the test into the refusal it has become.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Text, Uuid, event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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
    read keeps the tenant filter."""
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
    finally:
        event.remove(sync_engine, "before_cursor_execute", observe)
    assert any(_reads_the_notes(s) and "tenant_id" in s for s in seen)


@pytest.mark.asyncio
async def test_a_retval_listener_that_replaces_the_sql_reaches_another_tenants_rows(
    env: _Env,
) -> None:
    """The seam: the guard approved the filtered statement, the driver gets the other one."""
    assert await _bodies_read_as(env, env.a) == ["a-note"]  # without the listener
    sync_engine = env.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _drop_the_filter, retval=True)
    try:
        assert await _bodies_read_as(env, env.a) == ["a-note", "b-secret"]
    finally:
        event.remove(sync_engine, "before_cursor_execute", _drop_the_filter)
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
    """What the documents claim: the listener adds nothing that ``exec_driver_sql`` does not
    already give, a string the guard has no compiled statement to read for."""
    async with env.maker() as session:
        with bind_tenant(env.a):
            connection = await session.connection()
            result = await connection.exec_driver_sql(
                "SELECT body FROM tgcl_notes ORDER BY id"
            )
            assert list(result.scalars()) == ["a-note", "b-secret"]


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
