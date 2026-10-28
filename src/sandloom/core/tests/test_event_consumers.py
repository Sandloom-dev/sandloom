"""Tests for the generic durable event-consumer primitive.

The "react to a domain event with a side effect in your own UoW" seam:
registration through ModuleAPI (atomic, dup-name preflight shared with projection
consumers) and wiring/delivery through the bus.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.consumers.callbacks import EventConsumerDescriptor
from sandloom.core.consumers.runtime import start_event_consumers
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import ModuleRegistrationConflict
from sandloom.core.projections import (
    ProjectionConsumerDescriptor,
    QueryProjectionDescriptor,
)
from sandloom.core.tests._schemas import PermissiveSchemaRegistry


async def _noop(session: object, envelope: object) -> None:  # pragma: no cover
    return None


def test_module_registration_applies_event_consumer(core_facade: CoreFacade) -> None:
    with core_facade.module_registration("notifier") as api:
        api.register_event_consumer(
            "billing.subscription.created.v1", _noop, name="notifier@sub.created"
        )
    assert [c.name for c in core_facade.event_consumers] == ["notifier@sub.created"]


def test_consumer_name_unique_across_event_and_projection(
    core_facade: CoreFacade,
) -> None:
    # A projection consumer and a generic event consumer share the durable
    # consumer-group namespace: within ONE module the same name is rejected.
    with core_facade.module_registration("read_mod") as api:
        api.register_query_projection(
            QueryProjectionDescriptor(name="proj", owner_module="read_mod")
        )
        api.register_projection_consumer(
            ProjectionConsumerDescriptor(
                name="dup",
                source_channel="a.b.c.v1",
                projection="proj",
                handler=_noop,
            )
        )
    with pytest.raises(ModuleRegistrationConflict, match="read_mod:dup"):
        with core_facade.module_registration("read_mod") as api:
            api.register_event_consumer("a.b.c.v1", _noop, name="dup")
    assert all(c.name != "dup" for c in core_facade.event_consumers)


def test_same_consumer_name_in_two_modules_gets_two_groups(
    core_facade: CoreFacade,
) -> None:
    # Groups are namespaced by module: two modules reusing a name never share
    # one consumer group (which would split the channel's events between them).
    for module in ("svc_a.notifier", "svc_b.notifier"):
        with core_facade.module_registration(module) as api:
            api.register_event_consumer("a.b.c.v1", _noop, name="notify")
    groups = sorted(c.group_name for c in core_facade.event_consumers)
    assert groups == ["svc_a.notifier:notify", "svc_b.notifier:notify"]


def test_explicit_group_pin_conflicts_across_modules(core_facade: CoreFacade) -> None:
    with core_facade.module_registration("svc_a.notifier") as api:
        api.register_event_consumer("a.b.c.v1", _noop, name="n", group="legacy")
    with pytest.raises(ModuleRegistrationConflict, match="legacy"):
        with core_facade.module_registration("svc_b.notifier") as api:
            api.register_event_consumer("a.b.c.v1", _noop, name="m", group="legacy")


@pytest.mark.asyncio
async def test_start_event_consumers_wires_and_delivers() -> None:
    registry = PermissiveSchemaRegistry()
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(registry)
    bus = InMemoryEventBus(registry)
    core.set_event_bus(bus)

    seen: list[dict] = []

    async def handler(session: object, envelope: dict) -> None:
        seen.append(dict(envelope["payload"]))

    core.register_event_consumer(
        EventConsumerDescriptor(
            name="notifier@thing",
            channel="demo.thing.created.v1",
            handler=handler,
            durable=False,
        )
    )

    @asynccontextmanager
    async def opener(_name: str) -> AsyncGenerator[object, None]:
        yield object()

    wired = await start_event_consumers(core, session_opener=opener)
    assert wired == 1
    assert await start_event_consumers(core, session_opener=opener) == 1

    await bus.publish_event("demo.thing.created.v1", {"x": 1}, "v1")
    await bus.drain()
    assert seen == [{"x": 1}]
    await core.app.state.consumer_runtime.stop()


@pytest.mark.asyncio
async def test_bind_tenant_from_binds_the_envelope_tenant() -> None:
    """Opt-in: the handler runs with the payload's tenant bound (L4)."""
    import uuid

    from sandloom.core.consumers.callbacks import build_consumer_callback
    from sandloom.core.tenant_context import current_tenant_id_optional

    observed: list[object] = []

    async def handler(session: object, envelope: dict) -> None:
        observed.append(current_tenant_id_optional())

    @asynccontextmanager
    async def opener(_name: str) -> AsyncGenerator[object, None]:
        yield object()

    def _cb(bind: str | None):
        return build_consumer_callback(
            name="t",
            database="default",
            handler=handler,
            durable=False,
            opener=opener,
            metric_processed="event_consumer_events_processed_total",
            metric_failed="event_consumer_events_failed_total",
            labels={"consumer": "t"},
            bind_tenant_from=bind,
        )

    tenant = uuid.uuid4()
    await _cb("tenant_id")({"id": "e1", "payload": {"tenant_id": str(tenant)}})
    await _cb(None)({"id": "e2", "payload": {"tenant_id": str(tenant)}})
    assert observed == [tenant, None]  # default stays off
    assert current_tenant_id_optional() is None  # not leaked past the handler
    with pytest.raises(ValueError, match="tenant"):
        await _cb("tenant_id")({"id": "e3", "payload": {}})  # fail closed
    assert len(observed) == 2


def test_start_from_and_group_are_validated() -> None:
    with pytest.raises(ValueError, match="start_from"):
        EventConsumerDescriptor(
            name="n", channel="a.b.c.v1", handler=_noop, start_from="yesterday"
        )
    with pytest.raises(ValueError, match="group"):
        EventConsumerDescriptor(name="n", channel="a.b.c.v1", handler=_noop, group="")
    from sandloom.core.eventing.delivery import stream_start_id

    assert stream_start_id("earliest") == "0"
    assert stream_start_id("latest") == "$"
    assert (
        stream_start_id("2026-01-01T00:00:00Z") == "1767225599999-18446744073709551615"
    )
    assert stream_start_id(0) == stream_start_id("0") == "0"
    assert stream_start_id(1) == stream_start_id("1") == "0-18446744073709551615"
    assert stream_start_id("2026-01-01T00:00:00.000999Z") == stream_start_id(
        1767225600000
    )


@pytest.mark.parametrize("durable", [True, False])
def test_consumer_callback_advertises_durable_dedupe(durable: bool) -> None:
    """The bus skips its cache inbox only for callbacks tagged durable."""
    from sandloom.core.consumers.callbacks import build_consumer_callback

    async def handler(_session: Any, _envelope: Any) -> None:
        return None

    callback = build_consumer_callback(
        name="c",
        database="default",
        handler=handler,
        durable=durable,
        opener=lambda _db: None,  # type: ignore[arg-type,return-value]
        metric_processed="p",
        metric_failed="f",
        labels={},
    )
    assert callback.sandloom_durable is durable  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_consumed_line_is_debug_and_failures_stay_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sandloom.core.consumers.callbacks import build_consumer_callback

    @asynccontextmanager
    async def opener(_database: str) -> AsyncGenerator[Any, None]:
        yield object()

    async def handler(_session: Any, envelope: Any) -> None:
        if envelope["payload"].get("fail"):
            raise RuntimeError("boom")

    callback = build_consumer_callback(
        name="c.logs",
        database="default",
        handler=handler,
        durable=False,
        opener=opener,
        metric_processed="p",
        metric_failed="f",
        labels={"consumer": "c.logs"},
    )
    ok = {"id": "ok", "payload": {}}
    caplog.set_level("INFO", logger="sandloom.core.eventing.consumers")
    await callback(ok)
    assert not [r for r in caplog.records if r.getMessage() == "event.consumed"]

    caplog.set_level("DEBUG", logger="sandloom.core.eventing.consumers")
    await callback(ok)
    (line,) = [r for r in caplog.records if r.getMessage() == "event.consumed"]
    assert line.levelname == "DEBUG" and line.event_id == "ok"  # type: ignore[attr-defined]

    caplog.set_level("INFO", logger="sandloom.core.eventing.consumers")
    with pytest.raises(RuntimeError):
        await callback({"id": "bad", "payload": {"fail": True}})
    assert [r.levelname for r in caplog.records if "failed" in r.getMessage()] == [
        "ERROR"
    ]


def test_register_event_consumer_has_one_signature() -> None:
    """``di.register_event_consumer`` takes exactly ``ModuleAPI``'s keywords."""
    import inspect

    from sandloom.core import di
    from sandloom.core.module_api import ModuleAPI

    def params(func: Any, skip: str) -> list[tuple[str, Any, Any]]:
        return [
            (p.name, p.kind, p.default)
            for p in inspect.signature(func).parameters.values()
            if p.name != skip
        ]

    assert params(di.register_event_consumer, "core") == params(
        ModuleAPI.register_event_consumer, "self"
    )


def test_di_register_event_consumer_forwards_every_option(
    core_facade: CoreFacade,
) -> None:
    from sandloom.core.di import register_event_consumer

    register_event_consumer(
        core_facade,
        "billing.subscription.created.v1",
        _noop,
        name="script@sub.created",
        group="legacy-group",
        start_from="latest",
        bind_tenant_from="tenant_id",
        dead_letter=False,
    )
    (descriptor,) = core_facade.event_consumers
    assert descriptor.group == "legacy-group"
    assert descriptor.start_from == "latest"
    assert descriptor.bind_tenant_from == "tenant_id"
    assert descriptor.dead_letter is False
