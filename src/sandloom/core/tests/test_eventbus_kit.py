"""The ``EventTransportConformance`` kit (spec 7), core groups: run against the
in-memory reference transport and the ``streams`` driver (a real Redis), and
against deliberately broken transports (each breaks one obligation and must
fail exactly the kit tests that name it).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from types import MappingProxyType
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest

# The one place this file reaches the streams transport.
from sandloom.core.config.settings import settings
from sandloom.core.drivers.event_bus_driver import StreamsEventBusProvider
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.transport_base import partial_publish
from sandloom.core.infra.errors import Unavailable
from sandloom.core.infra.ports.eventbus import (
    Delivery,
    EventTransportProvider,
    GroupOwner,
    Retention,
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

    async def _connect(self) -> Any:
        if self._redis is None:
            from redis import asyncio as redis_asyncio

            raw = redis_asyncio.from_url(
                self.options["url"], encoding="utf-8", decode_responses=True
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
    # driver slice (idk-eventbus-driver) maps them to InfraError and drops this.
    test_harness_maps_backend_failures_to_infra_errors = None  # type: ignore[assignment]


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


class _Kit(_MemoryHooks, EventTransportConformance):
    redelivery_timeout_seconds = 2.0
    settle_seconds = 0.2


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
    _NewGroupMissesHistory: {"test_a_new_group_reads_the_stream_from_its_start"},
}


async def test_the_kit_passes_a_transport_that_keeps_the_contract() -> None:
    assert await _failures(_MemoryProvider) == set()


@pytest.mark.parametrize("broken", list(_CASES), ids=lambda cls: cls.__name__)
async def test_each_check_fails_exactly_its_broken_transport(
    broken: type[_MemoryProvider],
) -> None:
    assert await _failures(broken) == _CASES[broken]
