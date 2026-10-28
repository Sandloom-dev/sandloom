"""An asyncio QueuePool whose overflow connections linger instead of churning.

Stock :class:`~sqlalchemy.pool.AsyncAdaptedQueuePool` keeps at most
``pool_size`` idle connections: an overflow connection returned while
``pool_size`` others sit idle is closed on the spot, and the next checkout that
finds the idle queue empty opens a brand-new one. Under sustained load above
``pool_size`` concurrent checkouts the idle count oscillates around
``pool_size`` (requests spend part of their life outside the database), so the
pool closes and reopens connections continuously — a TCP + TLS negotiation +
SCRAM authentication + session setup per cycle, all CPU on the event loop
(asyncpg 0.30's pure-Python SCRAM alone is ~15 ms of CPU per connect). Measured
on the benchmark S3 route at 64 connections: ~0.1 connects per request, which
made the old stack ~1.5-3x more CPU per request — and, being a positive
feedback loop (connect CPU -> longer queues -> deeper oscillation -> more
connects), sensitive to event-loop scheduling: it showed up as a uvicorn
``--loop uvloop`` vs ``asyncio`` anomaly on Linux.

:class:`LingeringQueuePool` adds hysteresis: while the pool has been short of
idle connections within the last ``overflow_linger_seconds`` (a checkout found
the idle queue empty), a returned overflow connection is kept idle instead of
closed. Once demand has stayed within ``pool_size`` for that long, returns
shrink the pool back to ``pool_size`` exactly as stock QueuePool does. The
total-connection bound (``pool_size + max_overflow``), checkout timeout, LIFO
order and every pool event are unchanged. ``overflow_linger_seconds=0`` is the
stock behaviour.

**Non-blocking checkout.** Stock ``QueuePool._do_get`` asks its queue for a
connection with ``block = (overflow is at max_overflow)``. A lingering pool
sits at max overflow for the whole linger window after a burst (e.g. the
contract pool, size 1 + overflow 2, keeps 3 idle connections), so EVERY
checkout then went through the blocking path — ``await_(asyncio.wait_for(
queue.get(), timeout))``: a greenlet switch to the event loop and back plus a
``Timeout`` context — although an idle connection was right there.
``_do_get`` now takes an idle connection with ``get_nowait()`` first and falls
back to the stock path only when the queue is empty. This is the item the
stock path hands out too (``asyncio.Queue.get`` on a non-empty queue returns
``get_nowait()`` without suspending). Connection order (LIFO/FIFO),
overflow accounting and pool events stay intact; record-level work (recycle,
invalidation, idle ping, dead connection replacement) is untouched. The queue
internals are probed once per process (``pool_fast_checkout`` in
:mod:`sandloom.core.sql_alchemy.driver_compat`); on a mismatch the pool uses the
stock path only. SQLAlchemy 2.1.5 made the stock queue ``get`` try
``get_nowait()`` first as well, so from that release the fast path saves only
the stock call layering; the fair checkout below is still Sandloom's.

**Fair checkout with bounded barging.** ``asyncio.Queue`` wakes a getter
without reserving the item for it: a new arrival can take the connection
before the woken waiter resumes, on the stock path and the fast path alike,
so repeated arrivals can starve a waiter. A contended checkout (the pool is
exhausted, or someone is already waiting) therefore parks on its OWN future
in a FIFO, stamped with the time it started waiting. A returned connection
then either

* goes back to the idle queue and wakes the oldest waiter, while that waiter
  has waited less than ``fair_barge_ms`` — whoever runs first takes it: a
  caller already on the CPU in this loop iteration ("barging"), or the woken
  waiter when it resumes. A waiter that lost the race re-queues by its
  original age; or
* is handed straight to the oldest waiter (reserved: nobody can overtake it)
  once it has waited ``fair_barge_ms`` or longer. Newcomers queue too while
  such a waiter exists.

So no checkout waits more than about ``fair_barge_ms`` (plus one connection
hold) behind newcomers, yet below the bound a connection is never parked
for a task the loop has not resumed yet. ``fair_barge_ms=0`` is the strict
FIFO handoff of 2026-10-22, which on a CPU-saturated loop left every
contended connection idle for a loop iteration (mean 6.7 ms on Oracle) and
kept waiters resuming in lockstep cohorts — the 2026-10-23 read-tail
regression. Before that, d194ad84 serialised contended checkouts behind an
``asyncio.Lock`` held across the queue wait: one handoff per loop iteration,
a lock convoy (``docs/design/pool-checkout-fairness-2026-10-22.md``). When
room for a new connection appears while callers wait (a failed connect,
``dispose()``), the oldest waiter is woken with a reserved overflow slot and
connects itself. One pool timeout covers the whole wait, re-queues
included; a waiter cancelled or timed out after being served hands its
connection, slot or wake-up on, never leaks it.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Literal

from sqlalchemy import exc
from sqlalchemy.pool import AsyncAdaptedQueuePool, ConnectionPoolEntry
from sqlalchemy.util import queue as sqla_queue
from sqlalchemy.util.concurrency import await_

from sandloom.core.sql_alchemy.driver_compat import pool_fast_checkout_ok

__all__ = ["LingeringQueuePool"]

#: Monotonic clock of the linger window (a seam for tests).
_clock = time.monotonic

#: Handoff result: an overflow slot was reserved for the waiter; it connects.
_CREATE: Any = object()
#: Wake-up under the barging bound: a connection went to the idle queue; try.
_RETRY: Any = object()


def _expire(waiter: _Waiter) -> None:
    """The pool timeout of a contended checkout ran out."""
    if not waiter.fut.done():
        waiter.fut.set_exception(TimeoutError())


class _Waiter:
    """A contended checkout: its wake-up future and when it started waiting."""

    __slots__ = ("fut", "since")

    def __init__(self, fut: asyncio.Future[Any], since: float) -> None:
        self.fut = fut
        self.since = since


class LingeringQueuePool(AsyncAdaptedQueuePool):
    """AsyncAdaptedQueuePool + an overflow linger window (see module docs)."""

    def __init__(
        self,
        creator: Any,
        pool_size: int = 5,
        max_overflow: int = 10,
        timeout: float = 30.0,
        use_lifo: bool = False,
        overflow_linger_seconds: float = 0.0,
        fair_barge_ms: float = 0.0,
        **kw: Any,
    ) -> None:
        super().__init__(
            creator,
            pool_size=pool_size,
            max_overflow=max_overflow,
            timeout=timeout,
            use_lifo=use_lifo,
            **kw,
        )
        self._core_size = pool_size
        self._linger = float(overflow_linger_seconds)
        #: monotonic time a checkout last found no idle connection.
        self._last_shortage = float("-inf")
        #: take an idle connection without the blocking wait_for path.
        self._fast_get = pool_fast_checkout_ok()
        #: contended checkouts, oldest first (see "Fair checkout" above).
        self._waiters: deque[_Waiter] = deque()
        #: a waiter older than this (seconds) is never overtaken.
        self._barge_bound = max(0.0, float(fair_barge_ms)) / 1000.0
        if self._linger > 0 and pool_size > 0 and max_overflow > 0:
            # Room for every connection the overflow bound allows, so an idle
            # overflow connection has somewhere to wait. The total stays capped
            # by ``_overflow`` / ``_max_overflow`` as before.
            self._pool = self._queue_class(pool_size + max_overflow, use_lifo=use_lifo)

    # -- fair checkout (bounded barging) -----------------------------------

    def _do_get(self) -> ConnectionPoolEntry:  # noqa: C901
        if not (
            self._max_overflow >= 0
            and self._overflow >= self._max_overflow
            and self._pool.empty()
        ) and (not self._waiters or self._may_barge()):
            return self._get_conn()
        # Contended: queue up behind earlier waiters.
        now = _clock()
        self._last_shortage = now
        loop = asyncio.get_running_loop()
        waiter = _Waiter(loop.create_future(), now)
        self._waiters.append(waiter)
        # One deadline for the whole wait, re-queues included. A bare timer on
        # the waiter's future instead of ``asyncio.timeout`` + a coroutine:
        # under saturation many checkouts wait tens of ms, and every container
        # object alive that long survives young GC and is promoted, which
        # drives full collections (Oracle 2026-10-23: 31 vs 12 gen2 runs).
        deadline = loop.time() + self._timeout
        timer = loop.call_at(deadline, _expire, waiter)
        try:
            while True:
                try:
                    got = await_(waiter.fut)
                except BaseException as error:
                    self._abandon(waiter)
                    if isinstance(error, TimeoutError):
                        raise self._timed_out() from error
                    raise
                if got is _RETRY:
                    # Woken under the bound: the connection went to the idle
                    # queue and a newcomer may have taken it first.
                    if not self._pool.empty():
                        record: ConnectionPoolEntry = self._pool.get_nowait()
                        return record
                    if self._overflow < self._max_overflow and self._inc_overflow():
                        got = _CREATE
                    elif loop.time() >= deadline:
                        raise self._timed_out()
                    else:
                        # Barged: back in line by age, so the bound still holds.
                        self._last_shortage = _clock()
                        waiter.fut = loop.create_future()
                        self._requeue(waiter)
                        continue
                if got is _CREATE:
                    try:
                        return self._create_connection()
                    except BaseException:
                        self._dec_overflow()
                        raise
                record = got
                return record
        finally:
            timer.cancel()

    def _timed_out(self) -> exc.TimeoutError:
        return exc.TimeoutError(
            f"QueuePool limit of size {self.size()} overflow "
            f"{self._max_overflow} reached, connection timed out, "
            f"timeout {self._timeout:.2f}",
            code="3o7r",
        )

    def _oldest(self) -> _Waiter | None:
        """The oldest live waiter (dead ones at the head are dropped)."""
        waiters = self._waiters
        while waiters:
            waiter = waiters[0]
            fut = waiter.fut
            if fut.done() or fut.get_loop().is_closed():
                waiters.popleft()
                continue
            return waiter
        return None

    def _may_barge(self) -> bool:
        """A newcomer may take an idle connection: nobody is past the bound."""
        oldest = self._oldest()
        return oldest is None or _clock() - oldest.since < self._barge_bound

    def _requeue(self, waiter: _Waiter) -> None:
        """Put a barged waiter back in line by age (oldest first)."""
        waiters = self._waiters
        at = len(waiters)
        for i, other in enumerate(waiters):
            if other.since > waiter.since:
                at = i
                break
        waiters.insert(at, waiter)

    def _abandon(self, waiter: _Waiter) -> None:
        """A waiter gave up (timeout / cancellation): pass on what it got."""
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass  # already popped by a handoff / wake-up
        fut = waiter.fut
        if fut.done() and not fut.cancelled() and fut.exception() is None:
            got = fut.result()
            if got is _CREATE:
                self._dec_overflow()
            elif got is not _RETRY:
                self._do_return_conn(got)
        # Never leave a connection idle while someone still waits for one
        # (a later waiter may have queued behind this one's dead future, or
        # this one swallowed the wake-up meant for an idle connection).
        for _ in range(self._pool.qsize()):
            if self._oldest() is None:
                break
            self._do_return_conn(self._pool.get_nowait())

    def _wake_for_room(self) -> None:
        """Room for a new connection: reserve it for the oldest waiter."""
        while (waiter := self._oldest()) is not None:
            if not self._inc_overflow():
                return
            self._waiters.popleft()
            waiter.fut.set_result(_CREATE)

    def _get_conn(self) -> ConnectionPoolEntry:
        pool = self._pool
        if pool.empty():
            self._last_shortage = _clock()
        elif self._fast_get:
            # An idle connection is free: hand it out without the blocking
            # ``wait_for(queue.get())`` the stock path takes at max overflow.
            try:
                return pool.get_nowait()
            except sqla_queue.Empty:  # pragma: no cover - single-threaded loop
                pass
        return super()._do_get()

    def _do_return_conn(self, record: ConnectionPoolEntry) -> None:
        waiter = self._oldest()
        if waiter is not None:
            self._waiters.popleft()
            if _clock() - waiter.since >= self._barge_bound:
                waiter.fut.set_result(record)  # reserved: past the bound
                return
            # Under the bound: back to the idle queue, where whoever runs first
            # (a newcomer already on the CPU, or this waiter) takes it.
            waiter.fut.set_result(_RETRY)
            super()._do_return_conn(record)
            return
        if (
            self._pool.qsize() >= self._core_size
            and _clock() - self._last_shortage >= self._linger
        ):
            # Quiet for the whole linger window: shrink to pool_size (stock).
            try:
                record.close()
            finally:
                self._dec_overflow()
            return
        super()._do_return_conn(record)

    def _dec_overflow(self) -> Literal[True]:
        super()._dec_overflow()
        self._wake_for_room()
        return True

    def dispose(self) -> None:
        super().dispose()
        self._wake_for_room()

    # -- accounting against the CONFIGURED size, not the enlarged queue -----

    def size(self) -> int:
        return self._core_size

    def overflow(self) -> int:
        return self._overflow if self._core_size else 0

    def checkedout(self) -> int:
        return self._core_size - self._pool.qsize() + self._overflow

    def checkout_waiters(self) -> int:
        """Contended checkouts still waiting for a connection (for metrics)."""
        return sum(1 for waiter in self._waiters if not waiter.fut.done())

    def recreate(self) -> LingeringQueuePool:
        self.logger.info("Pool recreating")
        return self.__class__(
            self._creator,
            pool_size=self._core_size,
            max_overflow=self._max_overflow,
            pre_ping=self._pre_ping,
            use_lifo=self._pool.use_lifo,
            timeout=self._timeout,
            recycle=self._recycle,
            echo=self.echo,
            logging_name=self._orig_logging_name,
            reset_on_return=self._reset_on_return,
            _dispatch=self.dispatch,
            dialect=self._dialect,
            overflow_linger_seconds=self._linger,
            fair_barge_ms=self._barge_bound * 1000.0,
        )
