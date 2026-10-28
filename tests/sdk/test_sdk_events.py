"""Typed events and consumers (spec §5.2, §5.4; ledger G-DECL-01/02, C-DECL-01)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import Field

from sandloom.core.testing import recording_uow, register_event_schemas
from sandloom.sdk import Event, on
from sandloom.sdk._decl.events import consumer_callback, event_role, resolve_role
from sandloom.sdk.errors import (
    ConsumerDefinitionError,
    EventDefinitionError,
    EventMetaUnavailable,
    UnitOfWorkRequiredError,
)
from sandloom.sdk.testing import published


class Shipped(Event, channel="sdkev.order.shipped.v1"):
    order_id: UUID
    tenant_id: UUID


class Mentioned(Event, channel="other.thing.mentioned.v1"):
    tenant_id: UUID
    note: str = ""


def test_channel_must_be_versioned() -> None:
    for channel in ("orders", "Orders.x.v1", "orders.shipped", "orders.x.v0"):
        with pytest.raises(EventDefinitionError, match="SANDLOOM-B015"):
            type("Bad", (Event,), {"__annotations__": {"x": int}}, channel=channel)


@pytest.mark.filterwarnings("ignore:Field name .meta.:UserWarning")
def test_reserved_and_non_wire_fields_are_refused() -> None:
    with pytest.raises(EventDefinitionError, match="SANDLOOM-B015.*'meta'.*reserved"):

        class _Meta(Event, channel="sdkev.a.b.v1"):
            meta: str

    with pytest.raises(EventDefinitionError, match="role"):

        class _Role(Event, channel="sdkev.a.c.v1", role="owner"):  # type: ignore[arg-type]
            x: int


@pytest.mark.parametrize("reserved", ["channel", "meta", "publish", "wire_schema"])
@pytest.mark.filterwarnings("ignore:Field name:UserWarning")
def test_a_reserved_field_name_fails_at_definition(reserved: str) -> None:
    """A field named like the Event API (``channel`` first of all: as a
    field it replaced the class attribute, so the class passed for an
    abstract base and only its consumer failed) is refused at class
    definition, naming the name and the alias fix."""
    with pytest.raises(EventDefinitionError) as info:
        type(
            "Clash",
            (Event,),
            {"__annotations__": {"tenant_id": UUID, reserved: str}},
            channel="sdkev.clash.happened.v1",
        )
    text = str(info.value)
    assert "SANDLOOM-B015" in text and repr(reserved) in text and "reserved" in text
    assert f'Field(alias="{reserved}")' in text


class Notified(Event, channel="sdkev.notice.sent.v1"):
    """A producer whose wire key ``channel`` is an aliased field."""

    tenant_id: UUID
    via: str = Field(alias="channel")
    recipient: str


class NotifiedView(Event, channel="sdkev.notice.sent.v1", role="view"):
    """Another module's view of it, keeping the alias."""

    tenant: UUID = Field(alias="tenant_id")
    via: str = Field(alias="channel")


async def test_an_aliased_field_survives_publish_deliver_and_handler() -> None:
    """Publish serialises by alias, the wire schema names the alias, and a
    consumer (producer class or view) reads the alias back."""
    resolve_role(Notified, "app.modules.sdkev")
    assert "channel" in Notified.wire_schema()["properties"]
    register_event_schemas({Notified.channel: Notified.wire_schema()})
    tenant = uuid4()
    event = Notified(tenant_id=tenant, via="email", recipient="ann@example.com")
    assert Notified(tenant_id=tenant, channel="email", recipient="x").via == "email"
    with recording_uow() as session:
        await event.publish()
    [(channel, payload, _v)] = session.events
    assert channel == Notified.channel
    assert payload["channel"] == "email" and "via" not in payload

    received: list[Event] = []

    @on(Notified)
    async def producer_side(event: Notified) -> None:
        received.append(event)

    @on(NotifiedView)
    async def view_side(event: NotifiedView) -> None:
        received.append(event)

    (view_spec,) = view_side.__sandloom_consumers__  # type: ignore[attr-defined]
    assert view_spec.bind_tenant_from is None  # no `tenant_id` attribute
    envelope: dict[str, Any] = {"id": "evt-a", "channel": channel, "payload": payload}
    for handler in (producer_side, view_side):
        (spec,) = handler.__sandloom_consumers__  # type: ignore[attr-defined]
        await consumer_callback(spec)(None, envelope)
    mine, theirs = received
    assert isinstance(mine, Notified) and mine.model_dump() == event.model_dump()
    assert isinstance(theirs, NotifiedView)
    assert (theirs.via, theirs.tenant) == ("email", tenant)
    [seen] = published(NotifiedView)
    assert (seen.via, seen.tenant) == ("email", tenant)


def test_on_binds_the_tenant_by_its_wire_name() -> None:
    @on(NotifiedView, tenant="tenant")
    async def bound(event: NotifiedView) -> None: ...

    (spec,) = bound.__sandloom_consumers__  # type: ignore[attr-defined]
    assert spec.bind_tenant_from == "tenant_id"


def test_abstract_base_shares_fields() -> None:
    class Base(Event):
        tenant_id: UUID

    class Created(Base, channel="sdkev.item.created.v1"):
        item_id: UUID

    assert set(Created.model_fields) == {"tenant_id", "item_id"}
    assert Created.channel == "sdkev.item.created.v1"


def test_role_is_derived_from_the_channel_prefix() -> None:
    assert resolve_role(Shipped, "app.modules.sdkev") == "producer"
    assert resolve_role(Mentioned, "app.modules.sdkev") == "view"
    assert event_role(Shipped) == "producer"

    class Stated(Event, channel="other.x.y.v1", role="producer"):
        a: int

    with pytest.raises(EventDefinitionError, match="producer outside|defined in"):
        resolve_role(Stated, "app.modules.sdkev")


def test_construction_is_typed_and_strict() -> None:
    with pytest.raises(ValueError):
        Shipped(order_id="not-a-uuid", tenant_id=uuid4())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Shipped(order_id=uuid4(), tenant_id=uuid4(), extra=1)  # type: ignore[call-arg]


def test_meta_is_unavailable_before_delivery() -> None:
    with pytest.raises(EventMetaUnavailable):
        _ = Shipped(order_id=uuid4(), tenant_id=uuid4()).meta


async def test_publish_writes_the_outbox_row_and_is_recorded() -> None:
    # Discovery fixes the role (this file is not a module package); stated
    # here so the test does not depend on another test having resolved it.
    resolve_role(Shipped, "app.modules.sdkev")
    register_event_schemas({Shipped.channel: Shipped.model_json_schema()})
    event = Shipped(order_id=uuid4(), tenant_id=uuid4())
    with recording_uow() as session:
        await event.publish()
    assert [(c, p) for c, p, _v in session.events] == [
        (Shipped.channel, event.model_dump(mode="json"))
    ]
    assert published(Shipped) == [event]
    assert published("sdkev.order.shipped.v1") == [event]


async def test_a_view_cannot_publish() -> None:
    resolve_role(Mentioned, "app.modules.sdkev")
    with recording_uow() as session, pytest.raises(EventDefinitionError, match="view"):
        await Mentioned(tenant_id=uuid4()).publish()
    assert session.events == []
    assert published(Mentioned) == []


def test_on_derives_the_tenant_binding_and_keeps_every_option() -> None:
    @on(Shipped)
    async def first(event: Shipped) -> None: ...

    stamp = datetime(2026, 1, 1, tzinfo=UTC)

    @on(
        Shipped,
        name="ship-2",
        durable=False,
        required=False,
        dead_letter=False,
        start_from=stamp,
        group="g",
        database="analytics",
        tenant=None,
    )
    async def second(event: Shipped) -> None: ...

    (spec1,) = first.__sandloom_consumers__  # type: ignore[attr-defined]
    (spec2,) = second.__sandloom_consumers__  # type: ignore[attr-defined]
    assert (spec1.name, spec1.bind_tenant_from, spec1.durable, spec1.start_from) == (
        "first",
        "tenant_id",
        True,
        "earliest",
    )
    assert (spec2.name, spec2.durable, spec2.required, spec2.dead_letter) == (
        "ship-2",
        False,
        False,
        False,
    )
    assert (spec2.start_from, spec2.group, spec2.database) == (
        stamp.isoformat(),
        "g",
        "analytics",
    )
    assert spec2.bind_tenant_from is None


def test_on_refuses_bad_handlers() -> None:
    with pytest.raises(ConsumerDefinitionError, match="async def"):

        @on(Shipped)
        def sync(event: Shipped) -> None: ...  # type: ignore[arg-type]

    with pytest.raises(ConsumerDefinitionError, match="exactly one"):

        @on(Shipped)
        async def two(event: Shipped, other: int) -> None: ...  # type: ignore[arg-type]

    with pytest.raises(ConsumerDefinitionError, match="not Shipped"):

        @on(Shipped)
        async def wrong(event: Mentioned) -> None: ...  # type: ignore[arg-type]

    with pytest.raises(ConsumerDefinitionError, match="no `order` field|UUID"):
        on(Shipped, tenant="order")
    with pytest.raises(ConsumerDefinitionError, match="Event class"):
        on(Event)


async def test_callback_decodes_the_payload_and_attaches_meta() -> None:
    received: list[Mentioned] = []

    @on(Mentioned)
    async def handle(event: Mentioned) -> None:
        received.append(event)

    (spec,) = handle.__sandloom_consumers__  # type: ignore[attr-defined]
    callback = consumer_callback(spec)
    assert callback.__qualname__ == handle.__qualname__
    tenant = uuid4()
    envelope: dict[str, Any] = {
        "id": "evt-1",
        "channel": Mentioned.channel,
        "schema_version": "v1",
        "trace_id": "t-1",
        "emitted_at": "2026-10-25T00:00:00+00:00",
        "payload": {"tenant_id": str(tenant), "note": "hi", "added_later": 1},
    }
    await callback(None, envelope)
    (event,) = received
    assert event.tenant_id == tenant and event.note == "hi"
    assert event.meta.id == "evt-1"
    assert event.meta.trace_id == "t-1"
    assert event.meta.channel == Mentioned.channel
    assert "meta" not in event.model_dump()


async def test_capture_effects_records_typed_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``capture_effects`` records ``Event.publish()`` made from the listed
    modules (no unit needed) and forwards any other call unchanged."""
    import json
    import sys

    from sandloom.sdk.testing import capture_effects

    resolve_role(Shipped, "app.modules.sdkev")
    event = Shipped(order_id=uuid4(), tenant_id=uuid4())
    calls = capture_effects(monkeypatch, sys.modules[__name__])
    await event.publish()
    assert calls["events"] == [(Shipped.channel, event.model_dump(mode="json"))]

    monkeypatch.undo()
    others = capture_effects(monkeypatch, json)
    with pytest.raises(UnitOfWorkRequiredError):
        await event.publish()  # not from a listed module: the real outbox path
    assert others["events"] == []


def test_the_wire_schema_requires_defaults_and_accepts_added_fields() -> None:
    """A producer's registered schema is its serialization schema: a
    defaulted field (always emitted) is required; it is open to fields a
    newer producer adds on the same ``.vN`` (a reader in another process must
    not break), while construction stays strict."""
    import jsonschema

    class Stamped(Event, channel="sdkev.order.stamped.v1"):
        order_id: UUID
        occurred_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    schema = Stamped.wire_schema()
    assert set(schema["required"]) == {"order_id", "occurred_at"}
    assert "additionalProperties" not in schema
    newer = {"order_id": str(uuid4()), "occurred_at": "2026-01-01T00:00:00Z"}
    jsonschema.validate({**newer, "added_later": 1}, schema)
    with pytest.raises(ValueError):
        Stamped(order_id=uuid4(), added_later=1)  # type: ignore[call-arg]
