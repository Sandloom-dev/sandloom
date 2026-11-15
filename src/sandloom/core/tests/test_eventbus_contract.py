"""The ``eventbus`` contract (spec 7): the typed transport, its declared
features, the two driver shapes and the rename table of the previous major."""

from __future__ import annotations

from typing import Any

import pytest
from sandloom_redis.eventbus.driver import StreamsEventBusProvider

from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus, build_event_bus
from sandloom.core.eventing.transport_base import declared_features, partial_publish
from sandloom.core.eventing.volatile_provider import (
    MemoryEventBusProvider,
    VolatileBusProvider,
)
from sandloom.core.infra.capabilities import EVENTBUS_CAPABILITY
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import (
    AlreadyExists,
    InvalidArgument,
    Unavailable,
    Unsupported,
)
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


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, Retention(max_age_seconds=7 * 86400, max_length=None)),
        (
            {"retention_seconds": 3600, "max_length": 500},
            Retention(max_age_seconds=3600, max_length=500),
        ),
        (
            {"retention_seconds": 0, "max_length": 0},
            Retention(max_age_seconds=None, max_length=None),
        ),
    ],
)
def test_retention_is_reported_from_the_streams_driver_options(
    options: dict[str, Any], expected: Retention
) -> None:
    provider = StreamsEventBusProvider(
        name="default", driver="streams", options=options, client_factory=_client
    )
    assert provider.retention() == expected


def test_the_kernel_fills_the_retention_options_from_the_settings() -> None:
    from types import SimpleNamespace

    from sandloom.core.infra.config import (
        InfraConfig,
        InstanceConfig,
        fill_driver_defaults,
    )

    cfg = InstanceConfig(
        capability="eventbus", name="default", driver="streams", options={}
    )
    config: InfraConfig = {"eventbus": {"default": cfg}}
    settings = SimpleNamespace(
        cache_url="redis://x/0",
        event_stream_retention_seconds=120,
        event_stream_maxlen=9,
    )
    fill_driver_defaults(config, settings)
    assert cfg.options["retention_seconds"] == 120
    assert cfg.options["max_length"] == 9
    own = InstanceConfig(
        capability="eventbus",
        name="x",
        driver="streams",
        options={"retention_seconds": 5},
    )
    fill_driver_defaults({"eventbus": {"x": own}}, settings)
    assert own.options["retention_seconds"] == 5
    assert own.options["max_length"] == 9


def test_a_transport_provider_missing_a_required_operation_cannot_be_built() -> None:
    required = {
        "publish",
        "publish_many",
        "ensure_group",
        "consume",
        "ack",
        "ack_many",
        "dead_letter",
        "never_dead_letter",
        "claim_group_owner",
        "group_owner",
        "list_group_owners",
        "transfer_group_owner",
        "release_group_owner",
        "retention",
    }
    assert required <= EventTransportProvider.__abstractmethods__
    assert required == EventTransportProvider.__abstractmethods__

    class _Incomplete(EventTransportProvider):
        contract_version = 2

    with pytest.raises(TypeError, match="abstract"):
        _Incomplete(name="x", driver="x", options={})  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_feature_operations_default_to_unsupported() -> None:
    provider = StreamsEventBusProvider(
        name="default", driver="streams", options={}, client_factory=_client
    )
    base = EventTransportProvider
    cases: list[tuple[str, tuple[Any, ...], dict[str, Any], str]] = [
        ("renew_pending", ([], "c"), {}, "lease_renewal"),
        ("list_dlq", ("s",), {}, "dlq_ops"),
        ("replay_dlq", ("s",), {}, "dlq_ops"),
        ("group_info", ("s",), {}, "inspection"),
        ("stream_info", ("s",), {}, "inspection"),
        ("oldest_unacked_age_seconds", ("s", "g"), {}, "inspection"),
        ("list_consumers", (), {}, "inventory"),
        ("forget_consumer", ("s", "g"), {}, "inventory"),
    ]
    for name, args, kwargs, feature in cases:
        with pytest.raises(Unsupported) as refused:
            await getattr(base, name)(provider, *args, **kwargs)
        assert refused.value.feature == feature
    with pytest.raises(Unsupported):
        base.renew_interval_seconds(provider)


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
    assert "durable" not in bus.bus_features

    streams = StreamsEventBusProvider(
        name="default", driver="streams", options={}, client_factory=_client
    )
    durable = build_event_bus(streams, schemas)
    assert isinstance(durable, StreamsEventBus)
    assert durable.transport is streams
    assert "durable" in durable.bus_features
    assert durable.native_batch_publish is True


# -- the bus reads declared features, nothing is probed ----------------------


def test_a_bus_over_a_transport_without_durable_is_not_durable() -> None:
    class _Volatileish(MemoryTransport):
        supported_features = frozenset({"inspection"})

    bus = StreamsEventBus(InMemorySchemaRegistry(), _Volatileish())
    assert "durable" not in bus.bus_features
    assert bus.transport_features == frozenset({"inspection"})
    assert declared_features(MemoryTransport()) == MemoryTransport.supported_features


def test_the_bus_refuses_a_retention_that_is_not_a_retention() -> None:
    """The purge guards read ``retention()``: a transport answering anything
    else breaks the contract, it is never cast into one."""

    class _Broken(MemoryTransport):
        def retention(self) -> Any:
            return {"max_age_seconds": 60}

    bus = StreamsEventBus(InMemorySchemaRegistry(), _Broken())
    with pytest.raises(InvalidArgument, match="not a Retention"):
        bus.retention()
    good = StreamsEventBus(InMemorySchemaRegistry(), MemoryTransport())
    assert good.retention() == MemoryTransport().retention()


def test_a_bus_states_its_transport_and_durable_transport_never_skips() -> None:
    from sandloom.core.eventing.bus_base import BaseEventBus, durable_transport
    from sandloom.core.eventing.memory_bus import InMemoryEventBus

    schemas = InMemorySchemaRegistry()
    transport = MemoryTransport()
    assert StreamsEventBus(schemas, transport).transport is transport
    assert InMemoryEventBus(schemas).transport is None
    assert durable_transport(InMemoryEventBus(schemas)) is None
    assert durable_transport(object()) is None  # a plain test bus
    durable = StreamsEventBus(schemas, _DurableTransport())
    assert "durable" in durable.bus_features
    assert durable_transport(durable) is durable.transport

    class _DurableNoTransport(BaseEventBus):
        bus_features = frozenset({"durable"})

        async def _publish_to_transport(self, channel: str, message: Any) -> None: ...

        async def subscribe(self, channel: str, callback: Any) -> Any: ...

        def unsubscribe(self, channel: str, callback: Any) -> None: ...

    with pytest.raises(InvalidArgument, match="durable"):
        durable_transport(_DurableNoTransport(schemas))


def test_the_plainest_bus_facts_claim_nothing() -> None:
    """``BusFacts`` defaults match its docstring; the kernel's buses say True."""
    from sandloom.core.eventing.bus_base import BaseEventBus, BusFacts

    plain = BusFacts()
    assert not (plain.supports_prevalidated or plain.supports_preflighted)
    assert not (plain.supports_batch_publish or plain.in_process)
    assert BaseEventBus.supports_prevalidated is True
    assert BaseEventBus.supports_batch_publish is True


class _DurableTransport(MemoryTransport):
    supported_features = frozenset({"durable"})


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
