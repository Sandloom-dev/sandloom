"""Budget: load above ``pool_size`` does not churn PostgreSQL connections.

Stock QueuePool closes an overflow connection on checkin as soon as
``pool_size`` others are idle and opens a fresh one on the next shortage, so
repeated bursts above ``pool_size`` reconnected every time (TCP + TLS + SCRAM +
session setup on the event loop — ~15 ms CPU each with asyncpg 0.30; the root
cause of the uvicorn ``--loop uvloop`` S3/S4 CPU anomaly in CI). With the
driver's LingeringQueuePool, N bursts of ``pool_size + max_overflow``
concurrent sessions open exactly ``pool_size + max_overflow`` connections.

Deterministic: connections counted with the pool ``connect`` event; bursts are
synchronised with an asyncio barrier (every session holds its connection at
the same time), no timing assertions.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from sandloom.core.config.settings import settings
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.testing.workers import worker_env_values

POOL_SIZE = 2
MAX_OVERFLOW = 3
BURSTS = 4


def _database_url() -> str:
    env = worker_env_values(Path(__file__).resolve().parents[2] / ".env.test")
    url = os.environ.get("DATABASE_URL") or env.get("DATABASE_URL")
    if not url or not url.startswith("postgresql+asyncpg"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    return url


async def _hold(engine: AsyncEngine, barrier: asyncio.Barrier) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        await barrier.wait()  # every session of the burst holds a connection


async def _bursts(engine: AsyncEngine, width: int) -> None:
    for _ in range(BURSTS):
        barrier = asyncio.Barrier(width)
        await asyncio.gather(*(_hold(engine, barrier) for _ in range(width)))


async def _connects(linger: float, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(settings, "db_pool_overflow_linger_seconds", linger)
    engine = driver._pooled_engine(
        _database_url(), pool_size=POOL_SIZE, max_overflow=MAX_OVERFLOW
    )
    opened = 0

    def _count(*_: object) -> None:
        nonlocal opened
        opened += 1

    event.listen(engine.sync_engine, "connect", _count)
    try:
        await _bursts(engine, POOL_SIZE + MAX_OVERFLOW)
    finally:
        await engine.dispose()
    return opened


async def test_bursts_above_pool_size_open_each_connection_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _connects(30.0, monkeypatch) <= POOL_SIZE + MAX_OVERFLOW


async def test_stock_pool_behaviour_churns_for_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DB_POOL_OVERFLOW_LINGER_SECONDS=0`` = stock: every burst reconnects."""
    assert await _connects(0.0, monkeypatch) == (
        POOL_SIZE + MAX_OVERFLOW + (BURSTS - 1) * MAX_OVERFLOW
    )
