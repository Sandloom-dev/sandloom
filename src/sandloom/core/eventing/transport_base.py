"""What the kernel's transports share: reliability knobs, the DLQ markers and
the defaults of the operations every transport has.

The contract itself is :class:`~sandloom.core.infra.ports.eventbus.EventTransport`
(spec 7); :class:`TransportDefaults` implements the operations a transport may
leave to a loop (``publish_many``, ``ack_many``) and the ``never_dead_letter``
exemption the same way for the built-in transports.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Sequence
from typing import Any

from prometheus_client import Counter

from sandloom.core.config.settings import settings
from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.infra.errors import Unsupported
from sandloom.core.infra.ports.eventbus import (
    Delivery,
    GroupOwner,
    PartialPublish,
)

_METRIC_TARGETED_SKIPPED = Counter(
    "streams_targeted_replay_skipped_total",
    "Replayed DLQ entries acked-and-skipped by a group they were not meant for",
    ["stream"],
)


#: Fingerprint fields that must MATCH for two processes to share a group
#: (replicas of one deployment). ``service`` / ``channel`` are informational.
GROUP_OWNER_IDENTITY_FIELDS = ("module", "database")
#: A subscription-inventory entry not refreshed for this long is stale.
INVENTORY_STALE_SECONDS = 300.0
#: Stream-entry field marking a DLQ replay meant for ONE consumer group only.
TARGET_GROUP_FIELD = "target_group"


def _owner_conflict_message(
    stream: str, group: str, existing: GroupOwner, owner: GroupOwner
) -> str:
    def _fmt(fp: GroupOwner) -> str:
        return " ".join(
            f"{field}={getattr(fp, field)!r}"
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


def _owners_match(existing: GroupOwner, owner: GroupOwner) -> bool:
    return all(
        getattr(existing, field) == getattr(owner, field)
        for field in GROUP_OWNER_IDENTITY_FIELDS
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


def declared_features(transport: Any) -> frozenset[str]:
    """The ``eventbus`` features ``transport`` declares.

    A transport provider answers ``features()``; a bare transport (the
    in-memory reference one, a test double) states ``supported_features`` on
    its class. Nothing is probed from its methods.
    """
    features = getattr(transport, "features", None)
    if callable(features):
        return frozenset(features())
    return frozenset(getattr(type(transport), "supported_features", ()))


def performance_hint(transport: Any, operation: str) -> str | None:
    """The speed claim the transport's class makes about ``operation``."""
    hints = getattr(type(transport), "performance_hints", None)
    return None if hints is None else hints.get(operation)


class TransportDefaults:
    """The operations the built-in transports implement the same way.

    ``publish_many`` and ``ack_many`` loop over their single forms (a transport
    with a native batch overrides them and says ``one_round_trip`` in its
    ``performance_hints``); ``never_dead_letter`` records the exempt groups.
    """

    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        raise NotImplementedError

    async def ack(self, delivery: Delivery) -> None:
        raise NotImplementedError

    def never_dead_letter(self, stream: str, group: str) -> None:
        """Exempt ``(stream, group)`` from delivery-count dead-lettering.

        A failing delivery of that group is retried FOREVER with the capped
        backoff instead of moving to the DLQ after ``max_deliveries`` — for
        consumers whose events must never be silently parked (security
        revocations). Only an explicit ``dead_letter`` (a handler raising
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

    async def publish_many(
        self, items: Sequence[tuple[str, EventEnvelope]]
    ) -> list[str]:
        """Append every ``(stream, envelope)`` IN ORDER; return their ids.

        One :meth:`publish` each, stopping at the first failure with
        :class:`PartialPublish` (``published`` = the ids of the leading items
        appended, the cause chained).
        """
        ids: list[str] = []
        for stream, envelope in items:
            try:
                ids.append(await self.publish(stream, envelope))
            except Exception as exc:
                raise partial_publish(
                    ids,
                    f"publish_many stopped at item {len(ids)} of {len(items)} "
                    f"(stream {stream!r}): {type(exc).__name__}: {exc}",
                    exc,
                ) from exc
        return ids

    async def ack_many(self, deliveries: Sequence[Delivery]) -> None:
        """Acknowledge several deliveries, one :meth:`ack` each."""
        for delivery in deliveries:
            await self.ack(delivery)

    async def renew_pending(
        self, deliveries: Sequence[Delivery], consumer: str
    ) -> None:
        """Without ``lease_renewal`` a transport has nothing to renew."""
        raise Unsupported("lease_renewal", capability="eventbus")

    def renew_interval_seconds(self) -> float:
        raise Unsupported("lease_renewal", capability="eventbus")


def partial_publish(
    published: Sequence[str], message: str, cause: BaseException
) -> PartialPublish:
    """A :class:`PartialPublish` whose ``retryable`` follows ``cause``.

    An :class:`InfraError` cause says whether it is retryable; any other
    exception is a backend fault the caller retries (a duplicate, never a loss).
    """
    retryable = getattr(cause, "retryable", True)
    return PartialPublish(message, published=published, retryable=bool(retryable))
