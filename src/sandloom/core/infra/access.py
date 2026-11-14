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

from typing import Literal, TypeVar, cast

from sandloom.core.infra.active import get_active_infra
from sandloom.core.infra.capabilities import CapabilityRef
from sandloom.core.infra.contracts import (
    Cache,
    CacheProvider,
    Notifier,
    ObjectStore,
    TaskQueue,
)
from sandloom.core.infra.ports.atomic_store import AtomicStore
from sandloom.core.infra.ports.broadcast import Broadcast
from sandloom.core.infra.ports.lease import LeaseStore

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
    """Return the raw ``TaskQueueProvider`` for a named instance.

    Kernel-facing: module code gets the instance behind the kernel's layer
    (``sandloom.core.tasks.queue.taskqueue``, the twin of
    ``CoreRuntime.taskqueue``; spec §2.2)."""
    return cast("TaskQueue", get_active_infra().resolve("taskqueue", name))


#: The roles of the kernel's ``atomic_store`` instances (spec 4, "Instances").
AtomicStoreRole = Literal["security", "idempotency"]


def atomic_store(role: AtomicStoreRole) -> AtomicStore:
    """The ``atomic_store`` instance of a kernel role.

    ``security`` holds revocation marks, subject blocks and RPC nonces;
    ``idempotency`` holds idempotency reservations and enqueue dedupe, and is
    the ``security`` instance when ``INFRA.atomic_store.idempotency`` is not
    configured. Resolves via the process-active registry.
    """
    registry = get_active_infra()
    name: str = role
    if (
        role == "idempotency"
        and registry.instance_config("atomic_store", "idempotency") is None
    ):
        name = "security"
    return cast("AtomicStore", registry.provider("atomic_store", name))


def lease(name: str) -> LeaseStore:
    """The ``lease`` instance ``name`` (the kernel uses ``beat``)."""
    return cast("LeaseStore", get_active_infra().provider("lease", name))


def broadcast(name: str = "default") -> Broadcast:
    """The ``broadcast`` instance ``name``."""
    return cast("Broadcast", get_active_infra().provider("broadcast", name))


def capability(ref: CapabilityRef[P], name: str = "default") -> P:
    """Return the instance ``name`` of the capability ``ref`` points to, typed by the
    ref. A capability's package publishes the ref and the caller's Protocol, and the
    capability's major version must match the ref's.

    Kernel-facing (a ``taskqueue`` instance is the raw provider here): module
    code resolves through ``sandloom.core.tasks.queue.capability``."""
    return cast("P", get_active_infra().resolve(ref.name, name, version=ref.version))


__all__ = [
    "AtomicStoreRole",
    "atomic_store",
    "broadcast",
    "lease",
    "cache",
    "capability",
    "notifier",
    "objectstore",
    "taskqueue",
]
