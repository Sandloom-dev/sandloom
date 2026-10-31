"""Capability specifications — capability as a first-class, versioned object.

A *capability* (``cache``, ``database``, ``graph_store``, ``workflow``, …) is no
longer a bare string: it is a :class:`CapabilitySpec` that names the provider
contract every driver of that capability must implement, the optional features a
driver MAY advertise, and an optional config schema. The kernel owns the spec
registry; drivers are plugins; modules declare which capabilities (and features)
they require. This is what lets graph DBs, document stores, workflows and any
future backend be added without bloating core — a plugin registers a new
``CapabilitySpec`` plus one or more drivers.

Phase 1 keeps the five built-in capabilities behaving exactly as before; the
spec layer is wrapped *around* the existing :class:`InfraRegistry` so nothing
breaks. See ``docs/archive/CAPABILITY_RUNTIME_TZ.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

from sandloom.core.infra.contracts import (
    CacheProvider,
    DatabaseProvider,
    EventBusProvider,
    NotifierProvider,
    ObjectStoreProvider,
    TaskQueueProvider,
)

#: The ``.vN`` that ends a versioned capability reference.
_VERSION_SUFFIX = re.compile(r"\.(v\d+)$")

#: The caller-facing type a capability reference resolves to.
P = TypeVar("P")

#: Maturity of a capability contract (see :attr:`CapabilitySpec.stability`).
CapabilityStability = Literal["stable", "experimental"]
_STABILITIES: frozenset[str] = frozenset({"stable", "experimental"})


@dataclass(frozen=True)
class CapabilitySpec:
    """A versioned description of one infrastructure capability.

    * ``name`` — capability key (``"cache"``, ``"graph_store"``, …).
    * ``version`` — contract version (``"v1"``); a breaking contract change bumps
      it so a module can pin the version it was written against.
    * ``provider_contract`` — the abstract base/Protocol every driver of this
      capability must implement. The registry rejects a driver that returns
      something else (:class:`ProviderContractError`).
    * ``config_schema`` — optional pydantic model validating an instance's
      ``options`` (driver config). ``None`` = no validation.
    * ``features`` — the full set of optional features the contract defines. A
      driver advertises the subset it actually supports via
      :meth:`InfraProvider.features`; a module may require a subset and the
      registry checks the requested features are both known to the spec and
      supported by the resolved driver.
    * ``description`` — human/AI-facing one-liner.
    * ``stability`` — ``"stable"`` (a supported contract with a shipped
      driver) or ``"experimental"`` (a contract the kernel knows about but ships
      NO driver for; its surface may still change). Rendered in
      ``/diagnostics/infra``; resolving an experimental capability that has no
      registered driver raises :class:`ExperimentalCapabilityError`.
    """

    name: str
    version: str
    provider_contract: type
    config_schema: type[Any] | None = None
    features: frozenset[str] = field(default_factory=frozenset)
    description: str = ""
    stability: CapabilityStability = "stable"

    def __post_init__(self) -> None:
        if self.stability not in _STABILITIES:
            raise ValueError(
                f"CapabilitySpec {self.name!r}: stability must be one of "
                f"{sorted(_STABILITIES)}, got {self.stability!r}"
            )

    @property
    def is_experimental(self) -> bool:
        """True when the contract is experimental (no core driver, may change)."""
        return self.stability == "experimental"


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
    features=frozenset(
        {
            "transactions",
            "write_admission",
            "savepoints",
            "transactional_outbox",
            "audit",
            "tenant_rls",
            "command_idempotency",
            "http_idempotency",
        }
    ),
    description="Relational (transactional) persistence — Unit of Work factory.",
)

#: Readability alias for :data:`DATABASE_CAPABILITY` — the relational paradigm.
RELATIONAL_CAPABILITY = DATABASE_CAPABILITY

CACHE_CAPABILITY = CapabilitySpec(
    name="cache",
    version="v1",
    provider_contract=CacheProvider,
    features=frozenset(
        {"cas", "get_many", "set_many"}
        # Fused single-round-trip ops (portable fallbacks exist for every driver).
        | {"incr_window", "incr_many", "set_nx_get", "set_max", "subject_state"}
    ),
    description="Ephemeral key/value store (operation-based).",
)

OBJECTSTORE_CAPABILITY = CapabilitySpec(
    name="objectstore",
    version="v1",
    provider_contract=ObjectStoreProvider,
    features=frozenset(),
    description="Blob/file storage (key-addressed).",
)

TASKQUEUE_CAPABILITY = CapabilitySpec(
    name="taskqueue",
    version="v1",
    provider_contract=TaskQueueProvider,
    features=frozenset({"delay", "idempotency_key"}),
    description="Background job broker (fire-and-forget enqueue).",
)

EVENTBUS_CAPABILITY = CapabilitySpec(
    name="eventbus",
    version="v1",
    provider_contract=EventBusProvider,
    # Delivery semantics differ by driver: a module that needs durable /
    # at-least-once delivery (billing, orders, provisioning) requires it
    # explicitly, so it can never silently run on a volatile in-memory bus.
    features=frozenset(
        {
            "publish",
            "subscribe",
            "volatile",
            "durable",
            "at_least_once",
            "wildcard_subscribe",
            "consumer_groups",
        }
    ),
    description="Pub/sub domain-event transport.",
)

NOTIFICATION_CAPABILITY = CapabilitySpec(
    name="notification",
    version="v1",
    provider_contract=NotifierProvider,
    # Delivery channels a driver may support; a module that must send email
    # requires the ``email`` feature so it never silently runs on a driver that
    # cannot deliver it.
    features=frozenset({"email", "sms", "push", "inapp"}),
    description="Outbound notifications (email / SMS / push / in-app).",
)

#: All capabilities shipped with core, registered at bootstrap.
BUILTIN_CAPABILITIES: tuple[CapabilitySpec, ...] = (
    DATABASE_CAPABILITY,
    CACHE_CAPABILITY,
    OBJECTSTORE_CAPABILITY,
    TASKQUEUE_CAPABILITY,
    EVENTBUS_CAPABILITY,
    NOTIFICATION_CAPABILITY,
)


__all__ = [
    "CapabilityRef",
    "CapabilitySpec",
    "CapabilityStability",
    "DATABASE_CAPABILITY",
    "RELATIONAL_CAPABILITY",
    "CACHE_CAPABILITY",
    "OBJECTSTORE_CAPABILITY",
    "TASKQUEUE_CAPABILITY",
    "EVENTBUS_CAPABILITY",
    "NOTIFICATION_CAPABILITY",
    "BUILTIN_CAPABILITIES",
]
