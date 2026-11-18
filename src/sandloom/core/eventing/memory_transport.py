"""In-process consumer-group transport (tests / single-process dev)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import ClassVar

from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.transport_base import (
    _DEFAULT_MAX_DELIVERIES,
    _DEFAULT_RETRY_MAX_MS,
    _METRIC_TARGETED_SKIPPED,
    DLQ_REASON_MAX_DELIVERIES,
    INVENTORY_STALE_SECONDS,
    TransportDefaults,
    _owner_conflict_message,
    _owners_match,
    retry_delay_ms,
)
from sandloom.core.infra.errors import InfraError, InvalidArgument
from sandloom.core.infra.ports.eventbus import (
    ConsumerInfo,
    DeadLetter,
    Delivery,
    GroupInfo,
    GroupOwnedElsewhere,
    GroupOwner,
    Retention,
    StreamInfo,
)
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.transport")


#: The shape the Streams transport accepts (ASCII digits only, each number an
#: unsigned 64-bit integer), so the same id is refused by both.
_ENTRY_ID = re.compile(r"[0-9]{1,20}-[0-9]{1,20}")
_ENTRY_ID_PART_MAX = 2**64 - 1


def _entry_key(entry_id: str, what: str = "a DLQ cursor") -> tuple[int, ...]:
    """Order key of an entry id (``<ms>-<seq>``); any other shape is refused."""
    parts = entry_id.split("-")
    if _ENTRY_ID.fullmatch(entry_id) is None or any(
        int(part) > _ENTRY_ID_PART_MAX for part in parts
    ):
        raise InvalidArgument(
            f"{what} is not an entry id of this transport (<ms>-<seq>)",
            capability="eventbus",
        )
    return tuple(map(int, parts))


class MemoryTransport(TransportDefaults):
    """In-process transport with consumer-group + ack semantics.

    Faithful enough for tests and single-process dev: an appended message stays
    deliverable to a group until that group acks it, and is redelivered on a
    later ``consume`` if left unacked (after ``retry_base_ms`` backoff — 0 by
    default so unit tests stay instantaneous). Not durable across process
    restarts — that is what the streams driver is for. Mirrors the streams
    driver's group start position, targeted DLQ replay, group ownership and
    subscription inventory so those contracts are testable without a server.
    """

    #: The ``eventbus`` features of this transport (spec 7). ``durable`` is
    #: its durability class: the process (see the class docstring).
    supported_features: ClassVar[frozenset[str]] = frozenset(
        {
            "durable",
            "blocking_consume",
            "group_start_id",
            "dlq_ops",
            "inspection",
            "inventory",
        }
    )
    performance_hints: ClassVar[MappingProxyType[str, str]] = MappingProxyType({})

    def __init__(
        self,
        *,
        max_deliveries: int = _DEFAULT_MAX_DELIVERIES,
        retry_base_ms: int = 0,
        retry_max_ms: int = _DEFAULT_RETRY_MAX_MS,
    ) -> None:
        # stream -> ordered list of (msg_id, envelope)
        self._log: dict[str, list[tuple[str, EventEnvelope]]] = {}
        # msg_id -> (publish time ms, target group or None)
        self._meta: dict[str, tuple[int, str | None]] = {}
        # (stream, group) -> next unread index into the log
        self._cursor: dict[tuple[str, str], int] = {}
        # (stream, group) -> {msg_id: envelope} delivered but not yet acked
        self._pending: dict[tuple[str, str], dict[str, EventEnvelope]] = {}
        # (stream, group, msg_id) -> delivery attempts
        self._delivery_count: dict[tuple[str, str, str], int] = {}
        # (stream, group, msg_id) -> monotonic time of the last delivery
        self._delivered_at: dict[tuple[str, str, str], float] = {}
        # stream -> list of (msg_id, envelope, attempts, group) dead-lettered
        self._dlq: dict[str, list[tuple[str, EventEnvelope, int, str]]] = {}
        # stream -> event set on publish (wakes a blocked consume)
        self._arrivals: dict[str, asyncio.Event] = {}
        # (stream, group) -> owner fingerprint
        self._owners: dict[tuple[str, str], GroupOwner] = {}
        # (stream, group) -> inventory entry
        self._inventory: dict[tuple[str, str], ConsumerInfo] = {}
        self._seq = 0
        self._max_deliveries = max_deliveries
        self._retry_base_ms = retry_base_ms
        self._retry_max_ms = retry_max_ms

    def _start_index(self, stream: str, start_id: str) -> int:
        log = self._log.get(stream, [])
        if start_id == "$":
            return len(log)
        start_ms = int(start_id.split("-", 1)[0] or 0)
        if start_ms <= 0:
            return 0
        for idx, (msg_id, _env) in enumerate(log):
            if self._meta.get(msg_id, (0, None))[0] >= start_ms:
                return idx
        return len(log)

    async def ensure_group(
        self,
        stream: str,
        group: str,
        *,
        start_id: str = "0",
    ) -> None:
        self._log.setdefault(stream, [])
        key = (stream, group)
        if key not in self._cursor:
            self._cursor[key] = self._start_index(stream, start_id)
        self._pending.setdefault(key, {})

    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        return self._append(stream, envelope, target_group=None)

    def _append(
        self, stream: str, envelope: EventEnvelope, *, target_group: str | None
    ) -> str:
        self._seq += 1
        msg_id = f"{self._seq}-0"
        self._log.setdefault(stream, []).append((msg_id, envelope))
        self._meta[msg_id] = (int(time.time() * 1000), target_group)
        arrival = self._arrivals.get(stream)
        if arrival is not None:
            arrival.set()
        return msg_id

    def _park(
        self,
        stream: str,
        msg_id: str,
        envelope: EventEnvelope,
        attempts: int,
        group: str,
    ) -> None:
        """Append to the stream's dead-letter queue: the one place that does,
        and the seam for a test that makes the append fail (it raises
        :class:`InfraError`; the source is acked only after this returns)."""
        self._dlq.setdefault(stream, []).append((msg_id, envelope, attempts, group))

    def _collect(self, stream: str, group: str, count: int) -> list[Delivery]:
        key = (stream, group)
        pending = self._pending[key]
        deliveries: list[Delivery] = []
        now = time.monotonic()

        # Redeliver still-unacked messages first (at-least-once), but only once
        # their retry backoff has elapsed. A message already delivered
        # ``max_deliveries`` times is poison: route it to the DLQ and stop
        # delivering it, so one bad event cannot wedge the group.
        for msg_id, env in list(pending.items()):
            if len(deliveries) >= count:
                break
            dkey = (stream, group, msg_id)
            attempts = self._delivery_count.get(dkey, 0)
            if attempts >= self._max_deliveries and self.dead_letters_by_count(
                stream, group
            ):
                try:
                    self._park(stream, msg_id, env, attempts, group)
                except InfraError:
                    # Ack the source only after the DLQ append succeeded: it
                    # stays pending and the append is tried again on a later
                    # consume (G-EVENT-10), as the Streams driver does.
                    safe_inc("streams_dlq_append_failures_total", {"stream": stream})
                    logger.error(
                        "Failed to dead-letter memory message; leaving it pending "
                        "stream=%s group=%s id=%s",
                        stream,
                        group,
                        msg_id,
                        exc_info=True,
                    )
                    continue
                pending.pop(msg_id, None)
                self._delivery_count.pop(dkey, None)
                self._delivered_at.pop(dkey, None)
                safe_inc("streams_messages_dlq_total", {"stream": stream})
                logger.warning(
                    "Streams message dead-lettered after %d deliveries "
                    "stream=%s group=%s id=%s",
                    attempts,
                    stream,
                    group,
                    msg_id,
                )
                continue
            delay = retry_delay_ms(
                attempts, base_ms=self._retry_base_ms, max_ms=self._retry_max_ms
            )
            if (now - self._delivered_at.get(dkey, 0.0)) * 1000 < delay:
                continue  # backing off
            self._delivery_count[dkey] = attempts + 1
            self._delivered_at[dkey] = now
            deliveries.append(
                Delivery(id=msg_id, stream=stream, group=group, envelope=env)
            )

        # Then deliver new messages past the cursor.
        log = self._log.setdefault(stream, [])
        idx = self._cursor[key]
        while idx < len(log) and len(deliveries) < count:
            msg_id, env = log[idx]
            idx += 1
            target = self._meta.get(msg_id, (0, None))[1]
            if target is not None and target != group:
                # A replay meant for another group: ack-skip it here.
                _METRIC_TARGETED_SKIPPED.labels(stream=stream).inc()
                continue
            pending[msg_id] = env
            self._delivery_count[(stream, group, msg_id)] = 1
            self._delivered_at[(stream, group, msg_id)] = now
            deliveries.append(
                Delivery(id=msg_id, stream=stream, group=group, envelope=env)
            )
        self._cursor[key] = idx
        return deliveries

    async def consume(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int | None = None,
    ) -> list[Delivery]:
        await self.ensure_group(stream, group)
        deliveries = self._collect(stream, group, count)
        if deliveries or not block_ms or block_ms <= 0:
            return deliveries
        arrival = self._arrivals.setdefault(stream, asyncio.Event())
        arrival.clear()
        try:
            await asyncio.wait_for(arrival.wait(), timeout=block_ms / 1000)
        except TimeoutError:
            pass
        return self._collect(stream, group, count)

    async def ack(self, delivery: Delivery) -> None:
        pending = self._pending.get((delivery.stream, delivery.group))
        if pending is not None:
            pending.pop(delivery.id, None)
        dkey = (delivery.stream, delivery.group, delivery.id)
        self._delivery_count.pop(dkey, None)
        self._delivered_at.pop(dkey, None)

    async def dead_letter(self, delivery: Delivery, *, reason: str) -> None:
        dkey = (delivery.stream, delivery.group, delivery.id)
        attempts = self._delivery_count.get(dkey, 0)
        self._park(
            delivery.stream, delivery.id, delivery.envelope, attempts, delivery.group
        )
        await self.ack(delivery)
        safe_inc("streams_messages_dlq_total", {"stream": delivery.stream})
        logger.critical(
            "Streams message dead-lettered (%s) stream=%s group=%s id=%s",
            reason,
            delivery.stream,
            delivery.group,
            delivery.id,
        )

    def pending_count(self, stream: str, group: str) -> int:
        """Unacked deliveries for a group (diagnostics/tests)."""
        return len(self._pending.get((stream, group), {}))

    def dlq_depth(self, stream: str) -> int:
        """Dead-lettered message count for a stream (diagnostics/tests)."""
        return len(self._dlq.get(stream, []))

    def retention(self) -> Retention:
        """Unbounded: the log is kept for the life of the process."""
        return Retention(max_age_seconds=None, max_length=None)

    async def group_info(self, stream: str) -> list[GroupInfo]:
        log = self._log.get(stream, [])
        return [
            GroupInfo(
                group=group,
                consumers=None,
                pending=len(self._pending.get((s, group), {})),
                last_delivered_id=log[cursor - 1][0] if cursor else "0-0",
                lag=len(log) - cursor,
            )
            for (s, group), cursor in sorted(self._cursor.items())
            if s == stream
        ]

    async def stream_info(self, stream: str) -> StreamInfo:
        log = self._log.get(stream, [])
        if not log:
            return StreamInfo(length=0, last_id=None, last_entry_age_seconds=None)
        last_id = log[-1][0]
        appended_ms = self._meta.get(last_id, (0, None))[0]
        age = max(0.0, time.time() - appended_ms / 1000) if appended_ms else None
        return StreamInfo(length=len(log), last_id=last_id, last_entry_age_seconds=age)

    async def oldest_unacked_age_seconds(self, stream: str, group: str) -> float | None:
        key = (stream, group)
        if key not in self._cursor:
            return None
        log = self._log.get(stream, [])
        pending = self._pending.get(key, {})
        candidates = list(pending)
        if self._cursor[key] < len(log):
            candidates.append(log[self._cursor[key]][0])
        times = [self._meta[m][0] for m in candidates if m in self._meta]
        if not times:
            return 0.0
        return max(0.0, time.time() - min(times) / 1000)

    async def claim_group_owner(
        self, stream: str, group: str, owner: GroupOwner
    ) -> None:
        existing = self._owners.setdefault((stream, group), owner)
        if not _owners_match(existing, owner):
            raise GroupOwnedElsewhere(
                _owner_conflict_message(stream, group, existing, owner),
                capability="eventbus",
            )

    async def group_owner(self, stream: str, group: str) -> GroupOwner | None:
        return self._owners.get((stream, group))

    async def release_group_owner(self, stream: str, group: str) -> bool:
        return self._owners.pop((stream, group), None) is not None

    async def list_group_owners(self) -> list[tuple[str, str, GroupOwner]]:
        return [
            (stream, group, owner)
            for (stream, group), owner in sorted(self._owners.items())
        ]

    async def transfer_group_owner(
        self, stream: str, group: str, *, expected: GroupOwner, owner: GroupOwner
    ) -> bool:
        if self._owners.get((stream, group)) != expected:
            return False
        self._owners[(stream, group)] = owner
        return True

    async def register_consumer(
        self, stream: str, group: str, info: ConsumerInfo
    ) -> None:
        self._inventory[(stream, group)] = replace(
            info, stream=stream, group=group, last_seen=time.time(), stale=False
        )

    async def list_consumers(
        self, *, include_stale: bool = False
    ) -> list[ConsumerInfo]:
        cutoff = time.time() - INVENTORY_STALE_SECONDS
        listed: list[ConsumerInfo] = []
        for entry in self._inventory.values():
            stale = (entry.last_seen or 0.0) < cutoff
            if include_stale or not stale:
                listed.append(replace(entry, stale=stale))
        return listed

    async def forget_consumer(self, stream: str, group: str) -> bool:
        return self._inventory.pop((stream, group), None) is not None

    async def list_dlq(
        self, stream: str, *, count: int = 100, after: str | None = None
    ) -> list[DeadLetter]:
        after_key = None if after is None else _entry_key(after)
        return [
            DeadLetter(
                id=msg_id,
                original_id=msg_id,
                group=group,
                event_id=env.get("id"),
                attempts=attempts,
                reason=DLQ_REASON_MAX_DELIVERIES,
                replayable=True,
            )
            for msg_id, env, attempts, group in [
                row
                for row in self._dlq.get(stream, [])
                if after_key is None or _entry_key(row[0]) > after_key
            ][:count]
        ]

    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: Sequence[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        # Refused before anything is replayed, as the Streams transport does: a
        # malformed id is the caller's input, whatever the DLQ holds.
        for entry_id in ids or ():
            _entry_key(entry_id, "a replay id")
        entries = self._dlq.get(stream, [])
        keep: list[tuple[str, EventEnvelope, int, str]] = []
        replayed = 0
        for entry in entries:
            msg_id, env, _attempts, owner_group = entry
            wanted = (ids is None or msg_id in ids) and (
                group is None or owner_group == group
            )
            if wanted and replayed < count:
                self._append(stream, env, target_group=owner_group)
                replayed += 1
            else:
                keep.append(entry)
        self._dlq[stream] = keep
        return replayed
