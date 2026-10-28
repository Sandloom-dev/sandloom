"""Outbox delivery stand: can event delivery keep up with committed writes?

DIAGNOSTIC ONLY. Drives committing units of work at FIXED rates through the
real delivery machinery and measures whether the transactional outbox drains:

* writer processes (``--workers``, like gunicorn workers) each run an
  open-loop schedule of class-F-shaped writes on ONE event loop: a cache
  ``SET NX`` (idempotency reserve), a UoW with an INSERT + ``publish_event``
  + ``record_audit`` that COMMITS on PostgreSQL, the request-path post-commit
  hand-off (``_dispatch_outbox_after_commit(wait=False)`` → the per-loop
  :class:`PostCommitDispatcher`), a cache ``SET`` (idempotency complete) and
  ``--burn-us`` of synthetic CPU split into ``--burn-chunks`` slices between
  awaits (the HTTP / auth / serialization work of a real request competing
  for the same loop);
* the real Redis Streams bus/transport (``StreamsEventBus`` +
  ``RedisStreamsTransport`` with its own key prefix) and the real
  ``SQLAlchemyOutboxStore``;
* the real relay batch (``_relay_batch_for_store``, same grace/batch size,
  circuit and loop policy as ``_relay_loop``) in writer 0 only (the elected
  gunicorn worker);
* a consumer process reading the stream through the transport's consumer
  group (``consume`` + ``ack_many``: the redelivery / DLQ path is the real
  one) that counts unique event ids, duplicates and the delivery latency
  (commit → read);
* the parent samples pending outbox rows, dead rows, XLEN and the DLQ length
  every ``--sample`` seconds and, after the load stops, waits for the drain.

Subcommands (run with the backend venv; ``--src`` puts another checkout's
``src`` first on ``sys.path`` for an A/B of the framework code)::

    python ladder/outbox_delivery.py ladder --rates 150,200,250 --seconds 30
    python ladder/outbox_delivery.py cpu --mode dispatch --n 2000 --burst 64
    python ladder/outbox_delivery.py cpu --mode relay --n 2000
    # cachegrind (in the sandloom-ladder-cg image): Ir(K) - Ir(0) per event
    python ladder/outbox_delivery.py cgrun --mode dispatch --k 400

Env: ``XADD_PG`` (owner DSN WITHOUT the database, default the test
container), ``XADD_DB`` (own database, created on first use), ``XADD_REDIS``
(own Redis DSN with db index). Only keys under ``XADD_PREFIX`` are deleted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

PG = os.environ.get(
    "XADD_PG", "postgresql+asyncpg://test_user:test_password@localhost:5433"
)
DB = os.environ.get("XADD_DB", "sandloom_bench_xadd")
REDIS = os.environ.get("XADD_REDIS", "redis://localhost:6380/197")
PREFIX = os.environ.get("XADD_PREFIX", "xaddbench:")
CHANNEL = "bench.outbox.created.v1"
GROUP = "bench-consumer"
SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}, "t": {"type": "number"}},
    "required": ["n", "t"],
}


def _setup_path(src: str | None) -> None:
    if src:
        sys.path.insert(0, str(Path(src).resolve()))
    os.environ.setdefault("ENVIRONMENT", "dev")
    os.environ["DATABASE_URL"] = f"{PG}/{DB}"
    os.environ["CACHE_URL"] = REDIS
    os.environ.setdefault("BROKER_URL", REDIS)
    # a non-secret placeholder: the stand mints no tokens (settings need one)
    os.environ.setdefault("JWT_SECRET", "outbox-delivery-stand-not-a-secret-000000")
    os.environ.setdefault("LOG_LEVEL", "WARNING")


# -- shared wiring ------------------------------------------------------------
class Stack:
    """The real outbox / bus / relay objects of one process."""

    def __init__(self, pool_size: int = 10) -> None:
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        from sandloom.core.eventing.streams_bus import StreamsEventBus
        from sandloom.core.eventing.streams_transport import RedisStreamsTransport
        from sandloom.core.drivers.redis_cache import build_transport_client
        from sandloom.core.schema.registry import InMemorySchemaRegistry
        from sandloom.core.sql_alchemy.outbox_store.audit import SQLAlchemyAuditStore
        from sandloom.core.sql_alchemy.outbox_store import SQLAlchemyOutboxStore
        from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork

        self.engine = create_async_engine(
            f"{PG}/{DB}", pool_size=pool_size, max_overflow=pool_size
        )
        self.maker = async_sessionmaker(
            bind=self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self.store = SQLAlchemyOutboxStore()
        self.audit = SQLAlchemyAuditStore()
        self._bus_client: Any = None
        self.cache = build_transport_client(REDIS, read_timeout=5.0)

        async def client() -> Any:
            if self._bus_client is None:
                self._bus_client = build_transport_client(REDIS, read_timeout=15.0)
            return self._bus_client

        registry = InMemorySchemaRegistry()
        registry.register(CHANNEL, "bench", SCHEMA)
        self.transport = RedisStreamsTransport(client_factory=client, prefix=PREFIX)
        self.bus = StreamsEventBus(registry, self.transport)
        maker = self.maker

        def factory() -> Any:
            return SQLAlchemyUnitOfWork(session_factory=maker)

        self.factory = factory

    async def close(self) -> None:
        await self.bus.close()
        for c in (self._bus_client, self.cache):
            if c is not None:
                await c.aclose()
        await self.engine.dispose()


async def _prepare(reset: bool = True) -> None:
    """Create the own database + tables; truncate them and drop own keys."""
    import redis.asyncio as redis
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
    from sandloom.core.sql_alchemy.models.outbox import OutboxMessage

    admin = create_async_engine(f"{PG}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": DB}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{DB}"'))
    await admin.dispose()
    eng = create_async_engine(f"{PG}/{DB}")
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda c: OutboxMessage.metadata.create_all(
                c, tables=[OutboxMessage.__table__, AuditLogEntry.__table__]
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS xb_writes (id uuid PRIMARY KEY, "
                "n integer NOT NULL, worker integer NOT NULL, "
                "created_at timestamptz NOT NULL DEFAULT now())"
            )
        )
        if reset:
            await conn.execute(text("TRUNCATE outbox_messages, audit_log, xb_writes"))
    await eng.dispose()
    if reset:
        r = redis.from_url(REDIS)
        keys = [k async for k in r.scan_iter(match=f"{PREFIX}*", count=1000)]
        for start in range(0, len(keys), 500):
            await r.delete(*keys[start : start + 500])
        await r.aclose()


def _burn(us: float) -> None:
    end = time.perf_counter() + us / 1e6
    while time.perf_counter() < end:
        pass


# -- writer process -----------------------------------------------------------
async def _write(st: Stack, wid: int, n: int, burn_us: float, chunks: int) -> None:
    from sqlalchemy import text

    from sandloom.core import unit_of_work as uow_mod

    slice_us = burn_us / max(1, chunks)
    key = f"{PREFIX}idem:{wid}:{n}"
    await st.cache.set(key, "pending", nx=True, ex=120)
    _burn(slice_us)
    async with st.factory() as uow:
        session = uow.session
        session.info[uow_mod_scope._OUTBOX_STORE_KEY] = st.store
        session.info[uow_mod_scope._AUDIT_STORE_KEY] = st.audit
        session.info[uow_mod_scope._UOW_FACTORY_KEY] = st.factory
        session.info[uow_mod_scope._EVENT_BUS_KEY] = st.bus
        with uow_mod_scope._bind_uow_scope(session, "default"):
            rid = uuid.uuid4()
            await session.execute(
                text("INSERT INTO xb_writes (id, n, worker) VALUES (:i, :n, :w)"),
                {"i": rid, "n": n, "w": wid},
            )
            for _ in range(max(0, chunks - 3)):
                await asyncio.sleep(0)
                _burn(slice_us)
            await uow_mod_effects.publish_event(CHANNEL, {"n": n, "t": time.time()})
            uow_mod_effects.record_audit(
                "bench.write.created",
                actor_id=uuid.UUID(int=wid + 1),
                resource_type="xb_write",
                resource_id=str(rid),
            )
    await uow_mod._dispatch_outbox_after_commit(session)
    _burn(slice_us)
    await st.cache.set(key, "done", ex=120)
    if chunks > 2:
        _burn(slice_us)


async def _relay_forever(st: Stack, stop: asyncio.Event) -> None:
    """``_relay_loop``'s policy (interval 1 s, idle 5 s, full → again)."""
    import random

    from sandloom.core.tasks import relay as relay_mod

    while not stop.is_set():
        full = False
        processed = 0
        if not relay_mod.broker_circuit().is_open():
            try:
                res = await relay_mod._relay_batch_for_store(
                    st.factory, st.store, st.bus, sweep=False
                )
                processed, full = res.attempted, res.full and not res.unavailable
            except Exception as exc:  # noqa: BLE001
                print("relay error", exc, file=sys.stderr)
        if full:
            await asyncio.sleep(0)
            continue
        base = 1.0 if processed else 5.0
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=base + random.random() * 0.3 * base
            )
        except TimeoutError:
            pass


def writer_main(
    src: str | None,
    wid: int,
    rate: float,
    seconds: float,
    start_at: float,
    burn_us: float,
    chunks: int,
    relay: bool,
    stop_flag: Any,
    out: Any,
) -> None:
    _setup_path(src)

    async def main() -> dict[str, Any]:
        from sandloom.core import unit_of_work as uow_mod
        from sandloom.core.unit_of_work import dispatch as uow_mod_dispatch
        from sandloom.core.unit_of_work import effects as uow_mod_effects
        from sandloom.core.unit_of_work import scope as uow_mod_scope

        st = Stack()
        relay_stop = asyncio.Event()
        relay_task = (
            asyncio.create_task(_relay_forever(st, relay_stop)) if relay else None
        )
        total = int(rate * seconds)
        ok = err = shed = 0
        inflight: set[asyncio.Task[None]] = set()
        loop = asyncio.get_running_loop()
        # absolute start shared with the other writers / sampler
        await asyncio.sleep(max(0.0, start_at - time.time()))
        t0 = loop.time()
        cpu0 = time.process_time()

        def done(task: asyncio.Task[None]) -> None:
            nonlocal ok, err
            inflight.discard(task)
            if task.cancelled() or task.exception() is not None:
                err += 1
                if err <= 3 and not task.cancelled():
                    print("write error", repr(task.exception()), file=sys.stderr)
            else:
                ok += 1

        for i in range(total):
            due = t0 + i / rate
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if len(inflight) >= 256:
                shed += 1
                continue
            task = asyncio.create_task(_write(st, wid, i, burn_us, chunks))
            inflight.add(task)
            task.add_done_callback(done)
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        load_cpu = time.process_time() - cpu0
        load_wall = loop.time() - t0
        # keep delivering (dispatcher + relay) until the parent says stop
        while not stop_flag.is_set():
            await asyncio.sleep(0.2)
        await uow_mod_dispatch.drain_post_commit(timeout=10)
        if relay_task is not None:
            relay_stop.set()
            await relay_task
        total_cpu = time.process_time() - cpu0
        await st.close()
        return {
            "wid": wid,
            "ok": ok,
            "err": err,
            "shed": shed,
            "load_wall_s": load_wall,
            "load_cpu_s": load_cpu,
            "total_cpu_s": total_cpu,
        }

    out.put(asyncio.run(main()))


# -- consumer process ---------------------------------------------------------
def consumer_main(src: str | None, stop_flag: Any, counters: Any, out: Any) -> None:
    _setup_path(src)

    async def main() -> dict[str, Any]:
        st = Stack(pool_size=1)
        tr = st.transport
        await tr.ensure_group(CHANNEL, GROUP, start_id="0")
        seen: set[str] = set()
        dups = 0
        lat: list[float] = []
        cpu0 = time.process_time()
        while not stop_flag.is_set():
            batch = await tr.consume(
                CHANNEL, group=GROUP, consumer="c1", count=512, block_ms=200
            )
            now = time.time()
            for d in batch:
                eid = str(d.envelope["id"])
                if eid in seen:
                    dups += 1
                else:
                    seen.add(eid)
                    lat.append(now - float(d.envelope["payload"]["t"]))
            if batch:
                await tr.ack_many(batch)
            counters[0] = len(seen)
            counters[1] = dups
        cpu = time.process_time() - cpu0
        await st.close()
        lat.sort()

        def q(p: float) -> float:
            return lat[min(len(lat) - 1, int(p * len(lat)))] * 1000 if lat else 0.0

        return {
            "unique": len(seen),
            "dups": dups,
            "lat_p50_ms": q(0.50),
            "lat_p99_ms": q(0.99),
            "lat_max_ms": lat[-1] * 1000 if lat else 0.0,
            "cpu_s": cpu,
        }

    out.put(asyncio.run(main()))


# -- parent: one rate ---------------------------------------------------------
async def _sample(eng: Any, rds: Any) -> dict[str, Any]:
    from sqlalchemy import text

    async with eng.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT count(*) FILTER (WHERE delivered_at IS NULL AND "
                    "dead_at IS NULL), count(*) FILTER (WHERE dead_at IS NOT "
                    "NULL), count(*) FROM outbox_messages"
                )
            )
        ).one()
    xlen = await rds.xlen(f"{PREFIX}{CHANNEL}")
    dlq = await rds.xlen(f"{PREFIX}{CHANNEL}.dlq")
    return {
        "pending": int(row[0]),
        "dead": int(row[1]),
        "rows": int(row[2]),
        "xlen": int(xlen),
        "dlq": int(dlq),
    }


def run_rate(args: argparse.Namespace, rate: float) -> dict[str, Any]:
    import redis.asyncio as redis
    from sqlalchemy.ext.asyncio import create_async_engine

    asyncio.run(_prepare())
    ctx = mp.get_context("spawn")
    stop_writers = ctx.Event()
    stop_consumer = ctx.Event()
    out = ctx.Queue()
    cout = ctx.Queue()
    counters = ctx.Array("q", 2)
    consumer = ctx.Process(
        target=consumer_main, args=(args.src, stop_consumer, counters, cout)
    )
    consumer.start()
    start_at = time.time() + args.startup
    per = rate / args.workers
    writers = [
        ctx.Process(
            target=writer_main,
            args=(
                args.src,
                w,
                per,
                args.seconds,
                start_at,
                args.burn_us,
                args.burn_chunks,
                w == 0,
                stop_writers,
                out,
            ),
        )
        for w in range(args.workers)
    ]
    for p in writers:
        p.start()

    async def watch() -> dict[str, Any]:
        eng = create_async_engine(f"{PG}/{DB}", pool_size=1)
        rds = redis.from_url(REDIS)
        samples: list[dict[str, Any]] = []
        await asyncio.sleep(max(0.0, start_at - time.time()))
        load_end = start_at + args.seconds
        drained_at: float | None = None
        expected = int(per * args.seconds) * args.workers
        uniq_at_end = 0
        while True:
            s = await _sample(eng, rds)
            now = time.time()
            s["t"] = round(now - start_at, 2)
            s["unique"] = counters[0]
            s["dups"] = counters[1]
            samples.append(s)
            if now >= load_end and not uniq_at_end:
                uniq_at_end = counters[0] or 1
            prev_rows = samples[-2]["rows"] if len(samples) > 1 else -1
            if (
                now >= load_end
                and s["pending"] == 0
                and s["rows"] == prev_rows  # every in-flight write finished
                and s["rows"] >= min(expected, 1)
                and counters[0] >= s["rows"] - s["dead"]
            ):
                drained_at = now
                break
            if now > load_end + args.drain_timeout:
                break
            await asyncio.sleep(args.sample)
        await eng.dispose()
        await rds.aclose()
        return {
            "samples": samples,
            "drain_s": (drained_at - load_end) if drained_at else None,
            "unique_at_load_end": uniq_at_end,
        }

    watched = asyncio.run(watch())
    stop_writers.set()
    wres = [out.get(timeout=120) for _ in writers]
    for p in writers:
        p.join(timeout=30)
    # let the consumer read anything a final drain published
    time.sleep(1.0)
    stop_consumer.set()
    cres = cout.get(timeout=60)
    consumer.join(timeout=30)
    final = asyncio.run(_final())
    samples = watched["samples"]
    load = [s for s in samples if s["t"] <= args.seconds]
    second_half = [s for s in load if s["t"] >= args.seconds / 2]
    slope = 0.0
    if len(second_half) >= 2:
        xs = [s["t"] for s in second_half]
        ys = [s["pending"] for s in second_half]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        den = sum((x - mx) ** 2 for x in xs) or 1.0
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den
    ok = sum(w["ok"] for w in wres)
    delivered_events = cres["unique"]
    total_cpu = sum(w["total_cpu_s"] for w in wres)
    load_wall = max(w["load_wall_s"] for w in wres)
    pending_end_load = load[-1]["pending"] if load else 0
    result = {
        "rate": rate,
        "workers": args.workers,
        "burn_us": args.burn_us,
        "seconds": args.seconds,
        "writes_ok": ok,
        "writes_err": sum(w["err"] for w in wres),
        "writes_shed": sum(w["shed"] for w in wres),
        "achieved_write_rps": ok / load_wall if load_wall else 0.0,
        "delivered_eps_during_load": watched["unique_at_load_end"] / args.seconds,
        "max_pending": max((s["pending"] for s in samples), default=0),
        "pending_at_load_end": pending_end_load,
        "pending_slope_per_s": round(slope, 2),
        "drain_s": watched["drain_s"],
        "dead_rows": final["dead"],
        "pending_final": final["pending"],
        "dlq": final["dlq"],
        "delivered_unique": delivered_events,
        "duplicates": cres["dups"],
        "lost": max(0, final["rows"] - final["dead"] - delivered_events),
        "lat_p50_ms": cres["lat_p50_ms"],
        "lat_p99_ms": cres["lat_p99_ms"],
        "writer_cpu_us_per_event": total_cpu / max(1, delivered_events) * 1e6,
        "writer_load_cpu_s": sum(w["load_cpu_s"] for w in wres),
        "writer_total_cpu_s": total_cpu,
        "sustainable": (
            watched["drain_s"] is not None
            and pending_end_load <= max(20, rate * 2)
            and slope <= max(1.0, rate * 0.02)
            and final["dead"] == 0
            and final["dlq"] == 0
        ),
        "samples": samples,
    }
    return result


async def _final() -> dict[str, Any]:
    import redis.asyncio as redis
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(f"{PG}/{DB}", pool_size=1)
    rds = redis.from_url(REDIS)
    s = await _sample(eng, rds)
    await eng.dispose()
    await rds.aclose()
    return s


def cmd_ladder(args: argparse.Namespace) -> None:
    import sandloom

    print(json.dumps({"sandloom": str(Path(sandloom.__file__).parent)}), flush=True)
    rates = [float(r) for r in args.rates.split(",")]
    out = Path(args.out) if args.out else None
    for rate in rates:
        res = run_rate(args, rate)
        res["label"] = args.label
        summary = {k: v for k, v in res.items() if k != "samples"}
        print(json.dumps(summary), flush=True)
        if out:
            with out.open("a") as f:
                f.write(json.dumps(res) + "\n")


# -- CPU per delivered event (no concurrent writes) ---------------------------
async def _insert_rows(st: Stack, n: int, age_s: float) -> list[Any]:
    """Bulk-insert ``n`` committed outbox rows; return ``_PendingEvent``s."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import insert

    from sandloom.core import unit_of_work as uow_mod
    from sandloom.core.infra.contracts import OutboxHandle
    from sandloom.core.sql_alchemy.models.outbox import OutboxMessage

    emitted = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=age_s)
    rows = []
    events = []
    for i in range(n):
        rid = uuid.uuid4()
        payload = {"n": i, "t": time.time()}
        rows.append(
            {
                "id": rid,
                "channel": CHANNEL,
                "schema_version": "v1",
                "payload": payload,
                "emitted_at": emitted,
            }
        )
        events.append(
            uow_mod_scope._PendingEvent(OutboxHandle(id=rid), CHANNEL, "v1", payload)
        )
    async with st.engine.begin() as conn:
        for start in range(0, n, 1000):
            await conn.execute(
                insert(OutboxMessage.__table__), rows[start : start + 1000]
            )
    return events


async def _cpu(
    mode: str, n: int, burst: int, events_per_job: int, deliver: int | None = None
) -> dict[str, Any]:
    from sandloom.core import unit_of_work as uow_mod
    from sandloom.core.tasks import relay as relay_mod

    await _prepare()
    st = Stack()
    # warm the connections / code paths
    warm = await _insert_rows(st, 8, 30.0 if mode == "relay" else 0.0)
    if mode == "relay":
        await relay_mod._relay_batch_for_store(
            st.factory, st.store, st.bus, sweep=False
        )
    elif mode == "dispatch":
        d = uow_mod_dispatch.post_commit_dispatcher()
        d.submit(uow_mod_dispatch._DispatchJob(st.bus, st.store, st.factory, tuple(warm), ()))
        await uow_mod_dispatch.drain_post_commit()
    events = await _insert_rows(st, n, 30.0 if mode == "relay" else 0.0) if n else []
    # cachegrind: both runs insert the same rows, only one delivers them
    if deliver is not None:
        events = events[:deliver]
    n = len(events)
    cpu0 = time.process_time()
    wall0 = time.perf_counter()
    if mode == "xadd":
        # transport only: XADD per event (burst 1) vs publish_many(burst)
        envs = [
            (
                CHANNEL,
                {
                    "id": str(ev.handle.id),
                    "channel": CHANNEL,
                    "schema_version": "v1",
                    "trace_id": None,
                    "emitted_at": "2026-10-21T00:00:00+00:00",
                    "payload": ev.payload,
                },
            )
            for ev in events
        ]
        tr: Any = st.transport
        for start in range(0, len(envs), burst):
            chunk = envs[start : start + burst]
            if burst == 1 or not hasattr(tr, "publish_many"):
                for stream, env in chunk:
                    await tr.publish(stream, env)
            else:
                await tr.publish_many(chunk)
    elif mode == "relay" and events:
        while True:
            res = await relay_mod._relay_batch_for_store(
                st.factory, st.store, st.bus, sweep=False
            )
            if not res.claimed:
                break
    else:
        jobs = [
            tuple(events[i : i + events_per_job])
            for i in range(0, len(events), events_per_job)
        ]
        for start in range(0, len(jobs), burst):
            d = uow_mod_dispatch.post_commit_dispatcher()
            for job in jobs[start : start + burst]:
                d.submit(uow_mod_dispatch._DispatchJob(st.bus, st.store, st.factory, job, ()))
            await uow_mod_dispatch.drain_post_commit()
    cpu = time.process_time() - cpu0
    wall = time.perf_counter() - wall0
    final = await _final()
    await st.close()
    return {
        "mode": mode,
        "n": n,
        "burst": burst,
        "events_per_job": events_per_job,
        "cpu_us_per_event": cpu / max(1, n) * 1e6,
        "wall_us_per_event": wall / max(1, n) * 1e6,
        "pending_after": final["pending"],
        "xlen": final["xlen"],
    }


def cmd_cpu(args: argparse.Namespace) -> None:
    runs = [
        asyncio.run(
            _cpu(args.mode, args.n, args.burst, args.events_per_job, args.deliver)
        )
        for _ in range(args.repeat)
    ]
    cpu = sorted(r["cpu_us_per_event"] for r in runs)
    res = {
        **runs[0],
        "label": args.label,
        "cpu_us_per_event": statistics.median(cpu),
        "cpu_us_runs": [round(c, 1) for c in cpu],
        "wall_us_per_event": statistics.median(r["wall_us_per_event"] for r in runs),
    }
    import sandloom

    res["sandloom"] = str(Path(sandloom.__file__).parent)
    print(json.dumps(res), flush=True)


def cmd_cgrun(args: argparse.Namespace) -> None:
    """Inside the cachegrind image: Ir(k) - Ir(0) over k delivered events."""
    import re
    import subprocess

    def ir(n: int) -> int:
        r = subprocess.run(
            [
                "valgrind",
                "--tool=cachegrind",
                "--cache-sim=no",
                "--cachegrind-out-file=/dev/null",
                sys.executable,
                __file__,
                *(["--src", args.src] if args.src else []),
                "cpu",
                "--mode",
                args.mode,
                "--n",
                str(args.k),
                "--deliver",
                str(n),
                "--burst",
                str(args.burst),
                "--repeat",
                "1",
            ],
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONHASHSEED="0"),
        )
        m = re.search(r"I\s+refs:\s+([\d,]+)", r.stderr)
        if not m or r.returncode != 0:
            raise RuntimeError(r.stderr[-3000:])
        return int(m.group(1).replace(",", ""))

    a, z = ir(0), ir(args.k)
    print(
        json.dumps(
            {
                "label": args.label,
                "mode": args.mode,
                "burst": args.burst,
                "k": args.k,
                "instructions_per_event": (z - a) / args.k,
            }
        ),
        flush=True,
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", help="src of the framework code to measure")
    p.add_argument("--label", default="")
    sub = p.add_subparsers(dest="cmd", required=True)
    lad = sub.add_parser("ladder")
    lad.add_argument("--rates", default="150,200,250,300")
    lad.add_argument("--seconds", type=float, default=30)
    lad.add_argument("--workers", type=int, default=2)
    lad.add_argument("--burn-us", type=float, default=0.0)
    lad.add_argument("--burn-chunks", type=int, default=4)
    lad.add_argument("--sample", type=float, default=0.5)
    lad.add_argument("--startup", type=float, default=4.0)
    lad.add_argument("--drain-timeout", type=float, default=120.0)
    lad.add_argument("--out")
    cpu = sub.add_parser("cpu")
    cpu.add_argument(
        "--mode", choices=["dispatch", "relay", "xadd"], default="dispatch"
    )
    cpu.add_argument("--n", type=int, default=2000)
    cpu.add_argument("--deliver", type=int, default=None, help="deliver only N")
    cpu.add_argument("--burst", type=int, default=64)
    cpu.add_argument("--events-per-job", type=int, default=1)
    cpu.add_argument("--repeat", type=int, default=3)
    cg = sub.add_parser("cgrun")
    cg.add_argument("--mode", choices=["dispatch", "relay", "xadd"], default="dispatch")
    cg.add_argument("--k", type=int, default=400)
    cg.add_argument("--burst", type=int, default=64)
    args = p.parse_args()
    _setup_path(args.src)
    {"ladder": cmd_ladder, "cpu": cmd_cpu, "cgrun": cmd_cgrun}[args.cmd](args)


if __name__ == "__main__":
    main()
