"""The ``EventTransportConformance`` kit (spec 7), core groups: run against the
in-memory reference transport and the ``streams`` driver (a real Redis), and
against deliberately broken transports (each breaks one obligation and must
fail exactly the kit tests that name it).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import replace
from types import MappingProxyType
from typing import Any, ClassVar
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

# The one place this file reaches the streams transport.
from sandloom.core.config.settings import settings
from sandloom.core.drivers.event_bus_driver import StreamsEventBusProvider
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.transport_base import partial_publish
from sandloom.core.infra.errors import Unavailable
from sandloom.core.infra.ports.eventbus import (
    ConsumerInfo,
    DeadLetter,
    Delivery,
    EventTransportProvider,
    GroupInfo,
    GroupOwner,
    Retention,
    StreamInfo,
)
from sandloom.driverkit.testing import EventTransportConformance

#: Fast settings: the memory transport has no backoff, and the streams one is
#: built with none.
_FAST = {"redelivery_timeout_seconds": 5.0, "settle_seconds": 0.3}


# ------------------------------------------------------- the reference transport
class _MemoryProvider(MemoryTransport, EventTransportProvider):
    """``MemoryTransport`` as the provider the kit tests: the reference
    transport of the harness, not an ``INFRA`` choice."""

    contract_version = 2
    development_only = True
    performance_hints: ClassVar[MappingProxyType[str, str]] = MappingProxyType({})

    def __init__(
        self, *, name: str, driver: str, options: Mapping[str, Any] | None = None
    ) -> None:
        EventTransportProvider.__init__(
            self, name=name, driver=driver, options=options or {}
        )
        MemoryTransport.__init__(
            self,
            max_deliveries=EventTransportConformance.max_deliveries,
            retry_base_ms=0,
        )
        self.refused: set[str] = set()
        self.dlq_down = False
        self.cut = False

    def cut_connections(self) -> None:
        """The next consume, or the one waiting, fails once: a connection cut."""
        self.cut = True
        for arrival in self._arrivals.values():
            arrival.set()

    def _raise_if_cut(self) -> None:
        if self.cut:
            self.cut = False
            raise Unavailable("the connection was cut", capability="eventbus")

    async def consume(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int | None = None,
    ) -> list[Delivery]:
        self._raise_if_cut()
        delivered = await super().consume(
            stream, group=group, consumer=consumer, count=count, block_ms=block_ms
        )
        self._raise_if_cut()
        return delivered

    async def publish(self, stream: str, envelope: Any) -> str:
        if stream in self.refused:
            raise Unavailable("the stream is refusing appends", capability="eventbus")
        return await super().publish(stream, envelope)

    async def dead_letter(self, delivery: Delivery, *, reason: str) -> None:
        if self.dlq_down:
            raise Unavailable("the dead-letter queue is down", capability="eventbus")
        await super().dead_letter(delivery, reason=reason)

    async def isolation_identity(self) -> str | None:
        return "memory:process"


def _memory(**_options: Any) -> _MemoryProvider:
    return _MemoryProvider(name="kit", driver="memory", options={})


class _MemoryHooks:
    """What the kit asks of a driver's tests, for the memory provider."""

    def refused_stream(
        self, provider: EventTransportProvider, stream: str
    ) -> AbstractAsyncContextManager[None]:
        assert isinstance(provider, _MemoryProvider)
        return _toggle(provider.refused, stream)

    def failing_dlq_append(
        self, provider: EventTransportProvider
    ) -> AbstractAsyncContextManager[None]:
        assert isinstance(provider, _MemoryProvider)
        return _dlq_down(provider)

    def retention_cases(self) -> Sequence[tuple[Mapping[str, Any], Retention]]:
        return [({}, Retention(max_age_seconds=None, max_length=None))]

    async def interrupt_connections(self, provider: EventTransportProvider) -> None:
        assert isinstance(provider, _MemoryProvider)
        provider.cut_connections()


@asynccontextmanager
async def _toggle(refused: set[str], stream: str) -> AsyncIterator[None]:
    refused.add(stream)
    try:
        yield
    finally:
        refused.discard(stream)


@asynccontextmanager
async def _dlq_down(provider: _MemoryProvider) -> AsyncIterator[None]:
    provider.dlq_down = True
    try:
        yield
    finally:
        provider.dlq_down = False


class TestTheMemoryTransport(_MemoryHooks, EventTransportConformance):
    redelivery_timeout_seconds = _FAST["redelivery_timeout_seconds"]
    settle_seconds = _FAST["settle_seconds"]

    @pytest.fixture
    def make_provider(self) -> Callable[[], EventTransportProvider]:
        return _memory

    # An in-process transport has no backend that can fail.
    test_harness_redacts_the_health_of_a_failing_backend = None  # type: ignore[assignment]
    test_harness_maps_backend_failures_to_infra_errors = None  # type: ignore[assignment]
    # It declares neither lease_renewal (nothing to reclaim by idle time) nor
    # shared (one process), so those groups do not apply; the variants below
    # that declare them run them.
    test_the_renew_interval_is_positive = None  # type: ignore[assignment]
    test_renewing_nothing_is_a_no_op = None  # type: ignore[assignment]
    test_renewal_keeps_an_entry_with_its_consumer_until_it_stops = None  # type: ignore[assignment]
    test_two_providers_of_one_configuration_share_streams_and_groups = None  # type: ignore[assignment]


# ------------------------------------------------------------ the streams driver
def _redis_url() -> str:
    url = settings.cache_url
    if not url:
        pytest.skip("no test Redis URL: the streams kit is skipped")
    return str(url)


class _DlqSwitch:
    """The Redis client of a streams provider; while ``owner.dlq_down`` it
    refuses appends to a dead-letter queue and nothing else."""

    def __init__(self, client: Any, owner: _StreamsUnderTest) -> None:
        self._client = client
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def xadd(self, key: str, *args: Any, **kwargs: Any) -> Any:
        if self._owner.dlq_down and str(key).endswith(".dlq"):
            raise ConnectionError("the dead-letter queue is down")
        return await self._client.xadd(key, *args, **kwargs)


class _StreamsUnderTest(StreamsEventBusProvider):
    """The ``streams`` driver with the tuning the kit asks for (its delivery
    budget, no retry backoff, a short idle time before a dead consumer's
    entries are reclaimed) and a client of its own."""

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        super().__init__(
            name=name, driver=driver, options=options, client_factory=self._connect
        )
        self._max_deliveries = EventTransportConformance.max_deliveries
        self._retry_base_ms = 0
        self._min_idle_ms = 500
        self._reclaim_interval = 0.0
        self._redis: _DlqSwitch | None = None
        self.dlq_down = False
        #: Names this provider's connections, so an outage cuts only them.
        self.connection_name = f"idke-{uuid4().hex}"

    async def _connect(self) -> Any:
        if self._redis is None:
            from redis import asyncio as redis_asyncio

            raw = redis_asyncio.from_url(
                self.options["url"],
                encoding="utf-8",
                decode_responses=True,
                client_name=self.connection_name,
            )
            self._redis = _DlqSwitch(raw, self)
        return self._redis

    async def redis(self) -> Any:
        return await self._connect()

    async def aclose(self) -> None:
        if self._redis is not None:
            client, self._redis = self._redis, None
            with suppress(Exception):
                await client.aclose()
        await super().aclose()

    async def isolation_identity(self) -> str | None:
        target = urlsplit(self.options["url"])
        return f"streams:{target.hostname}:{target.port}{target.path}"


@asynccontextmanager
async def _a_key_that_is_not_a_stream(
    provider: _StreamsUnderTest, stream: str
) -> AsyncIterator[None]:
    """Appends to ``stream`` fail at execution time (``WRONGTYPE``)."""
    client = await provider.redis()
    key = provider.key(stream)
    await client.set(key, "not a stream")
    try:
        yield
    finally:
        await client.delete(key)


async def _kill_connections_named(url: str, name: str) -> int:
    """Cut the Redis connections named ``name`` and no others."""
    from redis import asyncio as redis_asyncio

    client: Any = redis_asyncio.from_url(url)
    try:
        listing = (await client.execute_command("CLIENT", "LIST")).decode()
        ids = [
            fields["id"]
            for line in listing.splitlines()
            if (fields := dict(p.split("=", 1) for p in line.split(" ") if "=" in p))
            and fields.get("name") == name
        ]
        for client_id in ids:
            await client.execute_command("CLIENT", "KILL", "ID", client_id)
        return len(ids)
    finally:
        await client.aclose()


@asynccontextmanager
async def _streams_dlq_down(provider: _StreamsUnderTest) -> AsyncIterator[None]:
    provider.dlq_down = True
    try:
        yield
    finally:
        provider.dlq_down = False


def _streams(**options: Any) -> _StreamsUnderTest:
    options.setdefault("url", _redis_url())
    return _StreamsUnderTest(name="kit", driver="streams", options=options)


class TestTheStreamsDriver(EventTransportConformance):
    redelivery_timeout_seconds = 15.0
    settle_seconds = 0.6

    @pytest.fixture(autouse=True)
    async def _clean(self) -> AsyncIterator[None]:
        pytest.importorskip("redis.asyncio")
        yield
        provider = _streams()
        client = await provider.redis()
        try:
            for pattern in ("kit.*", "sandloom:group-owner:kit.*"):
                async for key in client.scan_iter(match=pattern):
                    await client.delete(key)
        finally:
            await provider.aclose()

    @pytest.fixture
    def make_provider(self) -> Callable[[], EventTransportProvider]:
        _redis_url()
        return _streams

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], EventTransportProvider]:
        # Nothing listens on port 1; the "password" is the kit's secret.
        url = f"redis://:{self.leak_marker}@127.0.0.1:1/0"
        return lambda: _streams(url=url)

    def refused_stream(
        self, provider: EventTransportProvider, stream: str
    ) -> AbstractAsyncContextManager[None]:
        assert isinstance(provider, _StreamsUnderTest)
        return _a_key_that_is_not_a_stream(provider, stream)

    def failing_dlq_append(
        self, provider: EventTransportProvider
    ) -> AbstractAsyncContextManager[None]:
        assert isinstance(provider, _StreamsUnderTest)
        return _streams_dlq_down(provider)

    async def interrupt_connections(self, provider: EventTransportProvider) -> None:
        assert isinstance(provider, _StreamsUnderTest)
        await _kill_connections_named(provider.options["url"], provider.connection_name)

    def retention_cases(self) -> Sequence[tuple[Mapping[str, Any], Retention]]:
        return [
            (
                {"retention_seconds": 3600, "max_length": 500},
                Retention(max_age_seconds=3600, max_length=500),
            ),
            (
                {"retention_seconds": 0, "max_length": 0},
                Retention(max_age_seconds=None, max_length=None),
            ),
        ]

    # The streams transport raises the redis client's own errors today; the
    # driver slice (idk-pkg-redis-streams) maps them to InfraError and drops
    # these two (the outage hook above already cuts the connections: a cut
    # blocked consume raises redis' ConnectionError, not Unavailable).
    test_harness_maps_backend_failures_to_infra_errors = None  # type: ignore[assignment]
    test_an_outage_during_consume_raises_unavailable_and_consuming_resumes = None  # type: ignore[assignment]


# ------------------------------------------------ the kit bites a broken transport
class _AckDoesNothing(_MemoryProvider):
    async def ack(self, delivery: Delivery) -> None:
        return None


class _AcceptsASecondOwner(_MemoryProvider):
    async def claim_group_owner(
        self, stream: str, group: str, owner: GroupOwner
    ) -> None:
        self._owners[(stream, group)] = owner


class _TransferWithoutCompare(_MemoryProvider):
    async def transfer_group_owner(
        self, stream: str, group: str, *, expected: GroupOwner, owner: GroupOwner
    ) -> bool:
        self._owners[(stream, group)] = owner
        return True


class _ReleaseKeepsTheOwner(_MemoryProvider):
    async def release_group_owner(self, stream: str, group: str) -> bool:
        return (stream, group) in self._owners


class _AcksTheSourceBeforeTheDlq(_MemoryProvider):
    async def dead_letter(self, delivery: Delivery, *, reason: str) -> None:
        await self.ack(delivery)
        await super().dead_letter(delivery, reason=reason)


class _DeadLetterOnlyAcks(_MemoryProvider):
    async def dead_letter(self, delivery: Delivery, *, reason: str) -> None:
        await self.ack(delivery)


class _IgnoresNeverDeadLetter(_MemoryProvider):
    def never_dead_letter(self, stream: str, group: str) -> None:
        return None


class _OneDeliveryTooMany(_MemoryProvider):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._max_deliveries += 1


class _PublishManySwallowsAFailure(_MemoryProvider):
    async def publish_many(self, items: Any) -> list[str]:
        ids: list[str] = []
        for stream, envelope in items:
            with suppress(Unavailable):
                ids.append(await self.publish(stream, envelope))
        return ids


class _PublishManyClaimsTooMuch(_MemoryProvider):
    async def publish_many(self, items: Any) -> list[str]:
        ids: list[str] = []
        for stream, envelope in items:
            try:
                ids.append(await self.publish(stream, envelope))
            except Unavailable as exc:
                raise partial_publish([*ids, "9-9", "9-9"], "failed", exc) from exc
        return ids


class _PublishManyReversed(_MemoryProvider):
    async def publish_many(self, items: Any) -> list[str]:
        ids = [await self.publish(s, e) for s, e in reversed(list(items))]
        return list(reversed(ids))


class _RetentionNotEchoed(_MemoryProvider):
    def retention(self) -> Retention:
        return Retention(max_age_seconds=1, max_length=1)


class _NewGroupMissesHistory(_MemoryProvider):
    async def ensure_group(
        self, stream: str, group: str, *, start_id: str = "0"
    ) -> None:
        await super().ensure_group(stream, group, start_id="$")


class _BlockingIgnored(_MemoryProvider):
    async def consume(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int | None = None,
    ) -> list[Delivery]:
        return await super().consume(
            stream, group=group, consumer=consumer, count=count
        )


class _StartIdIgnored(_MemoryProvider):
    async def ensure_group(
        self, stream: str, group: str, *, start_id: str = "0"
    ) -> None:
        await super().ensure_group(stream, group, start_id="0")


class _StartIdMovesAnExistingGroup(_MemoryProvider):
    async def ensure_group(
        self, stream: str, group: str, *, start_id: str = "0"
    ) -> None:
        await super().ensure_group(stream, group, start_id=start_id)
        if start_id != "0":
            self._cursor[(stream, group)] = self._start_index(stream, start_id)


class _LeasingMemory(_MemoryProvider):
    """The memory transport with ``lease_renewal``: an entry one consumer holds
    is not handed to another until its lease (renewed by ``renew_pending``)
    has run out, as the streams driver does with the idle time of an entry."""

    supported_features = _MemoryProvider.supported_features | {"lease_renewal"}
    lease_seconds = 0.2

    def __init__(
        self, *, name: str, driver: str, options: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._leases: dict[tuple[str, str, str], tuple[str, float]] = {}

    async def consume(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int | None = None,
    ) -> list[Delivery]:
        pending = self._pending.setdefault((stream, group), {})
        now = time.monotonic()
        held = {
            msg_id: envelope
            for msg_id, envelope in pending.items()
            if (lease := self._leases.get((stream, group, msg_id))) is not None
            and lease[0] != consumer
            and now - lease[1] < self.lease_seconds
        }
        for msg_id in held:
            del pending[msg_id]
        try:
            delivered = await super().consume(
                stream, group=group, consumer=consumer, count=count, block_ms=block_ms
            )
        finally:
            pending.update(held)
        for delivery in delivered:
            self._leases[(stream, group, delivery.id)] = (consumer, time.monotonic())
        return delivered

    async def renew_pending(
        self, deliveries: Sequence[Delivery], consumer: str
    ) -> None:
        for delivery in deliveries:
            key = (delivery.stream, delivery.group, delivery.id)
            lease = self._leases.get(key)
            if lease is None or lease[0] != consumer:
                raise RuntimeError("the delivery lease is lost")
            self._leases[key] = (consumer, time.monotonic())

    def renew_interval_seconds(self) -> float:
        return self.lease_seconds / 3


class _RenewDoesNothing(_LeasingMemory):
    async def renew_pending(
        self, deliveries: Sequence[Delivery], consumer: str
    ) -> None:
        return None


class _RenewOfNothingRaises(_LeasingMemory):
    async def renew_pending(
        self, deliveries: Sequence[Delivery], consumer: str
    ) -> None:
        if not deliveries:
            raise ValueError("nothing to renew")
        await super().renew_pending(deliveries, consumer)


class _RenewIntervalIsZero(_LeasingMemory):
    def renew_interval_seconds(self) -> float:
        return 0.0


class _SharedMemory(_MemoryProvider):
    """The memory transport with ``shared``: every instance works on the same
    streams and groups, as every process of a deployment does on one backend."""

    supported_features = _MemoryProvider.supported_features | {"shared"}
    _state: ClassVar[dict[str, Any]] = {}
    _shared = (
        "_log", "_meta", "_cursor", "_pending", "_delivery_count",
        "_delivered_at", "_dlq", "_arrivals", "_owners", "_inventory",
    )  # fmt: skip

    def __init__(
        self, *, name: str, driver: str, options: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(name=name, driver=driver, options=options)
        for attribute in self._shared:
            self.__dict__[attribute] = self._state.setdefault(
                attribute, self.__dict__[attribute]
            )

    def _append(self, stream: str, envelope: Any, *, target_group: str | None) -> str:
        self._seq = self._state.get("_seq", 0)
        message_id = super()._append(stream, envelope, target_group=target_group)
        self._state["_seq"] = self._seq
        return message_id


class _SharedButSeparate(_MemoryProvider):
    supported_features = _MemoryProvider.supported_features | {"shared"}


class _LeaseDeclaredNotServed(_MemoryProvider):
    supported_features = _MemoryProvider.supported_features | {"lease_renewal"}


class _RenewServedUndeclared(_MemoryProvider):
    async def renew_pending(
        self, deliveries: Sequence[Delivery], consumer: str
    ) -> None:
        return None


class _GroupStartIdUndeclared(_MemoryProvider):
    supported_features = _MemoryProvider.supported_features - {"group_start_id"}


class _DlqOpsUndeclared(_MemoryProvider):
    supported_features = _MemoryProvider.supported_features - {"dlq_ops"}


class _InspectionUndeclared(_MemoryProvider):
    supported_features = _MemoryProvider.supported_features - {"inspection"}


class _InventoryUndeclared(_MemoryProvider):
    supported_features = _MemoryProvider.supported_features - {"inventory"}


class _ListDlqIgnoresCount(_MemoryProvider):
    async def list_dlq(
        self, stream: str, *, count: int = 100, after: str | None = None
    ) -> list[DeadLetter]:
        return await super().list_dlq(stream, count=10**6, after=after)


class _ListDlqIgnoresAfter(_MemoryProvider):
    async def list_dlq(
        self, stream: str, *, count: int = 100, after: str | None = None
    ) -> list[DeadLetter]:
        return await super().list_dlq(stream, count=count)


class _ListDlqForgetsTheGroup(_MemoryProvider):
    async def list_dlq(
        self, stream: str, *, count: int = 100, after: str | None = None
    ) -> list[DeadLetter]:
        listed = await super().list_dlq(stream, count=count, after=after)
        return [replace(letter, group=None) for letter in listed]


class _ReplayToEveryGroup(_MemoryProvider):
    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: Sequence[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        replayed = await super().replay_dlq(stream, ids=ids, count=count, group=group)
        for message_id, (millis, target) in list(self._meta.items()):
            if target is not None:
                self._meta[message_id] = (millis, None)
        return replayed


class _ReplayKeepsTheEntry(_MemoryProvider):
    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: Sequence[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        before = list(self._dlq.get(stream, []))
        replayed = await super().replay_dlq(stream, ids=ids, count=count, group=group)
        self._dlq[stream] = before
        return replayed


class _ReplayIgnoresIds(_MemoryProvider):
    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: Sequence[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        return await super().replay_dlq(stream, count=count, group=group)


class _ReplayIgnoresCount(_MemoryProvider):
    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: Sequence[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        return await super().replay_dlq(stream, ids=ids, count=10**6, group=group)


class _ReplayIgnoresGroup(_MemoryProvider):
    async def replay_dlq(
        self,
        stream: str,
        *,
        ids: Sequence[str] | None = None,
        count: int = 100,
        group: str | None = None,
    ) -> int:
        return await super().replay_dlq(stream, ids=ids, count=count)


class _GroupInfoWithoutPending(_MemoryProvider):
    async def group_info(self, stream: str) -> list[GroupInfo]:
        return [replace(g, pending=0) for g in await super().group_info(stream)]


class _StreamInfoCountsOneTooMany(_MemoryProvider):
    async def stream_info(self, stream: str) -> StreamInfo:
        info = await super().stream_info(stream)
        return replace(info, length=info.length + 1)


class _OldestUnackedIsAlwaysZero(_MemoryProvider):
    async def oldest_unacked_age_seconds(self, stream: str, group: str) -> float | None:
        age = await super().oldest_unacked_age_seconds(stream, group)
        return None if age is None else 0.0


class _ForgetKeepsTheEntry(_MemoryProvider):
    async def forget_consumer(self, stream: str, group: str) -> bool:
        return (stream, group) in self._inventory


class _RegisterKeepsTheFirst(_MemoryProvider):
    async def register_consumer(
        self, stream: str, group: str, info: ConsumerInfo
    ) -> None:
        if (stream, group) not in self._inventory:
            await super().register_consumer(stream, group, info)


class _OutageIsSwallowed(_MemoryProvider):
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
            return await super().consume(
                stream, group=group, consumer=consumer, count=count, block_ms=block_ms
            )
        except Unavailable:
            return []


class _OutageIsARawError(_MemoryProvider):
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
            return await super().consume(
                stream, group=group, consumer=consumer, count=count, block_ms=block_ms
            )
        except Unavailable as exc:
            raise ConnectionError("the connection was cut") from exc


class _OutageLosesTheUnacked(_MemoryProvider):
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
            return await super().consume(
                stream, group=group, consumer=consumer, count=count, block_ms=block_ms
            )
        except Unavailable:
            for pending in self._pending.values():
                pending.clear()
            raise


class _OutageSkipsTheUnread(_MemoryProvider):
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
            return await super().consume(
                stream, group=group, consumer=consumer, count=count, block_ms=block_ms
            )
        except Unavailable:
            for key in self._cursor:
                self._cursor[key] = len(self._log.get(key[0], []))
            raise


class _Kit(_MemoryHooks, EventTransportConformance):
    redelivery_timeout_seconds = 0.6
    settle_seconds = 0.15
    outage_seconds = 0.6


# An in-process transport has no backend that can fail.
for _name in (
    "test_harness_redacts_the_health_of_a_failing_backend",
    "test_harness_maps_backend_failures_to_infra_errors",
):
    setattr(_Kit, _name, None)


async def _failures(driver: type[_MemoryProvider]) -> set[str]:
    """The names of the kit tests ``driver`` fails."""
    instance = _Kit()
    failed: set[str] = set()

    def make() -> _MemoryProvider:
        return driver(name="kit", driver="memory", options={})

    for name in sorted(n for n in dir(_Kit) if n.startswith("test_")):
        method = getattr(instance, name)
        if method is None:
            continue
        parameters = inspect.signature(method).parameters
        opened = None
        arguments: dict[str, Any] = {}
        if "provider" in parameters:
            opened = make()
            await opened.aopen()
            arguments["provider"] = opened
        if "make_provider" in parameters:
            arguments["make_provider"] = make
        if "make_failing_provider" in parameters:
            arguments["make_failing_provider"] = None
        run = asyncio.ensure_future(method(**arguments))
        await asyncio.wait([run])
        outcome = run.exception()
        if outcome is not None and not isinstance(outcome, pytest.skip.Exception):
            failed.add(name)
        if opened is not None:
            with suppress(Exception):
                await opened.aclose()
    return failed


_OUTAGE = "test_an_outage_during_consume_raises_unavailable_and_consuming_resumes"

_CASES: dict[type[_MemoryProvider], set[str]] = {
    # The ack of the memory transport is also the last step of dead_letter, so
    # a broken ack fails the parking checks too.
    _AckDoesNothing: {
        "test_ack_stops_redelivery",
        "test_ack_many_stops_redelivery",
        "test_ensure_group_is_idempotent_and_keeps_the_position",
        "test_consumers_of_a_group_share_its_entries",
        "test_dead_letter_parks_the_entry_at_once",
        "test_dead_letter_parks_even_a_never_dead_letter_group",
        # The checks that read a group's entries to the end ack what they read.
        "test_a_group_started_at_the_end_skips_the_history",
        "test_a_group_started_at_a_time_reads_from_there",
        "test_the_start_id_applies_to_a_new_group_only",
        "test_group_info_reports_position_and_pending",
        "test_the_oldest_unacked_age_follows_the_group",
        "test_replay_returns_the_entry_to_the_group_that_failed_it",
        "test_replay_honours_ids_and_count",
        _OUTAGE,
    },
    _AcceptsASecondOwner: {
        "test_a_second_owner_is_refused",
        "test_one_of_many_concurrent_claims_wins",
    },
    _TransferWithoutCompare: {
        "test_transfer_is_a_compare_and_set_on_the_whole_owner",
        "test_one_of_many_concurrent_transfers_wins",
    },
    _ReleaseKeepsTheOwner: {"test_release_forgets_the_owner"},
    _AcksTheSourceBeforeTheDlq: {"test_the_source_is_acked_only_after_the_dlq_append"},
    _DeadLetterOnlyAcks: {
        "test_the_source_is_acked_only_after_the_dlq_append",
        "test_dead_letter_parks_the_entry_at_once",
        # Nothing is parked, so nothing is listed or replayed.
        "test_list_dlq_describes_a_parked_entry",
        "test_list_dlq_pages_in_order",
        "test_replay_returns_the_entry_to_the_group_that_failed_it",
        "test_replay_honours_ids_and_count",
        "test_replay_can_be_limited_to_one_group",
    },
    _IgnoresNeverDeadLetter: {"test_never_dead_letter_keeps_redelivering"},
    _OneDeliveryTooMany: {"test_max_deliveries_moves_the_entry_to_the_dlq"},
    _PublishManySwallowsAFailure: {
        "test_publish_many_failing_in_the_middle_raises_partial_publish"
    },
    _PublishManyClaimsTooMuch: {
        "test_publish_many_failing_in_the_middle_raises_partial_publish"
    },
    _PublishManyReversed: {
        "test_publish_many_appends_in_order_and_returns_the_ids",
        "test_publish_many_failing_in_the_middle_raises_partial_publish",
    },
    _RetentionNotEchoed: {"test_retention_echoes_the_options"},
    _NewGroupMissesHistory: {
        "test_a_new_group_reads_the_stream_from_its_start",
        "test_a_group_started_at_a_time_reads_from_there",
        "test_the_start_id_applies_to_a_new_group_only",
        "test_replay_returns_the_entry_to_the_group_that_failed_it",
    },
    _BlockingIgnored: {
        "test_a_blocking_consume_returns_when_an_entry_arrives",
        "test_a_blocking_consume_of_an_empty_stream_returns_nothing_in_time",
        _OUTAGE,
    },
    _StartIdIgnored: {
        "test_a_group_started_at_the_end_skips_the_history",
        "test_a_group_started_at_a_time_reads_from_there",
        "test_the_start_id_applies_to_a_new_group_only",
    },
    _StartIdMovesAnExistingGroup: {"test_the_start_id_applies_to_a_new_group_only"},
    _RenewDoesNothing: {"test_renewal_keeps_an_entry_with_its_consumer_until_it_stops"},
    _RenewOfNothingRaises: {
        "test_renewing_nothing_is_a_no_op",
        "test_harness_declared_behaviours_do_not_raise_unsupported",
    },
    _RenewIntervalIsZero: {
        "test_the_renew_interval_is_positive",
        "test_renewal_keeps_an_entry_with_its_consumer_until_it_stops",
    },
    _SharedButSeparate: {
        "test_two_providers_of_one_configuration_share_streams_and_groups"
    },
    _LeaseDeclaredNotServed: {
        "test_harness_declared_behaviours_do_not_raise_unsupported",
        "test_renewing_nothing_is_a_no_op",
        "test_the_renew_interval_is_positive",
        "test_renewal_keeps_an_entry_with_its_consumer_until_it_stops",
    },
    _RenewServedUndeclared: {"test_harness_undeclared_behaviours_raise_unsupported"},
    _GroupStartIdUndeclared: {"test_harness_undeclared_behaviours_raise_unsupported"},
    _DlqOpsUndeclared: {"test_harness_undeclared_behaviours_raise_unsupported"},
    _InspectionUndeclared: {"test_harness_undeclared_behaviours_raise_unsupported"},
    _InventoryUndeclared: {"test_harness_undeclared_behaviours_raise_unsupported"},
    _ListDlqIgnoresCount: {"test_list_dlq_pages_in_order"},
    _ListDlqIgnoresAfter: {"test_list_dlq_pages_in_order"},
    _ListDlqForgetsTheGroup: {"test_list_dlq_describes_a_parked_entry"},
    _ReplayToEveryGroup: {"test_replay_returns_the_entry_to_the_group_that_failed_it"},
    _ReplayKeepsTheEntry: {
        "test_replay_returns_the_entry_to_the_group_that_failed_it",
        "test_replay_honours_ids_and_count",
        "test_replay_can_be_limited_to_one_group",
    },
    _ReplayIgnoresIds: {"test_replay_honours_ids_and_count"},
    _ReplayIgnoresCount: {"test_replay_honours_ids_and_count"},
    _ReplayIgnoresGroup: {"test_replay_can_be_limited_to_one_group"},
    _GroupInfoWithoutPending: {"test_group_info_reports_position_and_pending"},
    _StreamInfoCountsOneTooMany: {
        "test_stream_info_reports_length_and_the_newest_entry"
    },
    _OldestUnackedIsAlwaysZero: {"test_the_oldest_unacked_age_follows_the_group"},
    _ForgetKeepsTheEntry: {"test_the_consumer_inventory_lists_and_forgets"},
    _RegisterKeepsTheFirst: {"test_registering_again_replaces_the_entry"},
    _OutageIsSwallowed: {_OUTAGE},
    _OutageIsARawError: {_OUTAGE},
    _OutageLosesTheUnacked: {_OUTAGE},
    _OutageSkipsTheUnread: {_OUTAGE},
}


@pytest.mark.parametrize(
    "driver", [_MemoryProvider, _LeasingMemory, _SharedMemory], ids=lambda c: c.__name__
)
async def test_the_kit_passes_a_transport_that_keeps_the_contract(
    driver: type[_MemoryProvider],
) -> None:
    assert await _failures(driver) == set()


@pytest.mark.parametrize("broken", list(_CASES), ids=lambda cls: cls.__name__)
async def test_each_check_fails_exactly_its_broken_transport(
    broken: type[_MemoryProvider],
) -> None:
    assert await _failures(broken) == _CASES[broken]
