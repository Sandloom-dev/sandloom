"""Idle-aware checkout ping + server-side timeouts of the database driver.

``pool_pre_ping`` cost three round trips (BEGIN / ``;`` / ROLLBACK) on EVERY
checkout. The driver now pings only a connection that idled at least
``DB_POOL_PING_IDLE_SECONDS`` — one round trip, no transaction — and a failed
ping reconnects transparently.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from sandloom.core.config.settings import settings
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.testing import reload_test_env


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url or not url.startswith("postgresql+asyncpg"):
        pytest.skip("needs the PostgreSQL test DATABASE_URL")
    return url


@pytest_asyncio.fixture
async def admin() -> AsyncIterator[Any]:
    engine = driver.build_disposable_engine(_url())
    yield engine
    await engine.dispose()


async def _provider(monkeypatch: pytest.MonkeyPatch, idle: float) -> Any:
    monkeypatch.setattr(settings, "db_pool_ping_idle_seconds", idle)
    return driver.create_sqlalchemy_database(
        InstanceConfig("database", "pingtest", "sqlalchemy", {"url": _url()})
    )


async def _pid(engine: Any) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _terminate(admin: Any, pid: int) -> None:
    async with admin.connect() as conn:
        await conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        await conn.commit()
    for _ in range(50):  # wait until the backend is really gone
        async with admin.connect() as conn:
            alive = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_stat_activity WHERE pid = :p"),
                    {"p": pid},
                )
            ).scalar_one()
        if not alive:
            return
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_killed_idle_connection_is_transparently_replaced(
    monkeypatch: pytest.MonkeyPatch, admin: Any
) -> None:
    provider = await _provider(monkeypatch, idle=0.2)
    try:
        engine = provider.engine
        assert engine.pool._pre_ping is False
        first = await _pid(engine)
        await _terminate(admin, first)
        await asyncio.sleep(0.3)  # idle past the ping threshold
        # The checkout pings the dead connection, the pool reconnects, and the
        # caller never sees the failure.
        second = await _pid(engine)
        assert second != first
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("idle", "expected_pings"), [(3600, 0), (0, 2)])
async def test_ping_only_after_idle_threshold(
    monkeypatch: pytest.MonkeyPatch, idle: float, expected_pings: int
) -> None:
    pings: list[Any] = []
    real_ping = driver._ping

    def _counting(dbapi_connection: Any, dialect: Any) -> None:
        pings.append(dbapi_connection)
        real_ping(dbapi_connection, dialect)

    monkeypatch.setattr(driver, "_ping", _counting)
    provider = await _provider(monkeypatch, idle=idle)
    try:
        engine = provider.engine
        first = await _pid(engine)  # fresh connection: never pinged
        assert await _pid(engine) == first  # LIFO: the hot connection again
        assert await _pid(engine) == first
        assert len(pings) == expected_pings
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_server_side_timeouts_ride_the_startup_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "db_statement_timeout_ms", 12_345)
    monkeypatch.setattr(settings, "db_idle_in_tx_timeout_ms", 23_456)
    monkeypatch.setattr(settings, "contract_db_lock_timeout_ms", 3_456)
    provider = await _provider(monkeypatch, idle=5)
    try:
        async with provider.engine.connect() as conn:
            stmt = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
            idle = (
                await conn.execute(text("SHOW idle_in_transaction_session_timeout"))
            ).scalar_one()
        assert stmt == "12345ms"
        assert idle == "23456ms"
        provider.contract_uow_factory()
        contract = provider._contract_engine
        assert contract is not None
        async with contract.connect() as conn:
            lock = (await conn.execute(text("SHOW lock_timeout"))).scalar_one()
            stmt = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
        assert lock == "3456ms"
        assert stmt == "12345ms"
    finally:
        await provider.aclose()


def test_timeouts_off_send_no_server_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "db_statement_timeout_ms", 0)
    monkeypatch.setattr(settings, "db_idle_in_tx_timeout_ms", 0)
    assert driver._server_settings("postgresql+asyncpg://u@h/db") == {}
    assert driver._server_settings("sqlite+aiosqlite://") == {}


def test_uow_factories_are_stable_per_provider() -> None:
    provider = driver.create_sqlalchemy_database(
        InstanceConfig(
            "database", "stable", "sqlalchemy", {"url": "postgresql+asyncpg://u@h/db"}
        )
    )
    assert provider.uow_factory() is provider.uow_factory()
    assert provider.contract_uow_factory() is provider.contract_uow_factory()
