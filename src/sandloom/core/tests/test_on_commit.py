"""``on_commit``: side effects outside the transaction run only after COMMIT.

Pins: commit -> runs (once, after the commit); rollback -> never runs; a
callback registered inside a rolled-back SAVEPOINT never runs while one in a
released savepoint does; outside any UoW it runs immediately; a failing
callback never fails the committed request.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core import unit_of_work as uow_mod
from sandloom.core.di import on_commit
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.testing import reload_test_env
from sandloom.core.unit_of_work import scope as uow_mod_scope


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured for tests")
    engine: AsyncEngine = create_async_engine(database_url)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@asynccontextmanager
async def _uow(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """The same commit/after-commit sequence as DbSession / db_session()."""
    async with SQLAlchemyUnitOfWork(session_factory=factory) as uow:
        session = uow.session
        assert session is not None
        with uow_mod_scope._bind_uow_scope(session, "default"):
            yield session
    await uow_mod._dispatch_outbox_after_commit(session)


class _Boom(Exception):
    pass


@pytest.mark.asyncio
async def test_runs_after_commit_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []

    async def _async_cb() -> None:
        calls.append("async")

    async with _uow(session_factory) as session:
        await session.execute(text("SELECT 1"))
        await on_commit(_async_cb)
        await on_commit(lambda: calls.append("sync"))
        assert calls == []  # not before the commit
    assert calls == ["async", "sync"]


@pytest.mark.asyncio
async def test_rollback_never_runs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []
    with pytest.raises(_Boom):
        async with _uow(session_factory) as session:
            await session.execute(text("SELECT 1"))
            await on_commit(lambda: calls.append("x"))
            raise _Boom
    assert calls == []


@pytest.mark.asyncio
async def test_rolled_back_savepoint_drops_its_callbacks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []
    async with _uow(session_factory) as session:
        await session.execute(text("SELECT 1"))
        await on_commit(lambda: calls.append("outer"))
        with pytest.raises(_Boom):
            async with session.begin_nested():
                await on_commit(lambda: calls.append("rolled-back"))
                raise _Boom
        async with session.begin_nested():
            await on_commit(lambda: calls.append("released"))
    assert calls == ["outer", "released"]


@pytest.mark.asyncio
async def test_failing_callback_does_not_fail_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[str] = []

    def _fail() -> None:
        raise RuntimeError("cache down")

    async with _uow(session_factory) as session:
        await session.execute(text("SELECT 1"))
        await on_commit(_fail)
        await on_commit(lambda: calls.append("after"))
    assert calls == ["after"]


@pytest.mark.asyncio
async def test_outside_uow_runs_immediately() -> None:
    calls: list[str] = []
    await on_commit(lambda: calls.append("now"))
    assert calls == ["now"]
