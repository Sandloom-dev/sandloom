"""The kernel's use of the ``lease`` capability: one holder at a time, renewed.

:class:`FencedLease` is the holder's state machine over a
:class:`~sandloom.core.infra.ports.lease.LeaseStore` (the instance
``INFRA.lease.beat``, :data:`BEAT_INSTANCE`): the Celery beat
(``sandloom_celery.worker``, through ``WorkerHost.run_scheduler``)
and the saga timer sweep
(``sandloom.core.sagas.sweep``) share it. The store owns the lease (owner,
expiry on its own clock, the fence); this class keeps the rules the contract
leaves to the holder (spec 5, "Time"):

* **acquire / renew** — a standby retries every ``ttl/3`` seconds; the holder
  renews at the same cadence. Every store call is bounded by ``ttl/3`` so a
  hung backend cannot stall the holder past its lease;
* **local deadline** — a holder treats its lease as lost at ``acquire/renew
  start + ttl`` on its own monotonic clock, measured from BEFORE the call that
  set it, so it never outlives the backend's expiry. A renewal that fails
  because the backend is unreachable keeps the holder going only until then;
* **lost lease** — a renewal that answers ``False`` stops the holder at once;
* **release** — best effort, so a standby takes over at once; the TTL frees
  it anyway.

The beat's lease name is ``sandloom:beat:lease:<scope>``; the scope defaults to
a digest of the broker URL, the taskqueue instance and the sorted loaded module
set, so replicas of one service share a lease while services with different
module sets (each with its own periodic tasks) never suppress each other.
``BEAT_LEASE_NAME`` pins an explicit scope. The diagnostics
(:func:`periodic_needs`, :func:`read_beat_lease`) read the holder of that name.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import time
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from sandloom.core.config.settings import settings
from sandloom.core.infra import access as _access
from sandloom.core.infra.errors import InfraError, InvalidArgument, PermissionDenied
from sandloom.core.infra.ports.lease import Lease, LeaseStore

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The ``lease`` instance the kernel uses (beat and saga sweep): ``INFRA.lease.beat``.
BEAT_INSTANCE = "beat"
LEASE_KEY_PREFIX = "sandloom:beat:lease:"
#: How often a standby repeats "another beat holds the lease" (seconds).
STANDBY_LOG_INTERVAL_SECONDS = 300.0
#: Retry cadence of a renewal that failed on a backend error (seconds).
RENEW_ERROR_RETRY_SECONDS = 1.0


def lease_scope(
    broker_url: str | None, taskqueue_instance: str, modules: Iterable[str]
) -> str:
    """Default lease scope: one beat per (broker, taskqueue, module set)."""
    material = "|".join(
        [str(broker_url or ""), taskqueue_instance, ",".join(sorted(modules))]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def beat_scope(broker_url: str | None, instance: str, core: Any) -> str:
    """The lease scope of a beat of ``core``'s composition on ``instance``:
    ``BEAT_LEASE_NAME`` when set, else :func:`lease_scope`."""
    return settings.beat_lease_name.strip() or lease_scope(
        broker_url, instance, list(getattr(core, "loaded_modules", []) or [])
    )


def beat_lease_store(core: Any) -> LeaseStore:
    """The ``lease`` instance ``beat`` of ``core``'s registry (the process-active
    one when ``core`` carries none). Raises when it is not configured."""
    infra = getattr(core, "infra", None)
    if infra is not None:
        store: LeaseStore = infra.provider("lease", BEAT_INSTANCE)
        return store
    return _access.lease(BEAT_INSTANCE)


class LeaseNotSharedError(RuntimeError):
    """The configured ``lease`` instance cannot serve a beat (not shared)."""


def require_beat_lease(core: Any) -> None:
    """Mark the instance ``lease.beat`` as used by this process, so the registry
    opens and probes it like a kernel instance (a refusing backend then fails
    startup in production, with its real cause, and ``/ready`` elsewhere).

    A process that hosts sagas calls it: its sweep runs on that lease. A
    synthesized instance nothing requires is otherwise never opened, and a
    refusal (the Redis evicts keys, ``CONFIG GET`` is refused) would leave the
    holder on standby for good with no health signal.
    """
    try:
        infra = getattr(core, "infra", None)
    except RuntimeError:  # a facade whose registry is not configured yet
        return
    mark = getattr(infra, "use_kernel_instance", None)
    if callable(mark):
        mark("lease", BEAT_INSTANCE)


def resolve_beat_lease(
    core: Any, *, broker_url: str | None, instance: str, shared_required: bool
) -> tuple[LeaseStore, str]:
    """The lease store of the beat of ``core``'s composition on taskqueue
    ``instance`` and the lease name it holds there.

    ``shared_required`` (production) refuses a store that does not declare
    ``shared``: with a memory lease, or a SQLite file, two beats could both
    hold the lease.
    """
    store = beat_lease_store(core)
    features = store.features() if hasattr(store, "features") else frozenset()
    if shared_required and "shared" not in features:
        raise LeaseNotSharedError(
            f"lease instance {BEAT_INSTANCE!r} (driver "
            f"{getattr(store, 'driver', '?')!r}) is not shared between processes, "
            "so two beats could both hold it; point INFRA.lease.beat at a "
            "redis lease or a PostgreSQL database lease"
        )
    return store, LEASE_KEY_PREFIX + beat_scope(broker_url, instance, core)


class FencedLease:
    """A fenced, renewable lease on one name of a lease store (async state machine).

    ``clock`` is monotonic seconds (tests inject a fake one); ``label`` prefixes
    the log lines; ``identity`` is the owner label (``<host>:<pid>``).
    """

    def __init__(
        self,
        store: LeaseStore,
        name: str,
        *,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        identity: str | None = None,
        label: str = "[lease]",
    ) -> None:
        if ttl_seconds < 3:
            raise ValueError("lease TTL must be at least 3 seconds")
        self._store = store
        self._name = name
        self.ttl_seconds = int(ttl_seconds)
        self._renew_interval = self.ttl_seconds / 3.0
        self.label = label
        self._clock = clock
        self._identity = identity or f"{socket.gethostname()}:{os.getpid()}"
        self._lease: Lease | None = None
        #: The fence of the last grant (kept after a release, dropped on a loss).
        self._fence: int | None = None
        self._valid_until = 0.0
        self._renew_at = 0.0
        self._retry_at = 0.0
        self._standby_logged_at: float | None = None

    @property
    def store(self) -> LeaseStore:
        """The lease store this lease runs against (read-only)."""
        return self._store

    @property
    def name(self) -> str:
        """The leased name (read-only)."""
        return self._name

    @property
    def renew_interval(self) -> float:
        """Seconds between renewals, a third of the TTL (read-only)."""
        return self._renew_interval

    @property
    def fence(self) -> int | None:
        """The fence of the last grant (read-only; kept after a release,
        ``None`` after a loss)."""
        return self._fence

    @property
    def held(self) -> bool:
        return self._lease is not None

    @property
    def token(self) -> str | None:
        return self._lease.token if self._lease is not None else None

    async def _bounded(self, coro: Coroutine[Any, Any, T]) -> T:
        return await asyncio.wait_for(coro, timeout=self.renew_interval)

    def fast_path(self) -> bool | None:
        """The answer without a store round trip, or ``None`` when one is due."""
        now = self._clock()
        if self._lease is not None and now < self._renew_at:
            return True
        if self._lease is None and now < self._retry_at:
            return False
        return None

    async def ensure_async(self) -> bool:
        """Acquire or renew as due; ``True`` while this process holds the lease."""
        fast = self.fast_path()
        if fast is not None:
            return fast
        now = self._clock()
        if self._lease is not None:
            return await self._renew(now)
        return await self._acquire(now)

    def next_check_in(self) -> float:
        """Seconds until :meth:`ensure_async` has work to do (renew or retry)."""
        due = self._renew_at if self._lease is not None else self._retry_at
        return max(0.05, due - self._clock())

    async def _holder_text(self) -> str:
        """Who holds the name, for the standby log line (best effort)."""
        try:
            holder = await self._bounded(self.store.holder(self.name))
        except (InfraError, TimeoutError):  # a log detail, never a failure
            return "unknown"
        return "none" if holder is None else f"{holder.owner} (fence {holder.fence})"

    async def _acquire(self, now: float) -> bool:
        try:
            lease = await self._bounded(
                self.store.acquire(
                    self.name, ttl_seconds=self.ttl_seconds, owner=self._identity
                )
            )
        except Exception as exc:  # noqa: BLE001 - the lease backend is unreachable
            if isinstance(exc, PermissionDenied | InvalidArgument):
                # The backend answers and refuses (the eviction policy, a
                # credential): a configuration to fix, not an outage to wait out.
                logger.error(
                    "%s the lease backend refuses to serve (%s: %s); not scheduling",
                    self.label,
                    type(exc).__name__,
                    exc,
                )
            else:
                logger.warning(
                    "%s cannot reach the lease backend (%s); not scheduling",
                    self.label,
                    type(exc).__name__,
                )
            self._retry_at = now + self.renew_interval
            return False
        if lease is None:
            self._retry_at = now + self.renew_interval
            if (
                self._standby_logged_at is None
                or now - self._standby_logged_at >= STANDBY_LOG_INTERVAL_SECONDS
            ):
                logger.warning(
                    "%s standby: another holder has lease %s (%s); this process "
                    "schedules nothing until it is released or expires",
                    self.label,
                    self.name,
                    await self._holder_text(),
                )
                self._standby_logged_at = now
            return False
        self._lease = lease
        self._fence = lease.fence
        self._valid_until = now + self.ttl_seconds
        self._renew_at = now + self.renew_interval
        self._standby_logged_at = None
        logger.warning(
            "%s acquired lease %s (fence %s, ttl %ss); scheduling",
            self.label,
            self.name,
            lease.fence,
            self.ttl_seconds,
        )
        return True

    async def _renew(self, now: float) -> bool:
        lease = self._lease
        assert lease is not None  # noqa: S101 - type narrowing of an invariant
        try:
            kept = await self._bounded(self.store.renew(lease))
        except Exception as exc:  # noqa: BLE001 - the lease backend is unreachable
            if now < self._valid_until:
                logger.warning(
                    "%s cannot renew lease %s (%s); still scheduling for "
                    "%.1fs until the local lease deadline",
                    self.label,
                    self.name,
                    type(exc).__name__,
                    self._valid_until - now,
                )
                self._renew_at = min(now + RENEW_ERROR_RETRY_SECONDS, self._valid_until)
                return True
            logger.error(
                "%s lease %s expired locally without a renewal; stopping scheduling",
                self.label,
                self.name,
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
            self.name,
            lease.fence,
        )
        self._drop(now + self.renew_interval)
        return False

    def _drop(self, retry_at: float) -> None:
        self._lease = None
        self._fence = None
        self._retry_at = retry_at

    async def release_async(self) -> None:
        """Release our own lease (best-effort) so a standby takes over at once."""
        lease, self._lease = self._lease, None
        if lease is None:
            return
        try:
            await self._bounded(self.store.release(lease))
            logger.info("%s released lease %s", self.label, self.name)
        except Exception:  # noqa: BLE001 - the TTL frees it anyway
            logger.warning("%s could not release lease %s", self.label, self.name)


# --------------------------------------------------- periodic diagnostics ----


@dataclass(frozen=True)
class PeriodicNeed:
    """A loaded module whose taskqueue requirement asks for ``periodic``."""

    module: str
    instance: str
    required: bool


def periodic_needs(
    descriptors: Iterable[Any], loaded: Iterable[str]
) -> list[PeriodicNeed]:
    """The loaded modules that need periodic jobs, from each descriptor's
    effective requirements (declared, plus what the kernel derives)."""
    names = set(loaded)
    needs: list[PeriodicNeed] = []
    for descriptor in descriptors:
        if getattr(descriptor, "name", None) not in names:
            continue
        effective = getattr(descriptor, "effective_capabilities", None)
        requirements = (
            effective()
            if callable(effective)
            else getattr(descriptor, "capabilities", [])
        )
        needs.extend(
            PeriodicNeed(
                module=descriptor.name,
                instance=requirement.instance or "default",
                required=bool(requirement.required),
            )
            for requirement in requirements
            if requirement.capability == "taskqueue"
            and "periodic" in requirement.features
        )
    return needs


def broker_url_of(core: Any, instance: str) -> str:
    """The broker URL a beat on taskqueue ``instance`` scopes its lease with
    (the ``celery`` driver: the instance's ``broker`` option, else
    ``BROKER_URL``)."""
    infra = getattr(core, "infra", None)
    cfg = infra.instance_config("taskqueue", instance) if infra is not None else None
    options = dict(getattr(cfg, "options", None) or {})
    # ``BROKER_URL`` is optional: neither set is "" (a scope of no broker), never
    # the text "None".
    return str(options.get("broker") or settings.broker_url or "")


async def beat_lease_holder(core: Any, *, instance: str = "default") -> str | None:
    """The holder (``<fence>:<host>:<pid>``) of the lease of the beat of
    ``core``'s composition on ``instance``, or ``None`` when no beat schedules
    its periodic jobs. One read of the lease store; raises when it cannot be
    read.

    A beat started with another module set (or another broker) holds another
    scope: the answer is for THIS composition, the one ``sandloom doctor``
    loaded or the one this process serves.
    """
    store = beat_lease_store(core)
    scope = beat_scope(broker_url_of(core, instance), instance, core)
    holder = await store.holder(LEASE_KEY_PREFIX + scope)
    if holder is None:
        return None
    return f"{holder.fence}:{holder.owner}"


async def read_beat_lease(
    core: Any, *, instance: str = "default", limit_seconds: float
) -> tuple[str, str | None]:
    """:func:`beat_lease_holder` for a diagnostic, bounded and never raising:
    ``("held", holder)``, ``("absent", None)`` or ``("unknown", why)``."""
    try:
        holder = await asyncio.wait_for(
            beat_lease_holder(core, instance=instance), limit_seconds
        )
    except TimeoutError:
        return "unknown", f"the beat lease was not read within {limit_seconds:g}s"
    except Exception as exc:
        logger.debug("beat lease unreadable", exc_info=True)
        return "unknown", f"the beat lease cannot be read: {type(exc).__name__}"
    return ("held", holder) if holder is not None else ("absent", None)


__all__ = [
    "BEAT_INSTANCE",
    "LEASE_KEY_PREFIX",
    "FencedLease",
    "LeaseNotSharedError",
    "PeriodicNeed",
    "beat_lease_holder",
    "beat_lease_store",
    "beat_scope",
    "broker_url_of",
    "lease_scope",
    "periodic_needs",
    "read_beat_lease",
    "require_beat_lease",
    "resolve_beat_lease",
]
