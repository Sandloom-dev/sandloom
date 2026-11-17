"""Run coroutines from synchronous worker code on ONE loop per process.

Why not ``asyncio.run`` per task: the async engines/pools (SQLAlchemy +
asyncpg, async cache and broker clients) are process-wide singletons, and every
pooled connection is bound to the event loop that opened it. ``asyncio.run``
creates — and then closes — a fresh loop per call, so the 2nd task in a worker
child gets a pooled connection whose loop is already closed (``Event loop is closed`` /
``attached to a different loop``). :func:`run_async` keeps one persistent loop
per worker PROCESS, so the pool and the loop live (and die) together.

Lifecycle (a host that forks task processes):

* ``WorkerBoot.on_process_start`` (after the fork) — drop any loop inherited from
  the parent and start a fresh one for this child (:func:`reset_loop`).
* ``WorkerBoot.on_process_stop`` — run the registered async cleanups (the worker
  registers "dispose the database engines / caches / registry instances") on
  that same loop, then close it (:func:`shutdown_loop`).

The kernel's boot (:class:`sandloom.core.worker.KernelWorkerBoot`) calls both; the
loop is also created lazily and re-created when the PID changes, so a host that
forks nothing, a script or a test works without them. The kernel names no
task-queue backend here.

Concurrency comes from worker *processes*: calls are serialized per process
(a lock), so a ``threads`` pool still works correctly, just without overlap.
Business modules reach this through ``sandloom.core.di.run_async``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar

import sandloom.core.unit_of_work.dispatch as _dispatch

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: An async (or sync) callable run on the worker loop before it closes.
LoopCleanup = Callable[[], Awaitable[Any] | None]

_lock = threading.RLock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_pid: int | None = None
_cleanups: list[LoopCleanup] = []
_POST_COMMIT_DRAIN_SECONDS = 5.0


async def _drain_post_commit() -> None:
    # Drain before the loop pauses/closes and BEFORE registered cleanups dispose
    # its DB pools. A timed-out dispatch remains durable for the API relay.
    try:
        await _dispatch.drain_post_commit(timeout=_POST_COMMIT_DRAIN_SECONDS)
    except Exception:  # committed effects must not be reported failed
        logger.warning(
            "Worker post-commit drain failed; outbox relay will retry", exc_info=True
        )


async def _run_and_drain(coro: Coroutine[Any, Any, T]) -> T:
    try:
        return await coro
    finally:
        await _drain_post_commit()


def _current_loop() -> asyncio.AbstractEventLoop:
    """Return this process's persistent loop, creating it on first use."""
    global _loop, _loop_pid
    pid = os.getpid()
    if _loop is None or _loop.is_closed() or _loop_pid != pid:
        # A loop inherited across fork() is unusable here (its selector and
        # the connections bound to it belong to the parent): replace it.
        _loop = asyncio.new_event_loop()
        _loop_pid = pid
    return _loop


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` to completion on this process's persistent event loop.

    Use from synchronous code that has no running loop — a Celery task body,
    a script. Inside ``async`` code ``await`` the coroutine instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError(
            "run_async() called from inside a running event loop; "
            "`await` the coroutine instead"
        )
    with _lock:
        loop = _current_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_run_and_drain(coro))


def add_loop_cleanup(fn: LoopCleanup) -> None:
    """Register ``fn`` to run on the worker loop at :func:`shutdown_loop`."""
    with _lock:
        if fn not in _cleanups:
            _cleanups.append(fn)


def reset_loop() -> None:
    """Forget the current loop and start fresh (call right after ``fork``).

    The inherited loop is deliberately NOT closed: closing it would tear down
    resources (selector, transports) that still belong to the parent.
    """
    global _loop, _loop_pid
    with _lock:
        _loop = None
        _loop_pid = None
        _current_loop()


def shutdown_loop() -> None:
    """Run the registered cleanups on the loop, then close it (idempotent)."""
    global _loop, _loop_pid
    with _lock:
        loop = _loop
        if loop is None or loop.is_closed() or _loop_pid != os.getpid():
            _loop, _loop_pid = None, None
            return
        try:
            loop.run_until_complete(_drain_post_commit())
            for fn in reversed(_cleanups):
                try:
                    res = fn()
                    if inspect.isawaitable(res):
                        loop.run_until_complete(_as_coro(res))
                except Exception:  # best-effort teardown
                    logger.warning(
                        "[async_runner] loop cleanup %r failed", fn, exc_info=True
                    )
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
            _loop, _loop_pid = None, None


async def _as_coro(aw: Awaitable[Any]) -> Any:
    return await aw


__all__ = [
    "LoopCleanup",
    "add_loop_cleanup",
    "reset_loop",
    "run_async",
    "shutdown_loop",
]
