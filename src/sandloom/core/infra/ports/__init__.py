"""The kernel door: the kernel-facing Protocols of the infrastructure ports.

Each capability's contract lives in ``ports/<capability>.py`` (its Protocol,
value types, provider ABC and ``CapabilitySpec``); this package exports only
the names the kernel calls through. They resolve lazily from a fixed table
(PEP 562), so importing the package imports no port, and a slice that delivers
one of these names never edits this file. A name whose port is not delivered
yet raises ``AttributeError`` naming the slice that delivers it.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

#: Kernel-facing name -> (port module, the slice that delivers the name).
_TABLE: dict[str, tuple[str, str]] = {
    "AtomicStore": ("atomic_store", "idk-foundation"),
    "Broadcast": ("broadcast", "idk-foundation"),
    "EventTransport": ("eventbus", "idk-eventbus-contract"),
    "LeaseStore": ("lease", "idk-foundation"),
    "ProviderLifecycle": ("_base", "idk-foundation"),
    "WorkerHost": ("taskqueue", "idk-worker-port"),
}

__all__ = sorted(_TABLE)


def _port(port: str) -> ModuleType:
    """The port module ``port``, by a literal import each (SANDLOOM-T017)."""
    if port == "atomic_store":
        return importlib.import_module("sandloom.core.infra.ports.atomic_store")
    if port == "broadcast":
        return importlib.import_module("sandloom.core.infra.ports.broadcast")
    if port == "eventbus":
        return importlib.import_module("sandloom.core.infra.ports.eventbus")
    if port == "lease":
        return importlib.import_module("sandloom.core.infra.ports.lease")
    if port == "taskqueue":
        return importlib.import_module("sandloom.core.infra.ports.taskqueue")
    return importlib.import_module("sandloom.core.infra.ports._base")


def __getattr__(name: str) -> Any:
    entry = _TABLE.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    port, slice_ = entry
    module = _port(port)
    try:
        found = getattr(module, name)
    except AttributeError:
        raise AttributeError(
            f"{__name__}.{name} is not delivered yet: {slice_} adds it to "
            f"{__name__}.{port}"
        ) from None
    globals()[name] = found
    return found


def __dir__() -> list[str]:
    return sorted({*globals(), *_TABLE})
