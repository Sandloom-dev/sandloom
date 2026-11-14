"""Conformance kits: subclass one in your driver's tests, provide ``make_provider``.

Each obligation is its own test, so a failure names what to fix; every
provider kit runs the shared harness first (lifecycle, ``contract_version``,
features, performance hints, health, ``isolation_identity``, feature groups,
error mapping), then its capability's groups. Requires pytest and
pytest-asyncio.

This module is the only import path for the kits. Each name resolves lazily
from a fixed table (PEP 562) to its file in ``sandloom.driverkit.kits``, so
importing this module imports no kit. A kit whose file is not delivered yet
raises ``AttributeError`` naming the slice that delivers it.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # What a type checker sees for each name of the table, so a driver's kit
    # subclass is typed. A kit file not delivered yet is ignored until its
    # slice lands; the ignore then lapses silently.
    from sandloom.driverkit.kits.atomic_store import (  # type: ignore[import-not-found, unused-ignore]
        AtomicStoreConformance as AtomicStoreConformance,
    )
    from sandloom.driverkit.kits.broadcast import (  # type: ignore[import-not-found, unused-ignore]
        BroadcastConformance as BroadcastConformance,
    )
    from sandloom.driverkit.kits.cache import (  # type: ignore[import-not-found, unused-ignore]
        CacheConformance as CacheConformance,
    )
    from sandloom.driverkit.kits.database import (  # type: ignore[import-not-found, unused-ignore]
        DatabaseProviderConformance as DatabaseProviderConformance,
    )
    from sandloom.driverkit.kits.eventbus import (  # type: ignore[import-not-found, unused-ignore]
        EventTransportConformance as EventTransportConformance,
    )
    from sandloom.driverkit.kits.lease import (  # type: ignore[import-not-found, unused-ignore]
        LeaseConformance as LeaseConformance,
    )
    from sandloom.driverkit.kits.notification import (
        NotifierConformance as NotifierConformance,
    )
    from sandloom.driverkit.kits.objectstore import (
        ObjectStoreConformance as ObjectStoreConformance,
    )
    from sandloom.driverkit.kits.sql_dialect import (
        JSON_PATH_DOCUMENT as JSON_PATH_DOCUMENT,
    )
    from sandloom.driverkit.kits.sql_dialect import (
        JSON_PATH_READS as JSON_PATH_READS,
    )
    from sandloom.driverkit.kits.sql_dialect import (
        SqlDialectConformance as SqlDialectConformance,
    )
    from sandloom.driverkit.kits.taskqueue import (  # type: ignore[import-not-found, unused-ignore]
        TaskQueueConformance as TaskQueueConformance,
    )

#: Public kit name -> (kit file in ``sandloom.driverkit.kits``, the slice
#: that delivers it).
_TABLE: dict[str, tuple[str, str]] = {
    "AtomicStoreConformance": ("atomic_store", "idk-atomic-store"),
    "BroadcastConformance": ("broadcast", "idk-broadcast"),
    "CacheConformance": ("cache", "idk-cache-contract"),
    "DatabaseProviderConformance": ("database", "idk-db-provider"),
    "EventTransportConformance": ("eventbus", "idk-eventbus-kit"),
    "JSON_PATH_DOCUMENT": ("sql_dialect", "idk-kit-harness"),
    "JSON_PATH_READS": ("sql_dialect", "idk-kit-harness"),
    "LeaseConformance": ("lease", "idk-lease"),
    "NotifierConformance": ("notification", "idk-kit-harness"),
    "ObjectStoreConformance": ("objectstore", "idk-kit-harness"),
    "SqlDialectConformance": ("sql_dialect", "idk-kit-harness"),
    "TaskQueueConformance": ("taskqueue", "idk-task-api"),
}

__all__ = sorted(_TABLE)


def _kit(kit: str) -> ModuleType:
    """The kit file ``kit``, by a literal import each (SANDLOOM-T017)."""
    if kit == "atomic_store":
        return importlib.import_module("sandloom.driverkit.kits.atomic_store")
    if kit == "broadcast":
        return importlib.import_module("sandloom.driverkit.kits.broadcast")
    if kit == "cache":
        return importlib.import_module("sandloom.driverkit.kits.cache")
    if kit == "database":
        return importlib.import_module("sandloom.driverkit.kits.database")
    if kit == "eventbus":
        return importlib.import_module("sandloom.driverkit.kits.eventbus")
    if kit == "lease":
        return importlib.import_module("sandloom.driverkit.kits.lease")
    if kit == "notification":
        return importlib.import_module("sandloom.driverkit.kits.notification")
    if kit == "objectstore":
        return importlib.import_module("sandloom.driverkit.kits.objectstore")
    if kit == "taskqueue":
        return importlib.import_module("sandloom.driverkit.kits.taskqueue")
    return importlib.import_module("sandloom.driverkit.kits.sql_dialect")


def __getattr__(name: str) -> Any:
    entry = _TABLE.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    kit, slice_ = entry
    missing = AttributeError(
        f"{__name__}.{name} is not delivered yet: {slice_} adds it in "
        f"sandloom.driverkit.kits.{kit}"
    )
    try:
        module = _kit(kit)
    except ModuleNotFoundError as exc:
        if exc.name != f"sandloom.driverkit.kits.{kit}":
            raise  # the kit file exists; something it imports does not
        raise missing from None
    try:
        found = getattr(module, name)
    except AttributeError:
        raise missing from None
    globals()[name] = found
    return found


def __dir__() -> list[str]:
    return sorted({*globals(), *_TABLE})
