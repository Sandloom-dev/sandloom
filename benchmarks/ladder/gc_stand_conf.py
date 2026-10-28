"""Gunicorn config of the GC stand (``ladder/gc_stand.py``): the shipped config
plus a per-worker GC probe and, optionally, STAND-LOCAL GC variants.

DIAGNOSTIC ONLY. The probe is a ``gc.callbacks`` hook that writes scalars into
preallocated ``array('d')`` buffers (no allocation per collection beyond the
callback's own frame) and dumps them as JSON at lifespan shutdown
(``GCSTAND_OUT/<pid>.json``). Every event carries the wall-clock time it
ended, so the driver slices them by measurement window.

Stand-local variants (``GCSTAND_*``) exist to compare candidates BEFORE the
kernel ships them; with none set the worker runs whatever the framework does
(baseline on an old kernel, the ``GC_*`` settings on a new one):

* ``GCSTAND_FREEZE=early``  ``gc.freeze()`` in ``post_worker_init`` (app
  imported, lifespan not started)
* ``GCSTAND_FREEZE=startup`` ``gc.collect(); gc.freeze()`` when the lifespan
  reports ``startup.complete``
* ``GCSTAND_THRESHOLDS=a,b,c`` ``gc.set_threshold(a, b, c)`` at the same point
* ``GCSTAND_PERIODIC=young_s,full_s`` ``gc.disable()`` + a loop task running
  ``gc.collect(1)`` every ``young_s`` and ``gc.collect(2)`` every ``full_s``
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import resource
import time
from array import array
from typing import Any

from sandloom.core.cli.gunicorn_conf import *  # noqa: F403
from sandloom.core.cli.gunicorn_conf import post_worker_init as _sandloom_post_worker_init

accesslog = None

_CAP = 400_000
_T_END = array("d", bytes(8 * _CAP))
_WALL = array("d", bytes(8 * _CAP))
_CPU = array("d", bytes(8 * _CAP))
_GEN = array("b", bytes(_CAP))
_COLLECTED = array("d", bytes(8 * _CAP))
_state = {"n": 0, "t0": 0.0, "c0": 0.0}
_meta: dict[str, Any] = {}


def _probe(phase: str, info: dict[str, int]) -> None:
    if phase == "start":
        _state["t0"] = time.perf_counter()
        _state["c0"] = time.thread_time()
        return
    n = int(_state["n"])
    if n >= _CAP:
        return
    _WALL[n] = time.perf_counter() - _state["t0"]
    _CPU[n] = time.thread_time() - _state["c0"]
    _T_END[n] = time.time()
    _GEN[n] = info["generation"]
    _COLLECTED[n] = info["collected"]
    _state["n"] = n + 1


def _tracked() -> dict[str, int]:
    return {"tracked": len(gc.get_objects()), "frozen": gc.get_freeze_count()}


def _apply_startup_variant() -> None:
    thr = os.environ.get("GCSTAND_THRESHOLDS", "")
    if thr:
        gc.set_threshold(*(int(x) for x in thr.split(",")))
    if os.environ.get("GCSTAND_FREEZE") == "startup":
        gc.collect()
        gc.freeze()
    periodic = os.environ.get("GCSTAND_PERIODIC", "")
    if periodic:
        young_s, full_s = (float(x) for x in periodic.split(","))
        gc.disable()
        loop = asyncio.get_running_loop()

        def young() -> None:
            gc.collect(1)
            loop.call_later(young_s, young)

        def full() -> None:
            gc.collect(2)
            loop.call_later(full_s, full)

        loop.call_later(young_s, young)
        loop.call_later(full_s, full)
    _meta["after_startup"] = {
        **_tracked(),
        "threshold": gc.get_threshold(),
        "enabled": gc.isenabled(),
        "t": time.time(),
    }


class _StartupHook:
    """ASGI wrapper: apply the variant right after lifespan startup completes."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        async def hooked_send(message: Any) -> None:
            if message.get("type") == "lifespan.startup.complete":
                _apply_startup_variant()
            elif message.get("type") == "lifespan.shutdown.complete":
                # uvicorn re-raises the captured SIGTERM after shutdown, so
                # the worker dies by the signal: gunicorn's worker_exit hook
                # never runs. Dump here instead.
                _dump()
            await send(message)

        await self.app(scope, receive, hooked_send)


def post_worker_init(worker: Any) -> None:
    _sandloom_post_worker_init(worker)
    _meta["after_import"] = {**_tracked(), "t": time.time()}
    if os.environ.get("GCSTAND_FREEZE") == "early":
        gc.freeze()
    worker.wsgi = _StartupHook(worker.wsgi)
    gc.callbacks.append(_probe)


def _dump() -> None:
    out = os.environ.get("GCSTAND_OUT")
    if not out:
        return
    n = int(_state["n"])
    doc = {
        **_meta,
        "pid": os.getpid(),
        "maxrss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "at_exit": {**_tracked(), "threshold": gc.get_threshold()},
        "stats": gc.get_stats(),
        "events": {
            "t_end": _T_END[:n].tolist(),
            "wall": _WALL[:n].tolist(),
            "cpu": _CPU[:n].tolist(),
            "gen": _GEN[:n].tolist(),
            "collected": _COLLECTED[:n].tolist(),
        },
    }
    with open(os.path.join(out, f"{os.getpid()}.json"), "w") as fh:
        json.dump(doc, fh)
