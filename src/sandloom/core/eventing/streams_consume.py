"""Redis Streams transport: consume, pending scan, reclaim, leases, ack, DLQ moves."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

import orjson

from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.streams_groups import _StreamsGroups
from sandloom.core.eventing.streams_transport_base import (
    _is_nogroup,
    _PendingEntry,
    _stream_id_key,
)
from sandloom.core.eventing.transport_base import (
    _METRIC_TARGETED_SKIPPED,
    _POISON_SCAN_COUNT,
    DLQ_REASON_MALFORMED,
    DLQ_REASON_MAX_DELIVERIES,
    DLQ_REASON_SOURCE_MISSING,
    TARGET_GROUP_FIELD,
    Delivery,
)
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.transport")


class _StreamsConsume(_StreamsGroups):
    """Consume path (XREADGROUP / XCLAIM / XAUTOCLAIM), acks, leases, DLQ moves."""

    def _note_delivered(self, stream: str, group: str, msg_id: str, raw: Any) -> None:
        """Record ``msg_id`` as the group's newest delivered position."""
        envelope_id: str | None = None
        if raw is not None:
            try:
                decoded = orjson.loads(raw)
            except Exception:  # noqa: BLE001 - malformed: position only
                decoded = None
            if isinstance(decoded, dict) and decoded.get("id") is not None:
                envelope_id = str(decoded["id"])
        self._last_delivered[(stream, group)] = (msg_id, envelope_id)

    # -- consume ------------------------------------------------------------
    async def _reclaim_stale(
        self, client: Any, stream: str, group: str, consumer: str, count: int
    ) -> None:
        """Reclaim entries idle past ``min_idle_ms`` from other consumers.

        Best-effort: a Redis without ``XAUTOCLAIM`` (<6.2) simply skips reclaim.
        Claimed entries become this consumer's pending and are redelivered by
        the backoff-gated ``XCLAIM`` path.

        Reclaim uses ``JUSTID`` so it does NOT bump Redis' delivery counter: a
        reclaim is not a handler attempt, and counting it would let churn from a
        crashed/restarting consumer burn the poison budget and dead-letter a
        message a healthy consumer would have processed.
        """
        key = (stream, group)
        now = time.monotonic()
        if now - self._last_reclaim.get(key, float("-inf")) < self._reclaim_interval:
            return
        self._last_reclaim[key] = now
        try:
            result = await client.xautoclaim(
                self.key(stream),
                group,
                consumer,
                self._min_idle_ms,
                start_id=self._reclaim_cursors.get(key, "0-0"),
                count=count,
                justid=True,
            )
        except TypeError:  # pragma: no cover - client without justid kwarg
            try:
                result = await client.xautoclaim(
                    self.key(stream),
                    group,
                    consumer,
                    self._min_idle_ms,
                    start_id=self._reclaim_cursors.get(key, "0-0"),
                    count=count,
                )
            except Exception:  # noqa: BLE001 - older redis / transient: skip
                safe_inc(
                    "streams_maintenance_failures_total",
                    {"stream": stream, "operation": "xautoclaim"},
                )
                return
        except Exception:  # noqa: BLE001 - older redis / transient: skip reclaim
            safe_inc(
                "streams_maintenance_failures_total",
                {"stream": stream, "operation": "xautoclaim"},
            )
            return
        # xautoclaim returns (next_cursor, claimed_entries[, deleted]); count it.
        has_claimed = isinstance(result, (list, tuple)) and len(result) > 1
        if has_claimed:
            cursor = result[0]
            self._reclaim_cursors[key] = (
                cursor.decode() if isinstance(cursor, bytes) else str(cursor)
            )
        claimed = result[1] if has_claimed else []
        if claimed:
            # JUSTID replies ids; a client without it replies (id, fields).
            self._track(
                stream,
                group,
                consumer,
                (
                    str(item[0] if isinstance(item, (list, tuple)) else item)
                    for item in claimed
                ),
            )
            safe_inc("streams_messages_claimed_total", {"stream": stream})

    def _track(
        self, stream: str, group: str, consumer: str, msg_ids: Iterable[str]
    ) -> None:
        """Record ``msg_ids`` as entries in ``consumer``'s PEL."""
        by_consumer = self._outstanding.setdefault((stream, group), {})
        by_consumer.setdefault(consumer, set()).update(msg_ids)

    def _untrack(self, stream: str, group: str, msg_ids: Iterable[str]) -> None:
        """Forget ``msg_ids`` once an ``XACK`` of them succeeded."""
        by_consumer = self._outstanding.get((stream, group))
        if by_consumer:
            ids = msg_ids if isinstance(msg_ids, (set, frozenset)) else set(msg_ids)
            for outstanding in by_consumer.values():
                outstanding.difference_update(ids)

    def _pending_scan_needed(self, stream: str, group: str, consumer: str) -> bool:
        """Whether ``consume`` must look at the consumer's PEL (XPENDING).

        Not when every entry this transport put there was acked and nothing
        went wrong since the last complete scan: entries enter a consumer's
        PEL only through its own reads / claims (its name is unique per
        process and bus), so there is nothing to retry or dead-letter. A
        failed handler's entry stays outstanding (it is not acked), so it is
        rescanned until it is redelivered or dead-lettered. A scan is forced
        every ``reclaim_interval_seconds`` regardless.
        """
        ckey = (stream, group, consumer)
        if ckey not in self._pel_known:
            return True
        if self._outstanding.get((stream, group), {}).get(consumer):
            return True
        last = self._last_pending_scan.get(ckey, float("-inf"))
        return time.monotonic() - last >= self._reclaim_interval

    async def _pending_entries(
        self, client: Any, stream: str, group: str, count: int, consumer: str
    ) -> list[_PendingEntry]:
        """A rotating window of this consumer's pending entries.

        Server-side ownership filtering prevents another replica's backlog from
        hiding our retries. Advance past backing-off entries too, so a large
        local PEL cannot permanently hide eligible retries behind its head.
        """
        cursor_key = (stream, group, consumer)
        self._last_pending_scan[cursor_key] = time.monotonic()
        cursor = self._pending_cursors.get(cursor_key)
        if cursor and cursor_key in self._pel_known:
            # The outstanding set is exact enough to know whether anything
            # lies past the cursor: wrap to the head in ONE call, not two.
            mark = _stream_id_key(cursor)
            outstanding = self._outstanding.get((stream, group), {}).get(consumer)
            if not any(_stream_id_key(i) > mark for i in outstanding or ()):
                cursor = None
        from_head = not cursor
        try:
            pend = await client.xpending_range(
                self.key(stream),
                group,
                min=f"({cursor}" if cursor else "-",
                max="+",
                count=count,
                consumername=consumer,
            )
            if not pend and cursor:
                from_head = True
                pend = await client.xpending_range(
                    self.key(stream),
                    group,
                    min="-",
                    max="+",
                    count=count,
                    consumername=consumer,
                )
        except Exception:  # noqa: BLE001 - no pending / unsupported: empty
            self._pel_known.discard(cursor_key)
            return []
        entries: list[_PendingEntry] = []
        for item in pend or []:
            if isinstance(item, Mapping):
                entries.append(
                    _PendingEntry(
                        msg_id=str(item.get("message_id")),
                        consumer=str(item.get("consumer", "")),
                        idle_ms=int(item.get("time_since_delivered", 0) or 0),
                        times=int(item.get("times_delivered", 1) or 1),
                    )
                )
            else:  # tuple form: (id, consumer, idle_ms, times)
                entries.append(
                    _PendingEntry(
                        msg_id=str(item[0]),
                        consumer=str(item[1]),
                        idle_ms=int(item[2]),
                        times=int(item[3]),
                    )
                )
        if entries:
            self._pending_cursors[cursor_key] = entries[-1].msg_id
        else:
            self._pending_cursors.pop(cursor_key, None)
        if from_head and len(entries) < count:
            # A complete view of the consumer's PEL: resynchronise the set
            # (drops ids acked elsewhere or claimed away by a successor).
            self._outstanding.setdefault((stream, group), {})[consumer] = {
                e.msg_id for e in entries
            }
            self._pel_known.add(cursor_key)
        elif entries:
            self._track(stream, group, consumer, (e.msg_id for e in entries))
        return entries

    async def _targeted_elsewhere(
        self, client: Any, stream: str, group: str, msg_id: str, fields: Any
    ) -> bool:
        target = fields.get(TARGET_GROUP_FIELD) if isinstance(fields, dict) else None
        if target is None or str(target) == group:
            return False
        _METRIC_TARGETED_SKIPPED.labels(stream=stream).inc()
        await client.xack(self.key(stream), group, msg_id)
        self._untrack(stream, group, (msg_id,))
        return True

    async def _to_dlq(
        self,
        client: Any,
        stream: str,
        group: str,
        msg_id: str,
        raw: str | None,
        attempts: int,
        *,
        reason: str | None = None,
    ) -> None:
        dlq = self._dlq_key(stream)
        if reason is None:
            reason = (
                DLQ_REASON_MAX_DELIVERIES
                if raw is not None
                else DLQ_REASON_SOURCE_MISSING
            )
        fields: dict[str, Any] = {
            "source": stream,
            "group": group,
            "original_id": str(msg_id),
            "attempts": str(attempts),
            "reason": reason,
        }
        if raw is not None:
            fields["data"] = raw
        # Ack the source ONLY after the DLQ append durably succeeds. Acking in a
        # finally (even when xadd raised) would drop the poison message on the
        # floor: it is gone from the source PEL and never reached the DLQ, and
        # the upstream outbox already considers it delivered to the broker — so
        # it is unrecoverable. Instead, on a DLQ failure we leave it un-acked: it
        # stays in the pending list and a later consume retries the DLQ append
        # once the broker recovers, while the metric below stays flat so a
        # "dead-lettering is failing" alert can fire.
        try:
            await client.xadd(dlq, fields)
        except Exception:
            safe_inc("streams_dlq_append_failures_total", {"stream": stream})
            logger.error(
                "Failed to dead-letter streams message to %s; leaving it pending "
                "for reclaim (NOT acked) stream=%s group=%s id=%s",
                dlq,
                stream,
                group,
                msg_id,
                exc_info=True,
            )
            return
        safe_inc("streams_messages_dlq_total", {"stream": stream})
        logger.warning(
            "Streams message dead-lettered (%s) after %d deliveries "
            "stream=%s group=%s id=%s -> %s",
            reason,
            attempts,
            stream,
            group,
            msg_id,
            dlq,
        )
        await client.xack(self.key(stream), group, msg_id)
        self._untrack(stream, group, (msg_id,))

    def _decode(self, stream: str, group: str, msg_id: str, raw: Any) -> Delivery:
        envelope = cast(EventEnvelope, orjson.loads(raw))
        if not isinstance(envelope, dict) or not isinstance(
            envelope.get("payload"), dict
        ):
            raise ValueError("Stream envelope must contain an object payload")
        if not isinstance(envelope.get("id"), str) or not envelope["id"]:
            raise ValueError("Stream envelope must contain a nonempty event id")
        return Delivery(id=msg_id, stream=stream, group=group, envelope=envelope)

    async def _decode_entry(
        self,
        client: Any,
        stream: str,
        group: str,
        msg_id: str,
        fields: Any,
        attempts: int,
    ) -> Delivery | None:
        """Apply identical replay targeting and corruption rules on every path."""
        fields = fields if isinstance(fields, dict) else {}
        if await self._targeted_elsewhere(client, stream, group, msg_id, fields):
            return None
        raw = fields.get("data")
        if raw is None:
            await self._to_dlq(
                client,
                stream,
                group,
                msg_id,
                None,
                attempts,
                reason=DLQ_REASON_SOURCE_MISSING,
            )
            return None
        try:
            return self._decode(stream, group, msg_id, raw)
        except (ValueError, TypeError):
            safe_inc("events_malformed_total", {"channel": stream})
            await self._to_dlq(
                client,
                stream,
                group,
                msg_id,
                raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw),
                attempts,
                reason=DLQ_REASON_MALFORMED,
            )
            return None

    async def consume(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int | None = None,
    ) -> list[Delivery]:
        try:
            return await self._consume(stream, group, consumer, count, block_ms)
        except BaseException:
            # A reply may be lost after Redis applied the command (entries
            # read or claimed into our PEL unseen): rescan on the next call.
            self._pel_known.discard((stream, group, consumer))
            raise

    async def _consume(
        self, stream: str, group: str, consumer: str, count: int, block_ms: int | None
    ) -> list[Delivery]:
        client = await self._client()
        key = self.key(stream)
        if (stream, group) not in self._groups:
            await self.ensure_group(stream, group)
        # Reclaim dead consumers' stale pending (throttled).
        await self._reclaim_stale(client, stream, group, consumer, count)
        pending = (
            await self._pending_entries(
                client, stream, group, max(count, _POISON_SCAN_COUNT), consumer
            )
            if self._pending_scan_needed(stream, group, consumer)
            else []
        )
        deliveries, next_due_ms = await self._redeliver_pending(
            client, key, stream, group, consumer, count, pending
        )

        remaining = count - len(deliveries)
        if remaining <= 0:
            return deliveries
        block: int | None = None
        if not deliveries and block_ms and block_ms > 0:
            # Wait server-side for new entries, but wake in time for the next
            # due retry of our own pending backlog.
            block = (
                block_ms if next_due_ms is None else max(1, min(block_ms, next_due_ms))
            )
        resp = await self._read_new(
            client, key, stream, group, consumer, remaining, block
        )
        for _stream_name, entries in resp or []:
            if not entries:
                continue
            await self._collect_new(
                client, stream, group, consumer, entries, deliveries
            )
        return deliveries

    async def _redeliver_pending(
        self,
        client: Any,
        key: str,
        stream: str,
        group: str,
        consumer: str,
        count: int,
        pending: list[_PendingEntry],
    ) -> tuple[list[Delivery], int | None]:
        """Retry due pending entries, dead-letter exhausted ones.

        Returns the redeliveries and how long (ms) until the next backing-off
        entry of this consumer is due (``None``: none is).
        """
        deliveries: list[Delivery] = []
        next_due_ms: int | None = None
        for entry in pending:
            delay = self._retry_delay(entry.times)
            backing_off = entry.idle_ms < delay
            if (
                entry.times >= self._max_deliveries
                and not backing_off
                and self.dead_letters_by_count(stream, group)
            ):
                # Delivered the full budget and not in flight: poison.
                await self._dead_letter_exhausted(client, key, stream, group, entry)
                continue
            if entry.consumer != consumer:
                continue  # another live consumer's in-flight entry
            if backing_off:
                wait = delay - entry.idle_ms
                next_due_ms = wait if next_due_ms is None else min(next_due_ms, wait)
                continue
            if len(deliveries) >= count:
                continue
            # Atomic, backoff-respecting redelivery: XCLAIM only succeeds if the
            # entry is still idle >= delay (another replica cannot double-take
            # it) and bumps its delivery counter (a real handler attempt).
            claimed = await client.xclaim(
                key, group, consumer, min_idle_time=delay, message_ids=[entry.msg_id]
            )
            if claimed:
                self._track(stream, group, consumer, (str(m) for m, _f in claimed))
            for msg_id, fields in claimed or []:
                msg_id = str(msg_id)
                delivery = await self._decode_entry(
                    client, stream, group, msg_id, fields, entry.times + 1
                )
                if delivery is not None:
                    deliveries.append(delivery)
        return deliveries, next_due_ms

    async def _dead_letter_exhausted(
        self, client: Any, key: str, stream: str, group: str, entry: _PendingEntry
    ) -> None:
        """Move an entry that used its whole delivery budget to the DLQ."""
        frames = await client.xrange(key, min=entry.msg_id, max=entry.msg_id, count=1)
        if frames and await self._targeted_elsewhere(
            client, stream, group, entry.msg_id, frames[0][1]
        ):
            return
        raw = await self._fetch_raw(client, stream, entry.msg_id)
        await self._to_dlq(client, stream, group, entry.msg_id, raw, entry.times)

    async def _read_new(
        self,
        client: Any,
        key: str,
        stream: str,
        group: str,
        consumer: str,
        remaining: int,
        block: int | None,
    ) -> Any:
        """``XREADGROUP >``, recreating a group lost to NOGROUP once."""
        try:
            return await client.xreadgroup(
                group, consumer, {key: ">"}, count=remaining, block=block
            )
        except Exception as exc:
            if not _is_nogroup(exc):
                raise
            # The stream key or the group vanished under us (FLUSHDB, DEL,
            # XGROUP DESTROY, a Redis restart without persistence). The cached
            # "group created" flag is now a lie and XREADGROUP would fail with
            # NOGROUP forever: drop it, recreate the group and read again.
            await self._recreate_group(stream, group)
            return await client.xreadgroup(
                group, consumer, {key: ">"}, count=remaining, block=block
            )

    async def _collect_new(
        self,
        client: Any,
        stream: str,
        group: str,
        consumer: str,
        entries: Any,
        deliveries: list[Delivery],
    ) -> None:
        """Decode one stream's new entries into ``deliveries``; note the position."""
        self._track(stream, group, consumer, (str(m) for m, _f in entries))
        # Only the newest entry is the group's position: note it once per
        # read, from its decoded envelope when it decoded (no extra parse).
        last_id, last_fields = entries[-1]
        last_id = str(last_id)
        for msg_id, fields in entries:
            msg_id = str(msg_id)
            fields = fields if isinstance(fields, dict) else {}
            delivery = await self._decode_entry(
                client, stream, group, msg_id, fields, 1
            )
            if delivery is not None:
                deliveries.append(delivery)
        if deliveries and deliveries[-1].id == last_id:
            self._last_delivered[(stream, group)] = (
                last_id,
                str(deliveries[-1].envelope["id"]),
            )
        else:  # targeted elsewhere / dead-lettered: decode its id only
            raw = last_fields.get("data") if isinstance(last_fields, dict) else None
            self._note_delivered(stream, group, last_id, raw)

    async def ack(self, delivery: Delivery) -> None:
        client = await self._client()
        await client.xack(self.key(delivery.stream), delivery.group, delivery.id)
        self._untrack(delivery.stream, delivery.group, (delivery.id,))

    @property
    def renew_interval_seconds(self) -> float:
        """Renew live batches before Redis' dead-consumer idle threshold."""
        return max(0.001, self._min_idle_ms / 3000)

    async def renew_pending(
        self, deliveries: Sequence[Delivery], consumer: str
    ) -> None:
        """Reset idle only while this process STILL owns each pending entry.

        The ownership check and JUSTID claim are atomic. Never steal a delivery
        from a successor, and never burn a handler attempt for a heartbeat.
        """
        if not deliveries:
            return
        script = """
local group, owner = ARGV[1], ARGV[2]
local lost = 0
for i=3,#ARGV do
  local p = redis.call('XPENDING', KEYS[1], group, ARGV[i], ARGV[i], 1)
  if #p > 0 and p[1][2] == owner then
    redis.call('XCLAIM', KEYS[1], group, owner, 0, ARGV[i], 'JUSTID')
  else
    lost = lost + 1
  end
end
return lost
"""
        client = await self._client()
        first = deliveries[0]
        lost = await client.eval(
            script,
            1,
            self.key(first.stream),
            first.group,
            consumer,
            *(delivery.id for delivery in deliveries),
        )
        if lost:
            raise RuntimeError("Streams delivery lease lost; aborting active handler")

    async def dead_letter(self, delivery: Delivery, *, reason: str) -> None:
        client = await self._client()
        raw = orjson.dumps(delivery.envelope).decode()
        logger.critical(
            "Poison event dead-lettered (%s) stream=%s group=%s id=%s — "
            "inspect with `sandloom streams dlq-list %s`",
            reason,
            delivery.stream,
            delivery.group,
            delivery.id,
            delivery.stream,
        )
        await self._to_dlq(
            client, delivery.stream, delivery.group, delivery.id, raw, 0, reason=reason
        )

    async def ack_many(self, deliveries: Sequence[Delivery]) -> None:
        """One multi-id ``XACK`` per ``(stream, group)`` of the batch."""
        if not deliveries:
            return
        by_group: dict[tuple[str, str], list[str]] = {}
        for delivery in deliveries:
            by_group.setdefault((delivery.stream, delivery.group), []).append(
                delivery.id
            )
        client = await self._client()
        for (stream, group), ids in by_group.items():
            await client.xack(self.key(stream), group, *ids)
            self._untrack(stream, group, ids)

    async def stats(self, stream: str, group: str) -> dict[str, int]:
        """Pending + DLQ depth for diagnostics. Best-effort (0 on error)."""
        client = await self._client()
        pending = 0
        dlq = 0
        try:
            summary = await client.xpending(self.key(stream), group)
            pending = int(summary.get("pending", 0) if isinstance(summary, dict) else 0)
        except Exception:  # noqa: BLE001 - diagnostics only
            pending = 0
        try:
            dlq = int(await client.xlen(self._dlq_key(stream)))
        except Exception:  # noqa: BLE001 - diagnostics only
            dlq = 0
        return {"pending": pending, "dlq": dlq}
