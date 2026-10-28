"""Soak-stand server wrapper (DIAGNOSTIC ONLY): the benchsuite Sandloom app plus

* **cancel-on-disconnect** — uvicorn never cancels a handler whose client went
  away; this ASGI wrapper does (like a request-timeout middleware or a server
  that cancels on disconnect would), so a client-side abort becomes a REAL
  ``CancelledError`` at a random point of the request: auth, pool checkout,
  the merged ``BEGIN; set_config`` message, a statement, COMMIT. The body is
  buffered first (small JSON), then the app runs as a task while the wrapper
  waits for ``http.disconnect``; a disconnect before the final response body
  cancels the task. Work after the response (post-commit dispatch) is never
  cancelled.
* **instrumentation** — counts every call / outcome of
  ``tenant_rls._merged_begin`` (looked up by global name at call time, so the
  patch is seen by the installed hook), the pool's ``connect`` / ``checkout``
  / ``invalidate`` events, and serves them with the pool state on
  ``GET /_soak/state`` (the wrapper answers it; it never reaches the app).

    uvicorn soak_app:app   # cwd/PYTHONPATH: <export>/benchmarks + this dir
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from typing import Any

from sandloom.core.sql_alchemy import driver_compat, tenant_rls
from benchsuite.apps.sandloom_app import app as sandloom_app

STATS: dict[str, Any] = {
    "merged_calls": 0,
    "merged_ok": 0,
    "merged_noop": 0,
    "merged_err": Counter(),
    "cancelled": 0,
    "cancelled_by_path": Counter(),
    "checkouts": 0,
    "connects": 0,
    "invalidations": Counter(),
    "requests": 0,
}

_orig_merged_begin = tenant_rls._merged_begin


async def _counting_merged_begin(adapter: Any, value: str) -> bool:
    STATS["merged_calls"] += 1
    try:
        done = await _orig_merged_begin(adapter, value)
    except BaseException as error:
        STATS["merged_err"][type(error).__name__] += 1
        raise
    STATS["merged_ok" if done else "merged_noop"] += 1
    return done


tenant_rls._merged_begin = _counting_merged_begin  # type: ignore[assignment]
_listening = False


def _engine() -> Any:
    return sandloom_app.state.core.database("default").engine


def _listen() -> None:
    global _listening
    if _listening:
        return
    from sqlalchemy import event

    pool = _engine().sync_engine.pool

    def on_connect(*_a: Any) -> None:
        STATS["connects"] += 1

    def on_checkout(*_a: Any) -> None:
        STATS["checkouts"] += 1

    def on_invalidate(_c: Any, _r: Any, exc: Any) -> None:
        STATS["invalidations"][type(exc).__name__ if exc else "None"] += 1

    event.listen(pool, "connect", on_connect)
    event.listen(pool, "checkout", on_checkout)
    event.listen(pool, "invalidate", on_invalidate)
    _listening = True


def _state() -> dict[str, Any]:
    _listen()
    pool = _engine().sync_engine.pool
    from sandloom.core.config.settings import settings

    return {
        "pid": os.getpid(),
        "merged_setting": settings.db_rls_merged_begin,
        "probe_ok": driver_compat.asyncpg_fast_paths_ok(),
        "pool": {
            "size": pool.size(),
            "checkedout": pool.checkedout(),
            "overflow": pool.overflow(),
            "status": pool.status(),
        },
        "inflight": len(_INFLIGHT),
        "dead_conn_replaced": _dead_conn_replaced(),
        **{k: (dict(v) if isinstance(v, Counter) else v) for k, v in STATS.items()},
    }


def _dead_conn_replaced() -> dict[str, float]:
    """Dead pooled connections replaced before a byte was sent, by phase
    (``sandloom_db_dead_connection_replaced_total``; empty on older servers)."""
    try:
        from sandloom.core.observability.metrics import db_dead_connection_replaced_total
    except ImportError:
        return {}
    out: dict[str, float] = {}
    for metric in db_dead_connection_replaced_total.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                out[sample.labels.get("phase", "?")] = sample.value
    return out


_INFLIGHT: set[asyncio.Task[Any]] = set()


async def _send_json(send: Any, body: dict[str, Any]) -> None:
    raw = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": raw})


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        await sandloom_app(scope, receive, send)
        return
    if scope["path"] == "/_soak/state":
        await _send_json(send, _state())
        return
    STATS["requests"] += 1
    # 1. buffer the request body (the stand's bodies are tiny JSON)
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    body = b"".join(chunks)
    gone = asyncio.Event()
    replayed = False

    async def app_receive() -> dict[str, Any]:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    finished = False

    async def app_send(message: dict[str, Any]) -> None:
        nonlocal finished
        if message["type"] == "http.response.body" and not message.get("more_body"):
            finished = True
        await send(message)

    task = asyncio.ensure_future(sandloom_app(scope, app_receive, app_send))
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
    watcher = asyncio.ensure_future(receive())
    try:
        while True:
            done, _ = await asyncio.wait(
                {task, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return task.result()
            msg = watcher.result()
            if msg["type"] != "http.disconnect":  # unexpected extra message
                watcher = asyncio.ensure_future(receive())
                continue
            gone.set()
            if not finished:  # client aborted before the full response
                STATS["cancelled"] += 1
                STATS["cancelled_by_path"][scope["path"].split("/")[1]] += 1
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return None
            await task  # response sent; let post-response work finish
            return None
    finally:
        if not watcher.done():
            watcher.cancel()
