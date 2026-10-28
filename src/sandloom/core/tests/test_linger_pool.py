"""LingeringQueuePool: overflow connections linger instead of churning.

Regression for the uvicorn ``--loop uvloop`` S3/S4 CPU anomaly (Linux CI,
asyncpg 0.30): stock QueuePool closes an overflow connection on checkin as
soon as ``pool_size`` others sit idle, and the next checkout that finds the
idle queue empty opens a new one — so load hovering above ``pool_size``
reconnected ~0.1x per request, each connect ~15 ms of event-loop CPU.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy
from sqlalchemy import exc
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.util import greenlet_spawn

from sandloom.core.config.settings import settings
from sandloom.core.drivers import linger_pool
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.drivers.linger_pool import LingeringQueuePool

POOL_SIZE = 2
MAX_OVERFLOW = 3
TOTAL = POOL_SIZE + MAX_OVERFLOW

#: SQLAlchemy 2.1.5 made the stock ``AsyncAdaptedQueue.get`` take an available
#: item with ``get_nowait()`` before ``wait_for`` (the finding below, adopted
#: upstream); 2.1.4 paid a ``wait_for`` for every checkout at max overflow.
_SA_VERSION = tuple(int(p) for p in sqlalchemy.__version__.split(".")[:3])
STOCK_GET_WAITS = _SA_VERSION < (2, 1, 5)


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _Creator:
    def __init__(self) -> None:
        self.made: list[_FakeConn] = []

    def __call__(self) -> _FakeConn:
        conn = _FakeConn()
        self.made.append(conn)
        return conn

    @property
    def open(self) -> int:
        return sum(not c.closed for c in self.made)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(linger_pool, "_clock", c)
    return c


def _pool(cls: type[AsyncAdaptedQueuePool], creator: _Creator, **kw: Any) -> Any:
    return cls(
        creator,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        use_lifo=True,
        timeout=0.05,
        **kw,
    )


def _wave(pool: Any, n: int = TOTAL) -> None:
    """``n`` concurrent checkouts, then all returned (a load burst)."""
    held = [pool.connect() for _ in range(n)]
    for c in held:
        c.close()


async def test_stock_queuepool_churns_overflow_connections() -> None:
    """The faulty condition, pinned: every burst above pool_size reconnects."""
    creator = _Creator()
    pool = _pool(AsyncAdaptedQueuePool, creator)
    for _ in range(3):
        await greenlet_spawn(_wave, pool)
    assert len(creator.made) == TOTAL + 2 * MAX_OVERFLOW


async def test_overflow_connections_linger_across_bursts(clock: _Clock) -> None:
    creator = _Creator()
    pool = _pool(LingeringQueuePool, creator, overflow_linger_seconds=30)
    for _ in range(5):
        await greenlet_spawn(_wave, pool)
        clock.now += 1
    assert len(creator.made) == TOTAL  # no connect/close churn
    assert creator.open == TOTAL
    assert pool.checkedin() == TOTAL
    assert pool.checkedout() == 0
    assert pool.size() == POOL_SIZE


async def test_shrinks_back_to_pool_size_after_quiet_window(clock: _Clock) -> None:
    creator = _Creator()
    pool = _pool(LingeringQueuePool, creator, overflow_linger_seconds=30)
    await greenlet_spawn(_wave, pool)
    clock.now += 31  # demand stayed within pool_size for the whole window
    for _ in range(TOTAL):
        await greenlet_spawn(_wave, pool, 1)
    assert creator.open == POOL_SIZE
    assert pool.checkedin() == POOL_SIZE
    assert pool.checkedout() == 0
    assert pool.overflow() == 0


async def test_total_connection_bound_is_unchanged(clock: _Clock) -> None:
    creator = _Creator()
    pool = _pool(LingeringQueuePool, creator, overflow_linger_seconds=30)

    def _over_bound() -> None:
        held = [pool.connect() for _ in range(TOTAL)]
        assert pool.checkedout() == TOTAL
        with pytest.raises(exc.TimeoutError):
            pool.connect()
        for c in held:
            c.close()

    await greenlet_spawn(_over_bound)
    assert len(creator.made) == TOTAL


async def test_zero_linger_is_stock_behaviour() -> None:
    creator = _Creator()
    pool = _pool(LingeringQueuePool, creator, overflow_linger_seconds=0)
    for _ in range(3):
        await greenlet_spawn(_wave, pool)
    assert len(creator.made) == TOTAL + 2 * MAX_OVERFLOW
    assert pool.checkedin() == POOL_SIZE


async def test_recreate_and_dispose_keep_the_configuration(clock: _Clock) -> None:
    creator = _Creator()
    pool = _pool(LingeringQueuePool, creator, overflow_linger_seconds=12)
    await greenlet_spawn(_wave, pool)
    pool.dispose()
    assert creator.open == 0
    assert pool.checkedout() == 0
    fresh = pool.recreate()
    assert isinstance(fresh, LingeringQueuePool)
    assert (fresh.size(), fresh._max_overflow, fresh._linger) == (
        POOL_SIZE,
        MAX_OVERFLOW,
        12.0,
    )


async def test_driver_uses_the_lingering_pool_for_server_databases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "db_pool_overflow_linger_seconds", 7.0)
    monkeypatch.setattr(settings, "db_pool_fair_barge_ms", 12.0)
    engine = driver._pooled_engine(
        "postgresql+asyncpg://u:p@127.0.0.1:1/x", pool_size=4, max_overflow=6
    )
    try:
        pool = engine.sync_engine.pool
        assert isinstance(pool, LingeringQueuePool)
        assert (pool.size(), pool._max_overflow, pool._linger) == (4, 6, 7.0)
        assert pool._barge_bound == pytest.approx(0.012)
    finally:
        await engine.dispose()


# -- non-blocking checkout at max overflow ------------------------------------


class _WaitForSpy:
    """Counts ``asyncio.wait_for`` — the blocking checkout path."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = asyncio.wait_for

        def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(asyncio, "wait_for", spy)


def _at_max_overflow(pool: Any) -> None:
    """One burst: every connection open and idle, overflow at its max."""
    _wave(pool)
    assert pool.overflow() == MAX_OVERFLOW
    assert pool.checkedin() == TOTAL


def _sequential_checkouts(pool: Any, n: int) -> list[Any]:
    seen = []
    for _ in range(n):
        c = pool.connect()
        seen.append(c.dbapi_connection)
        c.close()
    return seen


async def test_stock_pool_blocks_on_wait_for_at_max_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding pinned: an idle connection still costs a wait_for (< 2.1.5)."""
    creator = _Creator()
    pool = _pool(AsyncAdaptedQueuePool, creator)
    pool._pool = pool._queue_class(TOTAL, use_lifo=True)  # keep all idle

    def _run() -> None:
        _at_max_overflow(pool)
        spy = _WaitForSpy(monkeypatch)
        _sequential_checkouts(pool, 4)
        assert spy.calls == (4 if STOCK_GET_WAITS else 0)

    await greenlet_spawn(_run)


async def test_idle_connection_at_max_overflow_takes_the_fast_path(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator = _Creator()
    pool = _pool(LingeringQueuePool, creator, overflow_linger_seconds=30)

    def _run() -> None:
        _at_max_overflow(pool)
        spy = _WaitForSpy(monkeypatch)
        seen = _sequential_checkouts(pool, 4)
        held = [pool.connect() for _ in range(TOTAL)]  # drain it all, fast
        assert spy.calls == 0
        assert pool.checkedout() == TOTAL
        for c in held:
            c.close()
        # LIFO preserved: the hottest connection is reused every time.
        assert len(set(map(id, seen))) == 1

    await greenlet_spawn(_run)
    assert len(creator.made) == TOTAL  # nothing opened
    assert pool.overflow() == MAX_OVERFLOW
    assert pool.checkedin() == TOTAL
    assert pool.checkedout() == 0


async def test_exhausted_pool_still_times_out(clock: _Clock) -> None:
    creator = _Creator()
    pool = _pool(LingeringQueuePool, creator, overflow_linger_seconds=30)

    def _run() -> None:
        held = [pool.connect() for _ in range(TOTAL)]
        with pytest.raises(exc.TimeoutError, match="connection timed out"):
            pool.connect()
        assert not pool._waiters
        for c in held:
            c.close()

    await greenlet_spawn(_run)
    assert len(creator.made) == TOTAL
    assert pool.overflow() == MAX_OVERFLOW
    assert pool.checkedout() == 0


async def test_a_waiter_still_gets_the_connection_released_to_it(
    clock: _Clock,
) -> None:
    """Contention: a checkout blocked on an exhausted pool is served by the
    next return (the blocking path is untouched)."""
    creator = _Creator()
    pool = LingeringQueuePool(
        creator,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        use_lifo=True,
        timeout=5,
        overflow_linger_seconds=30,
    )
    held = await greenlet_spawn(lambda: [pool.connect() for _ in range(TOTAL)])
    waiter = asyncio.ensure_future(greenlet_spawn(pool.connect))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    last = held.pop()
    released = last.dbapi_connection
    await greenlet_spawn(last.close)
    got = await asyncio.wait_for(waiter, 5)
    assert got.dbapi_connection is released
    assert pool.checkedout() == TOTAL

    def _release_all() -> None:
        got.close()
        for c in held:
            c.close()

    await greenlet_spawn(_release_all)
    assert len(creator.made) == TOTAL
    assert pool.overflow() == MAX_OVERFLOW


@pytest.mark.parametrize("fast_get", [True, False])
async def test_new_arrival_cannot_overtake_a_blocked_checkout(fast_get: bool) -> None:
    """A returning caller must not steal the connection promised to a waiter.

    Queue.get_nowait (and Queue.get on a nonempty queue) can both overtake
    the woken waiter before its task resumes. Repeated arrivals starve it.
    """
    pool = LingeringQueuePool(
        _Creator(),
        pool_size=1,
        max_overflow=0,
        timeout=1,
        overflow_linger_seconds=30,
    )
    pool._fast_get = fast_get
    held = await greenlet_spawn(pool.connect)
    order = []

    async def checkout(name: str) -> None:
        conn = await greenlet_spawn(pool.connect)
        order.append(name)
        await greenlet_spawn(conn.close)

    waiting = asyncio.create_task(checkout("waiting"))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    await greenlet_spawn(held.close)
    await checkout("new")
    await waiting
    assert order == ["waiting", "new"]
    assert pool.checkedout() == 0
    pool.dispose()


async def test_contended_checkouts_keep_one_timeout_and_survive_cancellation() -> None:
    # Margins are wide for loaded CI: the waiter must fail within ONE pool
    # timeout (0.2 s) plus slack, well before a second full timeout (0.4 s).
    pool = LingeringQueuePool(_Creator(), pool_size=1, max_overflow=0, timeout=0.2)
    held = await greenlet_spawn(pool.connect)
    first = asyncio.create_task(greenlet_spawn(pool.connect))
    second = asyncio.create_task(greenlet_spawn(pool.connect))
    await asyncio.sleep(0.01)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # Includes time spent queued at the fair gate, not a second full timeout.
    with pytest.raises(exc.TimeoutError, match="connection timed out"):
        await asyncio.wait_for(second, 0.35)
    assert not pool._waiters
    await greenlet_spawn(held.close)
    recovered = await greenlet_spawn(pool.connect)
    await greenlet_spawn(recovered.close)
    assert pool.checkedout() == 0
    pool.dispose()


async def test_overflow_accounting_across_bursts_and_fast_checkouts(
    clock: _Clock,
) -> None:
    creator = _Creator()
    pool = _pool(LingeringQueuePool, creator, overflow_linger_seconds=30)

    def _mixed() -> None:
        for n in (TOTAL, 1, 3, TOTAL, 2):
            _wave(pool, n)
            assert pool.checkedin() + pool.checkedout() == TOTAL
            assert pool.overflow() == MAX_OVERFLOW

    await greenlet_spawn(_mixed)
    assert len(creator.made) == TOTAL
    clock.now += 31  # quiet window: fast checkouts shrink it like stock
    await greenlet_spawn(_sequential_checkouts, pool, TOTAL)
    assert pool.overflow() == 0
    assert creator.open == POOL_SIZE
    assert pool.checkedin() == POOL_SIZE


async def test_invalidated_connection_is_replaced_on_the_fast_path(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator = _Creator()
    pool = _pool(LingeringQueuePool, creator, overflow_linger_seconds=30)

    def _run() -> None:
        _at_max_overflow(pool)
        spy = _WaitForSpy(monkeypatch)
        c = pool.connect()
        dead = c.dbapi_connection
        c.invalidate()
        c.close()
        assert dead.closed
        fresh = pool.connect()  # same record, reconnected at checkout
        assert fresh.dbapi_connection is not dead
        fresh.close()
        assert spy.calls == 0

    await greenlet_spawn(_run)
    assert len(creator.made) == TOTAL + 1
    assert creator.open == TOTAL
    assert pool.overflow() == MAX_OVERFLOW
    assert pool.checkedout() == 0


async def test_failed_probe_falls_back_to_the_stock_checkout(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linger_pool, "pool_fast_checkout_ok", lambda: False)
    creator = _Creator()
    pool = _pool(LingeringQueuePool, creator, overflow_linger_seconds=30)
    assert pool.recreate()._fast_get is False

    def _run() -> None:
        _at_max_overflow(pool)
        spy = _WaitForSpy(monkeypatch)
        _sequential_checkouts(pool, 3)
        assert spy.calls == (3 if STOCK_GET_WAITS else 0)

    await greenlet_spawn(_run)
    assert len(creator.made) == TOTAL
    assert pool.overflow() == MAX_OVERFLOW


# -- fair handoff: no convoy, no leaks ----------------------------------------


async def test_released_connections_serve_every_waiter_in_one_iteration() -> None:
    """Regression (Oracle 2026-10-22 write tail): connections returned
    together reach their waiters together, in FIFO order.

    The first fairness fix held an asyncio.Lock across the queue wait, so
    each waiter needed its own loop iterations after the previous one: on a
    CPU-saturated loop, waiters sat at the lock while connections sat idle.
    """
    size = 4
    pool = LingeringQueuePool(
        _Creator(),
        pool_size=size,
        max_overflow=0,
        timeout=5,
        overflow_linger_seconds=30,
    )
    held = await greenlet_spawn(lambda: [pool.connect() for _ in range(size)])
    served: list[int] = []

    async def checkout(n: int) -> Any:
        conn = await greenlet_spawn(pool.connect)
        served.append(n)
        return conn

    waiters = [asyncio.create_task(checkout(n)) for n in range(size)]
    await asyncio.sleep(0.01)
    assert not any(w.done() for w in waiters)

    def _release_all() -> None:
        for c in held:
            c.close()

    await greenlet_spawn(_release_all)
    assert pool.checkedin() == 0  # handed over, nothing parked in the queue
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert all(w.done() for w in waiters)
    assert served == list(range(size))
    got = [w.result() for w in waiters]
    await greenlet_spawn(lambda: [c.close() for c in got])
    assert pool.checkedout() == 0
    pool.dispose()


async def test_a_waiter_cancelled_after_its_handoff_passes_the_connection_on() -> None:
    pool = LingeringQueuePool(_Creator(), pool_size=1, max_overflow=0, timeout=5)
    held = await greenlet_spawn(pool.connect)
    first = asyncio.create_task(greenlet_spawn(pool.connect))
    second = asyncio.create_task(greenlet_spawn(pool.connect))
    await asyncio.sleep(0.01)
    await greenlet_spawn(held.close)  # reserved for `first` ...
    first.cancel()  # ... which is cancelled before it resumes
    with pytest.raises(asyncio.CancelledError):
        await first
    conn = await asyncio.wait_for(second, 1)  # passed on, not leaked
    assert pool.checkedout() == 1
    await greenlet_spawn(conn.close)
    assert pool.checkedout() == 0
    assert not pool._waiters
    pool.dispose()


async def test_room_for_a_connection_wakes_the_oldest_waiter() -> None:
    """dispose() / a failed connect free an overflow slot: the oldest waiter
    connects at once instead of waiting for a return or its timeout."""
    creator = _Creator()
    pool = LingeringQueuePool(creator, pool_size=1, max_overflow=0, timeout=5)
    held = await greenlet_spawn(pool.connect)
    waiter = asyncio.create_task(greenlet_spawn(pool.connect))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    pool.dispose()  # forgets the checked-out connection: room for one
    fresh = await asyncio.wait_for(waiter, 1)
    assert fresh.dbapi_connection is creator.made[-1]
    assert len(creator.made) == 2
    await greenlet_spawn(fresh.close)
    await greenlet_spawn(held.close)
    pool.dispose()


async def test_a_failed_connect_releases_the_reserved_slot() -> None:
    class _Down(Exception):
        pass

    creator = _Creator()
    pool = LingeringQueuePool(creator, pool_size=1, max_overflow=0, timeout=5)
    held = await greenlet_spawn(pool.connect)
    waiter = asyncio.create_task(greenlet_spawn(pool.connect))
    await asyncio.sleep(0.01)

    def down() -> _FakeConn:
        raise _Down

    pool._creator = down  # type: ignore[method-assign]
    pool.dispose()
    with pytest.raises(_Down):
        await asyncio.wait_for(waiter, 1)
    assert not pool._waiters
    assert pool._overflow == -1  # the slot went back
    await greenlet_spawn(held.close)
    pool.dispose()


async def test_checkout_waiters_counts_contended_checkouts() -> None:
    """The pool-waiters metric reads this count (sandloom_db_pool_checkout_waiters)."""
    pool = LingeringQueuePool(_Creator(), pool_size=1, max_overflow=0, timeout=1.0)
    held = await greenlet_spawn(pool.connect)
    assert pool.checkout_waiters() == 0
    waiting = [asyncio.create_task(greenlet_spawn(pool.connect)) for _ in range(2)]
    await asyncio.sleep(0.01)
    assert pool.checkout_waiters() == 2
    await greenlet_spawn(held.close)
    first = await waiting[0]
    assert pool.checkout_waiters() == 1
    await greenlet_spawn(first.close)
    second = await waiting[1]
    assert pool.checkout_waiters() == 0
    await greenlet_spawn(second.close)
    pool.dispose()


# -- bounded barging (Oracle 2026-10-23 read tail) -----------------------------


def _barging_pool(bound_ms: float = 50.0) -> Any:
    return LingeringQueuePool(
        _Creator(),
        pool_size=1,
        max_overflow=0,
        timeout=5,
        overflow_linger_seconds=30,
        fair_barge_ms=bound_ms,
    )


async def test_a_newcomer_may_barge_under_the_bound(clock: _Clock) -> None:
    """Under the bound a returned connection goes back to the idle queue: a
    caller already running takes it at once instead of it idling, reserved,
    until the woken waiter's task resumes. The waiter keeps its age."""
    pool = _barging_pool()
    held = await greenlet_spawn(pool.connect)
    order: list[str] = []

    async def checkout(name: str) -> Any:
        conn = await greenlet_spawn(pool.connect)
        order.append(name)
        return conn

    waiting = asyncio.create_task(checkout("waiting"))
    await asyncio.sleep(0.01)
    assert pool.checkout_waiters() == 1
    clock.now += 0.01  # 10 ms < the 50 ms bound

    def _release_then_take() -> Any:
        held.close()  # queue + wake the waiter (not reserved)
        assert pool.checkedin() == 1
        conn = pool.connect()  # same iteration: the newcomer barges
        order.append("new")
        return conn

    new = await greenlet_spawn(_release_then_take)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not waiting.done()  # barged: back in line, by age
    assert pool.checkout_waiters() == 1
    await greenlet_spawn(new.close)
    got = await asyncio.wait_for(waiting, 1)
    assert order == ["new", "waiting"]
    await greenlet_spawn(got.close)
    assert pool.checkedout() == 0
    assert not pool._waiters
    pool.dispose()


async def test_a_waiter_past_the_bound_is_served_before_a_barging_newcomer(
    clock: _Clock,
) -> None:
    """Starvation stays bounded: once the oldest waiter has waited longer than
    the bound, the next return is reserved for it and newcomers queue."""
    pool = _barging_pool()
    held = await greenlet_spawn(pool.connect)
    order: list[str] = []

    async def checkout(name: str) -> None:
        conn = await greenlet_spawn(pool.connect)
        order.append(name)
        await greenlet_spawn(conn.close)

    waiting = asyncio.create_task(checkout("waiting"))
    await asyncio.sleep(0.01)
    clock.now += 0.051  # past the 50 ms bound
    await greenlet_spawn(held.close)
    assert pool.checkedin() == 0  # reserved, not in the idle queue
    await checkout("new")
    await waiting
    assert order == ["waiting", "new"]
    assert pool.checkedout() == 0
    pool.dispose()


async def test_a_barged_waiter_crossing_the_bound_is_served_next(
    clock: _Clock,
) -> None:
    """A waiter that lost its wake-up to a newcomer is re-queued by its
    original age: once past the bound it beats a younger waiter AND a
    newcomer to the next return."""
    pool = _barging_pool()
    held = await greenlet_spawn(pool.connect)
    order: list[str] = []

    async def checkout(name: str) -> Any:
        conn = await greenlet_spawn(pool.connect)
        order.append(name)
        return conn

    old = asyncio.create_task(checkout("old"))
    await asyncio.sleep(0.01)

    def _release_then_take() -> Any:
        held.close()
        return pool.connect()  # barges under the bound

    barger = await greenlet_spawn(_release_then_take)
    await asyncio.sleep(0.01)  # `old` resumed, found nothing, re-queued
    clock.now += 0.02
    young = asyncio.create_task(checkout("young"))
    await asyncio.sleep(0.01)
    clock.now += 0.04  # old: 60 ms (past the bound), young: 40 ms
    assert pool.checkout_waiters() == 2

    def _release_then_try() -> None:
        barger.close()
        assert pool.checkedin() == 0  # reserved for `old`

    await greenlet_spawn(_release_then_try)
    first = await asyncio.wait_for(old, 1)
    assert not young.done()
    await greenlet_spawn(first.close)
    second = await asyncio.wait_for(young, 1)
    assert order == ["old", "young"]
    await greenlet_spawn(second.close)
    assert pool.checkedout() == 0
    pool.dispose()


async def test_a_cancelled_woken_waiter_passes_its_wake_up_on(clock: _Clock) -> None:
    """A waiter woken under the bound (connection in the idle queue) and then
    cancelled must not strand the waiter behind it."""
    pool = _barging_pool()
    held = await greenlet_spawn(pool.connect)
    first = asyncio.create_task(greenlet_spawn(pool.connect))
    second = asyncio.create_task(greenlet_spawn(pool.connect))
    await asyncio.sleep(0.01)
    await greenlet_spawn(held.close)  # wakes `first`, connection queued
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    conn = await asyncio.wait_for(second, 1)
    await greenlet_spawn(conn.close)
    assert pool.checkedout() == 0
    assert not pool._waiters
    pool.dispose()


async def test_barging_keeps_one_timeout(clock: _Clock) -> None:
    """Losing wake-ups to newcomers never restarts the pool timeout."""
    pool = LingeringQueuePool(
        _Creator(), pool_size=1, max_overflow=0, timeout=0.2, fair_barge_ms=1e6
    )
    held = await greenlet_spawn(pool.connect)
    waiting = asyncio.create_task(greenlet_spawn(pool.connect))
    await asyncio.sleep(0.01)

    def _release_then_take(conn: Any) -> Any:
        conn.close()
        return pool.connect()

    for _ in range(3):  # three wake-ups, each lost to a newcomer
        held = await greenlet_spawn(_release_then_take, held)
        await asyncio.sleep(0.03)
    with pytest.raises(exc.TimeoutError, match="connection timed out"):
        await asyncio.wait_for(waiting, 0.35)
    assert not pool._waiters
    await greenlet_spawn(held.close)
    assert pool.checkedout() == 0
    pool.dispose()


async def test_zero_bound_is_the_strict_handoff(clock: _Clock) -> None:
    pool = _barging_pool(bound_ms=0)
    held = await greenlet_spawn(pool.connect)
    waiting = asyncio.create_task(greenlet_spawn(pool.connect))
    await asyncio.sleep(0.01)
    await greenlet_spawn(held.close)
    assert pool.checkedin() == 0  # reserved at once
    conn = await asyncio.wait_for(waiting, 1)
    await greenlet_spawn(conn.close)
    assert pool.recreate()._barge_bound == 0
    pool.dispose()


async def test_barging_allocates_no_future_or_timer(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a checkout that actually waits builds a future and a deadline
    timer; returning a connection and barging take none (objects that live
    across loop iterations get promoted and drive full GC under load)."""
    pool = _barging_pool()
    held = await greenlet_spawn(pool.connect)
    waiting = asyncio.create_task(greenlet_spawn(pool.connect))
    await asyncio.sleep(0.01)

    def _no_loop() -> Any:
        raise AssertionError("the uncontended path touched the event loop")

    def _release_then_take() -> Any:
        with monkeypatch.context() as m:
            m.setattr(linger_pool.asyncio, "get_running_loop", _no_loop)
            held.close()
            return pool.connect()

    new = await greenlet_spawn(_release_then_take)
    await greenlet_spawn(new.close)
    got = await asyncio.wait_for(waiting, 1)
    await greenlet_spawn(got.close)
    assert pool.checkedout() == 0
    pool.dispose()
