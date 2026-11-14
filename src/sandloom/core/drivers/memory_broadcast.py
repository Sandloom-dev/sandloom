"""The in-memory ``broadcast`` driver (``memory``): tests, demos and
single-process apps.

Messages reach the subscriptions of this process only (it does not declare
``shared``). Each subscription holds a bounded buffer: when a subscriber that
does not read lets it fill, the subscription ends with ``Unavailable`` after
the messages it already holds, never a silent skip (spec section 6). Providers
of one ``namespace`` option share one hub, so two instances of one store reach
each other, as two Redis connections do.

A built-in driver imports only ``sandloom.driverkit`` (ADR-0016), so the argument
checks below are this driver's own.
"""

from __future__ import annotations

import asyncio
import os
import uuid
import weakref
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from sandloom.driverkit import (
    BroadcastMessage,
    BroadcastProvider,
    InvalidArgument,
    Subscription,
    TooLarge,
    Unavailable,
)

_CAPABILITY = "broadcast"
_PATTERN_SUFFIX = ".*"
_MIN_PAYLOAD_LIMIT_BYTES = 1024 * 1024

#: Names this process in the isolation identity.
_PROCESS = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


class MemoryBroadcastOptions(BaseModel):
    """Options of the ``memory`` driver."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    #: The payload limit (every driver accepts at least 1 MiB).
    max_payload_bytes: int = Field(default=8 * 1024 * 1024, ge=_MIN_PAYLOAD_LIMIT_BYTES)
    #: Messages a subscription holds for a subscriber that does not read.
    buffer_messages: int = Field(default=16_384, ge=1)
    #: Providers with the same namespace share one hub.
    namespace: str = "default"


class _Channels(NamedTuple):
    """A subscription's channels: exact names and the prefixes of its patterns
    (``a.*`` is the prefix ``a.``)."""

    exact: tuple[str, ...]
    prefixes: tuple[str, ...]

    def matches(self, channel: str) -> bool:
        return channel in self.exact or any(
            channel.startswith(prefix) for prefix in self.prefixes
        )


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


class _Hub:
    """The live subscriptions of one namespace."""

    def __init__(self) -> None:
        self.subscriptions: set[_MemorySubscription] = set()

    def publish(self, message: BroadcastMessage) -> None:
        for subscription in tuple(self.subscriptions):
            if subscription.channels.matches(message.channel):
                subscription.deliver(message)


_HUBS: dict[str, _Hub] = {}


class _MemorySubscription:
    def __init__(self, hub: _Hub, channels: _Channels, capacity: int) -> None:
        self.channels = channels
        self._hub = hub
        self._capacity = capacity
        self._buffer: deque[BroadcastMessage] = deque()
        self._loop = asyncio.get_running_loop()
        self._waiter: asyncio.Future[None] | None = None
        self._overflowed = False
        self._closed = False
        hub.subscriptions.add(self)

    # -- publisher side ----------------------------------------------------
    def deliver(self, message: BroadcastMessage) -> None:
        """Hand one message over, on the subscriber's own loop."""
        if self._closed or self._overflowed:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - publish is a coroutine
            running = None
        if running is self._loop:
            self._append(message)
            return
        try:
            self._loop.call_soon_threadsafe(self._append, message)
        except RuntimeError:  # the subscriber's loop is gone
            self._hub.subscriptions.discard(self)

    def _append(self, message: BroadcastMessage) -> None:
        if self._closed or self._overflowed:
            return
        if len(self._buffer) >= self._capacity:
            self._overflowed = True
            self._hub.subscriptions.discard(self)
        else:
            self._buffer.append(message)
        self._wake()

    def _wake(self) -> None:
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    # -- subscriber side ---------------------------------------------------
    def __aiter__(self) -> AsyncIterator[BroadcastMessage]:
        return self

    async def __anext__(self) -> BroadcastMessage:
        while True:
            if self._closed:
                raise StopAsyncIteration
            if self._buffer:
                return self._buffer.popleft()
            if self._overflowed:
                raise Unavailable(
                    "the subscription fell behind its buffer and ended",
                    capability=_CAPABILITY,
                )
            self._waiter = self._loop.create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None

    async def aclose(self) -> None:
        self._closed = True
        self._buffer.clear()
        self._hub.subscriptions.discard(self)
        if asyncio.get_running_loop() is self._loop:
            self._wake()
        else:  # pragma: no cover - closed from another loop
            self._loop.call_soon_threadsafe(self._wake)


class MemoryBroadcastProvider(BroadcastProvider):
    """``broadcast`` / ``memory``: fan-out inside this process."""

    contract_version = 1
    supported_features = frozenset({"pattern_subscribe"})
    development_only = True
    options_schema: ClassVar[type[MemoryBroadcastOptions]] = MemoryBroadcastOptions

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._options = MemoryBroadcastOptions.model_validate(options)
        self._hub = _HUBS.setdefault(self._options.namespace, _Hub())
        self._subscriptions: weakref.WeakSet[_MemorySubscription] = weakref.WeakSet()
        self._closed = False

    async def publish(self, channel: str, payload: bytes) -> None:
        channel, data = _check_publish(
            self.name, channel, payload, self._options.max_payload_bytes
        )
        self._ensure_open()
        self._hub.publish(BroadcastMessage(channel, data))

    async def subscribe(self, channels: Sequence[str]) -> Subscription:
        parsed = _check_subscribe(self.name, channels)
        self._ensure_open()
        subscription = _MemorySubscription(
            self._hub, parsed, self._options.buffer_messages
        )
        self._subscriptions.add(subscription)
        return subscription

    async def isolation_identity(self) -> str | None:
        return f"memory:{_PROCESS}-{self._options.namespace}"

    async def aclose(self) -> None:
        self._closed = True
        subscriptions = tuple(self._subscriptions)
        self._subscriptions.clear()
        for subscription in subscriptions:
            await subscription.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise Unavailable(
                "the broadcast instance is closed",
                capability=_CAPABILITY,
                instance=self.name,
            )


__all__ = ["MemoryBroadcastOptions", "MemoryBroadcastProvider"]
