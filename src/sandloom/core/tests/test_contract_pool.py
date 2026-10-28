"""Monolith contract calls use a DEDICATED pool (no request-pool starvation).

A request holding its connection that calls an in-process contract (or mints
a token, which runs claims contributors) used to take a SECOND connection
from the same pool: with ``pool_size=1`` that deadlocked until the pool
timeout. Contract UoWs are now served from their own pool, and their
connections carry ``lock_timeout`` so waiting on a row lock held by the caller
fails fast.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import Table, text

from sandloom.core.drivers.sqlalchemy_database import (
    SQLAlchemyDatabaseProvider,
    create_sqlalchemy_database,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.module_api import contract_method, contract_session
from sandloom.core.orm import Base
from sandloom.core.sql_alchemy.models.contract_command import ContractCommandRecord
from sandloom.core.testing.workers import worker_env_values


def _database_url() -> str:
    env = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    url = os.environ.get("DATABASE_URL") or env.get("DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    return url


class Probe:
    @classmethod
    @contract_method
    async def one(cls) -> int:
        return int((await contract_session().execute(text("SELECT 1"))).scalar_one())

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def touch(cls, operation_key: str) -> int:
        await contract_session().execute(
            text("UPDATE sandloom_contract_lock_probe SET n = n + 1 WHERE id = 1")
        )
        return 1


@pytest_asyncio.fixture
async def provider(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[SQLAlchemyDatabaseProvider]:
    from sandloom.core.config.settings import settings

    url = _database_url()
    # The request pool holds exactly ONE connection and fails fast.
    monkeypatch.setattr(settings, "db_pool_size", 1)
    monkeypatch.setattr(settings, "db_max_overflow", 0)
    monkeypatch.setattr(settings, "db_pool_timeout", 2)
    monkeypatch.setattr(settings, "contract_db_lock_timeout_ms", 300)
    db = create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="pool_probe",
            driver="sqlalchemy",
            options={"url": url},
        )
    )
    try:
        yield db
    finally:
        await db.aclose()


def _core(db: SQLAlchemyDatabaseProvider) -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": db  # type: ignore[method-assign]
    core.register_service("demo.pool.v1", Probe)
    return core


@pytest.mark.asyncio
async def test_nested_contract_call_with_request_pool_size_one_succeeds(
    provider: SQLAlchemyDatabaseProvider,
) -> None:
    core = _core(provider)
    request_uow = core.get_uow_factory("default")()
    async with request_uow:
        # The request holds the ONLY request-pool connection...
        await request_uow.session.execute(text("SELECT 1"))  # type: ignore[union-attr]
        # ...and its nested contract call still gets one (own pool).
        assert await core.get_service("demo.pool.v1").one() == 1


@pytest.mark.asyncio
async def test_without_the_contract_pool_the_nested_call_starves(
    provider: SQLAlchemyDatabaseProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Control: route contract UoWs through the request pool again -> the
    # nested call cannot get a connection (the defect this pool fixes).
    monkeypatch.setattr(provider, "contract_uow_factory", provider.uow_factory)
    core = _core(provider)
    request_uow = core.get_uow_factory("default")()
    async with request_uow:
        await request_uow.session.execute(text("SELECT 1"))  # type: ignore[union-attr]
        with pytest.raises(Exception, match="QueuePool limit|timed out"):
            await core.get_service("demo.pool.v1").one()


@pytest.mark.asyncio
async def test_contract_waiting_on_callers_row_lock_fails_fast(
    provider: SQLAlchemyDatabaseProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.idempotency import cache_store as idempotency_cache_store
    from sandloom.core.testing import InMemoryCache

    mem = InMemoryCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": mem)
    async with provider.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS sandloom_contract_lock_probe "
                "(id int PRIMARY KEY, n int NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO sandloom_contract_lock_probe VALUES (1, 0) "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        # The command claims its key in the provider's durable command log
        # (core chain) before the UPDATE: on a fresh database that table must
        # exist, or the call fails on it instead of on the row lock.
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[cast(Table, ContractCommandRecord.__table__)],
        )
    core = _core(provider)
    try:
        request_uow = core.get_uow_factory("default")()
        async with request_uow:
            await request_uow.session.execute(  # type: ignore[union-attr]
                text(
                    "SELECT n FROM sandloom_contract_lock_probe WHERE id = 1 FOR UPDATE"
                )
            )
            started = time.monotonic()
            with pytest.raises(Exception, match="lock"):
                await core.get_service("demo.pool.v1").touch("op-lock")
            assert time.monotonic() - started < 5
    finally:
        async with provider.engine.begin() as conn:
            await conn.execute(
                text("DROP TABLE IF EXISTS sandloom_contract_lock_probe")
            )


async def test_default_contract_engine_honours_a_later_tenant_rls_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GLM review #7: the process-wide default contract engine is built once;
    a later registry (another create_app in the same process) that enables
    tenant_rls must not get the earlier RLS-less engine — mirrors the main
    default engine, on which every factory call installs the hook."""
    from sandloom.core.drivers import sqlalchemy_database as drv
    from sandloom.core.sql_alchemy.tenant_rls import is_tenant_rls_installed

    monkeypatch.setattr(drv, "_default_contract_engine", None)
    engine = drv.get_default_contract_engine(tenant_rls=False)
    try:
        assert engine is not None
        assert not is_tenant_rls_installed(engine)
        assert drv.get_default_contract_engine(tenant_rls=True) is engine
        assert is_tenant_rls_installed(engine)
    finally:
        await engine.dispose()
