"""Per-request wait attribution inside a served Sandloom app (``write_tail.py``).

DIAGNOSTIC ONLY. Imported as the gunicorn app (``tail_probe:app``): it wraps
``benchsuite.apps.sandloom_app:app`` and patches, in each worker, the places a
request WAITS, timing each with ``perf_counter`` (wall) into a per-request
record (a contextvar):

* ``pool``     — SQLAlchemy pool checkout (incl. the idle ping), i.e. waiting
  for a free connection;
* ``pg_begin`` — the transaction start (Sandloom's merged ``BEGIN; set_config``);
* ``pg_stmt``  — every other statement round trip (prepare / bind / execute;
  a row-lock wait shows up here);
* ``pg_commit`` — ``COMMIT`` (WAL flush: ``fsync`` / ``synchronous_commit``);
* ``redis:<instance>`` — each command / pipeline, by the Redis it targets
  (``security``, ``cache``, ``broker``);
* ``loop_lag`` — the worker's event-loop lag (a 5 ms sampler) during the
  request, recorded separately and joined offline.

Work of the post-commit dispatcher (XADD + mark-delivered) runs off the
request path; it gets records of its own (``kind = dispatch``). Records are
JSON lines in ``$TAIL_OUT/tail-<pid>.jsonl``. The client tags requests with
``x-tail-label`` so one server run can hold several load phases.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import time
from typing import Any
from urllib.parse import urlsplit

_REC: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "tail_rec", default=None
)
_OUT = os.environ.get("TAIL_OUT", "/out")
_buf: list[str] = []
_lags: list[tuple[float, float]] = []
#: (wall end, kind, seconds): GC collections (gen) and relay passes — the
#: synchronous stretches a loop-lag spike can be blamed on.
_events: list[tuple[float, str, float]] = []
_fh: Any = None


def _emit(rec: dict[str, Any]) -> None:
    global _fh
    _buf.append(json.dumps(rec, separators=(",", ":")))
    if len(_buf) >= 200:
        _flush()


def _flush() -> None:
    global _fh
    if _fh is None:
        os.makedirs(_OUT, exist_ok=True)
        _fh = open(os.path.join(_OUT, f"tail-{os.getpid()}.jsonl"), "a")  # noqa: SIM115
    if _events:
        evs = list(_events)
        _events.clear()
        _fh.write(json.dumps({"kind": "events", "events": evs}) + "\n")
    if _lags:
        lags = list(_lags)
        _lags.clear()
        _fh.write(json.dumps({"kind": "lag", "samples": lags}) + "\n")
    if _buf:
        _fh.write("\n".join(_buf) + "\n")
        _buf.clear()
    _fh.flush()


def _add(phase: str, seconds: float) -> None:
    rec = _REC.get()
    if rec is not None and rec.get("open"):
        ph = rec["ph"]
        ph[phase] = ph.get(phase, 0.0) + seconds
        cnt = rec["n"]
        cnt[phase] = cnt.get(phase, 0) + 1


def _redis_labels() -> dict[tuple[str, int, int], str]:
    labels: dict[tuple[str, int, int], str] = {}

    def put(url: str | None, name: str) -> None:
        if not url:
            return
        p = urlsplit(url)
        db = int((p.path or "/0").lstrip("/") or 0)
        labels.setdefault((p.hostname or "", p.port or 6379, db), name)

    infra = json.loads(os.environ.get("INFRA") or "{}")
    put((infra.get("cache", {}).get("security") or {}).get("url"), "security")
    put((infra.get("cache", {}).get("idem") or {}).get("url"), "idem")
    put((infra.get("eventbus", {}).get("default") or {}).get("url"), "broker")
    put(os.environ.get("CACHE_URL"), "cache")
    put(os.environ.get("BROKER_URL"), "broker")
    return labels


def _install() -> None:
    idem = os.environ.get("TAIL_IDEMPOTENCY_INSTANCE")
    if idem:
        # EXPERIMENT (write_tail.py --split-idempotency): idempotency records
        # on their own Redis instance; revocation / block marks stay put.
        from sandloom.core.idempotency import cache_store as idempotency_cache_store
        from sandloom.core.infra.access import cache

        idempotency_cache_store._idempotency_cache = lambda: cache(idem)  # type: ignore[assignment]
    import asyncpg
    import redis.asyncio.client as rc
    from sqlalchemy.dialects.postgresql import asyncpg as sa_pg
    from sqlalchemy.pool import base as pool_base

    labels = _redis_labels()

    def redis_label(client: Any) -> str:
        kw = getattr(getattr(client, "connection_pool", None), "connection_kwargs", {})
        key = (kw.get("host", ""), int(kw.get("port", 6379)), int(kw.get("db", 0)))
        return "redis:" + labels.get(key, f"{key[0]}:{key[1]}/{key[2]}")

    orig_exec = rc.Redis.execute_command

    async def execute_command(self: Any, *args: Any, **kw: Any) -> Any:
        t = time.perf_counter()
        try:
            return await orig_exec(self, *args, **kw)
        finally:
            _add(redis_label(self), time.perf_counter() - t)

    rc.Redis.execute_command = execute_command  # type: ignore[method-assign]
    orig_pipe = rc.Pipeline.execute

    async def pipe_execute(self: Any, *args: Any, **kw: Any) -> Any:
        t = time.perf_counter()
        try:
            return await orig_pipe(self, *args, **kw)
        finally:
            _add(redis_label(self), time.perf_counter() - t)

    rc.Pipeline.execute = pipe_execute  # type: ignore[method-assign]

    orig_sql = sa_pg.AsyncAdapt_asyncpg_cursor._prepare_and_execute

    async def prepare_and_execute(self: Any, operation: Any, parameters: Any) -> Any:
        t = time.perf_counter()
        try:
            return await orig_sql(self, operation, parameters)
        finally:
            _add("pg_stmt", time.perf_counter() - t)

    sa_pg.AsyncAdapt_asyncpg_cursor._prepare_and_execute = prepare_and_execute

    orig_simple = asyncpg.Connection.execute

    async def simple_execute(self: Any, query: str, *args: Any, **kw: Any) -> Any:
        t = time.perf_counter()
        try:
            return await orig_simple(self, query, *args, **kw)
        finally:
            head = query.lstrip()[:8].upper()
            phase = (
                "pg_commit"
                if head.startswith("COMMIT")
                else "pg_begin"
                if head.startswith(("BEGIN", "START"))
                else "pg_rollback"
                if head.startswith("ROLLBACK")
                else "pg_stmt"
            )
            _add(phase, time.perf_counter() - t)

    asyncpg.Connection.execute = simple_execute  # type: ignore[method-assign]

    orig_checkout = pool_base._ConnectionFairy._checkout.__func__  # classmethod

    def checkout(cls: Any, *args: Any, **kw: Any) -> Any:
        t = time.perf_counter()
        try:
            return orig_checkout(cls, *args, **kw)
        finally:
            _add("pool", time.perf_counter() - t)

    pool_base._ConnectionFairy._checkout = classmethod(checkout)  # type: ignore[assignment]

    from sandloom.core import unit_of_work as uow
    from sandloom.core.unit_of_work import dispatch as uow_dispatch

    orig_round = uow_dispatch.PostCommitDispatcher._process_round

    async def process_round(self: Any, jobs: Any) -> Any:
        rec = {
            "kind": "dispatch",
            "open": True,
            "jobs": len(jobs),
            "t0": time.time(),
            "ph": {},
            "n": {},
        }
        token = _REC.set(rec)
        t = time.perf_counter()
        try:
            return await orig_round(self, jobs)
        finally:
            rec["total"] = time.perf_counter() - t
            rec["open"] = False
            _REC.reset(token)
            rec.pop("open", None)
            _emit(rec)

    uow_dispatch.PostCommitDispatcher._process_round = process_round

    import gc

    gc_start: dict[str, float] = {}

    def on_gc(phase: str, info: dict[str, Any]) -> None:
        if phase == "start":
            gc_start["t"] = time.perf_counter()
        elif "t" in gc_start:
            _events.append(
                (
                    time.time(),
                    f"gc{info['generation']}",
                    time.perf_counter() - gc_start.pop("t"),
                )
            )

    gc.callbacks.append(on_gc)

    from sandloom.core.sql_alchemy import outbox_store

    for meth in ("deliver_batch", "deliver_pending"):
        orig = getattr(outbox_store.SQLAlchemyOutboxStore, meth, None)
        if orig is None:
            continue

        def make(orig: Any, meth: str) -> Any:
            async def timed(self: Any, *args: Any, **kw: Any) -> Any:
                t = time.perf_counter()
                cpu = time.thread_time()
                try:
                    return await orig(self, *args, **kw)
                finally:
                    _events.append(
                        (time.time(), f"relay:{meth}", time.perf_counter() - t)
                    )
                    _events.append(
                        (time.time(), f"relay_cpu:{meth}", time.thread_time() - cpu)
                    )

            return timed

        setattr(outbox_store.SQLAlchemyOutboxStore, meth, make(orig, meth))


async def _lag_sampler() -> None:
    period = 0.005
    loop = asyncio.get_running_loop()
    while True:
        t = loop.time()
        await asyncio.sleep(period)
        lag = loop.time() - t - period
        _lags.append((time.time(), max(lag, 0.0)))
        if len(_lags) >= 2000:
            _flush()


_installed = False
_sampler: asyncio.Task[Any] | None = None


def _wrap(inner: Any) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        global _sampler
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                await inner(scope, receive, send)
                _flush()
                return
            await inner(scope, receive, send)
            return
        if _sampler is None:
            _sampler = asyncio.get_running_loop().create_task(_lag_sampler())
        label = ""
        for k, v in scope.get("headers") or ():
            if k == b"x-tail-label":
                label = v.decode()
        rec: dict[str, Any] = {
            "kind": "req",
            "open": True,
            "label": label,
            "path": scope.get("path"),
            "t0": time.time(),
            "ph": {},
            "n": {},
        }
        token = _REC.set(rec)
        t = time.perf_counter()
        status = [0]

        async def _send(msg: Any) -> None:
            if msg["type"] == "http.response.start":
                status[0] = msg["status"]
            await send(msg)

        try:
            await inner(scope, receive, _send)
        finally:
            rec["total"] = time.perf_counter() - t
            rec["status"] = status[0]
            rec["open"] = False
            _REC.reset(token)
            rec.pop("open", None)
            _emit(rec)
            if label == "flush":
                _flush()

    return app


def _build() -> Any:
    global _installed
    from benchsuite.apps.sandloom_app import app as inner

    if not _installed:
        _install()
        _installed = True
    import atexit

    atexit.register(_flush)
    return _wrap(inner)


app = _build()
