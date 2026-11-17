"""The ``eventbus`` capability (kernel-facing; spec section 7).

One capability, two driver shapes, both an :class:`InfraProvider` with
``capability = "eventbus"`` and contract major 2:

* **Transport shape** (``streams``, and every external driver: NATS, Kafka, ...):
  the class is an :class:`EventTransportProvider`, the typed durable transport
  the kernel builds its durable bus over. Schema validation, payload limits and
  the bus logic above the transport stay kernel code. This is the only shape a
  driver package implements.
* **Volatile-bus shape** (``memory``, ``redis``): a kernel-internal
  ``VolatileBusProvider`` (:mod:`sandloom.core.eventing.volatile_provider`)
  that implements no transport; it is not exported to drivers.

The feature vocabulary is below in :data:`EVENTBUS_CAPABILITY`; a name of the
previous major is refused at load with its replacement named
(``removed_features``).

**Not ``kernel_only``** (spec section 13, decided by ``idk-eventbus-contract``):
the ``eventbus`` capability is not marked. The kernel derives an ``eventbus``
requirement for the modules that publish (a module may also declare one), and
the facade's ``eventbus()`` resolves it through ``resolve``; ``kernel_only``
would refuse both with SANDLOOM-B041. The transport itself is still used only
by the kernel's bus.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

from sandloom.core.infra.errors import AlreadyExists, InfraError, Unsupported
from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider

if TYPE_CHECKING:
    from sandloom.core.eventing.envelope import EventEnvelope


@dataclass(frozen=True, slots=True)
class Delivery:
    """One delivered message awaiting acknowledgement.

    ``id`` is the transport-level delivery id used to ``ack``; ``envelope.id``
    is the stable domain event id used for consumer-side dedupe.
    """

    id: str
    stream: str
    group: str
    envelope: EventEnvelope


@dataclass(frozen=True, slots=True)
class GroupOwner:
    """The fingerprint of the deployment that owns a consumer group.

    ``module`` and ``database`` must match for two processes to share a group
    (replicas of one deployment); ``service`` is informational.
    """

    module: str
    database: str
    service: str | None = None


@dataclass(frozen=True, slots=True)
class Retention:
    """What a transport keeps of a stream (``None``: unbounded)."""

    max_age_seconds: int | None
    max_length: int | None


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """One entry of a stream's dead-letter queue (``list_dlq``)."""

    id: str
    original_id: str | None
    group: str | None
    event_id: str | None
    attempts: int
    reason: str
    #: False when the payload was lost (``source_entry_missing``).
    replayable: bool


@dataclass(frozen=True, slots=True)
class GroupInfo:
    """One consumer group of a stream (``group_info``); ``None``: unknown."""

    group: str
    consumers: int | None
    pending: int | None
    last_delivered_id: str | None
    lag: int | None


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """Length and newest entry of a stream (``stream_info``)."""

    length: int
    last_id: str | None
    last_entry_age_seconds: float | None


@dataclass(frozen=True, slots=True)
class ConsumerInfo:
    """One live subscription of the shared inventory.

    A consumer registers itself with ``last_seen`` and ``stale`` left at their
    defaults; the transport stamps both when it lists the entry.
    """

    stream: str
    group: str
    module: str
    database: str
    service: str | None = None
    #: The consumer name of the subscribing loop (unique per process).
    consumer: str | None = None
    last_delivered_id: str | None = None
    last_seen: float | None = None
    stale: bool = False


class PartialPublish(InfraError):
    """A :meth:`EventTransport.publish_many` batch did not fully append.

    ``published`` holds the ids of the LEADING items known to be appended
    before the failure (in order; empty when nothing, or nothing known, was
    appended). The cause is chained (``__cause__``) and ``retryable`` follows
    it. Callers treat everything past ``published`` as undelivered and leave it
    to a retry: a retried item may be a duplicate, never a loss.
    """

    def __init__(
        self,
        message: str,
        *,
        published: Sequence[str] = (),
        retryable: bool = True,
        capability: str = "eventbus",
        instance: str | None = None,
    ) -> None:
        super().__init__(message, capability=capability, instance=instance)
        self.published: list[str] = list(published)
        self._retryable = retryable

    @property
    def retryable(self) -> bool:  # type: ignore[override]
        """Whether the same batch may succeed when repeated (from the cause)."""
        return self._retryable


class GroupOwnedElsewhere(AlreadyExists):
    """A consumer group is already owned by a different module / database.

    Two deployments sharing one consumer group split its events between them
    (each sees only part of the stream), and with their exactly-once claims in
    different databases a hand-over double-applies events. The transport
    records the owner of every group and refuses a second one. Not
    ``retryable``: the consumer runtime treats it as permanent.
    """


class EventTransport(Protocol):
    """The durable transport the kernel's bus runs over (at least once)."""

    # -- publishing ---------------------------------------------------------
    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        """Append ``envelope`` to ``stream``; return the transport message id."""
        ...

    async def publish_many(
        self, items: Sequence[tuple[str, EventEnvelope]]
    ) -> list[str]:
        """Append every ``(stream, envelope)`` IN ORDER; return their ids.

        Any failure raises :class:`PartialPublish`. Hint ``one_round_trip``:
        the whole batch is one round trip.
        """
        ...

    # -- consuming ----------------------------------------------------------
    async def ensure_group(
        self, stream: str, group: str, *, start_id: str = "0"
    ) -> None:
        """Idempotently create the group. ``start_id`` (``"0"``, ``"$"``,
        ``"<ms>-0"``) applies to a NEW group; anything but ``"0"`` needs
        ``group_start_id`` (``Unsupported`` without it)."""
        ...

    async def consume(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int | None = None,
    ) -> list[Delivery]:
        """Up to ``count`` deliveries; unacked ones return after their backoff.

        ``block_ms`` waits server-side for a new entry (``blocking_consume``).
        """
        ...

    async def ack(self, delivery: Delivery) -> None:
        """Acknowledge ``delivery`` so it is not redelivered."""
        ...

    async def ack_many(self, deliveries: Sequence[Delivery]) -> None:
        """Acknowledge several deliveries. Hint ``one_round_trip``."""
        ...

    async def dead_letter(self, delivery: Delivery, *, reason: str) -> None:
        """Move ``delivery`` to ``<stream>.dlq`` now; ack the source only after
        the DLQ append succeeded (G-EVENT-07)."""
        ...

    def never_dead_letter(self, stream: str, group: str) -> None:
        """Exempt ``(stream, group)`` from count-based dead-lettering."""
        ...

    async def renew_pending(
        self, deliveries: Sequence[Delivery], consumer: str
    ) -> None:
        """Keep in-flight deliveries from being reclaimed (``lease_renewal``)."""
        ...

    def renew_interval_seconds(self) -> float:
        """How often to call :meth:`renew_pending` (``lease_renewal``)."""
        ...

    # -- group ownership (G-EVENT-02, extract-module) -----------------------
    async def claim_group_owner(
        self, stream: str, group: str, owner: GroupOwner
    ) -> None:
        """Record the owner on first claim; raise :class:`GroupOwnedElsewhere`
        when a different ``(module, database)`` holds it. No TTL, no lease."""
        ...

    async def group_owner(self, stream: str, group: str) -> GroupOwner | None: ...

    async def list_group_owners(self) -> list[tuple[str, str, GroupOwner]]:
        """Every recorded owner as ``(stream, group, owner)``."""
        ...

    async def transfer_group_owner(
        self, stream: str, group: str, *, expected: GroupOwner, owner: GroupOwner
    ) -> bool:
        """Compare-and-set on the whole owner."""
        ...

    async def release_group_owner(self, stream: str, group: str) -> bool: ...

    # -- retention (G-EVENT-12, guarantee windows) ---------------------------
    def retention(self) -> Retention: ...

    # -- dead-letter operations (dlq_ops) -----------------------------------
    async def list_dlq(
        self, stream: str, *, count: int = 100, after: str | None = None
    ) -> list[DeadLetter]: ...

    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: Sequence[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        """Re-publish dead-lettered entries for the ONE group that failed them
        (G-EVENT-11); return how many."""
        ...

    # -- inspection ---------------------------------------------------------
    async def group_info(self, stream: str) -> list[GroupInfo]: ...

    async def stream_info(self, stream: str) -> StreamInfo: ...

    async def oldest_unacked_age_seconds(self, stream: str, group: str) -> float | None:
        """Age of the oldest entry ``group`` has not acknowledged (0: none)."""
        ...

    # -- inventory ----------------------------------------------------------
    async def register_consumer(
        self, stream: str, group: str, info: ConsumerInfo
    ) -> None: ...

    async def list_consumers(
        self, *, include_stale: bool = False
    ) -> list[ConsumerInfo]: ...

    async def forget_consumer(self, stream: str, group: str) -> bool: ...


class EventTransportProvider(InfraProvider):
    """An ``eventbus`` driver of the transport shape (contract major 2).

    The kernel builds its durable bus over an instance. A driver class states
    ``contract_version = 2``; this base does not (spec 13.37).

    An ABC, not a Protocol subclass: every REQUIRED operation is abstract, so a
    driver missing one cannot be instantiated. (A Protocol base would hand it
    a ``...`` body returning ``None``: ``publish_many`` would report success
    with nothing appended, ``claim_group_owner`` would let a second owner in.)
    The operations behind a feature (``lease_renewal``, ``dlq_ops``,
    ``inspection``, ``inventory``) default to ``Unsupported(feature)``; a driver
    declaring the feature overrides them. An instance is also an
    :class:`EventTransport` (the same signatures).
    """

    capability: ClassVar[str] = "eventbus"

    # -- publishing (required) ---------------------------------------------
    @abstractmethod
    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        """Append ``envelope`` to ``stream``; return the transport message id."""

    @abstractmethod
    async def publish_many(
        self, items: Sequence[tuple[str, EventEnvelope]]
    ) -> list[str]:
        """Append every ``(stream, envelope)`` IN ORDER; return one id per item.

        Any failure raises :class:`PartialPublish`. A result with fewer ids than
        items is refused by the bus, never read as success.
        """

    # -- consuming (required) ----------------------------------------------
    @abstractmethod
    async def ensure_group(
        self, stream: str, group: str, *, start_id: str = "0"
    ) -> None:
        """Idempotently create the group (see :meth:`EventTransport.ensure_group`)."""

    @abstractmethod
    async def consume(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int | None = None,
    ) -> list[Delivery]:
        """Up to ``count`` deliveries (see :meth:`EventTransport.consume`)."""

    @abstractmethod
    async def ack(self, delivery: Delivery) -> None:
        """Acknowledge ``delivery`` so it is not redelivered."""

    @abstractmethod
    async def ack_many(self, deliveries: Sequence[Delivery]) -> None:
        """Acknowledge several deliveries."""

    @abstractmethod
    async def dead_letter(self, delivery: Delivery, *, reason: str) -> None:
        """Move ``delivery`` to ``<stream>.dlq`` now (G-EVENT-07)."""

    @abstractmethod
    def never_dead_letter(self, stream: str, group: str) -> None:
        """Exempt ``(stream, group)`` from count-based dead-lettering."""

    # -- group ownership (required, G-EVENT-02) ----------------------------
    @abstractmethod
    async def claim_group_owner(
        self, stream: str, group: str, owner: GroupOwner
    ) -> None:
        """Record the owner on first claim; :class:`GroupOwnedElsewhere` otherwise."""

    @abstractmethod
    async def group_owner(self, stream: str, group: str) -> GroupOwner | None:
        """The recorded owner, or ``None`` when the group has none."""

    @abstractmethod
    async def list_group_owners(self) -> list[tuple[str, str, GroupOwner]]:
        """Every recorded owner as ``(stream, group, owner)``."""

    @abstractmethod
    async def transfer_group_owner(
        self, stream: str, group: str, *, expected: GroupOwner, owner: GroupOwner
    ) -> bool:
        """Compare-and-set on the whole owner."""

    @abstractmethod
    async def release_group_owner(self, stream: str, group: str) -> bool:
        """Forget the owner; whether there was one."""

    # -- retention (required, G-EVENT-12) ----------------------------------
    @abstractmethod
    def retention(self) -> Retention:
        """The stream retention the transport applies."""

    # -- ``lease_renewal`` -------------------------------------------------
    async def renew_pending(
        self, deliveries: Sequence[Delivery], consumer: str
    ) -> None:
        raise Unsupported("lease_renewal", capability="eventbus")

    def renew_interval_seconds(self) -> float:
        raise Unsupported("lease_renewal", capability="eventbus")

    # -- ``dlq_ops`` -------------------------------------------------------
    async def list_dlq(
        self, stream: str, *, count: int = 100, after: str | None = None
    ) -> list[DeadLetter]:
        raise Unsupported("dlq_ops", capability="eventbus")

    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: Sequence[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        raise Unsupported("dlq_ops", capability="eventbus")

    # -- ``inspection`` ----------------------------------------------------
    async def group_info(self, stream: str) -> list[GroupInfo]:
        raise Unsupported("inspection", capability="eventbus")

    async def stream_info(self, stream: str) -> StreamInfo:
        raise Unsupported("inspection", capability="eventbus")

    async def oldest_unacked_age_seconds(self, stream: str, group: str) -> float | None:
        raise Unsupported("inspection", capability="eventbus")

    # -- ``inventory`` -----------------------------------------------------
    async def register_consumer(
        self, stream: str, group: str, info: ConsumerInfo
    ) -> None:
        raise Unsupported("inventory", capability="eventbus")

    async def list_consumers(
        self, *, include_stale: bool = False
    ) -> list[ConsumerInfo]:
        raise Unsupported("inventory", capability="eventbus")

    async def forget_consumer(self, stream: str, group: str) -> bool:
        raise Unsupported("inventory", capability="eventbus")


EVENTBUS_CAPABILITY = CapabilitySpec(
    name="eventbus",
    version="v1",
    # The driver obligations changed (typed transport, spec 7); the
    # module-facing shape did not.
    major=2,
    provider_contract=InfraProvider,
    feature_kinds={
        # Entries survive a transport restart within its durability class and
        # a consumer crash, and are delivered at least once. Only a transport
        # declares it: the volatile buses (memory, broadcast) never do, so a
        # module that needs delivery to survive requires it and can never
        # silently run on one (G-EVENT-14, G-RISK-04).
        "durable": "guarantee",
        # Every process of the deployment sees the same streams and groups.
        "shared": "guarantee",
        # ``prefix.*`` subscriptions (volatile-bus shape only).
        "wildcard_subscribe": "behaviour",
        # ``consume(block_ms=)`` waits server-side.
        "blocking_consume": "behaviour",
        # ``ensure_group(start_id=)`` other than ``"0"`` (G-EVENT-09).
        "group_start_id": "behaviour",
        # ``renew_pending`` and ``renew_interval_seconds``.
        "lease_renewal": "behaviour",
        # ``list_dlq`` and ``replay_dlq`` (G-EVENT-11, ``sandloom streams``).
        "dlq_ops": "behaviour",
        # ``group_info``, ``stream_info``, ``oldest_unacked_age_seconds``.
        "inspection": "behaviour",
        # ``register_consumer``, ``list_consumers``, ``forget_consumer``.
        "inventory": "behaviour",
    },
    # The previous major's vocabulary: loading a removed name is refused with
    # its replacement named. No alias.
    removed_features={
        "publish": "nothing (a required operation of every driver: drop it "
        "from the requirement)",
        "subscribe": "nothing (a required operation of every driver: drop it "
        "from the requirement)",
        "volatile": "nothing (a volatile bus declares no 'durable')",
        "at_least_once": "durable",
        "consumer_groups": "durable",
    },
    description="Domain-event transport: a durable typed transport or a volatile bus.",
)


__all__ = [
    "EVENTBUS_CAPABILITY",
    "ConsumerInfo",
    "DeadLetter",
    "Delivery",
    "EventTransport",
    "EventTransportProvider",
    "GroupInfo",
    "GroupOwnedElsewhere",
    "GroupOwner",
    "PartialPublish",
    "Retention",
    "StreamInfo",
]
