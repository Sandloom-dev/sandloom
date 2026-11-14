"""The Redis ``broadcast`` driver (``redis``): Pub/Sub fan-out to every process
that reaches the same Redis (it declares ``shared`` and ``pattern_subscribe``).

- A subscription owns one connection. It confirms its ``SUBSCRIBE`` before
  ``subscribe`` returns, so a message published after that reaches it. It reads
  only when iterated: a subscriber that does not read lets the messages wait in
  the socket and in Redis's output buffer, and if Redis cuts it off for that
  (``client-output-buffer-limit pubsub``) the next read raises ``Unavailable``
  after the messages that arrived.
- A lost connection ends the subscription with ``Unavailable``, and the driver
  never reconnects behind an open iterator (the client is built without
  retries): the subscriber resubscribes and treats the interval as a gap.
- Pub/Sub is not scoped to a Redis database, so the ``prefix`` option puts
  every channel of an instance under a namespace.

A built-in driver imports only ``sandloom.driverkit`` (ADR-0016), so the argument
checks below are this driver's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from typing import Any, ClassVar, NamedTuple, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from sandloom.driverkit import (
    BroadcastMessage,
    BroadcastProvider,
    HealthResult,
    InfraError,
    InvalidArgument,
    PermissionDenied,
    Subscription,
    Timeout,
    TooLarge,
    Unavailable,
)

_CAPABILITY = "broadcast"
_PATTERN_SUFFIX = ".*"
_MIN_PAYLOAD_LIMIT_BYTES = 1024 * 1024

#: How long one read of a subscription waits before it looks at ``aclose``.
_POLL_SECONDS = 1.0

_GLOB = re.compile(r"([\\*?\[\]])")
_T = TypeVar("_T")


class RedisBroadcastOptions(BaseModel):
    """Options of the ``redis`` driver. The kernel fills ``url`` and the client
    bounds left out from the ``CACHE_*`` settings."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = None
    #: The payload limit (every driver accepts at least 1 MiB).
    max_payload_bytes: int = Field(default=8 * 1024 * 1024, ge=_MIN_PAYLOAD_LIMIT_BYTES)
    #: Put every channel of this instance under ``prefix`` (Pub/Sub ignores the
    #: Redis database index).
    prefix: str = ""
    #: The bound of ``publish`` and of confirming a ``subscribe``.
    socket_timeout_seconds: float = Field(default=5.0, gt=0)
    socket_connect_timeout_seconds: float = Field(default=0.5, gt=0)
    health_check_interval_seconds: int = Field(default=30, ge=0)


class _Channels(NamedTuple):
    """A subscription's channels: exact names and the prefixes of its patterns
    (``a.*`` is the prefix ``a.``)."""

    exact: tuple[str, ...]
    prefixes: tuple[str, ...]


def _redis_asyncio() -> Any:
    """redis-py, imported when the driver first needs it (the entry point stays
    importable without the dependency)."""
    return importlib.import_module("redis.asyncio")


def _redis_errors() -> Any:
    return importlib.import_module("redis.exceptions")


def _no_retry() -> Any:
    """A client retry policy that never retries: a subscription must not
    reconnect silently behind its iterator."""
    retry = importlib.import_module("redis.retry")
    backoff = importlib.import_module("redis.backoff")
    return retry.Retry(backoff.NoBackoff(), 0)


def _invalid(instance: str, what: str) -> InvalidArgument:
    return InvalidArgument(what, capability=_CAPABILITY, instance=instance)


def _check_publish(
    instance: str, channel: object, payload: object, limit: int
) -> tuple[str, bytes]:
    if not isinstance(channel, str) or not channel:
        raise _invalid(instance, "a channel is a non-empty string")
    if channel.endswith(_PATTERN_SUFFIX):
        raise _invalid(instance, "a message is not published to a pattern channel")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise _invalid(instance, "a payload is bytes")
    data = bytes(payload)
    if len(data) > limit:
        raise TooLarge(
            f"a payload is at most {limit} bytes",
            capability=_CAPABILITY,
            instance=instance,
        )
    return channel, data


def _check_subscribe(instance: str, channels: Sequence[str]) -> _Channels:
    if isinstance(channels, str) or not channels:
        raise _invalid(instance, "subscribe takes a non-empty sequence of channels")
    exact: list[str] = []
    prefixes: list[str] = []
    for channel in channels:
        if not isinstance(channel, str) or not channel:
            raise _invalid(instance, "a channel is a non-empty string")
        if channel.endswith(_PATTERN_SUFFIX):
            prefix = channel[:-1]  # "a.*" -> "a."
            if prefix not in prefixes:
                prefixes.append(prefix)
        elif channel not in exact:
            exact.append(channel)
    return _Channels(tuple(exact), tuple(prefixes))


def _escape(text: str) -> str:
    """``text`` as a literal inside a Redis glob pattern."""
    return _GLOB.sub(r"\\\1", text)


def _text(raw: object) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "surrogateescape")
    return str(raw)


def _map(exc: BaseException, instance: str, *, during: str) -> InfraError:
    """The ``InfraError`` of a Redis or socket failure: the type is named, the
    text (which can carry a host or a credential) never is."""
    errors = _redis_errors()
    kind = type(exc).__name__
    if isinstance(exc, errors.AuthenticationError) or (
        isinstance(exc, errors.ResponseError)
        and str(exc).split(" ", 1)[0] in {"NOAUTH", "WRONGPASS", "NOPERM"}
    ):
        return PermissionDenied(
            f"redis refused the credentials ({kind})",
            capability=_CAPABILITY,
            instance=instance,
        )
    if isinstance(exc, (TimeoutError, errors.TimeoutError)):
        return Timeout(
            f"redis did not answer in time during {during} ({kind})",
            capability=_CAPABILITY,
            instance=instance,
        )
    if isinstance(exc, ValueError):
        return InvalidArgument(
            f"the redis url option is not usable ({kind})",
            capability=_CAPABILITY,
            instance=instance,
        )
    return Unavailable(
        f"redis is unavailable during {during} ({kind})",
        capability=_CAPABILITY,
        instance=instance,
    )


class _RedisSubscription:
    def __init__(
        self,
        provider: RedisBroadcastProvider,
        client: Any,
        pubsub: Any,
        channels: _Channels,
    ) -> None:
        self._provider = provider
        self._client = client
        self._pubsub = pubsub
        self._channels = channels
        prefix = provider._options.prefix
        self._prefix = prefix
        self._patterns = [(p, _escape(prefix + p) + "*") for p in channels.prefixes]
        self._pending: deque[BroadcastMessage] = deque()
        self._closed = False
        self._failed = False
        self._reader: asyncio.Future[Any] | None = None

    async def start(self) -> None:
        """``SUBSCRIBE`` and wait for Redis to confirm every channel."""
        options = self._provider._options
        names = [self._prefix + c for c in self._channels.exact]
        patterns = [redis_pattern for _, redis_pattern in self._patterns]
        expected = len(names) + len(patterns)
        try:
            if names:
                await self._pubsub.subscribe(*names)
            if patterns:
                await self._pubsub.psubscribe(*patterns)
            confirmed = 0
            deadline = time.monotonic() + options.socket_timeout_seconds
            while confirmed < expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                raw = await self._pubsub.get_message(
                    ignore_subscribe_messages=False, timeout=remaining
                )
                if raw is None:
                    continue
                kind = _text(raw.get("type"))
                if kind in ("subscribe", "psubscribe"):
                    confirmed += 1
                elif (message := self._convert(raw)) is not None:
                    self._pending.append(message)
        except BaseException as exc:
            await self._release()
            if isinstance(exc, Exception):
                raise _map(exc, self._provider.name, during="subscribe") from exc
            raise

    def _convert(self, raw: Mapping[str, Any]) -> BroadcastMessage | None:
        """The message of one ``message`` / ``pmessage`` reply; ``None`` for
        anything else, and for the second copy Redis sends of a message that
        matches two of this subscription's channels."""
        kind = _text(raw.get("type"))
        if kind not in ("message", "pmessage"):
            return None
        channel = _text(raw.get("channel"))
        if not channel.startswith(self._prefix):
            return None
        channel = channel[len(self._prefix) :]
        if kind == "pmessage":
            if channel in self._channels.exact:
                return None  # delivered as a plain message
            first = next(
                (redis for p, redis in self._patterns if channel.startswith(p)), None
            )
            if first is None or _text(raw.get("pattern")) != first:
                return None  # delivered through the first matching pattern
        data = raw.get("data")
        if isinstance(data, str):
            data = data.encode("utf-8", "surrogateescape")
        if not isinstance(data, bytes):
            return None
        return BroadcastMessage(channel, data)

    # -- subscriber side ---------------------------------------------------
    def __aiter__(self) -> AsyncIterator[BroadcastMessage]:
        return self

    def _unavailable(self, why: str) -> Unavailable:
        return Unavailable(
            f"the subscription {why} and ended; resubscribe",
            capability=_CAPABILITY,
            instance=self._provider.name,
        )

    async def __anext__(self) -> BroadcastMessage:
        while True:
            if self._closed:
                raise StopAsyncIteration
            if self._pending:
                return self._pending.popleft()
            if self._failed:
                raise self._unavailable("lost its connection")
            raw = await self._read()
            message = self._convert(raw) if raw is not None else None
            if message is not None:
                return message

    async def _read(self) -> Mapping[str, Any] | None:
        """One bounded read of the connection (``None``: nothing yet, or closed)."""
        if self._reader is None:
            self._reader = asyncio.ensure_future(
                self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=_POLL_SECONDS
                )
            )
        # ``wait`` leaves the read running when this call is cancelled (a
        # ``wait_for`` timeout), so the next call takes its result: a message is
        # never lost to a cancelled read.
        reader = self._reader
        await asyncio.wait({reader})
        if self._reader is reader:
            self._reader = None
        try:
            return reader.result()  # type: ignore[no-any-return]
        except asyncio.CancelledError:
            return None  # closed while waiting: the caller sees ``_closed``
        except Exception as exc:
            if self._closed:
                return None
            self._failed = True
            await self._release()
            raise self._unavailable(
                f"lost its connection ({type(exc).__name__})"
            ) from exc

    async def aclose(self) -> None:
        self._closed = True
        self._pending.clear()
        await self._release()

    async def _release(self) -> None:
        """Close the connection, once, whatever state it is in."""
        pubsub, client = self._pubsub, self._client
        self._provider._subscriptions.discard(self)
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
        with contextlib.suppress(Exception):
            await pubsub.aclose()
        with contextlib.suppress(Exception):
            await client.aclose()


class RedisBroadcastProvider(BroadcastProvider):
    """``broadcast`` / ``redis``: Pub/Sub on one Redis."""

    contract_version = 1
    supported_features = frozenset({"pattern_subscribe", "shared"})
    options_schema: ClassVar[type[RedisBroadcastOptions]] = RedisBroadcastOptions
    required_imports = ("redis",)
    install_hint = "pip install 'sandloom[redis]'"

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._options = RedisBroadcastOptions.model_validate(options)
        self._publisher: Any | None = None
        self._publisher_loop: asyncio.AbstractEventLoop | None = None
        self._subscriptions: set[_RedisSubscription] = set()
        self._closed = False

    @classmethod
    def test_isolation(
        cls, options: Mapping[str, Any], worker_id: str
    ) -> Mapping[str, Any]:
        """One pytest-xdist worker per channel prefix: Pub/Sub ignores the
        database index of the URL."""
        return {**options, "prefix": f"{options.get('prefix', '')}w{worker_id}:"}

    # -- clients -----------------------------------------------------------
    def _new_client(self, *, socket_timeout: float | None) -> Any:
        options = self._options
        if not options.url:
            raise _invalid(
                self.name, "the redis broadcast instance has no 'url' option"
            )
        redis = _redis_asyncio()
        try:
            return redis.from_url(
                options.url,
                decode_responses=False,
                socket_timeout=socket_timeout,
                socket_connect_timeout=float(options.socket_connect_timeout_seconds),
                socket_keepalive=True,
                health_check_interval=int(options.health_check_interval_seconds),
                # A subscription never reconnects silently behind its iterator.
                retry=_no_retry(),
            )
        except Exception as exc:
            raise _map(exc, self.name, during="connect") from exc

    async def _publisher_client(self) -> Any:
        """The publishing client, built per event loop (a redis.asyncio client
        and its pool belong to the loop they first ran on)."""
        self._ensure_open()
        loop = asyncio.get_running_loop()
        if self._publisher is not None and self._publisher_loop is not loop:
            stale, self._publisher = self._publisher, None
            with contextlib.suppress(Exception):
                await stale.aclose()
        if self._publisher is None:
            self._publisher = self._new_client(
                socket_timeout=self._options.socket_timeout_seconds
            )
            self._publisher_loop = loop
        return self._publisher

    def _ensure_open(self) -> None:
        if self._closed:
            raise Unavailable(
                "the broadcast instance is closed",
                capability=_CAPABILITY,
                instance=self.name,
            )

    async def _bounded(self, call: Awaitable[_T], *, during: str) -> _T:
        """``call`` within the instance's timeout, its failures mapped."""
        try:
            return await asyncio.wait_for(
                call, timeout=self._options.socket_timeout_seconds
            )
        except InfraError:
            raise
        except Exception as exc:
            raise _map(exc, self.name, during=during) from exc

    # -- lifecycle ---------------------------------------------------------
    async def aopen(self) -> None:
        self._closed = False
        client = await self._publisher_client()
        await self._bounded(client.ping(), during="open")

    async def health(self) -> HealthResult:
        try:
            client = await self._publisher_client()
            await self._bounded(client.ping(), during="health")
        except InfraError as exc:
            # The type only: a redis error text names the host (spec 2.4).
            return HealthResult(status="fail", details={"error": type(exc).__name__})
        return HealthResult(status="ok")

    async def isolation_identity(self) -> str | None:
        """``redis:<run_id>`` from ``INFO server`` (needs that ACL permission)."""
        client = await self._publisher_client()
        info = await self._bounded(client.info(section="server"), during="attest")
        run_id = info.get("run_id") or info.get(b"run_id")
        if isinstance(run_id, bytes):
            run_id = run_id.decode("ascii", "replace")
        if not isinstance(run_id, str) or not run_id:
            raise Unavailable(
                "redis INFO server returned no process identity",
                capability=_CAPABILITY,
                instance=self.name,
            )
        return f"redis:{run_id}"

    async def aclose(self) -> None:
        self._closed = True
        for subscription in tuple(self._subscriptions):
            await subscription.aclose()
        publisher, self._publisher = self._publisher, None
        if publisher is not None:
            with contextlib.suppress(Exception):
                await publisher.aclose()

    # -- operations --------------------------------------------------------
    async def publish(self, channel: str, payload: bytes) -> None:
        channel, data = _check_publish(
            self.name, channel, payload, self._options.max_payload_bytes
        )
        client = await self._publisher_client()
        await self._bounded(
            client.publish(self._options.prefix + channel, data), during="publish"
        )

    async def subscribe(self, channels: Sequence[str]) -> Subscription:
        parsed = _check_subscribe(self.name, channels)
        self._ensure_open()
        client = self._new_client(socket_timeout=None)
        subscription = _RedisSubscription(self, client, client.pubsub(), parsed)
        self._subscriptions.add(subscription)
        await subscription.start()
        return subscription


__all__ = ["RedisBroadcastOptions", "RedisBroadcastProvider"]
