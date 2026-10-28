"""The durable message transport contract and its shared value types."""

from __future__ import annotations

import abc
import os
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from prometheus_client import Counter

from sandloom.core.eventing.envelope import EventEnvelope

if TYPE_CHECKING:
    from sandloom.core.eventing.transport_capabilities import TransportCapabilities
from sandloom.core.config.settings import settings

_METRIC_TARGETED_SKIPPED = Counter(
    "streams_targeted_replay_skipped_total",
    "Replayed DLQ entries acked-and-skipped by a group they were not meant for",
    ["stream"],
)


class GroupOwnershipError(ValueError):
    """A consumer group is already owned by a different module / database.

    Two deployments sharing one consumer group split its events between them
    (each sees only part of the stream), and with their exactly-once claims in
    different databases an ``XAUTOCLAIM`` hand-over double-applies events. The
    framework records the owner of every group it creates
    (``sandloom:group-owner:<stream>:<group>``) and refuses a second owner. A
    ``ValueError`` so the consumer runtime treats it as permanent (no retry).
    """


#: Fingerprint fields that must MATCH for two processes to share a group
#: (replicas of one deployment). ``service`` / ``channel`` are informational.
GROUP_OWNER_IDENTITY_FIELDS = ("module", "database")
#: A subscription-inventory entry not refreshed for this long is stale.
INVENTORY_STALE_SECONDS = 300.0
#: Stream-entry field marking a DLQ replay meant for ONE consumer group only.
TARGET_GROUP_FIELD = "target_group"


def _owner_conflict_message(
    stream: str, group: str, existing: Mapping[str, Any], owner: Mapping[str, Any]
) -> str:
    def _fmt(fp: Mapping[str, Any]) -> str:
        return " ".join(
            f"{field}={fp.get(field)!r}"
            for field in (*GROUP_OWNER_IDENTITY_FIELDS, "service")
        )

    return (
        f"consumer group {group!r} on stream {stream!r} is owned by "
        f"[{_fmt(existing)}] but this process is [{_fmt(owner)}]: sharing one "
        "group would split its events between the two deployments and apply "
        "them against different exactly-once inboxes. Give the consumer a "
        "distinct name (groups are namespaced by module) or an explicit "
        "group=..., or — if the module deliberately moved to another database "
        "after draining the group — release the old owner with "
        f"`sandloom streams release-group {stream} {group}`."
    )


def _owners_match(existing: Mapping[str, Any], owner: Mapping[str, Any]) -> bool:
    return all(
        existing.get(field) == owner.get(field) for field in GROUP_OWNER_IDENTITY_FIELDS
    )


# Default reliability knobs. A message delivered this many times without an ack
# is poison; it is routed to the stream's DLQ instead of being retried forever.
_DEFAULT_MAX_DELIVERIES = 5
# How long a delivered-but-unacked entry must be idle before another consumer
# may reclaim it (dead-consumer recovery). Milliseconds.
_DEFAULT_MIN_IDLE_MS = 60_000
# Per-message retry backoff for a delivery the handler failed (left unacked):
# the entry is not redelivered until it has been idle for
# ``base * 2**(deliveries-1)`` ms (capped). Without it a failing handler was
# retried in a tight loop and dead-lettered within milliseconds — no time for a
# transient dependency (DB failover, rate limit, …) to recover.
_DEFAULT_RETRY_BASE_MS = 1_000
_DEFAULT_RETRY_MAX_MS = 60_000
# Dead-consumer reclaim (XAUTOCLAIM) is throttled to once per this interval per
# (stream, group) — it only matters at ``min_idle_ms`` granularity anyway.
_RECLAIM_INTERVAL_SECONDS = 5.0
# How many pending entries to scan for poison/due retries per consume. Wider
# than the read budget so a poison entry deep in the backlog is dead-lettered
# promptly instead of blocking head-of-line behind retrying entries ahead of it.
_POISON_SCAN_COUNT = 256

# DLQ entry ``reason`` markers (an operator can tell WHY an entry is there).
DLQ_REASON_MAX_DELIVERIES = "max_deliveries_exceeded"
DLQ_REASON_SOURCE_MISSING = "source_entry_missing"
DLQ_REASON_POISON = "poison_event"
DLQ_REASON_MALFORMED = "malformed_envelope"


def service_name() -> str | None:
    """The configured ``SANDLOOM_SERVICE_NAME`` (``None`` when unset)."""

    name = getattr(settings, "sandloom_service_name", None)
    return str(name) if name else None


def process_consumer_id() -> str:
    """A consumer name unique to this process (``[service@]host-pid``).

    Each process gets its own consumer within a group, so Redis tracks pending
    entries per process and ``XAUTOCLAIM`` can reclaim a dead process's backlog.
    A fixed name (e.g. ``group.1``) would make two replicas share one pending
    list and mask a crashed consumer. ``SANDLOOM_SERVICE_NAME`` (when set) prefixes
    it so ``XINFO CONSUMERS`` shows which deployment a consumer belongs to.
    """
    base = f"{socket.gethostname()}-{os.getpid()}"
    service = service_name()
    return f"{service}@{base}" if service else base


def retry_delay_ms(deliveries: int, *, base_ms: int, max_ms: int) -> int:
    """Idle time (ms) a failed entry must reach before its next delivery."""
    if base_ms <= 0 or deliveries <= 0:
        return 0
    return int(min(max_ms, base_ms * (2 ** (deliveries - 1))))


class TransportBatchError(Exception):
    """A :meth:`MessageTransport.publish_many` batch did not fully append.

    ``published`` holds the message ids of the LEADING items that were
    appended before the failure (in order; empty when nothing — or nothing
    known — was appended). The original error is the ``__cause__``. Items
    after the first failure may or may not have been appended: callers treat
    everything past ``published`` as undelivered and leave it to a retry
    (at-least-once — a retried item may be a duplicate, never a loss).
    """

    def __init__(self, published: Sequence[str], message: str) -> None:
        super().__init__(message)
        self.published: list[str] = list(published)


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


class MessageTransport(abc.ABC):
    """Consumer-group transport with explicit acknowledgement (at-least-once)."""

    #: The optional features THIS class implements (see
    #: :mod:`sandloom.core.eventing.transport_capabilities`). Read from the class
    #: that sets it only — a subclass that does not redeclare it is probed.
    capabilities: ClassVar[TransportCapabilities | None] = None

    def never_dead_letter(self, stream: str, group: str) -> None:
        """Exempt ``(stream, group)`` from delivery-count dead-lettering.

        A failing delivery of that group is retried FOREVER with the capped
        backoff instead of moving to the DLQ after ``max_deliveries`` — for
        consumers whose events must never be silently parked (security
        revocations). Only an explicit :meth:`dead_letter` (a handler raising
        :class:`~sandloom.core.eventing.delivery.PoisonEventError`) parks one.
        """
        exempt: set[tuple[str, str]] = self.__dict__.setdefault(
            "_never_dead_letter", set()
        )
        exempt.add((stream, group))

    def dead_letters_by_count(self, stream: str, group: str) -> bool:
        """False when :meth:`never_dead_letter` exempted ``(stream, group)``."""
        exempt = self.__dict__.get("_never_dead_letter", set())
        return (stream, group) not in exempt

    async def dead_letter(self, delivery: Delivery, *, reason: str) -> None:
        """Move ``delivery`` to the DLQ NOW (a poison event) and ack it.

        Default: unsupported (the delivery is left pending and redelivered).
        """
        raise NotImplementedError(f"{type(self).__name__} has no explicit DLQ")

    @abc.abstractmethod
    async def ensure_group(
        self,
        stream: str,
        group: str,
        *,
        start_id: str = "0",
    ) -> None:
        """Idempotently create the consumer group for ``stream``.

        ``start_id`` is where a NEW group starts (``"0"`` = all retained
        entries, ``"$"`` = only new ones, ``"<ms>-0"`` = from a timestamp); an
        existing group keeps its position. (The bus feature-detects the
        keyword, so a transport with the bare ``(stream, group)`` signature
        works too.)
        """

    async def claim_group_owner(
        self, stream: str, group: str, owner: Mapping[str, Any]
    ) -> None:
        """Record/verify the owner fingerprint of ``(stream, group)``.

        Raises :class:`GroupOwnershipError` when another module / database
        already owns the group. Default: no ownership tracking.
        """
        return None

    async def list_group_owners(self) -> list[dict[str, Any]]:
        """Every recorded group owner: ``{"stream", "group", "owner"}`` dicts."""
        return []

    async def transfer_group_owner(
        self,
        stream: str,
        group: str,
        *,
        expected: Mapping[str, Any],
        owner: Mapping[str, Any],
    ) -> bool:
        """Replace the owner of ``(stream, group)`` iff it is still ``expected``.

        Operator path after a module's data moved to another database
        (``sandloom-migrate extract-module``). Default: no ownership tracking.
        """
        return False

    async def group_info(self, stream: str) -> list[dict[str, Any]]:
        """Consumer groups of ``stream`` with pending count and lag."""
        return []

    async def oldest_unacked_age_seconds(
        self,
        stream: str,
        group: str,
        *,
        last_delivered_id: str | None,
        pending: int | None,
        lag: int | None,
    ) -> float | None:
        """Age of the oldest entry ``group`` has not acknowledged (0 = none).

        The oldest pending (delivered, unacked) entry, else the first entry
        after ``last_delivered_id``. Default ``None``: not measurable.
        """
        return None

    async def register_consumer(
        self, stream: str, group: str, info: Mapping[str, Any]
    ) -> None:
        """Record (heartbeat) a live subscription in the shared inventory."""
        return None

    async def list_consumers(
        self, *, include_stale: bool = False
    ) -> list[dict[str, Any]]:
        """The subscription inventory (live entries unless ``include_stale``)."""
        return []

    @abc.abstractmethod
    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        """Append ``envelope`` to ``stream``; return the transport message id."""

    async def publish_many(
        self, items: Sequence[tuple[str, EventEnvelope]]
    ) -> list[str]:
        """Append every ``(stream, envelope)`` IN ORDER; return their ids.

        Default: one :meth:`publish` each, stopping at the first failure.
        Transports override it to append a whole batch in one round trip.
        Any failure raises :class:`TransportBatchError` (``published`` = the
        ids of the leading items known to be appended, the cause chained).
        """
        ids: list[str] = []
        for stream, envelope in items:
            try:
                ids.append(await self.publish(stream, envelope))
            except Exception as exc:
                raise TransportBatchError(
                    ids,
                    f"publish_many stopped at item {len(ids)} of {len(items)} "
                    f"(stream {stream!r}): {type(exc).__name__}: {exc}",
                ) from exc
        return ids

    @abc.abstractmethod
    async def consume(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int | None = None,
    ) -> list[Delivery]:
        """Return up to ``count`` deliveries for ``(stream, group)``.

        Unacknowledged deliveries are redelivered on a subsequent call (after
        the transport's retry backoff) — a message is never dropped until it is
        :meth:`ack`-ed or dead-lettered. Returns ``[]`` when nothing is due.
        ``block_ms`` lets the call wait up to that long for a NEW message
        instead of returning ``[]`` immediately (no busy polling).
        """

    @abc.abstractmethod
    async def ack(self, delivery: Delivery) -> None:
        """Acknowledge ``delivery`` so it is not redelivered."""

    async def ack_many(self, deliveries: Sequence[Delivery]) -> None:
        """Acknowledge several deliveries (default: one :meth:`ack` each).

        Transports override it to ack a consumed batch in one round trip.
        """
        for delivery in deliveries:
            await self.ack(delivery)

    async def list_dlq(
        self, stream: str, *, count: int = 100, after: str | None = None
    ) -> list[dict[str, Any]]:
        """Return dead-lettered entries of ``stream`` (operator inspection)."""
        raise NotImplementedError(f"{type(self).__name__} has no DLQ inspection")

    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: list[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        """Re-publish dead-lettered entries to ``stream``; return how many.

        A DLQ entry belongs to the ONE consumer group that kept failing it, so
        the replay is TARGETED at that group: every other group acks the
        replayed entry without running its handlers (they already processed
        the event — a non-durable handler elsewhere would otherwise repeat its
        side effect). ``group`` restricts the replay to entries dead-lettered by
        that group. The envelope (and its stable id) is unchanged; the target
        group gets a fresh delivery budget. Entries whose payload was lost
        (``source_entry_missing``) cannot be replayed and are left in the DLQ.
        """
        raise NotImplementedError(f"{type(self).__name__} has no DLQ replay")
