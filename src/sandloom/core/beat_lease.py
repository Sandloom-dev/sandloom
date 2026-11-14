"""Celery beat singleton lease: the Celery side of the beat's ``lease`` (spec 5).

Two ``celery beat`` processes on one broker both send every periodic task, so
each task runs twice per period. The framework beat therefore schedules only
while it holds a fenced lease of the ``lease`` instance ``INFRA.lease.beat``
(:class:`~sandloom.core.infra.fenced_lease.FencedLease`: the kernel's holder
state machine, shared with the saga sweep, with the acquire / renew / standby /
lost-lease rules and the lease name scope, documented there).

What stays here is Celery's: :class:`BeatLease`, a synchronous facade over the
holder for beat's synchronous scheduler loop, :class:`LeasedScheduler`, which
wraps whatever scheduler beat was started with (``-S`` included), and
:func:`install_beat_lease`, which applies it on Celery's ``beat_init`` signal
for standalone and embedded (``worker -B``) beats alike. The module moves to
``sandloom-celery`` with the worker (``idk-worker-port``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import sandloom.core.async_runner as _async_runner
from sandloom.core.config.settings import settings
from sandloom.core.infra.fenced_lease import (
    BEAT_INSTANCE,
    LEASE_KEY_PREFIX,
    FencedLease,
    beat_lease_store,
    beat_scope,
)
from sandloom.core.infra.ports.lease import LeaseStore

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BeatLeaseUnavailableError(RuntimeError):
    """The configured ``lease`` instance cannot serve a beat (not shared)."""


def _default_runner(coro: Coroutine[Any, Any, T]) -> T:
    return _async_runner.run_async(coro)


class BeatLease(FencedLease):
    """The beat's lease: a synchronous facade over :class:`FencedLease`.

    Beat's scheduler loop is synchronous; the lease's store operations run on
    the process's persistent loop (:func:`sandloom.core.async_runner.run_async`,
    or the injected ``runner``).
    """

    def __init__(
        self,
        store: LeaseStore,
        name: str,
        *,
        ttl_seconds: int,
        runner: Callable[[Coroutine[Any, Any, Any]], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        identity: str | None = None,
    ) -> None:
        super().__init__(
            store,
            name,
            ttl_seconds=ttl_seconds,
            clock=clock,
            identity=identity,
            label="[beat]",
        )
        self._run = runner or _default_runner

    def ensure(self) -> bool:
        """Acquire or renew as due; ``True`` while this process may schedule."""
        fast = self._fast_path()
        if fast is not None:
            return fast
        return bool(self._run(self.ensure_async()))

    def release(self) -> None:
        """Release our own lease (best-effort) so a standby takes over at once."""
        if self.token is not None:
            self._run(self.release_async())


class LeasedScheduler:
    """Wrap a Celery beat scheduler: it ticks only while the lease is held."""

    def __init__(self, inner: Any, lease: BeatLease) -> None:
        self._inner = inner
        self.lease = lease

    def tick(self, *args: Any, **kwargs: Any) -> float:
        if not self.lease.ensure():
            return self.lease.next_check_in()
        interval = self._inner.tick(*args, **kwargs)
        if interval and interval > 0:
            return min(float(interval), self.lease.next_check_in())
        return float(interval or 0)

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            self.lease.release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def build_lease(app: Any, core: Any, *, instance: str) -> BeatLease:
    """The lease for this beat (resolves the ``lease`` instance ``beat``;
    raises if unfit)."""
    store = beat_lease_store(core)
    features = store.features() if hasattr(store, "features") else frozenset()
    if settings.is_prod and "shared" not in features:
        raise BeatLeaseUnavailableError(
            f"lease instance {BEAT_INSTANCE!r} (driver "
            f"{getattr(store, 'driver', '?')!r}) is not shared between processes, "
            "so two beats could both hold it; point INFRA.lease.beat at a "
            "redis or database lease"
        )
    scope = beat_scope(getattr(app.conf, "broker_url", None), instance, core)
    return BeatLease(
        store, LEASE_KEY_PREFIX + scope, ttl_seconds=settings.beat_lease_ttl_seconds
    )


def install_beat_lease(app: Any, core: Any, *, instance: str = "default") -> None:
    """Wrap every beat scheduler of ``app`` in :class:`LeasedScheduler`.

    Connected to ``beat_init`` (sent by ``beat.Service.start`` before the first
    tick, for ``celery beat`` and ``worker -B``); a worker that runs no beat
    never resolves the cache.
    """
    from celery import signals  # noqa: PLC0415 - cold: Celery is imported only by the worker and the celery taskqueue driver

    if getattr(app, "_sandloom_beat_lease_installed", False):
        return

    def _on_beat_init(sender: Any = None, **_: Any) -> None:
        if sender is None or getattr(sender, "app", None) is not app:
            return
        scheduler = sender.scheduler
        if isinstance(scheduler, LeasedScheduler):
            return
        try:
            lease = build_lease(app, core, instance=instance)
        except Exception as exc:
            # Celery's signal dispatch logs and SWALLOWS receiver exceptions,
            # which would leave beat scheduling without a lease. SystemExit is
            # not swallowed: beat stops before its first tick (exit 1).
            logger.critical("[beat] refusing to schedule: %s", exc)
            sender.stop()
            raise SystemExit(1) from exc
        sender.scheduler = LeasedScheduler(scheduler, lease)
        logger.info("[beat] singleton lease %s installed", lease.name)

    signals.beat_init.connect(_on_beat_init, weak=False)
    app._sandloom_beat_lease_installed = True


__all__ = [
    "BeatLease",
    "BeatLeaseUnavailableError",
    "LeasedScheduler",
    "build_lease",
    "install_beat_lease",
]
