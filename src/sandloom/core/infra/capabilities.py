"""Capability specifications — capability as a first-class, versioned object.

A *capability* (``cache``, ``database``, ``graph_store``, ``workflow``, …) is no
longer a bare string: it is a :class:`CapabilitySpec` that names the provider
contract every driver of that capability must implement, the optional features a
driver MAY advertise, and an optional config schema. The kernel owns the spec
registry; drivers are plugins; modules declare which capabilities (and features)
they require. This is what lets graph DBs, document stores, workflows and any
future backend be added without bloating core — a plugin registers a new
``CapabilitySpec`` plus one or more drivers.

Each built-in capability's spec is defined beside its contract, in
``sandloom.core.infra.ports.<capability>`` (ADR-0016); this module lists them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Generic, TypeVar

from sandloom.core.descriptors.database_forms import DATABASE_FORM_FEATURES
from sandloom.core.infra.ports._base import (
    CapabilitySpec,
    CapabilityStability,
    FeatureKind,
)
from sandloom.core.infra.ports.atomic_store import ATOMIC_STORE_CAPABILITY
from sandloom.core.infra.ports.broadcast import BROADCAST_CAPABILITY
from sandloom.core.infra.ports.cache import CACHE_CAPABILITY
from sandloom.core.infra.ports.database import (
    DATABASE_CAPABILITY,
    DATABASE_RENDERER_FEATURES,
)
from sandloom.core.infra.ports.eventbus import EVENTBUS_CAPABILITY
from sandloom.core.infra.ports.lease import LEASE_CAPABILITY
from sandloom.core.infra.ports.notification import NOTIFICATION_CAPABILITY
from sandloom.core.infra.ports.objectstore import OBJECTSTORE_CAPABILITY
from sandloom.core.infra.ports.taskqueue import TASKQUEUE_CAPABILITY

#: The ``.vN`` that ends a versioned capability reference.
_VERSION_SUFFIX = re.compile(r"\.(v\d+)$")

#: The caller-facing type a capability reference resolves to.
P = TypeVar("P")


@dataclass(frozen=True)
class CapabilityRef(Generic[P]):
    """A typed key to a capability: ``CapabilityRef[Search]("search.v1")``.

    The caller's Protocol travels in the type, and a ``.vN`` suffix is checked against
    the capability when it is resolved; a ref without one skips that check. A class
    passed as ``type[T]`` cannot do this: mypy refuses an abstract class or a Protocol
    there.
    """

    value: str

    @property
    def name(self) -> str:
        return _VERSION_SUFFIX.sub("", self.value)

    @property
    def version(self) -> str | None:
        """The ``vN`` suffix, if any. Only a versioned ref is checked on resolve."""
        found = _VERSION_SUFFIX.search(self.value)
        return found.group(1) if found else None


# --------------------------------------------------------------------------- #
# Built-in capabilities: each spec lives in its port, this module lists them.
# --------------------------------------------------------------------------- #
#: Readability alias for :data:`DATABASE_CAPABILITY` — the relational paradigm.
RELATIONAL_CAPABILITY = DATABASE_CAPABILITY

#: All capabilities shipped with core, registered at bootstrap.
BUILTIN_CAPABILITIES: tuple[CapabilitySpec, ...] = (
    DATABASE_CAPABILITY,
    CACHE_CAPABILITY,
    OBJECTSTORE_CAPABILITY,
    TASKQUEUE_CAPABILITY,
    EVENTBUS_CAPABILITY,
    NOTIFICATION_CAPABILITY,
    ATOMIC_STORE_CAPABILITY,
    LEASE_CAPABILITY,
    BROADCAST_CAPABILITY,
)


__all__ = [
    "CapabilityRef",
    "CapabilitySpec",
    "CapabilityStability",
    "DATABASE_CAPABILITY",
    "DATABASE_FORM_FEATURES",
    "DATABASE_RENDERER_FEATURES",
    "RELATIONAL_CAPABILITY",
    "CACHE_CAPABILITY",
    "OBJECTSTORE_CAPABILITY",
    "TASKQUEUE_CAPABILITY",
    "EVENTBUS_CAPABILITY",
    "NOTIFICATION_CAPABILITY",
    "ATOMIC_STORE_CAPABILITY",
    "LEASE_CAPABILITY",
    "BROADCAST_CAPABILITY",
    "BUILTIN_CAPABILITIES",
    "FeatureKind",
]
