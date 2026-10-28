"""Pool checkout tail stand: contended asyncpg checkouts on a CPU-loaded loop.

Reproduces the Oracle 2026-10-22 write-tail finding (fair checkout gate waits
of hundreds of ms while the underlying queue wait was ~0) without the HTTP
stack: one event loop (= one Gunicorn worker), an open-loop Poisson arrival
stream, per-request CPU before / inside / after a short write transaction, and
more in-flight requests than the pool holds.

Per variant it reports request latency and checkout wait percentiles, split
into the part spent inside the queue (``_get_conn`` / stock ``_do_get``) and
the rest (the fair gate or the handoff wait), plus how often a checkout was
waiting while an idle connection sat in the pool.

    SANDLOOM_TEST_DATABASE_URL=postgresql+asyncpg://.../sandloom_test_mine \\
        uv run python tools/checks/bench_pool_checkout.py --rate 330 --seconds 20

``--virtual`` replaces the database and the wall clock with a deterministic
model (fake connections; CPU spin, DB round trips and connects advance a
virtual event-loop clock), so variants compare without machine noise — the
real-database mode on a shared, loaded machine flips randomly into overload.

``--e2e`` is the end-to-end mode (real clock, real CPU): request-like tasks
on one CPU-saturated loop — per-request live objects (GC pressure grows with
concurrency, as in a server), CPU, checkout, ``--queries`` short round trips
(``SELECT 1`` on the real database, or ``--fake-db``: a sleep), release, CPU —
at fixed Poisson arrival rates near saturation. It reports request
p50/p99/p99.9, the worst checkout wait (starvation), loop lag, goodput and
process CPU per request. The virtual-time model missed the Oracle 2026-10-23
read-tail regression of the strict FIFO handoff; this mode exists to show it.

    uv run python tools/checks/bench_pool_checkout.py --e2e --fake-db --rate 300 \\
        --pool-file gate=/tmp/b609.py --pool-file handoff=/tmp/f898.py \\
        --barge-ms 0,10,25,50

``--pool-file NAME=FILE`` (repeatable) loads another ``linger_pool.py`` as
variant ``NAME`` (linger 30 s); ``--barge-ms`` adds ``bargeN`` variants of the
current pool with ``fair_barge_ms=N``.

``--baseline FILE`` loads another ``linger_pool.py`` (e.g. a pre-fix copy) as
the ``base*`` variants for an A/B in one run. Variants are interleaved over
``--rounds`` so machine noise hits all of them alike.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib.util
import json
import os
import random
import selectors
import sys
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.util import greenlet_spawn

from sandloom.core.drivers import linger_pool
from sandloom.core.drivers.linger_pool import LingeringQueuePool

_now = time.perf_counter


def _real_spin(ms: float) -> None:
    end = _now() + ms / 1000.0
    while _now() < end:
        pass


_spin = _real_spin


# -- virtual time --------------------------------------------------------------


class _VClock:
    t = 0.0

    def __call__(self) -> float:
        return self.t


_vclock = _VClock()


class _VSelector(selectors.DefaultSelector):
    """Never sleeps: an idle wait jumps the virtual clock to the next timer."""

    def select(self, timeout: float | None = None) -> Any:
        ready = super().select(0)
        if not ready and timeout:
            _vclock.t += timeout
        return ready


class _VLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__(_VSelector())

    def time(self) -> float:
        return _vclock.t


def _vspin(ms: float) -> None:
    _vclock.t += ms / 1000.0  # CPU work: the loop is busy, time passes


# -- CPU time (``--e2e --fake-db --cpu-clock``) ---------------------------------


class _CpuClock:
    """Process CPU time + idle waits: the loop's own clock, minus preemption.

    On a shared machine the OS hands the CPU to other processes at random;
    in wall time that looks like a stalled loop and tips a near-saturated
    run into a backlog that has nothing to do with the code under test. Here
    time advances only while this process runs (all of its real CPU: the
    pool, futures, greenlets, GC, the spin) or while the loop is idle — an
    idle wait jumps the clock to the next timer, as in ``--virtual``.
    """

    offset = 0.0

    def __call__(self) -> float:
        return time.process_time() + self.offset


_cpuclock = _CpuClock()


class _CSelector(selectors.DefaultSelector):
    def select(self, timeout: float | None = None) -> Any:
        ready = super().select(0)
        if not ready and timeout:
            _cpuclock.offset += timeout
        return ready


class _CLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__(_CSelector())

    def time(self) -> float:
        return _cpuclock()


class _VConn:
    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


async def _run_virtual(
    name: str, poolclass: type, kw: dict[str, Any], a: Any
) -> dict[str, Any]:
    stats: dict[str, list[float]] = {"checkout": [], "queue": []}

    def creator() -> _VConn:
        _vspin(a.connect_ms)  # TCP + TLS + SCRAM on the loop
        return _VConn()

    pool = _instrumented(poolclass, stats)(
        creator,
        pool_size=a.pool_size,
        max_overflow=a.max_overflow,
        timeout=30,
        use_lifo=True,
        **kw,
    )
    loop = asyncio.get_running_loop()
    latencies: list[float] = []
    rng = random.Random(a.seed)

    async def request(arrival: float, record: bool) -> None:
        _spin(a.pre_ms)
        await asyncio.sleep(0)
        conn = await greenlet_spawn(pool.connect)
        await asyncio.sleep(a.rtt_ms / 1000.0)  # INSERT
        _spin(a.mid_ms)
        await asyncio.sleep(a.rtt_ms / 1000.0)  # COMMIT
        await greenlet_spawn(conn.close)
        _spin(a.post_ms)
        if record:
            latencies.append(loop.time() - arrival)

    tasks: set[asyncio.Task[None]] = set()
    start = loop.time()
    warm_end = start + a.warmup
    end = warm_end + a.seconds
    next_at = start
    while next_at < end:
        await asyncio.sleep(max(0.0, next_at - loop.time()))
        now = loop.time()
        while next_at <= now and next_at < end:
            t = asyncio.create_task(request(next_at, next_at >= warm_end))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
            next_at += rng.expovariate(a.rate)
    await asyncio.gather(*tasks)
    return _summarise(name, stats, latencies, [], 0, 0, a)


async def _run_e2e(
    name: str, poolclass: type, kw: dict[str, Any], a: Any
) -> dict[str, Any]:
    """Real clock + real CPU, request-shaped tasks (see ``--e2e``)."""
    stats: dict[str, list[float]] = {"checkout": [], "queue": []}
    engine: Any = None
    if a.fake_db:

        def creator() -> _VConn:
            _real_spin(a.connect_ms)
            return _VConn()

        pool: Any = _instrumented(poolclass, stats)(
            creator,
            pool_size=a.pool_size,
            max_overflow=a.max_overflow,
            timeout=30,
            use_lifo=True,
            **kw,
        )
    else:
        engine = create_async_engine(
            a.url,
            poolclass=_instrumented(poolclass, stats),
            pool_size=a.pool_size,
            max_overflow=a.max_overflow,
            pool_timeout=30,
            pool_use_lifo=True,
            pool_pre_ping=False,
            **kw,
        )
        pool = engine.sync_engine.pool
    latencies: list[float] = []
    lags: list[float] = []
    rng = random.Random(a.seed)
    stop = asyncio.Event()
    held_slice = a.mid_ms / max(1, a.queries)

    def live_objects() -> list[dict[str, Any]]:
        # Request state that lives across awaits (headers, rows, DTOs).
        return [{"k": i, "v": [i, str(i)]} for i in range(a.live_objects)]

    async def request(arrival: float, record: bool) -> None:
        state = live_objects()
        _spin(a.pre_ms)
        await asyncio.sleep(0)
        if engine is None:
            conn = await greenlet_spawn(pool.connect)
            for _ in range(a.queries):
                await asyncio.sleep(a.rtt_ms / 1000.0)
                _spin(held_slice)
            await greenlet_spawn(conn.close)
        else:
            async with engine.connect() as conn:
                for _ in range(a.queries):
                    await conn.exec_driver_sql("SELECT 1")
                    _spin(held_slice)
                await conn.rollback()
        _spin(a.post_ms)
        state.clear()
        if record:
            latencies.append(_now() - arrival)

    async def sampler() -> None:
        tick = 0.001
        last = _now()
        while not stop.is_set():
            await asyncio.sleep(tick)
            t = _now()
            lags.append(t - last - tick)
            last = t

    async def staller() -> None:
        # A periodic loop stall (GC pause, preemption, a slow callback): the
        # disturbance that tips a near-saturated loop into a backlog. What
        # differs between pools is how fast the backlog drains afterwards.
        while not stop.is_set():
            await asyncio.sleep(a.stall_every)
            if not stop.is_set():
                _spin(a.stall_ms)

    # An application-sized long-lived heap: CPython runs a full collection
    # only once objects promoted since the last one reach 25% of the
    # long-lived count, so on a real app (hundreds of thousands of tracked
    # objects) the number of full GCs follows how many request-time objects
    # survive into the old generation — which the pool's waiting path affects.
    heap = [{"i": i} for i in range(a.heap_objects)]

    # Warm the pool so connects do not pollute the window.
    if engine is None:
        c = await greenlet_spawn(pool.connect)
        await greenlet_spawn(c.close)
    else:
        async with engine.connect() as c:
            await c.exec_driver_sql("SELECT 1")
    gc.collect()
    gc_ms = [0.0, 0.0, 0.0]
    gc_t0 = [0.0]

    def gc_timer(phase: str, info: dict[str, Any]) -> None:
        # Real (process CPU) time inside each collection, by generation.
        if phase == "start":
            gc_t0[0] = time.process_time()
        else:
            gc_ms[info["generation"]] += 1000.0 * (time.process_time() - gc_t0[0])

    tasks: set[asyncio.Task[None]] = set()
    samp = asyncio.create_task(sampler())
    stall = asyncio.create_task(staller()) if a.stall_ms > 0 else None
    start = _now()
    warm_end = start + a.warmup
    end = warm_end + a.seconds
    cpu0: float | None = None
    gc0 = [0, 0, 0]
    next_at = start
    while True:
        now = _now()
        if cpu0 is None and now >= warm_end:
            cpu0 = time.process_time()
            gc0 = [g["collections"] for g in gc.get_stats()]
            gc.callbacks.append(gc_timer)
        if now >= end:
            break
        while next_at <= now:
            t = asyncio.create_task(request(next_at, next_at >= warm_end))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
            next_at += rng.expovariate(a.rate)
        await asyncio.sleep(max(0.0, next_at - _now()))
    if tasks:
        await asyncio.gather(*tasks)
    drained = _now()
    cpu = time.process_time() - (cpu0 if cpu0 is not None else 0.0)
    if gc_timer in gc.callbacks:
        gc.callbacks.remove(gc_timer)
    gc1 = [g["collections"] for g in gc.get_stats()]
    stop.set()
    await samp
    if stall is not None:
        stall.cancel()
    del heap
    if engine is not None:
        await engine.dispose()
    out = _summarise(name, stats, latencies, lags, 0, 0, a)
    for k in (
        "tail1pct_gate_mean",
        "tail1pct_queue_mean",
        "waiting_frac",
        "idle_while_waiting_frac",
    ):
        out.pop(k)
    out["goodput"] = len(latencies) / max(1e-9, drained - warm_end)
    out["cpu_us_per_req"] = 1e6 * cpu / max(1, len(latencies))
    # Busy share of the window: well below 1 in a backlog means the loop was
    # not the bottleneck (pool-bound) or the machine took the CPU away.
    out["cpu_util"] = cpu / max(1e-9, drained - warm_end)
    for gen in range(3):
        out[f"gc{gen}"] = gc1[gen] - gc0[gen]
    out["gc2_ms"] = gc_ms[2]
    out["gc_share"] = sum(gc_ms) / max(1e-9, 1000.0 * cpu)
    return out


def _instrumented(base: type, stats: dict[str, list[float]]) -> type:
    """Subclass timing ``_do_get`` (whole checkout) and the queue part."""
    has_inner = hasattr(base, "_get_conn")

    class Timed(base):  # type: ignore[misc]
        def _do_get(self) -> Any:
            t0 = _clock()
            self._sandloom_inner = 0.0
            try:
                return super()._do_get()
            finally:
                total = _clock() - t0
                inner = self._sandloom_inner if has_inner else total
                stats["checkout"].append(total)
                stats["queue"].append(inner)

        if has_inner:

            def _get_conn(self) -> Any:
                t0 = _clock()
                try:
                    return super()._get_conn()
                finally:
                    self._sandloom_inner += _clock() - t0

    return Timed


def _clock() -> float:
    return asyncio.get_running_loop().time()


def _waiting(pool: Any) -> int:
    if hasattr(pool, "_waiters"):
        return sum(not f.done() for f in pool._waiters)
    return int(getattr(pool, "_checkout_waiters", 0))


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] * 1000.0


async def _run_variant(
    name: str, poolclass: type, kw: dict[str, Any], a: Any
) -> dict[str, Any]:
    stats: dict[str, list[float]] = {"checkout": [], "queue": []}
    engine = create_async_engine(
        a.url,
        poolclass=_instrumented(poolclass, stats),
        pool_size=a.pool_size,
        max_overflow=a.max_overflow,
        pool_timeout=30,
        pool_use_lifo=True,
        pool_pre_ping=False,
        **kw,
    )
    pool: Any = engine.sync_engine.pool
    latencies: list[float] = []
    lags: list[float] = []
    idle_while_waiting = 0
    waiting_samples = 0
    rng = random.Random(a.seed)
    stop = asyncio.Event()

    async def request(arrival: float, record: bool) -> None:
        _spin(a.pre_ms)
        await asyncio.sleep(0)
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO pool_stand (v) VALUES (1)"))
            _spin(a.mid_ms)
        _spin(a.post_ms)
        if record:
            latencies.append(_now() - arrival)

    async def sampler() -> None:
        nonlocal idle_while_waiting, waiting_samples
        tick = 0.001
        last = _now()
        while not stop.is_set():
            await asyncio.sleep(tick)
            t = _now()
            lags.append(t - last - tick)
            last = t
            if _waiting(pool):
                waiting_samples += 1
                if pool._pool.qsize():
                    idle_while_waiting += 1

    # Warm the pool so connects do not pollute the window.
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    tasks: set[asyncio.Task[None]] = set()
    samp = asyncio.create_task(sampler())
    start = _now()
    warm_end = start + a.warmup
    end = warm_end + a.seconds
    next_at = start
    while True:
        now = _now()
        if now >= end:
            break
        while next_at <= now:
            t = asyncio.create_task(request(next_at, next_at >= warm_end))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
            next_at += rng.expovariate(a.rate)
        await asyncio.sleep(max(0.0, next_at - _now()))
    if tasks:
        await asyncio.gather(*tasks)
    stop.set()
    await samp
    await engine.dispose()
    return _summarise(
        name, stats, latencies, lags, waiting_samples, idle_while_waiting, a
    )


def _summarise(
    name: str,
    stats: dict[str, list[float]],
    latencies: list[float],
    lags: list[float],
    waiting_samples: int,
    idle_while_waiting: int,
    a: Any,
) -> dict[str, Any]:
    # Keep only waits recorded after warm-up (proportional trim).
    n = len(stats["checkout"])
    skip = int(n * a.warmup / (a.warmup + a.seconds))
    co = stats["checkout"][skip:]
    qu = stats["queue"][skip:]
    # The slowest 1% of CHECKOUTS: how much of it was outside the queue.
    order = sorted(range(len(co)), key=co.__getitem__)[int(0.99 * len(co)) :]
    gate_tail = [co[i] - qu[i] for i in order]
    queue_tail = [qu[i] for i in order]
    return {
        "variant": name,
        "requests": len(latencies),
        "rps": len(latencies) / a.seconds,
        "lat_p50": _pct(latencies, 0.50),
        "lat_p99": _pct(latencies, 0.99),
        "lat_p999": _pct(latencies, 0.999),
        "co_p50": _pct(co, 0.50),
        "co_p99": _pct(co, 0.99),
        "co_p999": _pct(co, 0.999),
        "co_max": max(co, default=0.0) * 1000.0,
        "tail1pct_gate_mean": 1000 * sum(gate_tail) / max(1, len(gate_tail)),
        "tail1pct_queue_mean": 1000 * sum(queue_tail) / max(1, len(queue_tail)),
        "lag_p99": _pct(lags, 0.99),
        "waiting_frac": waiting_samples / max(1, len(lags)),
        "idle_while_waiting_frac": idle_while_waiting / max(1, waiting_samples),
    }


def _load_baseline(path: str, modname: str = "sandloom_pool_baseline") -> type:
    spec = importlib.util.spec_from_file_location(modname, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if linger_pool._clock in (_vclock, _cpuclock):
        mod._clock = linger_pool._clock  # type: ignore[attr-defined]
    return mod.LingeringQueuePool  # type: ignore[no-any-return]


async def _main(a: Any) -> None:
    run = _run_virtual if a.virtual else _run_e2e if a.e2e else _run_variant
    if not a.virtual and not (a.e2e and a.fake_db):
        await _prepare(a)
    await _sweep(a, run)


async def _prepare(a: Any) -> None:
    eng = create_async_engine(a.url)
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS pool_stand"
                " (id bigserial PRIMARY KEY, v int)"
            )
        )
        await conn.execute(text("TRUNCATE pool_stand"))
    await eng.dispose()


async def _sweep(a: Any, run: Any) -> None:
    variants: dict[str, tuple[type, dict[str, Any]]] = {
        "stock": (AsyncAdaptedQueuePool, {}),
        "linger30": (LingeringQueuePool, {"overflow_linger_seconds": 30.0}),
        "linger0": (LingeringQueuePool, {"overflow_linger_seconds": 0.0}),
    }
    if a.baseline:
        base = _load_baseline(a.baseline)
        variants["base-linger30"] = (base, {"overflow_linger_seconds": 30.0})
        variants["base-linger0"] = (base, {"overflow_linger_seconds": 0.0})

    class NoGate(LingeringQueuePool):
        """The pool before any fairness fix (pre d194ad84): unfair queue."""

        def _do_get(self) -> Any:
            return self._get_conn()

    variants["nogate-linger30"] = (NoGate, {"overflow_linger_seconds": 30.0})
    for i, spec in enumerate(a.pool_file or []):
        vname, _, path = spec.partition("=")
        cls = _load_baseline(path, f"sandloom_pool_file_{i}")
        variants[vname] = (cls, {"overflow_linger_seconds": 30.0})
    for ms in (a.barge_ms or "").split(","):
        if ms.strip():
            variants[f"barge{ms.strip()}"] = (
                LingeringQueuePool,
                {"overflow_linger_seconds": 30.0, "fair_barge_ms": float(ms)},
            )
    names = [n for n in variants if not a.only or n in a.only.split(",")]
    results = []
    for r in range(a.rounds):
        order = names if r % 2 == 0 else list(reversed(names))
        for name in order:
            cls, kw = variants[name]
            res = await run(name, cls, kw, a)
            res["round"] = r
            results.append(res)
            print(json.dumps(res), flush=True)
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(results, fh, indent=1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default=os.environ.get("SANDLOOM_TEST_DATABASE_URL"))
    p.add_argument("--rate", type=float, default=330.0, help="arrivals/s")
    p.add_argument("--seconds", type=float, default=20.0)
    p.add_argument("--warmup", type=float, default=3.0)
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--pool-size", type=int, default=5)
    p.add_argument("--max-overflow", type=int, default=5)
    p.add_argument("--pre-ms", type=float, default=1.0)
    p.add_argument("--mid-ms", type=float, default=0.5)
    p.add_argument("--post-ms", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--virtual", action="store_true", help="deterministic model")
    p.add_argument("--rtt-ms", type=float, default=0.5, help="virtual DB round trip")
    p.add_argument("--connect-ms", type=float, default=15.0, help="virtual connect CPU")
    p.add_argument("--baseline", help="path of another linger_pool.py to A/B")
    p.add_argument("--only", help="comma list of variants")
    p.add_argument("--e2e", action="store_true", help="end-to-end, real CPU")
    p.add_argument("--fake-db", action="store_true", help="e2e: sleep as the DB")
    p.add_argument("--queries", type=int, default=2, help="e2e round trips")
    p.add_argument("--live-objects", type=int, default=300, help="e2e GC load")
    p.add_argument("--pool-file", action="append", help="NAME=path of a linger_pool.py")
    p.add_argument("--barge-ms", help="comma list of fair_barge_ms variants")
    p.add_argument(
        "--heap-objects", type=int, default=0, help="e2e long-lived app heap"
    )
    p.add_argument("--stall-ms", type=float, default=0.0, help="e2e loop stall")
    p.add_argument("--stall-every", type=float, default=2.0, help="seconds")
    p.add_argument(
        "--cpu-clock", action="store_true", help="e2e --fake-db: CPU-time clock"
    )
    p.add_argument("--out", help="write all results as JSON")
    a = p.parse_args()
    if a.virtual:
        global _spin
        _spin = _vspin
        linger_pool._clock = _vclock  # linger window in virtual time
        asyncio.run(_main(a), loop_factory=_VLoop)
        return
    if a.e2e and a.fake_db and a.cpu_clock:
        global _now
        _now = _cpuclock  # latencies, arrivals and the spin in CPU time
        linger_pool._clock = _cpuclock  # linger window and barging bound
        asyncio.run(_main(a), loop_factory=_CLoop)
        return
    if a.e2e and a.fake_db:
        asyncio.run(_main(a))
        return
    if not a.url or "test" not in a.url.rsplit("/", 1)[-1]:
        sys.exit("set --url / SANDLOOM_TEST_DATABASE_URL to YOUR OWN *test* database")
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()
