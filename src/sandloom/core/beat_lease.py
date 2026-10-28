"""Celery beat singleton lease — one scheduler per broker and module set.

Two ``celery beat`` processes on one broker both send every periodic task, so
each task runs twice per period. The framework beat therefore schedules only
while it holds a lease in the cache capability (``BEAT_LEASE_CACHE_INSTANCE``,
the shared Redis in every deploy profile):

* **acquire** — ``add`` (``SET NX`` with a TTL of ``BEAT_LEASE_TTL_SECONDS``)
  of a token ``<fence>:<host>:<pid>:<nonce>``; ``<fence>`` is a monotonically
  increasing counter (``INCR``), so every holder gets a larger number than the
  previous one and the logs show the takeover order;
* **renew** — every ``ttl/3`` seconds, ``compare_and_set(token, token)`` with a
  fresh TTL: only the holder of THAT token can extend it (feature ``cas``);
* **standby** — a beat that does not hold the lease sends nothing; it retries
  every ``ttl/3`` seconds and takes over within ``ttl + ttl/3`` after the
  holder died (its lease expires) or at once after a clean shutdown (the
  holder deletes its own token);
* **lost lease** — a renewal that finds another token stops scheduling at
  once. A renewal that fails because the cache is unreachable keeps
  scheduling only until the LOCAL lease deadline (measured from before the
  last successful write, so it never outlives the server-side TTL); after
  that the beat stands by until it can write again.

The lease key is ``sandloom:beat:lease:<scope>``. The scope defaults to a digest of
the broker URL, the taskqueue instance and the sorted loaded module set, so
replicas of one service share a lease while services with different module
sets (each with its own periodic tasks) never suppress each other.
``BEAT_LEASE_NAME`` pins an explicit scope. The lease wraps whatever scheduler
beat was started with (``-S`` included): it is applied on Celery's
``beat_init`` signal, for standalone and embedded (``worker -B``) beats alike.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable, Coroutine, Iterable
from typing import Any, TypeVar

import sandloom.core.async_runner as _async_runner
from sandloom.core.config.settings import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

LEASE_KEY_PREFIX = "sandloom:beat:lease:"
#: How often a standby repeats "another beat holds the lease" (seconds).
STANDBY_LOG_INTERVAL_SECONDS = 300.0
#: Retry cadence of a renewal that failed on a cache error (seconds).
RENEW_ERROR_RETRY_SECONDS = 1.0


class BeatLeaseUnavailableError(RuntimeError):
    """The configured cache cannot hold a fenced beat lease (no ``cas``)."""


def lease_scope(
    broker_url: str | None, taskqueue_instance: str, modules: Iterable[str]
) -> str:
    """Default lease scope: one beat per (broker, taskqueue, module set)."""
    material = "|".join(
        [str(broker_url or ""), taskqueue_instance, ",".join(sorted(modules))]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _default_runner(coro: Coroutine[Any, Any, T]) -> T:
    return _async_runner.run_async(coro)


class FencedLease:
    """A fenced, renewable lease on one cache key (async state machine).

    The protocol of the module docstring, shared by the Celery beat
    (:class:`BeatLease`, a synchronous facade) and the saga timer sweep
    (``sandloom.core.sagas.sweep``, one holder per database). Every cache operation
    is bounded by ``ttl/3`` so a hung cache cannot stall the holder past the
    lease. ``clock`` is monotonic seconds (tests inject a fake one); ``label``
    prefixes the log lines.
    """

    def __init__(
        self,
        cache: Any,
        key: str,
        *,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        identity: str | None = None,
        label: str = "[lease]",
    ) -> None:
        if ttl_seconds < 3:
            raise ValueError("lease TTL must be at least 3 seconds")
        self.cache = cache
        self.key = key
        self.fence_key = f"{key}:fence"
        self.ttl_seconds = int(ttl_seconds)
        self.renew_interval = self.ttl_seconds / 3.0
        self.label = label
        self._clock = clock
        self._identity = identity or f"{socket.gethostname()}:{os.getpid()}"
        self.token: str | None = None
        self.fence: int | None = None
        self._valid_until = 0.0
        self._renew_at = 0.0
        self._retry_at = 0.0
        self._standby_logged_at: float | None = None

    @property
    def held(self) -> bool:
        return self.token is not None

    async def _bounded(self, coro: Coroutine[Any, Any, T]) -> T:
        return await asyncio.wait_for(coro, timeout=self.renew_interval)

    def _fast_path(self) -> bool | None:
        """The answer without a cache round trip, or ``None`` when one is due."""
        now = self._clock()
        if self.token is not None and now < self._renew_at:
            return True
        if self.token is None and now < self._retry_at:
            return False
        return None

    async def ensure_async(self) -> bool:
        """Acquire or renew as due; ``True`` while this process holds the lease."""
        fast = self._fast_path()
        if fast is not None:
            return fast
        now = self._clock()
        if self.token is not None:
            return await self._renew(now)
        return await self._acquire(now)

    def next_check_in(self) -> float:
        """Seconds until :meth:`ensure_async` has work to do (renew or retry)."""
        due = self._renew_at if self.token is not None else self._retry_at
        return max(0.05, due - self._clock())

    async def _try_acquire(self) -> tuple[bool, str | None, int | None, str | None]:
        holder = await self.cache.get(self.key)
        if holder is not None:
            return False, holder, None, None
        fence = int(await self.cache.incr(self.fence_key))
        token = f"{fence}:{self._identity}:{uuid.uuid4().hex[:12]}"
        if await self.cache.add(self.key, token, ttl_seconds=self.ttl_seconds):
            return True, None, fence, token
        return False, await self.cache.get(self.key), None, None

    async def _acquire(self, now: float) -> bool:
        try:
            won, holder, fence, token = await self._bounded(self._try_acquire())
        except Exception as exc:  # noqa: BLE001 - the cache is unreachable
            logger.warning(
                "%s cannot reach the lease cache (%s); not scheduling",
                self.label,
                type(exc).__name__,
            )
            self._retry_at = now + self.renew_interval
            return False
        if not won:
            self._retry_at = now + self.renew_interval
            if (
                self._standby_logged_at is None
                or now - self._standby_logged_at >= STANDBY_LOG_INTERVAL_SECONDS
            ):
                logger.warning(
                    "%s standby: another holder has lease %s (token %s); "
                    "this process schedules nothing until it is released or "
                    "expires",
                    self.label,
                    self.key,
                    holder,
                )
                self._standby_logged_at = now
            return False
        self.token, self.fence = token, fence
        self._valid_until = now + self.ttl_seconds
        self._renew_at = now + self.renew_interval
        self._standby_logged_at = None
        logger.warning(
            "%s acquired lease %s (fence %s, ttl %ss); scheduling",
            self.label,
            self.key,
            fence,
            self.ttl_seconds,
        )
        return True

    async def _renew(self, now: float) -> bool:
        token = self.token
        assert token is not None  # noqa: S101 - type narrowing of an invariant
        try:
            kept = await self._bounded(
                self.cache.compare_and_set(
                    self.key, token, token, ttl_seconds=self.ttl_seconds
                )
            )
        except Exception as exc:  # noqa: BLE001 - the cache is unreachable
            if now < self._valid_until:
                logger.warning(
                    "%s cannot renew lease %s (%s); still scheduling for "
                    "%.1fs until the local lease deadline",
                    self.label,
                    self.key,
                    type(exc).__name__,
                    self._valid_until - now,
                )
                self._renew_at = min(now + RENEW_ERROR_RETRY_SECONDS, self._valid_until)
                return True
            logger.error(
                "%s lease %s expired locally without a renewal; stopping scheduling",
                self.label,
                self.key,
            )
            self._drop(now)
            return False
        if kept:
            self._valid_until = now + self.ttl_seconds
            self._renew_at = now + self.renew_interval
            return True
        logger.error(
            "%s lost lease %s (fence %s): another holder took it over; "
            "stopping scheduling",
            self.label,
            self.key,
            self.fence,
        )
        self._drop(now + self.renew_interval)
        return False

    def _drop(self, retry_at: float) -> None:
        self.token = None
        self.fence = None
        self._retry_at = retry_at

    async def release_async(self) -> None:
        """Delete our own token (best-effort) so a standby takes over at once."""
        token = self.token
        if token is None:
            return
        self.token = None
        try:
            await self._bounded(self.cache.compare_and_delete(self.key, token))
            logger.info("%s released lease %s", self.label, self.key)
        except Exception:  # noqa: BLE001 - the TTL frees it anyway
            logger.warning("%s could not release lease %s", self.label, self.key)


class BeatLease(FencedLease):
    """The beat's lease: a synchronous facade over :class:`FencedLease`.

    Beat's scheduler loop is synchronous; the lease's cache operations run on
    the process's persistent loop (:func:`sandloom.core.async_runner.run_async`,
    or the injected ``runner``).
    """

    def __init__(
        self,
        cache: Any,
        key: str,
        *,
        ttl_seconds: int,
        runner: Callable[[Coroutine[Any, Any, Any]], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        identity: str | None = None,
    ) -> None:
        super().__init__(
            cache,
            key,
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
        """Delete our own token (best-effort) so a standby takes over at once."""
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
    """The lease for this beat (resolves the cache provider; raises if unfit)."""

    cache = core.cache(settings.beat_lease_cache_instance)
    features = cache.features() if hasattr(cache, "features") else frozenset()
    if "cas" not in features:
        raise BeatLeaseUnavailableError(
            f"cache instance {settings.beat_lease_cache_instance!r} (driver "
            f"{getattr(cache, 'driver', '?')!r}) has no 'cas' feature, so the "
            "beat lease cannot be renewed safely; point "
            "BEAT_LEASE_CACHE_INSTANCE at a Redis cache"
        )
    scope = settings.beat_lease_name.strip() or lease_scope(
        getattr(app.conf, "broker_url", None),
        instance,
        list(getattr(core, "loaded_modules", []) or []),
    )
    return BeatLease(
        cache, LEASE_KEY_PREFIX + scope, ttl_seconds=settings.beat_lease_ttl_seconds
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
        logger.info("[beat] singleton lease %s installed", lease.key)

    signals.beat_init.connect(_on_beat_init, weak=False)
    app._sandloom_beat_lease_installed = True


__all__ = [
    "LEASE_KEY_PREFIX",
    "BeatLease",
    "BeatLeaseUnavailableError",
    "FencedLease",
    "LeasedScheduler",
    "build_lease",
    "install_beat_lease",
    "lease_scope",
]
