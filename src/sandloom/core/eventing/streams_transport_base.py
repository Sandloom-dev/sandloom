"""Redis Streams transport: state, key layout and raw entry access."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sandloom.core.config.settings import settings
from sandloom.core.eventing.transport_base import (
    _DEFAULT_MAX_DELIVERIES,
    _DEFAULT_MIN_IDLE_MS,
    _DEFAULT_RETRY_BASE_MS,
    _DEFAULT_RETRY_MAX_MS,
    _RECLAIM_INTERVAL_SECONDS,
    MessageTransport,
    retry_delay_ms,
)

logger = logging.getLogger("sandloom.core.eventing.transport")


def _stream_id_key(msg_id: str) -> tuple[int, int]:
    """Order Redis stream ids numerically (``"<ms>-<seq>"``)."""
    ms, _, seq = msg_id.partition("-")
    try:
        return int(ms), int(seq or 0)
    except ValueError:
        return (0, 0)


def _is_nogroup(exc: BaseException) -> bool:
    """True for Redis' ``NOGROUP`` reply (missing stream key or group).

    Matched on the message rather than ``redis.ResponseError`` so the kernel
    transport stays free of a concrete-client import.
    """
    return str(exc).startswith("NOGROUP")


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    msg_id: str
    consumer: str
    idle_ms: int
    times: int


def _as_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


class _StreamsTransportBase(MessageTransport):
    """Construction, key namespacing and single-entry reads.

    The bottom layer of the Redis Streams transport (``RedisStreamsTransport``).
    """

    #: Hash holding the subscription inventory (``<stream>|<group>`` -> json).
    INVENTORY_KEY = "sandloom:consumers"
    #: Throttle for the age-based ``XTRIM MINID`` per stream (seconds).
    TRIM_INTERVAL_SECONDS = 60.0

    def __init__(
        self,
        *,
        client_factory: Callable[[], Awaitable[Any]] | None = None,
        max_deliveries: int = _DEFAULT_MAX_DELIVERIES,
        min_idle_ms: int = _DEFAULT_MIN_IDLE_MS,
        retry_base_ms: int = _DEFAULT_RETRY_BASE_MS,
        retry_max_ms: int = _DEFAULT_RETRY_MAX_MS,
        reclaim_interval_seconds: float = _RECLAIM_INTERVAL_SECONDS,
        maxlen: int | None = None,
        retention_seconds: int | None = None,
        prefix: str | None = None,
        trim_interval_seconds: float | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._max_deliveries = max_deliveries
        self._min_idle_ms = min_idle_ms
        self._retry_base_ms = retry_base_ms
        self._retry_max_ms = retry_max_ms
        self._reclaim_interval = reclaim_interval_seconds
        self._last_reclaim: dict[tuple[str, str], float] = {}
        self._reclaim_cursors: dict[tuple[str, str], str] = {}
        self._pending_cursors: dict[tuple[str, str, str], str] = {}
        # (stream, group) -> consumer -> ids this transport put in that
        # consumer's PEL (XREADGROUP, XCLAIM, XAUTOCLAIM) and has not seen
        # acked. A superset of the consumer's PEL while the consumer is in
        # ``_pel_known``: an exception in ``consume`` (a reply lost after
        # Redis applied the command) drops it from there, and the next
        # consume scans. Lets an idle / fully-acked consumer skip XPENDING.
        self._outstanding: dict[tuple[str, str], dict[str, set[str]]] = {}
        self._pel_known: set[tuple[str, str, str]] = set()
        self._last_pending_scan: dict[tuple[str, str, str], float] = {}
        # (stream, group) -> (last delivered stream id, its envelope id or
        # None): where a group lost to NOGROUP is recreated.
        self._last_delivered: dict[tuple[str, str], tuple[str, str | None]] = {}
        self._groups: set[tuple[str, str]] = set()
        self._maxlen = int(
            maxlen
            if maxlen is not None
            else getattr(settings, "event_stream_maxlen", 0) or 0
        )
        self._retention_seconds = int(
            retention_seconds
            if retention_seconds is not None
            else getattr(settings, "event_stream_retention_seconds", 0) or 0
        )
        self._prefix = (
            prefix
            if prefix is not None
            else str(getattr(settings, "event_stream_prefix", "") or "")
        )
        self._trim_interval = (
            trim_interval_seconds
            if trim_interval_seconds is not None
            else self.TRIM_INTERVAL_SECONDS
        )
        self._last_trim: dict[str, float] = {}

    async def _client(self) -> Any:
        if self._client_factory is None:
            # The event-bus driver always injects a factory (a named instance's
            # own client, or the shared default for the ``default`` instance).
            # A transport built without one cannot do I/O — and core must not
            # reach into the driver layer to paper over missing wiring.
            raise RuntimeError(
                "RedisStreamsTransport has no client factory; it must be "
                "constructed by the event-bus driver, which injects one"
            )
        return await self._client_factory()

    @staticmethod
    def dlq_stream(stream: str) -> str:
        return f"{stream}.dlq"

    @property
    def prefix(self) -> str:
        return self._prefix

    def key(self, stream: str) -> str:
        """The Redis key of logical ``stream`` (``EVENT_STREAM_PREFIX`` applied)."""
        return f"{self._prefix}{stream}"

    def _dlq_key(self, stream: str) -> str:
        return self.key(self.dlq_stream(stream))

    def _owner_key(self, stream: str, group: str) -> str:
        return f"{self._prefix}sandloom:group-owner:{stream}:{group}"

    def _retry_delay(self, deliveries: int) -> int:
        return retry_delay_ms(
            deliveries, base_ms=self._retry_base_ms, max_ms=self._retry_max_ms
        )

    # -- groups -------------------------------------------------------------
    async def _xinfo_groups(self, client: Any, stream: str) -> list[dict[str, Any]]:
        try:
            raw = await client.xinfo_groups(self.key(stream))
        except Exception:  # noqa: BLE001 - no such key / unsupported: no groups
            return []
        groups: list[dict[str, Any]] = []
        for item in raw or []:
            if isinstance(item, Mapping):
                groups.append({str(k): v for k, v in item.items()})  # noqa: PERF401 - cold path (startup / diagnostics)
        return groups

    async def _fetch_raw(self, client: Any, stream: str, msg_id: str) -> str | None:
        """Return the ``data`` field of a single stream entry, or None if gone."""
        try:
            entries = await client.xrange(
                self.key(stream), min=msg_id, max=msg_id, count=1
            )
        except Exception:  # noqa: BLE001 - trimmed/unsupported: treat as gone
            return None
        for _mid, fields in entries or []:
            if isinstance(fields, dict):
                data = fields.get("data")
                return None if data is None else str(data)
        return None
