"""Event ops (``sandloom streams``, the owner console) run on the typed transport.

A bus without a durable transport and a transport that does not declare a
feature are refused with a clear message; a transport that declares it works.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.cli import events as cli_events
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.memory_transport import MemoryTransport
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.infra.errors import Unsupported
from sandloom.core.infra.ports.eventbus import (
    ConsumerInfo,
    DeadLetter,
    GroupInfo,
    GroupOwner,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tasks import event_ops


class _NoDlqTransport(MemoryTransport):
    """Declares inspection only: no ``dlq_ops``, no ``inventory``."""

    supported_features = frozenset({"inspection"})


class _NoInspectionTransport(MemoryTransport):
    """Declares ``dlq_ops`` and ``inventory`` only: no ``inspection``."""

    supported_features = frozenset({"dlq_ops", "inventory"})


_DRIVER_UNSUPPORTED = Unsupported("driver-native", capability="eventbus")


class _RaisingTransport(MemoryTransport):
    """Declares every feature, then raises ``Unsupported`` from each operation."""

    async def list_dlq(self, *args: Any, **kwargs: Any) -> Any:
        raise _DRIVER_UNSUPPORTED

    async def replay_dlq(self, *args: Any, **kwargs: Any) -> Any:
        raise _DRIVER_UNSUPPORTED

    async def group_info(self, *args: Any, **kwargs: Any) -> Any:
        raise _DRIVER_UNSUPPORTED

    async def list_consumers(self, *args: Any, **kwargs: Any) -> Any:
        raise _DRIVER_UNSUPPORTED


class _DurableWithoutTransport(InMemoryEventBus):
    bus_features = frozenset({"durable"})


async def _noop() -> None:
    return None


def _core(transport: MemoryTransport) -> Any:
    bus = StreamsEventBus(InMemorySchemaRegistry(), transport)
    return SimpleNamespace(event_bus=bus)


async def _dead_letter_one(transport: MemoryTransport, stream: str) -> None:
    await transport.ensure_group(stream, "g")
    await transport.publish(stream, {"id": "e1", "payload": {}})  # type: ignore[typeddict-item]
    (delivery,) = await transport.consume(stream, group="g", consumer="c")
    await transport.dead_letter(delivery, reason="boom")


async def test_a_bus_without_a_transport_is_refused() -> None:
    core = SimpleNamespace(event_bus=InMemoryEventBus(InMemorySchemaRegistry()))
    with pytest.raises(event_ops.EventOperationUnavailable) as caught:
        await event_ops.streams_groups(core, "a.b.v1")
    assert "no durable transport" in str(caught.value.code)
    with pytest.raises(event_ops.EventOperationUnavailable):
        await event_ops.streams_release_group(core, "a.b.v1", "g")


async def test_a_bus_that_is_not_a_kernel_bus_is_refused() -> None:
    core = SimpleNamespace(event_bus=object())
    with pytest.raises(event_ops.EventOperationUnavailable):
        event_ops.event_transport(core)


@pytest.mark.parametrize(
    ("operation", "feature"),
    [
        ("dlq_list", "dlq_ops"),
        ("dlq_replay", "dlq_ops"),
        ("consumers", "inventory"),
    ],
)
async def test_an_undeclared_feature_is_refused_by_name(
    operation: str, feature: str
) -> None:
    core = _core(_NoDlqTransport())
    with pytest.raises(Unsupported) as caught:
        if operation == "dlq_list":
            await event_ops.streams_dlq_list(core, "a.b.v1")
        elif operation == "dlq_replay":
            await event_ops.streams_dlq_replay(core, "a.b.v1")
        else:
            await event_ops.streams_consumers(core)
    assert caught.value.feature == feature
    assert event_ops.unsupported_message(caught.value) == (
        f"not supported by this driver: {feature}"
    )


async def test_inspection_is_refused_when_the_transport_does_not_declare_it() -> None:
    core = _core(_NoInspectionTransport())
    with pytest.raises(Unsupported) as caught:
        await event_ops.streams_groups(core, "a.b.v1")
    assert caught.value.feature == "inspection"


async def test_a_driver_raised_unsupported_propagates_unchanged() -> None:
    core = _core(_RaisingTransport())
    for call in (
        event_ops.streams_dlq_list(core, "a.b.v1"),
        event_ops.streams_dlq_replay(core, "a.b.v1"),
        event_ops.streams_groups(core, "a.b.v1"),
        event_ops.streams_consumers(core),
    ):
        with pytest.raises(Unsupported) as caught:
            await call
        assert caught.value.feature == "driver-native"
        assert caught.value.args == _DRIVER_UNSUPPORTED.args


async def test_a_durable_bus_without_a_transport_is_a_fixed_refusal() -> None:
    bus = _DurableWithoutTransport(InMemorySchemaRegistry())
    core = SimpleNamespace(event_bus=bus)
    with pytest.raises(event_ops.EventOperationUnavailable) as caught:
        await event_ops.streams_groups(core, "a.b.v1")
    assert str(caught.value.code) == (
        "error: the configured event bus declares the 'durable' feature "
        "but exposes no transport (check the event bus driver)"
    )
    assert "InMemoryEventBus" not in str(caught.value.code)


@pytest.mark.parametrize(
    "transport",
    [_NoDlqTransport(), _RaisingTransport()],
    ids=["undeclared", "driver-raised"],
)
def test_the_streams_cli_exits_with_the_unsupported_message(
    transport: MemoryTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = _core(transport)
    core.infra = SimpleNamespace(aclose_all=_noop)
    monkeypatch.setattr(cli_events, "_build_core", lambda: core)
    argv = ["dlq-list", "a.b.v1"]
    with pytest.raises(SystemExit) as caught:
        cli_events.streams_main(argv)
    assert str(caught.value.code).startswith("error: not supported by this driver: ")


def test_the_streams_cli_names_the_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    core = _core(_NoDlqTransport())
    core.infra = SimpleNamespace(aclose_all=_noop)
    monkeypatch.setattr(cli_events, "_build_core", lambda: core)
    with pytest.raises(SystemExit) as caught:
        cli_events.streams_main(["consumers"])
    assert caught.value.code == "error: not supported by this driver: inventory"


async def test_a_declared_feature_works_and_ownership_needs_none() -> None:
    transport = _NoDlqTransport()
    core = _core(transport)
    await transport.ensure_group("a.b.v1", "g")
    groups = await event_ops.streams_groups(core, "a.b.v1")
    assert [g.group for g in groups] == ["g"]
    assert all(isinstance(g, GroupInfo) for g in groups)
    # group ownership is required of every transport: no feature check
    await transport.claim_group_owner("a.b.v1", "g", GroupOwner("m", "d"))
    assert await event_ops.streams_release_group(core, "a.b.v1", "g") is True
    assert await event_ops.streams_release_group(core, "a.b.v1", "g") is False


async def test_a_full_transport_serves_every_operation_typed() -> None:
    transport = MemoryTransport()
    core = _core(transport)
    stream = "a.b.v1"
    await _dead_letter_one(transport, stream)

    (entry,) = await event_ops.streams_dlq_list(core, stream)
    assert isinstance(entry, DeadLetter)
    assert entry.group == "g"
    assert await event_ops.streams_dlq_list(core, stream, after=entry.id) == []

    assert await event_ops.streams_dlq_replay(core, stream, ids=[entry.id]) == 1

    await transport.register_consumer(stream, "g", ConsumerInfo(stream, "g", "m", "d"))
    (consumer,) = await event_ops.streams_consumers(core)
    assert isinstance(consumer, ConsumerInfo)
    assert (consumer.stream, consumer.group) == (stream, "g")
