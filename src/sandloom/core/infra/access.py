"""Module-facing infrastructure accessors (cache, notifications, object
stores, task queues, any capability).

The typed accessors business modules and core guards use to reach a capability
provider by logical name. ``sandloom.core.di`` re-exports them so modules keep
importing ``from sandloom.core.di import cache`` (the public surface), while
kernel-internal callers (idempotency, rate limiting, the event-bus inbox)
import them from HERE — so a low-level module never makes an upward import into
the public DI aggregator just to reach the cache.

Every accessor resolves through the process-active registry
(:func:`sandloom.core.infra.active.get_active_infra`); with none bound it raises, in
every environment, with a hint naming the composition roots that bind one.
"""

from __future__ import annotations

from typing import TypeVar, cast

from sandloom.core.config.settings import settings
from sandloom.core.infra.active import get_active_infra
from sandloom.core.infra.capabilities import CapabilityRef
from sandloom.core.infra.contracts import (
    Cache,
    CacheProvider,
    Notifier,
    ObjectStore,
    TaskQueue,
)

P = TypeVar("P")


def cache(name: str = "default") -> Cache:
    """Return the operation-based ``CacheProvider`` for a named instance.

    The sanctioned cache surface for modules and core guards — ``get``/``set``/
    ``add``/``delete``/``incr`` (see
    :class:`sandloom.core.infra.contracts.CacheProvider`). Resolves via the
    process-active :class:`InfraRegistry` (``RuntimeError`` when none is
    bound); the registry's configuration picks the driver, so this surface
    never names a concrete backend.
    """
    provider = get_active_infra().provider("cache", name)
    if not isinstance(provider, CacheProvider):  # pragma: no cover - defensive
        raise TypeError(f"cache instance {name!r} is not a CacheProvider")
    return provider


def security_cache_name() -> str:
    """Name of the cache instance holding security-critical state.

    ``SECURITY_CACHE_INSTANCE`` (default ``default``): access-token revocation
    marks, emergency user blocks, the RPC replay guard and — unless
    ``IDEMPOTENCY_CACHE_INSTANCE`` moves them (:func:`idempotency_cache_name`)
    — idempotency records (``idempotency_guard``, task-enqueue dedupe) live
    there. Contract COMMANDS are deduped durably in
    the provider's database (``contract_command_log``); only the cache fallback
    for a database without that log goes through this instance. Point it at
    a separate, non-evicting Redis so a best-effort cache filling up
    (``allkeys-lru``) can never evict a revocation mark or an idempotency
    reservation — the ``multi_tenant_strict`` production profile requires that.
    """
    try:
        name = getattr(settings, "security_cache_instance", None)
    except Exception:  # noqa: BLE001 - settings unavailable (static tooling)
        name = None
    return str(name) if name else "default"


def security_cache() -> CacheProvider:
    """The ``CacheProvider`` for security-critical state (see above)."""
    name = security_cache_name()
    return cast("CacheProvider", cache() if name == "default" else cache(name))


def idempotency_cache_name() -> str:
    """Name of the cache instance holding idempotency records.

    ``IDEMPOTENCY_CACHE_INSTANCE`` when set (opt-in), else the security cache
    (:func:`security_cache_name`) exactly as before. It holds
    ``idempotency_guard`` reservations / replays, task-enqueue dedupe records
    and the cache fallback of contract-command dedupe — never revocation
    marks or blocks, which stay in the security cache. A separate instance
    must be as durable as the security one (non-evicting, fsync'd): losing a
    reservation re-runs a mutation.
    """
    try:
        name = getattr(settings, "idempotency_cache_instance", None)
    except Exception:  # noqa: BLE001 - settings unavailable (static tooling)
        name = None
    return str(name) if name else security_cache_name()


def idempotency_cache() -> CacheProvider:
    """The ``CacheProvider`` for idempotency records (see above)."""
    name = idempotency_cache_name()
    return cast("CacheProvider", cache() if name == "default" else cache(name))


def notifier(name: str = "default") -> Notifier:
    """Return the ``NotifierProvider`` for a named instance.

    The sanctioned outbound-notification surface for modules: build a
    :class:`~sandloom.core.infra.contracts.Notification` and ``await
    notifier().send(...)`` — never import an email/SMS SDK. Resolves via the
    process-active :class:`InfraRegistry` (``RuntimeError`` when none is
    bound).
    """
    return cast("Notifier", get_active_infra().provider("notification", name))


def objectstore(name: str = "default") -> ObjectStore:
    """Return the ``ObjectStoreProvider`` for a named instance (the
    module-level twin of ``CoreRuntime.objectstore``; spec §2.2)."""
    return cast("ObjectStore", get_active_infra().resolve("objectstore", name))


def taskqueue(name: str = "default") -> TaskQueue:
    """Return the ``TaskQueueProvider`` for a named instance (the module-level
    twin of ``CoreRuntime.taskqueue``; spec §2.2)."""
    return cast("TaskQueue", get_active_infra().resolve("taskqueue", name))


def capability(ref: CapabilityRef[P], name: str = "default") -> P:
    """Return the instance ``name`` of the capability ``ref`` points to, typed by the
    ref. A capability's package publishes the ref and the caller's Protocol, and the
    capability's major version must match the ref's."""
    return cast("P", get_active_infra().resolve(ref.name, name, version=ref.version))


__all__ = [
    "cache",
    "capability",
    "idempotency_cache",
    "idempotency_cache_name",
    "notifier",
    "objectstore",
    "security_cache",
    "security_cache_name",
    "taskqueue",
]
