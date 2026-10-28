"""Redis Streams transport: consumer groups, XACK, reclaim, DLQ."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

import orjson
from prometheus_client import Counter

from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.eventing.streams_consume import _StreamsConsume
from sandloom.core.eventing.transport_base import (
    DLQ_REASON_MAX_DELIVERIES,
    TARGET_GROUP_FIELD,
    TransportBatchError,
)
from sandloom.core.eventing.transport_capabilities import TransportCapability
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.transport")


_METRIC_DLQ_REPLAYED = Counter(
    "streams_dlq_replayed_total",
    "Dead-lettered Streams entries re-published by an operator replay",
    ["stream"],
)


class RedisStreamsTransport(_StreamsConsume):
    """Production transport over Redis Streams (durable, consumer groups, ACK).

    Unlike Pub/Sub, a Streams entry persists until acknowledged, so an event
    published while a consumer is offline is delivered when it comes back.

    Reliability model:

    * **at-least-once** — ``consume`` redelivers this consumer's still-pending
      entries (via ``XCLAIM``) then reads new ones (``XREADGROUP >``, with
      ``BLOCK`` so an idle consumer waits server-side instead of polling);
      ``ack`` issues ``XACK``.
    * **retry backoff** — a pending (failed) entry is redelivered only once it
      has been idle for ``retry_base_ms * 2**(deliveries-1)`` (capped at
      ``retry_max_ms``), using the PEL idle time Redis already tracks, so a
      failing handler is retried over seconds/minutes instead of milliseconds.
      ``XCLAIM``'s ``min-idle-time`` makes the redelivery atomic across
      replicas.
    * **dead-consumer recovery** — ``XAUTOCLAIM`` (throttled) reclaims entries
      left pending by a consumer idle past ``min_idle_ms`` (e.g. a crashed
      replica), so a dead process's backlog is not stranded.
    * **poison handling** — an entry already delivered ``max_deliveries`` times
      (and idle past its backoff, i.e. not in flight) is moved to the per-stream
      DLQ (``<stream>.dlq``, with a ``reason`` marker and the owning ``group``)
      and acked. The ack happens only after the DLQ append succeeds; if it fails
      the entry is left pending rather than acked into oblivion.
      :meth:`replay_dlq` re-publishes DLQ entries — targeted at the group that
      dead-lettered them — once the cause is fixed.
    * **retention** — every ``XADD`` caps the stream (``MAXLEN ~`` when
      ``maxlen`` > 0) and a throttled ``XTRIM MINID ~`` drops entries older than
      ``retention_seconds``, so a stream cannot grow forever and a new group
      created at ``"0"`` replays at most the retention window.
    * **namespacing** — ``prefix`` (``EVENT_STREAM_PREFIX``) is prepended to
      every Redis key this transport touches (streams, DLQs, ownership and
      inventory keys) so several environments can share one Redis. Channel /
      stream names in the API stay logical (unprefixed).
    """

    capabilities = frozenset(TransportCapability)

    # -- publish + retention -------------------------------------------------
    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        return await self._append(stream, {"data": orjson.dumps(envelope)})

    async def publish_many(
        self, items: Sequence[tuple[str, EventEnvelope]]
    ) -> list[str]:
        """Append the whole batch in ONE round trip (``MULTI`` / ``EXEC``).

        Order is preserved (one connection, commands queued in order) and the
        batch is all-or-nothing for queue-time errors: an ``XADD`` Redis
        refuses while queueing (OOM, wrong arity) aborts the ``EXEC`` and
        nothing is appended. A connection error before the reply means
        "unknown" — reported as nothing appended, so the caller retries
        (duplicates possible, never loss). An execution-time error of one
        ``XADD`` (``WRONGTYPE``: the key is not a stream) is reported as the
        leading successes only; later entries of that batch that Redis did
        append become duplicates on retry. ``MAXLEN ~`` caps every append as
        in :meth:`publish`; the age trim is checked once per stream per batch.
        """
        if not items:
            return []
        if len(items) == 1:
            stream, envelope = items[0]
            try:
                return [await self.publish(stream, envelope)]
            except Exception as exc:
                raise TransportBatchError(
                    [], f"XADD to {stream!r} failed: {type(exc).__name__}: {exc}"
                ) from exc
        client = await self._client()
        keys: dict[str, None] = {}
        try:
            async with client.pipeline(transaction=True) as pipe:
                for stream, envelope in items:
                    key = self.key(stream)
                    keys[key] = None
                    fields = {"data": orjson.dumps(envelope)}
                    if self._maxlen > 0:
                        pipe.xadd(key, fields, maxlen=self._maxlen, approximate=True)
                    else:
                        pipe.xadd(key, fields)
                results = await pipe.execute(raise_on_error=False)
        except Exception as exc:
            raise TransportBatchError(
                [],
                f"MULTI/EXEC of {len(items)} XADD(s) failed: "
                f"{type(exc).__name__}: {exc}",
            ) from exc
        ids: list[str] = []
        for result in results:
            if isinstance(result, BaseException):
                raise TransportBatchError(
                    ids,
                    f"XADD {len(ids)} of {len(items)} failed in EXEC: "
                    f"{type(result).__name__}: {result}",
                ) from result
            ids.append(str(result))
        for key in keys:
            await self._maybe_trim_by_age(client, key)
        return ids

    async def _append(self, stream: str, fields: dict[str, Any]) -> str:
        client = await self._client()
        key = self.key(stream)
        if self._maxlen > 0:
            msg_id = await client.xadd(
                key, fields, maxlen=self._maxlen, approximate=True
            )
        else:
            msg_id = await client.xadd(key, fields)
        await self._maybe_trim_by_age(client, key)
        return str(msg_id)

    async def _maybe_trim_by_age(self, client: Any, key: str) -> None:
        """Throttled ``XTRIM MINID ~ <now - retention>`` (best-effort)."""
        if self._retention_seconds <= 0:
            return
        now = time.monotonic()
        if now - self._last_trim.get(key, float("-inf")) < self._trim_interval:
            return
        self._last_trim[key] = now
        min_id = f"{int((time.time() - self._retention_seconds) * 1000)}-0"
        try:
            await client.xtrim(key, minid=min_id, approximate=True)
        except Exception:  # Redis < 6.2 / transient: skip
            safe_inc(
                "streams_maintenance_failures_total",
                {"stream": key, "operation": "xtrim"},
            )
            logger.debug("XTRIM MINID failed for %s", key, exc_info=True)

    # -- DLQ ----------------------------------------------------------------
    async def list_dlq(
        self, stream: str, *, count: int = 100, after: str | None = None
    ) -> list[dict[str, Any]]:
        client = await self._client()
        entries = await client.xrange(
            self._dlq_key(stream),
            count=count,
            **({"min": f"({after}"} if after else {}),
        )
        listed: list[dict[str, Any]] = []
        for dlq_id, fields in entries or []:
            fields = fields if isinstance(fields, dict) else {}
            event_id: Any = None
            data = fields.get("data")
            if data is not None:
                try:
                    event_id = orjson.loads(data).get("id")
                except Exception:  # noqa: BLE001 - diagnostics only
                    event_id = None
            listed.append(
                {
                    "id": str(dlq_id),
                    "original_id": fields.get("original_id"),
                    "group": fields.get("group"),
                    "event_id": event_id,
                    "attempts": int(fields.get("attempts", 0) or 0),
                    "reason": fields.get("reason", DLQ_REASON_MAX_DELIVERIES),
                    "replayable": data is not None,
                }
            )
        return listed

    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: list[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        """Re-append DLQ entries to ``stream`` for their ORIGINAL group only.

        The replayed entry carries ``target_group=<the group that dead-lettered
        it>``: that group processes it with a fresh delivery budget; every other
        group of the stream acks it without running handlers (it already
        processed the event the first time — re-running a non-durable handler
        there would repeat its side effect). ``group`` restricts the replay to
        entries dead-lettered by that group. An entry whose target group no
        longer exists is left in the DLQ (nobody would process it). The DLQ
        entry is deleted only after the re-append succeeds.
        """
        client = await self._client()
        dlq = self._dlq_key(stream)
        if ids is not None:
            entries = []
            for entry_id in dict.fromkeys(ids):
                entries.extend(
                    await client.xrange(dlq, min=entry_id, max=entry_id, count=1)
                )
        else:
            entries = await client.xrange(dlq, count=count)
        live_groups = {g.get("name") for g in await self._xinfo_groups(client, stream)}
        replayed = 0
        for dlq_id, fields in entries or []:
            if replayed >= count:
                break
            dlq_id = str(dlq_id)
            if ids is not None and dlq_id not in ids:
                continue
            fields = fields if isinstance(fields, dict) else {}
            owner_group = fields.get("group")
            if group is not None and owner_group != group:
                continue
            data = fields.get("data")
            if data is None:
                logger.warning(
                    "DLQ entry %s on %s has no payload (%s); not replayable",
                    dlq_id,
                    dlq,
                    fields.get("reason"),
                )
                continue
            replay_fields: dict[str, Any] = {"data": data}
            if owner_group:
                if owner_group not in live_groups:
                    logger.warning(
                        "DLQ entry %s on %s belongs to group %s which no longer "
                        "exists on the stream; left in the DLQ",
                        dlq_id,
                        dlq,
                        owner_group,
                    )
                    continue
                replay_fields[TARGET_GROUP_FIELD] = owner_group
            await self._append(stream, replay_fields)
            await client.xdel(dlq, dlq_id)
            replayed += 1
            _METRIC_DLQ_REPLAYED.labels(stream=stream).inc()
        return replayed
