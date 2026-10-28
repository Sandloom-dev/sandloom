"""Run coroutines from synchronous worker code on ONE loop per process.

Why not ``asyncio.run`` per task: the async engines/pools (SQLAlchemy +
asyncpg, async redis) are process-wide singletons, and every pooled connection
is bound to the event loop that opened it. ``asyncio.run`` creates — and then
closes — a fresh loop per call, so the 2nd task in a worker child gets a pooled
connection whose loop is already closed (``Event loop is closed`` /
``attached to a different loop``). :func:`run_async` keeps one persistent loop
per worker PROCESS, so the pool and the loop live (and die) together.

Lifecycle (prefork Celery):

* ``worker_process_init`` (after the fork) — drop any loop inherited from the
  parent and start a fresh one for this child (:func:`reset_loop`).
* ``worker_process_shutdown`` — run the registered async cleanups (the worker
  registers "dispose the database engines / caches / registry instances") on
  that same loop, then close it (:func:`shutdown_loop`).

:func:`install_celery_hooks` connects both (the framework worker does it in
``build_worker_app``). The loop is also created lazily and re-created when the
PID changes, so the solo pool, a script or a test works without the signals.

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
_hooks_installed = False
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


def _on_worker_process_init(**_: Any) -> None:
    reset_loop()


def _on_worker_process_shutdown(**_: Any) -> None:
    shutdown_loop()


def install_celery_hooks() -> None:
    """Wire :func:`reset_loop` / :func:`shutdown_loop` to Celery's signals.

    Celery is imported lazily (the kernel imports no task-queue backend at
    import time); calling this more than once is a no-op.
    """
    global _hooks_installed
    with _lock:
        if _hooks_installed:
            return
        from celery import signals  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver

        signals.worker_process_init.connect(_on_worker_process_init, weak=False)
        signals.worker_process_shutdown.connect(_on_worker_process_shutdown, weak=False)
        _hooks_installed = True


__all__ = [
    "LoopCleanup",
    "add_loop_cleanup",
    "install_celery_hooks",
    "reset_loop",
    "run_async",
    "shutdown_loop",
]
