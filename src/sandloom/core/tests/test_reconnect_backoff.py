"""Recovery admission is bounded, jittered, cancellation-safe and never replays SQL."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.util import greenlet_spawn
from sqlalchemy.util.concurrency import await_

from sandloom.core.exceptions import DatabaseUnavailable
from sandloom.core.sql_alchemy import reconnect


async def test_exponential_jitter_is_capped_and_resets_after_success(monkeypatch):
    now = 0.0
    delays, ranges = [], []
    failing = True
    attempts = 0

    async def sleep(seconds):
        nonlocal now
        delays.append(seconds)
        now += seconds

    def jitter(low, high):
        ranges.append((low, high))
        return (low + high) / 2

    def connect():
        nonlocal attempts
        attempts += 1
        if failing:
            raise ConnectionRefusedError("database restarting")
        return "connection"

    monkeypatch.setattr(reconnect.asyncio, "sleep", sleep)
    gate = reconnect.ReconnectBackoff(
        base=0.1, cap=0.4, wait_timeout=2, clock=lambda: now, jitter=jitter
    )
    dialect = SimpleNamespace(connect=connect)
    for _ in range(4):
        with pytest.raises(DatabaseUnavailable):
            await greenlet_spawn(gate.connect, dialect, None, [], {})
    assert attempts == 4  # one establishment attempt per caller, no retries
    assert ranges == [(0.05, 0.1), (0.1, 0.2), (0.2, 0.4), (0.2, 0.4)]
    assert delays == pytest.approx([0.075, 0.15, 0.3])
    failing = False
    assert await greenlet_spawn(gate.connect, dialect, None, [], {}) == "connection"
    failing = True
    with pytest.raises(DatabaseUnavailable):
        await greenlet_spawn(gate.connect, dialect, None, [], {})
    assert ranges[-1] == (0.05, 0.1)


async def test_many_waiters_share_one_recovery_probe():
    active = peak = attempts = 0

    async def attempt():
        nonlocal active, peak, attempts
        active += 1
        peak = max(active, peak)
        attempts += 1
        try:
            await asyncio.sleep(0.001)
            raise ConnectionRefusedError("database restarting")
        finally:
            active -= 1

    dialect = SimpleNamespace(connect=lambda: await_(attempt()))
    gate = reconnect.ReconnectBackoff(base=0.001, cap=0.004, wait_timeout=2)
    with pytest.raises(DatabaseUnavailable):
        await greenlet_spawn(gate.connect, dialect, None, [], {})
    results = await asyncio.gather(
        *(greenlet_spawn(gate.connect, dialect, None, [], {}) for _ in range(32)),
        return_exceptions=True,
    )
    assert all(isinstance(e, DatabaseUnavailable) for e in results)
    assert peak == 1 and attempts == 33


async def test_cancelled_probe_releases_admission_and_wait_is_bounded():
    gate = reconnect.ReconnectBackoff(base=0.01, cap=0.01, wait_timeout=0.03)
    failing = SimpleNamespace(
        connect=lambda: (_ for _ in ()).throw(ConnectionRefusedError())
    )
    with pytest.raises(DatabaseUnavailable):
        await greenlet_spawn(gate.connect, failing, None, [], {})
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    blocked_dialect = SimpleNamespace(connect=lambda: await_(blocked()))
    probe = asyncio.create_task(
        greenlet_spawn(gate.connect, blocked_dialect, None, [], {})
    )
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(DatabaseUnavailable):
        await greenlet_spawn(gate.connect, failing, None, [], {})
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    assert (
        await greenlet_spawn(
            gate.connect, SimpleNamespace(connect=lambda: "ok"), None, [], {}
        )
        == "ok"
    )


async def test_programming_and_authentication_errors_keep_their_type():
    gate = reconnect.ReconnectBackoff(base=0.01, cap=0.01, wait_timeout=1)
    dialect = SimpleNamespace(
        connect=lambda: (_ for _ in ()).throw(ValueError("bad config"))
    )
    with pytest.raises(ValueError, match="bad config"):
        await greenlet_spawn(gate.connect, dialect, None, [], {})


async def test_healthy_connection_creation_remains_concurrent():
    active = peak = 0

    async def attempt():
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.01)
        active -= 1
        return "connection"

    gate = reconnect.ReconnectBackoff(base=0.01, cap=1, wait_timeout=1)
    dialect = SimpleNamespace(connect=lambda: await_(attempt()))
    assert (
        await asyncio.gather(
            *(greenlet_spawn(gate.connect, dialect, None, [], {}) for _ in range(4))
        )
        == ["connection"] * 4
    )
    assert peak == 4


async def test_invalidation_staggers_reconnection_even_if_database_is_already_back(
    monkeypatch,
):
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(reconnect.asyncio, "sleep", sleep)
    gate = reconnect.ReconnectBackoff(
        base=0.1,
        cap=1,
        wait_timeout=1,
        clock=lambda: 0,
        jitter=lambda low, high: high,
    )
    gate.invalidated(None, None, None)
    assert (
        await greenlet_spawn(
            gate.connect, SimpleNamespace(connect=lambda: "ok"), None, [], {}
        )
        == "ok"
    )
    assert delays == [0.1]


async def test_real_driver_installs_backoff_and_classifies_refused_connections(
    unused_tcp_port,
):
    from sandloom.core.drivers.sqlalchemy_database import _pooled_engine

    engine = _pooled_engine(
        f"postgresql+asyncpg://unused:unused@127.0.0.1:{unused_tcp_port}/unused",
        pool_size=1,
        max_overflow=0,
    )
    try:
        for _ in range(2):
            with pytest.raises(DatabaseUnavailable):
                async with engine.connect():
                    pytest.fail(
                        "the deliberately closed test port must refuse a connection"
                    )
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


def test_pool_recycle_and_soft_invalidation_do_not_count_as_failures():
    # GLM review #5 claimed "normal pool recycle" grows the backoff. It does
    # not: SQLAlchemy recycles (pool_recycle, soft / pool-wide invalidation)
    # by closing the record, without the hard "invalidate" event the gate
    # listens to. Only a hard invalidation (dead connection, failed ping,
    # cancelled planner lease) is a recovery boundary — by design, see
    # test_invalidation_staggers_reconnection_even_if_database_is_already_back.
    from unittest.mock import MagicMock

    from sqlalchemy import event
    from sqlalchemy.pool import QueuePool

    gate = reconnect.ReconnectBackoff(base=0.1, cap=1, wait_timeout=1)
    pool = QueuePool(MagicMock, pool_size=1, max_overflow=0, recycle=3600)
    event.listen(pool, "invalidate", gate.invalidated)

    fairy = pool.connect()
    record = fairy._connection_record
    fairy.close()
    record.starttime -= 7200  # exceeded pool_recycle
    pool.connect().close()  # recycled on checkout
    fairy = pool.connect()
    fairy.invalidate(soft=True)
    fairy.close()
    pool.connect().close()  # soft-invalidated record recycled
    pool._invalidate_time = record.starttime + 10**9  # pool-wide invalidation
    pool.connect().close()
    assert gate._failures == 0

    fairy = pool.connect()
    fairy.invalidate(ConnectionResetError("server closed the connection"))
    fairy.close()
    assert gate._failures == 1
