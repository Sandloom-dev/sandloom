"""Redis Streams transport: state, key layout and raw entry access."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from functools import wraps
from types import ModuleType
from typing import Any, Concatenate, ParamSpec, TypeVar

from sandloom.driverkit import (
    InfraError,
    InfraProvider,
    Retention,
)
from sandloom.driverkit._builtin_streams import RECLAIM_INTERVAL_SECONDS
from sandloom.driverkit.eventbus_support import (
    DEFAULT_EVENT_STREAM_RETENTION_SECONDS,
    DEFAULT_MAX_DELIVERIES,
    DEFAULT_MIN_IDLE_MS,
    DEFAULT_RETRY_BASE_MS,
    DEFAULT_RETRY_MAX_MS,
    TransportDefaults,
    retry_delay_ms,
)
from sandloom_redis._errors import map_redis_error

logger = logging.getLogger("sandloom.core.eventing.transport")

#: The ``streams`` driver's retention options when a configuration leaves them
#: out: entries older than 7 days are trimmed, no length bound (``0``: none).
DEFAULT_RETENTION_SECONDS = DEFAULT_EVENT_STREAM_RETENTION_SECONDS
DEFAULT_MAX_LENGTH = 0


_P = ParamSpec("_P")
_R = TypeVar("_R")
_S = TypeVar("_S", bound="_StreamsTransportBase")

#: The capability this transport serves (what an ``InfraError`` names).
_CAPABILITY = "eventbus"


def _redis_exceptions() -> ModuleType:
    """``redis.exceptions``, imported when the transport runs: the module
    imports without ``redis`` (the kernel does not require it)."""
    return importlib.import_module("redis.exceptions")


def _backend_errors() -> tuple[type[BaseException], ...]:
    """What a Redis backend raises: the client's own errors, a socket's
    ``OSError`` and a timeout."""
    return (_redis_exceptions().RedisError, OSError, TimeoutError)


#: The replies that mean "nothing there", by their text (the client parses the
#: code away from some replies: ``ERR no such key`` reads ``no such key``).
REPLY_NOGROUP = "NOGROUP"  # no such stream key or consumer group
REPLY_WRONGTYPE = "WRONGTYPE"  # the key holds no stream (another type)
REPLY_NO_SUCH_KEY = "no such key"
REPLY_UNKNOWN_COMMAND = "unknown command"  # a Redis-compatible store without it


def _is_absent(exc: BaseException, *replies: str) -> bool:
    """True when ``exc`` is one of the ``replies`` a read can answer as "absent".

    An allow-list, never a deny-list: a reply a call site did not name is a
    server in trouble (``MASTERDOWN``, ``BUSY``, ``LOADING``, ``READONLY``,
    ``CLUSTERDOWN``, ``MOVED``, ``NOPERM``, ``OOM``, a script or ``EXEC``
    failure, a module error ...) and must surface mapped, never read as an
    empty answer. Each site names what it can truly answer as absent."""
    if type(exc) is not _redis_exceptions().ResponseError:
        return False  # a subclass is a classified failure (permission, OOM ...)
    reply = str(exc).removeprefix("ERR ")
    return reply.startswith(replies)


def _reraise_unless_absent(exc: BaseException, *replies: str) -> None:
    """Return when ``exc`` is an absence (:func:`_is_absent`), else raise it."""
    if not _is_absent(exc, *replies):
        raise exc


def _mapped(
    func: Callable[Concatenate[_S, _P], Coroutine[Any, Any, _R]],
) -> Callable[Concatenate[_S, _P], Coroutine[Any, Any, _R]]:
    """The driver boundary of one public operation: a raw backend error leaves
    it as ``Unavailable`` / ``Timeout`` / ``PermissionDenied``, never as the
    redis client's own exception (it stays as ``__cause__``). The happy path
    costs one frame; the error classes are resolved only when one is raised."""

    @wraps(func)
    async def run(self: _S, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await func(self, *args, **kwargs)
        except InfraError:
            raise
        except _backend_errors() as exc:
            # Chained, not hidden: the kernel relay tells an outage from a
            # poison event by walking this chain (``is_broker_unavailable``).
            raise self._failure(func.__name__, exc) from exc

    return run


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


class _StreamsTransportBase(TransportDefaults):
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
        max_deliveries: int = DEFAULT_MAX_DELIVERIES,
        min_idle_ms: int = DEFAULT_MIN_IDLE_MS,
        retry_base_ms: int = DEFAULT_RETRY_BASE_MS,
        retry_max_ms: int = DEFAULT_RETRY_MAX_MS,
        reclaim_interval_seconds: float = RECLAIM_INTERVAL_SECONDS,
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
        # Retention is the transport's own option (the ``streams`` driver's
        # ``max_length`` / ``retention_seconds``), never a kernel setting.
        self._maxlen = int(DEFAULT_MAX_LENGTH if maxlen is None else maxlen)
        self._retention_seconds = int(
            DEFAULT_RETENTION_SECONDS
            if retention_seconds is None
            else retention_seconds
        )
        # The key namespace is the transport's own option (the ``streams``
        # driver's ``prefix``; the kernel fills it from EVENT_STREAM_PREFIX),
        # never a kernel setting read here.
        self._prefix = prefix or ""
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

    def _failure(self, operation: str, exc: BaseException) -> InfraError:
        """The ``InfraError`` for a backend failure: its type, never its text
        (which can carry the broker URL)."""
        # Not transient: DataError (a value the client cannot encode), WRONGTYPE
        # and any other plain reply are not retryable, so they must not read as
        # an outage (``Unavailable`` is retryable and the relay would park the
        # row for ever). Chained ``from exc`` by ``_mapped``: the relay walks the
        # chain of a publish error (``is_broker_unavailable``).
        instance = self.name if isinstance(self, InfraProvider) else None
        return map_redis_error(
            exc,
            capability=_CAPABILITY,
            instance=instance,
            what=f"eventbus streams {operation}",
        )

    def _cause(self, operation: str, exc: Exception) -> Exception:
        """The mapped error of a backend failure caught inside an operation
        (so a ``PartialPublish`` built there follows its ``retryable``), with
        the raw error as its ``__cause__``; any other exception is a bug and
        stays as it is. The caller chains ``PartialPublish`` from THIS error,
        so the relay judges the batch path like the per-row path."""
        if isinstance(exc, _backend_errors()):
            mapped = self._failure(operation, exc)
            mapped.__cause__ = exc
            return mapped
        return exc

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

    def retention(self) -> Retention:
        """What this transport keeps of a stream (``None``: unbounded)."""
        return Retention(
            max_age_seconds=self._retention_seconds or None,
            max_length=self._maxlen or None,
        )

    def _retry_delay(self, deliveries: int) -> int:
        return retry_delay_ms(
            deliveries, base_ms=self._retry_base_ms, max_ms=self._retry_max_ms
        )

    # -- groups -------------------------------------------------------------
    async def _xinfo_groups(self, client: Any, stream: str) -> list[dict[str, Any]]:
        try:
            raw = await client.xinfo_groups(self.key(stream))
        except _backend_errors() as exc:
            # No such key, or a store without XINFO: no groups. Nothing else.
            if not _is_absent(exc, REPLY_NO_SUCH_KEY, REPLY_UNKNOWN_COMMAND):
                raise
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
        except _backend_errors() as exc:
            # XRANGE of a missing key answers an empty list: the only reply
            # that says "gone" is a key that holds no stream any more.
            if not _is_absent(exc, REPLY_WRONGTYPE):
                raise
            return None
        for _mid, fields in entries or []:
            if isinstance(fields, dict):
                data = fields.get("data")
                return None if data is None else str(data)
        return None
