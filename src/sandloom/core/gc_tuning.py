"""Cyclic garbage-collector configuration of long-running server processes.

Why: CPython's full (generation-2) collection traverses EVERY tracked object
in the process. In a Sandloom worker most of them are the startup heap — modules,
classes, functions, SQLAlchemy mappers / metadata, pydantic validators, the DI
graph — none of which ever becomes garbage. Under load, request objects that
live across awaits get promoted to generation 2, so full collections run
every few seconds, each stopping the event loop for tens to hundreds of ms
(Oracle, 3.12, 650 req/s on 2 workers: 12-31 full collections per 30 s at
~136 ms each, 6-14 % of CPU). Measurements and sources:
``docs/design/gc-tuning-2026-10-23.md``.

What (settings, ``sandloom.core.config.settings``):

* ``GC_FREEZE_AFTER_STARTUP`` — after the app's lifespan startup completes
  (before the worker accepts traffic): ``gc.collect()`` then ``gc.freeze()``.
  Frozen objects are never traversed again; a full collection then costs what
  the request-era heap costs. Safe: references from frozen objects count as
  external references, so nothing they point to is freed early. Cost: a cycle
  that includes a frozen object is never collected — hence the collect first,
  and exactly one freeze per process (never periodically).
* ``GC_THRESHOLDS`` — ``gc.set_threshold(t0, t1, t2)`` at the same moment.
  Not applied on CPython 3.14.0-3.14.4, whose incremental collector gives the
  arguments other meanings (3.14.5 restored the generational collector).
* ``GC_METRICS_ENABLED`` — per-generation pause histogram
  (``sandloom_gc_pause_seconds``), collected-object counter and frozen-object gauge.

Where: the gunicorn worker hook (``sandloom.core.cli.gunicorn_conf``) wraps the
ASGI app with :class:`GcAfterStartup`; the Celery worker freezes in the
parent before the pool forks (:func:`install_celery_hooks`). ``create_app``
never touches the collector: test processes build many apps and must not pin
each one's heap.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sys
import time
from array import array
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("sandloom.gc")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: Seconds between flushes of the GC metric buffer into prometheus_client.
METRICS_FLUSH_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class GcConfig:
    freeze_after_startup: bool = True
    thresholds: tuple[int, int, int] | None = None
    metrics: bool = True

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> GcConfig:
        if settings is None:
            from sandloom.core.config.settings import settings as current  # noqa: PLC0415 - cold: gunicorn_conf imports this module in the master at config load; settings are built in the workers

            settings = current
        return cls(
            freeze_after_startup=bool(settings.gc_freeze_after_startup),
            thresholds=settings.gc_thresholds,
            metrics=bool(settings.gc_metrics_enabled),
        )


def thresholds_supported(version: tuple[int, ...] | None = None) -> bool:
    """False on 3.14.0-3.14.4: the incremental GC ignores threshold2 and reads
    threshold1 as an increment-size factor, so generational tuning misfires."""
    v = tuple((version or sys.version_info)[:3])
    return not ((3, 14, 0) <= v < (3, 14, 5))


_applied: dict[str, Any] | None = None


def apply_after_startup(config: GcConfig) -> dict[str, Any]:
    """Thresholds, then collect + freeze. Idempotent per process: the second
    call returns the first call's summary (a later freeze would pin
    request-era garbage)."""
    global _applied
    if _applied is not None:
        return _applied
    started = time.perf_counter()
    thresholds: tuple[int, ...] = gc.get_threshold()
    if config.thresholds is not None:
        if thresholds_supported():
            gc.set_threshold(*config.thresholds)
            thresholds = gc.get_threshold()
        else:
            logger.warning(
                "GC_THRESHOLDS ignored on Python %s: the incremental collector "
                "of 3.14.0-3.14.4 interprets the thresholds differently",
                sys.version.split()[0],
            )
    frozen = gc.get_freeze_count()
    collected = 0
    if config.freeze_after_startup:
        collected = gc.collect()
        gc.freeze()
        frozen = gc.get_freeze_count()
        if config.metrics:
            from sandloom.core.observability import metrics  # noqa: PLC0415 - cold: prometheus_client fixes its storage at import; the gunicorn master (which imports this module) must not load it before on_starting

            metrics.gc_frozen_objects.set(frozen)
    _applied = {
        "frozen": frozen,
        "collected_before_freeze": collected,
        "thresholds": thresholds,
        "duration_ms": round((time.perf_counter() - started) * 1e3, 1),
    }
    logger.info(
        "sandloom gc configured: frozen=%s collected_before_freeze=%s thresholds=%s "
        "in %s ms",
        frozen,
        collected,
        thresholds,
        _applied["duration_ms"],
    )
    return _applied


class GcMetrics:
    """``gc.callbacks`` hook feeding the GC metrics WITHOUT touching
    prometheus_client inside the collector.

    A collection can start inside any allocation — including one made while
    prometheus_client holds its (non-reentrant) value lock — so observing
    from the callback could deadlock the worker. The callback only writes
    scalars into a preallocated ring (single producer: the callback; single
    consumer: :meth:`flush`, run from the event loop); a full ring drops
    samples (``dropped``) rather than allocate.
    """

    def __init__(self, capacity: int = 4096) -> None:
        self.capacity = capacity
        self._pause = array("d", bytes(8 * capacity))
        self._collected = array("d", bytes(8 * capacity))
        self._gen = array("b", bytes(capacity))
        self._write = 0
        self._read = 0
        self._started = 0.0
        self.dropped = 0

    def __call__(self, phase: str, info: dict[str, int]) -> None:
        if phase == "start":
            self._started = time.perf_counter()
            return
        w = self._write
        if w - self._read >= self.capacity:
            self.dropped += 1
            return
        i = w % self.capacity
        self._pause[i] = time.perf_counter() - self._started
        self._collected[i] = info.get("collected", 0)
        self._gen[i] = info.get("generation", 0)
        self._write = w + 1

    def flush(self) -> int:
        """Move buffered samples into prometheus_client; returns how many."""
        from sandloom.core.observability import metrics  # noqa: PLC0415 - cold: prometheus_client fixes its storage at import; the gunicorn master (which imports this module) must not load it before on_starting

        end = self._write
        start = self._read
        for k in range(start, end):
            i = k % self.capacity
            gen = str(self._gen[i])
            metrics.gc_pause_seconds.labels(gen).observe(self._pause[i])
            if self._collected[i]:
                metrics.gc_collected_objects_total.labels(gen).inc(self._collected[i])
        self._read = end
        return end - start


_gc_metrics: GcMetrics | None = None


def install_gc_metrics() -> GcMetrics:
    """Register the callback once per process; returns the buffer."""
    global _gc_metrics
    if _gc_metrics is None:
        _gc_metrics = GcMetrics()
    if _gc_metrics not in gc.callbacks:
        gc.callbacks.append(_gc_metrics)
    return _gc_metrics


def _start_flusher(buffer: GcMetrics) -> None:
    loop = asyncio.get_running_loop()

    def tick() -> None:
        try:
            buffer.flush()
        except Exception:  # pragma: no cover - metrics are best effort
            logger.debug("gc metrics flush failed", exc_info=True)
        loop.call_later(METRICS_FLUSH_INTERVAL_SECONDS, tick)

    loop.call_later(METRICS_FLUSH_INTERVAL_SECONDS, tick)


class GcAfterStartup:
    """ASGI wrapper: apply :class:`GcConfig` when the wrapped app reports
    ``lifespan.startup.complete`` — after every startup task (pools, consumers,
    caches), before the server accepts a connection."""

    def __init__(self, app: ASGIApp, config: GcConfig) -> None:
        self.app = app
        self.config = config

    def __getattr__(self, name: str) -> Any:
        # attribute access (``app.state`` …) reaches the wrapped app
        return getattr(self.app, name)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        async def on_send(message: Message) -> None:
            if message.get("type") == "lifespan.startup.complete":
                try:
                    configure_process(self.config, flush_on_loop=True)
                except Exception:  # never fail a started app over GC tuning
                    logger.exception("sandloom gc configuration failed")
            await send(message)

        await self.app(scope, receive, on_send)


def configure_process(config: GcConfig, *, flush_on_loop: bool = False) -> None:
    """Metrics first (so the freeze's own collection is observed), then the
    thresholds and the freeze."""
    if config.metrics:
        buffer = install_gc_metrics()
        if flush_on_loop:
            _start_flusher(buffer)
    apply_after_startup(config)


def install_celery_hooks(config: GcConfig | None = None) -> None:
    """Freeze in the Celery PARENT, before the pool forks (``worker_init``):
    the children inherit the frozen startup heap (and a CoW-friendlier
    layout). Thresholds are inherited too. No metrics: a Celery worker
    exports no Prometheus endpoint."""
    from celery import signals  # noqa: PLC0415 - cold: Celery is imported only by the worker; gunicorn and the app use the rest of this module

    def on_worker_init(**_: Any) -> None:
        cfg = config or GcConfig.from_settings()
        try:
            apply_after_startup(
                GcConfig(cfg.freeze_after_startup, cfg.thresholds, False)
            )
        except Exception:  # pragma: no cover - never block the worker
            logger.exception("sandloom gc configuration failed")

    signals.worker_init.connect(on_worker_init, weak=False)


__all__ = [
    "GcAfterStartup",
    "GcConfig",
    "GcMetrics",
    "apply_after_startup",
    "configure_process",
    "install_celery_hooks",
    "install_gc_metrics",
    "thresholds_supported",
]
