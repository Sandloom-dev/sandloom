"""Redis Streams transport: consumer groups, NOGROUP recovery, ownership, inventory."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

import orjson
from prometheus_client import Counter

from sandloom.core.eventing.streams_transport_base import (
    _as_int,
    _stream_id_key,
    _StreamsTransportBase,
)
from sandloom.core.eventing.transport_base import (
    INVENTORY_STALE_SECONDS,
    GroupOwnershipError,
    _owner_conflict_message,
    _owners_match,
)

logger = logging.getLogger("sandloom.core.eventing.transport")


_METRIC_GROUP_RECREATED = Counter(
    "streams_group_recreated_total",
    "Streams consumer groups recreated after NOGROUP (stream/group deleted)",
    ["stream"],
)
_METRIC_GROUP_RECREATED_AT_TAIL = Counter(
    "streams_group_recreated_at_tail_total",
    "Streams consumer groups recreated at '$' after NOGROUP because no last "
    "delivered position was known (entries published meanwhile are skipped)",
    ["stream"],
)


class _StreamsGroups(_StreamsTransportBase):
    """Group lifecycle and diagnostics, group ownership, subscription inventory."""

    async def ensure_group(
        self,
        stream: str,
        group: str,
        *,
        start_id: str = "0",
    ) -> None:
        client = await self._client()
        try:
            # MKSTREAM so the group can be created before the first publish.
            await client.xgroup_create(
                self.key(stream), group, id=start_id, mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups.add((stream, group))
        if (stream, group) not in self._last_delivered:
            # Remember where the group stands, so a later NOGROUP recovery
            # resumes there (see ``_recreate_group``).
            for g in await self._xinfo_groups(client, stream):
                if str(g.get("name")) == group and g.get("last-delivered-id"):
                    self._last_delivered[(stream, group)] = (
                        str(g.get("last-delivered-id")),
                        None,
                    )

    async def _inventory_position(
        self, client: Any, stream: str, group: str
    ) -> str | None:
        """The last delivered id heartbeated into the subscription inventory."""
        try:
            raw = await client.hget(
                f"{self._prefix}{self.INVENTORY_KEY}", f"{stream}|{group}"
            )
            entry = orjson.loads(raw) if raw else None
        except Exception:  # noqa: BLE001 - unknown position
            return None
        if isinstance(entry, dict) and entry.get("last_delivered_id"):
            return str(entry["last_delivered_id"])
        return None

    async def _stream_reset_behind(
        self, client: Any, stream: str, position: str, envelope_id: str | None
    ) -> bool:
        """True if the stream was rebuilt under ``position`` (read it all).

        Its newest id is BELOW the position (a reset whose ids restarted
        lower), or the entry AT the position is another event than the one
        delivered there (rebuilt within the same millisecond).
        """
        key = self.key(stream)
        try:
            newest = await client.xrevrange(key, count=1)
        except Exception:  # noqa: BLE001 - cannot tell: keep the position
            return False
        if not newest:
            return False
        if _stream_id_key(str(newest[0][0])) < _stream_id_key(position):
            return True
        if envelope_id is None:
            return False
        raw = await self._fetch_raw(client, stream, position)
        if raw is None:
            return False  # trimmed / deleted entry: the position still holds
        try:
            decoded = orjson.loads(raw)
        except Exception:  # noqa: BLE001 - undecodable entry: position holds
            return True
        return not (isinstance(decoded, dict) and str(decoded.get("id")) == envelope_id)

    async def _recreate_group(self, stream: str, group: str) -> None:
        """Invalidate the cached group and recreate it after a NOGROUP.

        Recreated at the group's last delivered position — known from this
        process's own deliveries / ``ensure_group``, else from the
        subscription inventory heartbeat — never from ``"0"``, which replayed
        the whole stream retention (7 days) while the cache inbox dedupes only
        24 h. Entries published after that position (including between a key
        deletion and this recovery: the first ``XADD`` re-creates a bare
        stream without our group) are delivered. A stream rebuilt under the
        position (ids behind it, or another event at it) holds only new
        entries and is read from its start. With no known position the group
        is recreated at ``"$"`` — loudly (error log +
        ``streams_group_recreated_at_tail_total``): entries published while
        the group was missing are skipped; the outbox still holds them for
        ``sandloom outbox requeue``.
        """
        key = (stream, group)
        self._groups.discard(key)
        self._last_reclaim.pop(key, None)
        self._reclaim_cursors.pop(key, None)
        for cursor_key in list(self._pending_cursors):
            if cursor_key[:2] == key:
                self._pending_cursors.pop(cursor_key, None)
        # The PEL went with the group: rescan instead of trusting the set.
        self._outstanding.pop(key, None)
        self._pel_known = {ck for ck in self._pel_known if ck[:2] != key}
        _METRIC_GROUP_RECREATED.labels(stream=stream).inc()
        client = await self._client()
        position, envelope_id = self._last_delivered.get(key, (None, None))
        source = "this process"
        if position is None:
            position = await self._inventory_position(client, stream, group)
            source = "the subscription inventory"
        if position is None:
            create_id = "$"
            _METRIC_GROUP_RECREATED_AT_TAIL.labels(stream=stream).inc()
            logger.error(
                "Streams consumer group missing (NOGROUP) and its last delivered "
                "position is unknown; recreating at '$' stream=%s group=%s — "
                "entries published while it was missing are SKIPPED (requeue "
                "them from the outbox if needed)",
                stream,
                group,
            )
        elif await self._stream_reset_behind(client, stream, position, envelope_id):
            create_id = "0"
            logger.warning(
                "Streams consumer group missing (NOGROUP) and the stream was "
                "rebuilt under its last delivered position %s; recreating from "
                "the start of the new stream stream=%s group=%s",
                position,
                stream,
                group,
            )
        else:
            create_id = position
            logger.warning(
                "Streams consumer group missing (NOGROUP); recreating at its "
                "last delivered position %s (from %s) stream=%s group=%s",
                position,
                source,
                stream,
                group,
            )
        try:
            await client.xgroup_create(
                self.key(stream), group, id=create_id, mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups.add(key)

    async def group_info(self, stream: str) -> list[dict[str, Any]]:
        """Groups of ``stream``: pending, lag (Redis >= 7), last-delivered id."""
        client = await self._client()
        info: list[dict[str, Any]] = []
        for g in await self._xinfo_groups(client, stream):
            lag = g.get("lag")
            known = self._last_delivered.get((stream, str(g.get("name"))))
            last_delivered = g.get("last-delivered-id")
            if last_delivered and (known is None or known[0] != str(last_delivered)):
                self._last_delivered[(stream, str(g.get("name")))] = (
                    str(last_delivered),
                    None,
                )
            info.append(
                {
                    "group": str(g.get("name")),
                    "consumers": _as_int(g.get("consumers")),
                    "pending": _as_int(g.get("pending")),
                    "last_delivered_id": g.get("last-delivered-id"),
                    "lag": _as_int(lag),
                }
            )
        return info

    async def stream_info(self, stream: str) -> dict[str, Any]:
        """Length and newest entry id/age of ``stream`` (diagnostics)."""
        client = await self._client()
        key = self.key(stream)
        length = 0
        last_id: str | None = None
        try:
            length = int(await client.xlen(key))
            newest = await client.xrevrange(key, count=1)
            if newest:
                last_id = str(newest[0][0])
        except Exception:  # noqa: BLE001, S110 - diagnostics only
            pass
        age: float | None = None
        if last_id:
            try:
                age = max(0.0, time.time() - int(last_id.split("-", 1)[0]) / 1000)
            except ValueError:
                age = None
        return {"length": length, "last_id": last_id, "last_entry_age_seconds": age}

    async def oldest_unacked_age_seconds(
        self,
        stream: str,
        group: str,
        *,
        last_delivered_id: str | None,
        pending: int | None,
        lag: int | None,
    ) -> float | None:
        """At most one O(1)/O(log N) command: ``XPENDING`` summary or ``XRANGE``.

        Entry ids carry their append time (``<ms>-<seq>``), so the age of the
        oldest unacknowledged entry needs no payload read.
        """
        client = await self._client()
        key = self.key(stream)
        oldest: str | None = None
        if pending:
            summary = await client.xpending(key, group)
            if isinstance(summary, Mapping) and summary.get("min"):
                oldest = str(summary["min"])
        elif lag != 0:
            # lag None = unknown (Redis < 7 or an inexact counter): look.
            start = f"({last_delivered_id}" if last_delivered_id else "-"
            entries = await client.xrange(key, min=start, count=1)
            if entries:
                oldest = str(entries[0][0])
        if oldest is None:
            return 0.0
        try:
            appended_ms = int(oldest.split("-", 1)[0])
        except ValueError:
            return None
        return max(0.0, time.time() - appended_ms / 1000)

    # -- ownership + inventory ---------------------------------------------
    async def claim_group_owner(
        self, stream: str, group: str, owner: Mapping[str, Any]
    ) -> None:
        client = await self._client()
        key = self._owner_key(stream, group)
        value = orjson.dumps(dict(owner)).decode()
        for _ in range(2):
            if await client.set(key, value, nx=True):
                return
            raw = await client.get(key)
            if raw is None:
                continue  # released concurrently: retry the claim once
            try:
                existing = orjson.loads(raw)
            except Exception:  # noqa: BLE001 - corrupt marker: re-own it
                await client.set(key, value)
                return
            if not isinstance(existing, dict) or _owners_match(existing, owner):
                return
            raise GroupOwnershipError(
                _owner_conflict_message(stream, group, existing, owner)
            )

    async def group_owner(self, stream: str, group: str) -> dict[str, Any] | None:
        client = await self._client()
        raw = await client.get(self._owner_key(stream, group))
        if raw is None:
            return None
        try:
            parsed = orjson.loads(raw)
        except Exception:  # noqa: BLE001 - corrupt record reads as absent
            return None
        return parsed if isinstance(parsed, dict) else None

    async def release_group_owner(self, stream: str, group: str) -> bool:
        """Forget the owner of ``(stream, group)`` (operator, after a move)."""
        client = await self._client()
        return bool(await client.delete(self._owner_key(stream, group)))

    async def list_group_owners(self) -> list[dict[str, Any]]:
        client = await self._client()
        marker = f"{self._prefix}sandloom:group-owner:"
        out: list[dict[str, Any]] = []
        async for raw_key in client.scan_iter(match=f"{marker}*", count=500):
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            raw = await client.get(key)
            if raw is None:
                continue
            try:
                owner = orjson.loads(raw)
            except Exception:  # noqa: BLE001, S112 - corrupt marker: not listed
                continue
            if not isinstance(owner, dict):
                continue
            rest = key[len(marker) :]
            stream = str(owner.get("channel") or "")
            if not stream or not rest.startswith(f"{stream}:"):
                stream, _, _ = rest.partition(":")
            out.append(
                {"stream": stream, "group": rest[len(stream) + 1 :], "owner": owner}
            )
        return sorted(out, key=lambda e: (e["stream"], e["group"]))

    async def transfer_group_owner(
        self,
        stream: str,
        group: str,
        *,
        expected: Mapping[str, Any],
        owner: Mapping[str, Any],
    ) -> bool:
        client = await self._client()
        key = self._owner_key(stream, group)
        raw = await client.get(key)
        if raw is None:
            return False
        try:
            current = orjson.loads(raw)
        except Exception:  # noqa: BLE001 - corrupt record never matches
            return False
        if current != dict(expected):
            return False
        # Compare-and-set: only if the marker still holds what we read.
        script = (
            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
            "redis.call('SET', KEYS[1], ARGV[2]) return 1 end return 0"
        )
        old = raw.decode() if isinstance(raw, bytes) else str(raw)
        new = orjson.dumps(dict(owner)).decode()
        return bool(await client.eval(script, 1, key, old, new))

    async def register_consumer(
        self, stream: str, group: str, info: Mapping[str, Any]
    ) -> None:
        client = await self._client()
        entry = {**dict(info), "stream": stream, "group": group}
        entry["last_seen"] = time.time()
        await client.hset(
            f"{self._prefix}{self.INVENTORY_KEY}",
            f"{stream}|{group}",
            orjson.dumps(entry).decode(),
        )

    async def list_consumers(
        self, *, include_stale: bool = False
    ) -> list[dict[str, Any]]:
        client = await self._client()
        raw = await client.hgetall(f"{self._prefix}{self.INVENTORY_KEY}")
        cutoff = time.time() - INVENTORY_STALE_SECONDS
        entries: list[dict[str, Any]] = []
        for value in (raw or {}).values():
            try:
                entry = orjson.loads(value)
            except Exception:  # noqa: BLE001, S112 - skip a corrupt entry
                continue
            if not isinstance(entry, dict):
                continue
            stale = float(entry.get("last_seen", 0) or 0) < cutoff
            entry["stale"] = stale
            if include_stale or not stale:
                entries.append(entry)
        entries.sort(key=lambda e: (str(e.get("stream")), str(e.get("group"))))
        return entries

    async def forget_consumer(self, stream: str, group: str) -> bool:
        """Drop an inventory entry (operator cleanup of a retired consumer)."""
        client = await self._client()
        inventory = f"{self._prefix}{self.INVENTORY_KEY}"
        return bool(await client.hdel(inventory, f"{stream}|{group}"))
