"""The ORM tenant guard's criterion is built ONCE; the tenant VALUE is bound per
execution from the current context (real PostgreSQL, ORM guard only — no RLS).

The superuser test role ignores RLS, so every filtering observed here is the
ORM guard's ``tenant_id = :sandloom_tenant_id__`` criterion.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import String, event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core.di import bind_tenant, unscoped
from sandloom.core.tenant import (
    TENANT_CRITERIA_PARAM,
    MissingTenantError,
    TenantScoped,
    _tenant_criteria_value,
)
from sandloom.core.testing import reload_test_env

pytestmark = pytest.mark.asyncio

A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_TABLE = f"test_tcrit_{uuid.uuid4().hex[:8]}"


class _Base(DeclarativeBase):
    pass


class Note(TenantScoped, _Base):
    __tablename__ = _TABLE
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    eng = create_async_engine(url, pool_size=5, max_overflow=0)
    async with eng.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
        await conn.execute(
            text(
                f"INSERT INTO {_TABLE} (id, tenant_id, name) VALUES "
                f"(1, '{A}', 'a1'), (2, '{A}', 'a2'), (3, '{B}', 'b1')"
            )
        )
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await eng.dispose()


async def _names(session: Any) -> list[str]:
    return list((await session.scalars(select(Note.name).order_by(Note.id))).all())


async def test_concurrent_tasks_never_see_each_others_rows(engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    expected = {A: ["a1", "a2"], B: ["b1"]}
    rng = random.Random(7)

    async def request(tenant: uuid.UUID) -> None:
        with bind_tenant(tenant):
            async with sessions() as s, s.begin():
                await asyncio.sleep(0)  # interleave with the other tenant
                assert await _names(s) == expected[tenant]
                await asyncio.sleep(0)
                got = await s.get(Note, 3)
                assert (got is not None) == (tenant == B)
                await asyncio.sleep(0)
                assert await _names(s) == expected[tenant]

    for _ in range(25):
        await asyncio.gather(*(request(rng.choice((A, B))) for _ in range(16)))


async def test_previous_requests_tenant_never_leaks_on_one_connection() -> None:
    url = os.environ["DATABASE_URL"]
    eng = create_async_engine(url, pool_size=1, max_overflow=0)
    try:
        async with eng.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
            await conn.execute(text(f"DELETE FROM {_TABLE}"))
            await conn.execute(
                text(
                    f"INSERT INTO {_TABLE} (id, tenant_id, name) "
                    f"VALUES (1, '{A}', 'a1'), (3, '{B}', 'b1')"
                )
            )
        sessions = async_sessionmaker(eng, expire_on_commit=False)
        for i in range(40):
            tenant = (A, B)[i % 2]
            with bind_tenant(tenant):
                async with sessions() as s, s.begin():
                    assert await _names(s) == (["a1"] if tenant == A else ["b1"])
        async with sessions() as s, s.begin():
            with pytest.raises(MissingTenantError):
                await _names(s)
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await eng.dispose()


async def test_switch_mid_session_applies_immediately(engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s, s.begin():
        with bind_tenant(A):
            assert await _names(s) == ["a1", "a2"]
            with bind_tenant(B):
                assert await _names(s) == ["b1"]
                with unscoped():
                    assert await _names(s) == ["a1", "a2", "b1"]
                assert await _names(s) == ["b1"]
            assert await _names(s) == ["a1", "a2"]
        with pytest.raises(MissingTenantError):
            await _names(s)


async def test_value_is_read_at_execution_not_when_the_statement_was_built(
    engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    with bind_tenant(A):
        stmt = select(Note.name).order_by(Note.id)  # built under A
    with bind_tenant(B):
        async with sessions() as s, s.begin():
            assert list((await s.scalars(stmt)).all()) == ["b1"]


async def test_compiled_cache_does_not_capture_the_tenant(engine: AsyncEngine) -> None:
    sent: list[Any] = []

    def _capture(_c: Any, _cur: Any, statement: str, params: Any, *_a: Any) -> None:
        if _TABLE in statement and TENANT_CRITERIA_PARAM not in statement:
            sent.append(params)

    # A SEPARATE engine for the capture listener (events would slow the
    # fixture's engine; the guard itself installs none).
    probe = create_async_engine(os.environ["DATABASE_URL"], pool_size=1)
    event.listen(probe.sync_engine, "before_cursor_execute", _capture)
    sessions = async_sessionmaker(probe, expire_on_commit=False)
    cache = probe.sync_engine._compiled_cache
    assert cache is not None
    try:
        with bind_tenant(A):
            async with sessions() as s, s.begin():
                await _names(s)
        size_after_a = len(cache)
        with bind_tenant(B):
            async with sessions() as s, s.begin():
                await _names(s)
        assert len(cache) == size_after_a  # B reused A's compiled statement
        for key in list(cache.keys()):
            rendered = repr(key)
            assert str(A) not in rendered and str(B) not in rendered
            assert repr(A) not in rendered and repr(B) not in rendered
        # each execution carried its OWN tenant value
        assert [p[-1] for p in sent[-2:]] == [A, B], sent[-2:]
    finally:
        await probe.dispose()


async def test_no_tenant_fails_closed(engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        with pytest.raises(MissingTenantError):
            await _names(s)
        with pytest.raises(MissingTenantError):
            await s.get(Note, 1)
    with pytest.raises(MissingTenantError):
        _tenant_criteria_value()  # the per-execution value source itself
