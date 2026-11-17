"""The ``streams`` eventbus provider: what it declares, reports and builds.

No Redis is reached: the provider is built over a client factory that fails when
called. The kernel's contract tests (vocabulary, rename table, abstract surface,
feature defaults) stay in ``src/sandloom/core/tests/test_eventbus_contract.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sandloom_redis.eventbus.driver import StreamsEventBusProvider, make_streams_driver

from sandloom.core.config.common import Environment
from sandloom.core.config.settings import settings
from sandloom.core.eventing.streams_bus import StreamsEventBus, build_event_bus
from sandloom.core.infra.capabilities import EVENTBUS_CAPABILITY
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.ports.eventbus import EventTransportProvider, Retention
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


def test_the_kernel_builds_its_durable_bus_over_the_streams_provider() -> None:
    provider = StreamsEventBusProvider(
        name="default", driver="streams", options={}, client_factory=_client
    )
    bus = build_event_bus(provider, InMemorySchemaRegistry())
    assert isinstance(bus, StreamsEventBus)
    assert bus.transport is provider
    assert "durable" in bus.bus_features
    assert bus.native_batch_publish is True


async def test_broker_unavailability_is_reported_as_health_failure() -> None:
    async def client() -> Any:
        raise ConnectionError("unavailable")

    provider = StreamsEventBusProvider(
        name="default", driver="streams", options={}, client_factory=client
    )
    assert (await provider.health()).status == "fail"


def test_the_streams_driver_is_durable_and_not_a_volatile_bus_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The volatile-bus refusal covers the ``memory`` and ``broadcast`` Pub/Sub drivers
    only: ``streams`` builds in production without the ``volatile_event_bus`` risk."""
    monkeypatch.setattr(settings, "environment", Environment.prod)
    monkeypatch.setattr(settings, "sandloom_accepted_risks", [])
    streams = InstanceConfig(capability="eventbus", name="side", driver="streams")
    assert "durable" in make_streams_driver()(streams).features()
