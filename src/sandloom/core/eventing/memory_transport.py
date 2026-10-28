"""In-process consumer-group transport (tests / single-process dev)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.transport_base import (
    _DEFAULT_MAX_DELIVERIES,
    _DEFAULT_RETRY_MAX_MS,
    _METRIC_TARGETED_SKIPPED,
    DLQ_REASON_MAX_DELIVERIES,
    INVENTORY_STALE_SECONDS,
    Delivery,
    GroupOwnershipError,
    MessageTransport,
    _owner_conflict_message,
    _owners_match,
    retry_delay_ms,
)
from sandloom.core.eventing.transport_capabilities import TransportCapability as Cap
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.transport")


class MemoryTransport(MessageTransport):
    """In-process transport with consumer-group + ack semantics.

    Faithful enough for tests and single-process dev: an appended message stays
    deliverable to a group until that group acks it, and is redelivered on a
    later ``consume`` if left unacked (after ``retry_base_ms`` backoff — 0 by
    default so unit tests stay instantaneous). Not durable across process
    restarts — that is what the Redis Streams driver is for. Mirrors the Redis
    driver's group start position, targeted DLQ replay, group ownership and
    subscription inventory so those contracts are testable without Redis.
    """

    capabilities = frozenset(
        {
            Cap.BLOCKING_CONSUME,
            Cap.GROUP_START_ID,
            Cap.BATCH_PUBLISH,
            Cap.BATCH_ACK,
            Cap.GROUP_OWNERSHIP,
            Cap.NEVER_DEAD_LETTER,
            Cap.GROUP_INFO,
            Cap.OLDEST_UNACKED_AGE,
            Cap.REGISTER_CONSUMER,
            Cap.LIST_CONSUMERS,
        }
    )

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
        self._owners: dict[tuple[str, str], dict[str, Any]] = {}
        # (stream, group) -> inventory entry
        self._inventory: dict[tuple[str, str], dict[str, Any]] = {}
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
                self._dlq.setdefault(stream, []).append((msg_id, env, attempts, group))
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
        self._dlq.setdefault(delivery.stream, []).append(
            (delivery.id, delivery.envelope, attempts, delivery.group)
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

    async def group_info(self, stream: str) -> list[dict[str, Any]]:
        log = self._log.get(stream, [])
        return [
            {
                "group": group,
                "consumers": None,
                "pending": len(self._pending.get((s, group), {})),
                "last_delivered_id": log[cursor - 1][0] if cursor else "0-0",
                "lag": len(log) - cursor,
            }
            for (s, group), cursor in sorted(self._cursor.items())
            if s == stream
        ]

    async def claim_group_owner(
        self, stream: str, group: str, owner: Mapping[str, Any]
    ) -> None:
        existing = self._owners.setdefault((stream, group), dict(owner))
        if not _owners_match(existing, owner):
            raise GroupOwnershipError(
                _owner_conflict_message(stream, group, existing, owner)
            )

    async def release_group_owner(self, stream: str, group: str) -> bool:
        return self._owners.pop((stream, group), None) is not None

    async def list_group_owners(self) -> list[dict[str, Any]]:
        return [
            {"stream": stream, "group": group, "owner": dict(owner)}
            for (stream, group), owner in sorted(self._owners.items())
        ]

    async def transfer_group_owner(
        self,
        stream: str,
        group: str,
        *,
        expected: Mapping[str, Any],
        owner: Mapping[str, Any],
    ) -> bool:
        if self._owners.get((stream, group)) != dict(expected):
            return False
        self._owners[(stream, group)] = dict(owner)
        return True

    async def register_consumer(
        self, stream: str, group: str, info: Mapping[str, Any]
    ) -> None:
        self._inventory[(stream, group)] = {
            **dict(info),
            "stream": stream,
            "group": group,
            "last_seen": time.time(),
        }

    async def list_consumers(
        self, *, include_stale: bool = False
    ) -> list[dict[str, Any]]:
        cutoff = time.time() - INVENTORY_STALE_SECONDS
        return [
            dict(entry)
            for entry in self._inventory.values()
            if include_stale or float(entry.get("last_seen", 0)) >= cutoff
        ]

    async def list_dlq(
        self, stream: str, *, count: int = 100, after: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": msg_id,
                "original_id": msg_id,
                "group": group,
                "event_id": env.get("id"),
                "attempts": attempts,
                "reason": DLQ_REASON_MAX_DELIVERIES,
                "replayable": True,
            }
            for msg_id, env, attempts, group in [
                row
                for row in self._dlq.get(stream, [])
                if after is None
                or tuple(map(int, row[0].split("-")))
                > tuple(map(int, after.split("-")))
            ][:count]
        ]

    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: list[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
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
