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
``database`` keeps its spec here until its port is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Generic, TypeVar

from sandloom.core.descriptors.database_forms import DATABASE_FORM_FEATURES
from sandloom.core.infra.contracts import DatabaseProvider
from sandloom.core.infra.ports._base import (
    CapabilitySpec,
    CapabilityStability,
    FeatureKind,
)
from sandloom.core.infra.ports.atomic_store import ATOMIC_STORE_CAPABILITY
from sandloom.core.infra.ports.broadcast import BROADCAST_CAPABILITY
from sandloom.core.infra.ports.cache import CACHE_CAPABILITY
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
# Built-in capabilities (Phase 1: wrap the existing five, no behavior change)
# --------------------------------------------------------------------------- #
# The relational paradigm. ``features`` are its guarantee-set: a driver that
# cannot do ``transactional_outbox`` simply does not advertise it, and a module
# that publishes events requires it — so the guarantee is capability-gated, not
# assumed. Sibling storage paradigms (document_store, graph_store, …) are
# separate specs in ``extra_capabilities`` with their own, weaker features.
#: The ``database`` features a SQL dialect renderer may declare for its
#: database (design/infra-agnostic/spec.md, section 2): an instance advertises
#: them only when its URL's renderer does.
DATABASE_RENDERER_FEATURES = frozenset(
    {
        # Write transactions overlap, each holding its own row locks (what a
        # race test needs). Never declared by a renderer: derived from its
        # ``serial_writers`` fact (``portable.renderer_features``), so the
        # two cannot disagree.
        "concurrent_writers",
        "skip_locked",
        "transaction_locks",
        "snapshot_reads",
        "readonly_transactions",
        "arrays",
        "fulltext_search",
        "json_path",
        "json_contains",
        "list_aggregates",
    }
)

#: The kind of each ``database`` feature (spec 11): the unit's call shapes are
#: behaviours, what the kernel's guarantees rest on are guarantees, and the
#: renderer features are behaviours of the SQL the renderer emits.
_DATABASE_FEATURE_KINDS: dict[str, FeatureKind] = {
    "transactions": "behaviour",
    "savepoints": "behaviour",
    "write_admission": "guarantee",
    "transactional_outbox": "guarantee",
    "audit": "guarantee",
    "tenant_rls": "guarantee",
    "command_idempotency": "guarantee",
    "http_idempotency": "guarantee",
    **dict.fromkeys(DATABASE_RENDERER_FEATURES, "behaviour"),
}

DATABASE_CAPABILITY = CapabilitySpec(
    name="database",
    version="v1",
    provider_contract=DatabaseProvider,
    # ``tenant_rls``: the instance keeps PostgreSQL Row-Level Security session
    # settings in sync with the tenant context (opt-in per instance), so a
    # module can require RLS defense in depth for its tables.
    # ``command_idempotency``: command-contract dedupe records commit in the
    # command's own transaction (``DatabaseProvider.command_log()``).
    # ``http_idempotency``: durable ``idempotency_guard(durable=True)`` records
    # commit in the request's own transaction (``DatabaseProvider.request_log()``).
    # ``write_admission``: the driver enforces the request's unit rules at its
    # statement boundary — every write statement detected (conservatively) and
    # admitted for ONE writing unit per request (SANDLOOM-R007), a statement through
    # an exited unit refused (SANDLOOM-R002), the lazy request unit
    # (``LazyUnitOfWorkContract``) and ``has_pending_changes``. The kernel
    # requires it of every database that hosts request units (SANDLOOM-B010).
    feature_kinds=_DATABASE_FEATURE_KINDS,
    description="Relational (transactional) persistence — Unit of Work factory.",
)

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
