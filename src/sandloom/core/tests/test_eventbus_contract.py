"""The ``eventbus`` contract (spec 7): the typed transport, its declared
features, the two driver shapes and the rename table of the previous major."""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core.drivers.event_bus_driver import StreamsEventBusProvider
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus, build_event_bus
from sandloom.core.eventing.transport_base import declared_features, partial_publish
from sandloom.core.eventing.volatile_provider import (
    MemoryEventBusProvider,
    VolatileBusProvider,
)
from sandloom.core.infra.capabilities import EVENTBUS_CAPABILITY
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import AlreadyExists, Unavailable, Unsupported
from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.infra.ports.eventbus import (
    ConsumerInfo,
    EventTransportProvider,
    GroupOwnedElsewhere,
    GroupOwner,
    PartialPublish,
    Retention,
)
from sandloom.core.infra.registry import FeatureUnsupportedError, InfraRegistry
from sandloom.core.schema.registry import InMemorySchemaRegistry

_TRANSPORT_FEATURES = frozenset(
    {
        "durable",
        "shared",
        "blocking_consume",
        "group_start_id",
        "lease_renewal",
        "dlq_ops",
        "inspection",
        "inventory",
    }
)


async def _client() -> Any:  # pragma: no cover - never called
    raise AssertionError("no I/O in these tests")


# -- the vocabulary and its rename table -------------------------------------


def test_the_vocabulary_is_the_one_of_spec_7() -> None:
    spec = EVENTBUS_CAPABILITY
    assert spec.major == 2
    assert spec.features == _TRANSPORT_FEATURES | {"wildcard_subscribe"}
    assert spec.feature_kinds["durable"] == "guarantee"
    assert spec.feature_kinds["shared"] == "guarantee"
    assert spec.feature_kinds["wildcard_subscribe"] == "behaviour"
    assert dict(spec.removed_features) == {
        "publish": spec.removed_features["publish"],
        "subscribe": spec.removed_features["subscribe"],
        "volatile": spec.removed_features["volatile"],
        "at_least_once": "durable",
        "consumer_groups": "durable",
    }


class _Probe(InfraProvider):
    capability = "eventbus"
    contract_version = 2
    supported_features = frozenset({"durable"})


@pytest.mark.parametrize(
    "removed", ["publish", "subscribe", "volatile", "at_least_once", "consumer_groups"]
)
def test_a_removed_feature_is_refused_naming_its_replacement(removed: str) -> None:
    registry = InfraRegistry(strict_capabilities=True)
    registry.register_capability(EVENTBUS_CAPABILITY)
    registry.register_driver("eventbus", "probe", _Probe)
    registry.set_config(
        {"eventbus": {"default": InstanceConfig("eventbus", "default", "probe")}}
    )
    replacement = EVENTBUS_CAPABILITY.removed_features[removed]
    with pytest.raises(FeatureUnsupportedError, match=f"{removed} was renamed"):
        registry.validate_requirement("eventbus", features=frozenset({removed}))
    with pytest.raises(FeatureUnsupportedError, match=replacement.split()[0]):
        registry.resolve("eventbus", features=frozenset({removed}))


# -- the two driver shapes ----------------------------------------------------


def test_streams_is_a_transport_provider_with_the_declared_features() -> None:
    provider = StreamsEventBusProvider(
        name="default", driver="streams", options={}, client_factory=_client
    )
    assert isinstance(provider, EventTransportProvider)
    assert provider.capability == "eventbus"
    assert provider.contract_version == EVENTBUS_CAPABILITY.major == 2
    assert provider.features() == _TRANSPORT_FEATURES
    assert provider.performance_hints == {
        "publish_many": "one_round_trip",
        "ack_many": "one_round_trip",
    }
    assert isinstance(provider.retention(), Retention)


def test_memory_and_redis_are_volatile_providers_without_a_transport() -> None:
    assert issubclass(MemoryEventBusProvider, VolatileBusProvider)
    assert not issubclass(MemoryEventBusProvider, EventTransportProvider)
    assert MemoryEventBusProvider.contract_version == 2
    assert "durable" not in MemoryEventBusProvider.supported_features


def test_the_kernel_builds_its_bus_per_shape() -> None:
    schemas = InMemorySchemaRegistry()
    from sandloom.core.eventing.memory_bus import InMemoryEventBus

    bus = InMemoryEventBus(schemas)
    provider = MemoryEventBusProvider(
        name="default", driver="memory", options={}, bus_instance=bus
    )
    assert build_event_bus(provider, schemas) is bus
    assert bus.durable is False

    streams = StreamsEventBusProvider(
        name="default", driver="streams", options={}, client_factory=_client
    )
    durable = build_event_bus(streams, schemas)
    assert isinstance(durable, StreamsEventBus)
    assert durable.transport is streams
    assert durable.durable is True
    assert durable.native_batch_publish is True


# -- the bus reads declared features, nothing is probed ----------------------


def test_a_bus_over_a_transport_without_durable_is_not_durable() -> None:
    class _Volatileish(MemoryTransport):
        supported_features = frozenset({"inspection"})

    bus = StreamsEventBus(InMemorySchemaRegistry(), _Volatileish())
    assert bus.durable is False
    assert bus.transport_features == frozenset({"inspection"})
    assert declared_features(MemoryTransport()) == MemoryTransport.supported_features


# -- operations and errors ---------------------------------------------------


@pytest.mark.asyncio
async def test_group_ownership_is_required_and_refuses_a_second_owner() -> None:
    transport = MemoryTransport()
    mine, other = GroupOwner("billing", "db:a"), GroupOwner("identity", "db:a")
    await transport.claim_group_owner("a.b.v1", "g", mine)
    await transport.claim_group_owner("a.b.v1", "g", GroupOwner("billing", "db:a", "x"))
    with pytest.raises(GroupOwnedElsewhere) as refused:
        await transport.claim_group_owner("a.b.v1", "g", other)
    assert isinstance(refused.value, AlreadyExists)
    assert refused.value.retryable is False
    assert await transport.group_owner("a.b.v1", "g") == mine
    assert await transport.list_group_owners() == [("a.b.v1", "g", mine)]
    assert not await transport.transfer_group_owner(
        "a.b.v1", "g", expected=other, owner=other
    )
    assert await transport.transfer_group_owner(
        "a.b.v1", "g", expected=mine, owner=other
    )
    assert await transport.release_group_owner("a.b.v1", "g")
    assert await transport.group_owner("a.b.v1", "g") is None


@pytest.mark.asyncio
async def test_inventory_stamps_the_entry_and_forgets_it() -> None:
    transport = MemoryTransport()
    info = ConsumerInfo("a.b.v1", "g", "billing", "db:a", consumer="c")
    await transport.register_consumer("a.b.v1", "g", info)
    (entry,) = await transport.list_consumers()
    assert entry.last_seen is not None and entry.stale is False
    assert (entry.module, entry.consumer) == ("billing", "c")
    assert await transport.forget_consumer("a.b.v1", "g")
    assert await transport.list_consumers() == []


@pytest.mark.asyncio
async def test_lease_renewal_without_the_feature_is_unsupported() -> None:
    transport = MemoryTransport()
    assert "lease_renewal" not in declared_features(transport)
    with pytest.raises(Unsupported) as refused:
        await transport.renew_pending([], "c")
    assert refused.value.feature == "lease_renewal"
    with pytest.raises(Unsupported):
        transport.renew_interval_seconds()


def test_partial_publish_follows_its_cause() -> None:
    transient = partial_publish(
        ["1-0"], "x", Unavailable("down", capability="eventbus")
    )
    assert transient.published == ["1-0"] and transient.retryable is True
    assert PartialPublish("x", retryable=False).retryable is False
    assert partial_publish([], "x", ValueError("boom")).retryable is True
