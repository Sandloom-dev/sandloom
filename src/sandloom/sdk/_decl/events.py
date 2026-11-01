"""Typed events and consumers (L1, spec §2.3, §5.2, §5.4).

``class E(Event, channel="m.e.v1")`` declares an event: a **producer** when
the channel's first segment is the short name of the module that defines it,
else a **view** (``role="producer"`` / ``role="view"`` state it). Discovery
registers a producer's ``model_json_schema()``; ``await E(...).publish()``
publishes it through the outbox (``publish_event``); ``@on(E)`` registers a
consumer with every ``register_event_consumer`` option. The envelope is
unchanged; delivery metadata is ``event.meta`` (never part of the payload).
"""

from __future__ import annotations

import inspect
import re
import threading
import typing
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, PrivateAttr
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import SchemaValidator, core_schema

from sandloom.sdk._decl import _event_wire, _recording
from sandloom.sdk._decl._owner import module_package, short_name
from sandloom.sdk._decl.errors import (
    ConsumerDefinitionError,
    EventDefinitionError,
    EventMetaUnavailable,
)
from sandloom.sdk._prim.events import EventEnvelope, publish_event
from sandloom.sdk._prim.tenant import current_tenant_id_optional

EventRole = Literal["auto", "producer", "view"]

#: ``<module>.<name>[.<name>...].vN``: lower-case segments, a version last.
_CHANNEL = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+\.v[1-9][0-9]*$")
#: Names an event field may not take (they are the event's own API): the
#: ``channel`` class attribute, ``meta`` and the methods of ``Event``.
_RESERVED = frozenset({"channel", "meta", "publish", "wire_schema"})

#: The wire-schema annotation of a property whose field has a default of
#: any kind (literal or factory). The kernel's compatibility rules read it
#: as ``sandloom.core.contracts.schema_registry.DEFAULTED`` (the SDK may not
#: import that module; ``test_sdk_schema_differential`` pins the two).
_DEFAULTED = "x-sandloom-default"
#: Schema keywords whose value maps names to subschemas (a key there is a
#: name, never a keyword).
_NAMED_SUBSCHEMAS = frozenset(
    {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
)
#: Schema keywords whose value is data, not a subschema.
_VALUE_KEYWORDS = frozenset({"default", "examples", "const", "enum"})

_FIX_CHANNEL = (
    "name the channel <module>.<event>.vN (lower case), e.g. notes.note.created.v1"
)


@dataclass(frozen=True)
class EventMeta:
    """Delivery metadata of an event; mirrors the envelope, never part of
    the payload (``event.meta`` on a delivered event)."""

    id: str
    channel: str
    schema_version: str
    trace_id: str | None
    emitted_at: str
    tenant_id: UUID | None


class Event(BaseModel):
    """Base class of typed events: ``class E(Event, channel=..., role="auto")``.

    ::

        class NoteCreated(Event, channel="notes.note.created.v1"):
            note_id: UUID
            tenant_id: UUID

        await NoteCreated(note_id=note.id, tenant_id=note.tenant_id).publish()

    A class without ``channel`` is an abstract base (shared fields).
    """

    # extra="forbid": construction is strict (a typo is an error at the
    # producer). The registered schema is the SERIALIZATION schema
    # (:func:`wire_schema`): a defaulted field is always emitted, so readers
    # see it as required.
    # A field may carry a wire name (``Field(alias="channel")``): it is
    # published under the alias, a delivery is read by the alias, and the
    # producer constructs it by either name.
    # allow_inf_nan=False: JSON has no NaN / Infinity, so an event's floats
    # are finite (a DTO in it states the same: SANDLOOM-B015).
    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        allow_inf_nan=False,
    )

    channel: ClassVar[str]
    __sandloom_role__: ClassVar[EventRole] = "auto"
    __sandloom_resolved_role__: ClassVar[Literal["producer", "view"] | None] = None

    _sandloom_meta: EventMeta | None = PrivateAttr(default=None)

    def __init_subclass__(
        cls, *, channel: str | None = None, role: EventRole = "auto", **kwargs: Any
    ) -> None:
        super().__init_subclass__(**kwargs)
        if role not in ("auto", "producer", "view"):
            raise EventDefinitionError(
                f"{cls.__qualname__}: role={role!r} is not auto, producer or view",
                fix='leave role="auto" (derived from the channel) or state it',
            )
        if channel is None:
            if role != "auto":
                raise EventDefinitionError(
                    f"{cls.__qualname__}: role= needs a channel=",
                    fix=_FIX_CHANNEL,
                )
            return
        if not isinstance(channel, str) or not _CHANNEL.match(channel):
            raise EventDefinitionError(
                f"{cls.__qualname__}: channel {channel!r} is not versioned",
                fix=_FIX_CHANNEL,
            )
        cls.channel = channel
        cls.__sandloom_role__ = role
        cls.__sandloom_resolved_role__ = None

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        # Checked on every subclass, abstract bases included: a ``channel``
        # field replaces the class attribute, so the class would otherwise
        # pass for an abstract base and its consumers fail far from here.
        clash = sorted(_RESERVED & set(cls.model_fields))
        if clash:
            name = clash[0]
            raise EventDefinitionError(
                f"{cls.__qualname__}: the field name(s) {clash} are reserved: "
                f"{', '.join(sorted(_RESERVED))} are the Event API "
                "(the class's channel, delivery metadata, its methods)",
                fix=f"pick another name, or keep {name!r} as the wire key with "
                f'an alias: `{name}_: str = Field(alias="{name}")` (published '
                "and delivered as the alias; Python code reads the attribute)",
            )
        if not is_concrete(cls):
            return
        if not cls.__pydantic_complete__:  # defer_build, a forward reference
            try:
                cls.model_rebuild()
            except Exception as exc:
                raise EventDefinitionError(
                    f"{cls.__qualname__}: a field's type is not defined yet "
                    f"({exc}); events are checked at definition",
                    fix="define every DTO before the event class that uses it",
                ) from exc
        prepare(cls)  # the closed set of wire shapes, then the reader
        try:
            schemas = [
                cls.model_json_schema(),
                cls.model_json_schema(mode="serialization"),
            ]
        except Exception as exc:
            raise EventDefinitionError(
                f"{cls.__qualname__}: a field is not a wire type ({exc})",
                fix=_event_wire.FIX_TYPES,
            ) from exc
        if _walk_schemas(schemas, _carries_mark):
            # The compatibility rule trusts the mark to mean "has a default"
            # (round 8): only the wire schema's generator writes it.
            raise EventDefinitionError(
                f"{cls.__qualname__}: a json_schema_extra sets {_DEFAULTED!r}, "
                "the framework's mark of a field with a default",
                fix=f"drop {_DEFAULTED!r} from json_schema_extra: wire_schema() "
                "marks every field that has a default (give the field a "
                "default to make it optional)",
            )

    async def publish(self, *, database: str | None = None) -> None:
        """Publish this event in the running unit (outbox, spec §5.2).

        Allowed on a producer only; a view (another module's channel) is
        refused (SANDLOOM-C025). Raises ``UnitOfWorkRequiredError`` outside a unit.
        """
        cls = type(self)
        if not is_concrete(cls):
            raise EventDefinitionError(
                f"{cls.__qualname__} has no channel and cannot be published",
                fix=_FIX_CHANNEL,
            )
        if event_role(cls) == "view":
            raise EventDefinitionError(
                f"{cls.__qualname__} is a view of {cls.channel!r} (another "
                "module's channel); only the producing module publishes it",
                fix="publish your own channel, or call the owner through a contract",
            )
        await publish_event(cls.channel, to_wire(self), database=database)
        _recording.record(self)

    @classmethod
    def wire_schema(cls) -> dict[str, Any]:
        """The schema a producer registers and exports (the L2 form is
        ``core.register_event_schema(C, E.__name__, E.wire_schema())``)."""
        return wire_schema(cls)

    @property
    def meta(self) -> EventMeta:
        """Delivery metadata (``EventMetaUnavailable`` before delivery)."""
        meta = self._sandloom_meta
        if meta is None:
            raise EventMetaUnavailable(
                f"{type(self).__qualname__}.meta is available on delivered "
                "events only (in an @on consumer)"
            )
        return meta


E = TypeVar("E", bound=Event)


def wire_name(cls: type[Event], field: str) -> str:
    """The payload key of ``field``: its alias, else its name."""
    info = cls.model_fields[field]
    return info.serialization_alias or info.alias or field


#: The reader of each Event class (:func:`prepare`), built at definition,
#: with the core schema it was built from: a class rebuilt since
#: (``model_rebuild`` after a configuration change) has a new schema, so
#: its reader is checked and built again on the next decode.
_READERS: weakref.WeakKeyDictionary[type[Event], tuple[Any, SchemaValidator]] = (
    weakref.WeakKeyDictionary()
)
_READERS_LOCK = threading.RLock()


def to_wire(event: Event) -> dict[str, Any]:
    """The payload ``publish()`` emits: JSON values under the wire names."""
    return event.model_dump(mode="json", by_alias=True)


def from_wire(cls: type[E], payload: Mapping[str, Any]) -> E:
    """Decode a delivered payload into ``cls`` (a producer or a view): keys
    the class does not declare are ignored at EVERY depth (a newer
    producer's additive field, spec §5.2), each field is read by its wire
    name.

    One validation with the class's reader (:func:`prepare`): the class's
    own validation schema in which every model that forbids extra keys
    ignores them, so discriminators, aliases and validators behave as in
    the class, every validator runs once, a real error is raised as it is,
    and every value is an instance of its declared class. Construction
    stays strict: only the reader ignores.

    ``payload`` is JSON data (the envelope's decoded payload: dicts, lists
    and JSON scalars). A DTO instance in it is refused (``model_type``),
    where the class's own validator would take it as it is: no delivery
    carries one."""
    entry = _READERS.get(cls)
    if entry is None or entry[0] is not cls.__pydantic_core_schema__:
        return cast("E", prepare(cls).validate_python(payload))
    return cast("E", entry[1].validate_python(payload))


def prepare(cls: type[Event]) -> SchemaValidator:
    """Check ``cls`` against the closed set of wire shapes and build its
    reader, once per core schema of the class, at class definition (and
    again after a rebuild); SANDLOOM-B015 names the first field outside the set
    and its allowed alternative. No class is modified, so readers build
    concurrently; a lost entry is rebuilt on a decode."""
    with _READERS_LOCK:
        schema = cls.__pydantic_core_schema__
        entry = _READERS.get(cls)
        if entry is not None and entry[0] is schema:
            return entry[1]
        try:
            _event_wire.check_wire_shapes(cls)
        except _event_wire.ShapeError as exc:
            raise EventDefinitionError(
                f"{exc.where}: {exc.problem} (an event payload is plain data: "
                "modules/between/events.md, What an event field may be)",
                fix=exc.fix,
            ) from None
        _event_wire.self_check()
        reader = _event_wire.compile_reader(cls)
        _READERS[cls] = (schema, reader)
        return reader


def wire_schema(cls: type[Event]) -> dict[str, Any]:
    """The JSON schema a producer registers, snapshots and exports.

    The serialization schema (what ``publish()`` emits: a defaulted field
    such as ``occurred_at`` is always present, so it is required, and marked
    ``_DEFAULTED`` -- its reader fills it when an older payload omits it),
    with no ``additionalProperties: false`` anywhere, nested DTOs included:
    the reader (``from_wire``) ignores a newer producer's added optional
    field at every depth (the additive rule within a ``.vN``, SANDLOOM-T005),
    even in a type declared ``extra="forbid"``; construction stays strict.
    """
    schema = cls.model_json_schema(mode="serialization", schema_generator=_WireSchema)
    _walk_schemas(schema, _open)
    return schema


class _WireSchema(GenerateJsonSchema):
    """Marks every field with a default of any kind -- a literal or a
    factory -- with ``_DEFAULTED``: the serialization schema lists such a
    field as required (always emitted) and a factory default carries no
    ``default`` keyword, yet the reader fills it, so the compatibility rule
    (spec §5.2, G-DECL-01) treats its addition as an optional field."""

    def default_schema(self, schema: core_schema.WithDefaultSchema) -> JsonSchemaValue:
        json_schema = super().default_schema(schema)
        if "default" in schema or "default_factory" in schema:
            json_schema[_DEFAULTED] = True
        return json_schema

    def decimal_schema(self, schema: core_schema.DecimalSchema) -> JsonSchemaValue:
        # A Decimal is published as a string; without a format a retyping
        # to ``str`` would pass the compatibility rule while an old Decimal
        # reader rejects the new producer's "abc" (round 10). The format
        # makes it a type of its own (formats are compared, not validated).
        json_schema = super().decimal_schema(schema)
        if json_schema.get("type") == "string":
            json_schema["format"] = "decimal"
        return json_schema


def _walk_schemas(node: Any, visit: Callable[[dict[str, Any]], bool | None]) -> bool:
    """Call ``visit`` on every subschema of ``node`` (named subschemas and
    applicators followed, data keywords such as ``default`` skipped); true
    when any call returned true."""
    if isinstance(node, list):
        children: list[Any] = node
        found = False
    elif isinstance(node, dict):
        found = bool(visit(node))
        children = []
        for key, value in node.items():
            if key in _VALUE_KEYWORDS:
                continue
            named = key in _NAMED_SUBSCHEMAS and isinstance(value, dict)
            children.extend(value.values() if named else [value])
    else:
        return False
    for child in children:  # every child visited (no short circuit)
        found = _walk_schemas(child, visit) or found
    return found


def _open(schema: dict[str, Any]) -> None:
    """The reader ignores unknown keys at every depth: say so."""
    if schema.get("additionalProperties") is False:
        del schema["additionalProperties"]


def _carries_mark(schema: dict[str, Any]) -> bool:
    return _DEFAULTED in schema


def is_concrete(cls: type[Event]) -> bool:
    """``cls`` declares a channel (else it is an abstract base)."""
    return isinstance(cls.__dict__.get("channel"), str)


def event_role(cls: type[Event]) -> Literal["producer", "view"]:
    """The role of ``cls``: stated, resolved by discovery, or by layout."""
    if cls.__sandloom_role__ != "auto":
        return cls.__sandloom_role__
    resolved = cls.__dict__.get("__sandloom_resolved_role__")
    if resolved is not None:
        return cast(Literal["producer", "view"], resolved)
    package = module_package(cls.__module__)
    return "producer" if cls.channel.split(".", 1)[0] == short_name(package) else "view"


def resolve_role(cls: type[Event], package: str) -> Literal["producer", "view"]:
    """Fix the role of ``cls`` as defined in module ``package`` (discovery)."""
    owner = short_name(package)
    prefix = cls.channel.split(".", 1)[0]
    if cls.__sandloom_role__ == "producer" and prefix != owner:
        raise EventDefinitionError(
            f"{cls.__qualname__} declares role='producer' for {cls.channel!r}, "
            f"a channel of module {prefix!r}, but is defined in {package!r}",
            fix="define the producer in the module that owns the channel; "
            "other modules declare a view",
        )
    role: Literal["producer", "view"] = (
        cls.__sandloom_role__
        if cls.__sandloom_role__ != "auto"
        else ("producer" if prefix == owner else "view")
    )
    cls.__sandloom_resolved_role__ = role
    return role


# --------------------------------------------------------------------------- #
# Consumers
# --------------------------------------------------------------------------- #

#: The attribute ``@on`` records its declarations in (one per ``@on``).
CONSUMERS_ATTR = "__sandloom_consumers__"


@dataclass(frozen=True)
class ConsumerSpec:
    """One ``@on(E)`` declaration (the ``register_event_consumer`` options)."""

    event: type[Event]
    handler: Callable[..., Awaitable[None]]
    name: str
    durable: bool
    required: bool
    dead_letter: bool
    start_from: str
    group: str | None
    database: str
    bind_tenant_from: str | None


def _uuid_field(event: type[Event], field: str) -> bool:
    info = event.model_fields.get(field)
    return info is not None and info.annotation is UUID


def _check_handler(event: type[Event], fn: Callable[..., Any]) -> None:
    where = getattr(fn, "__qualname__", repr(fn))
    if not inspect.iscoroutinefunction(fn):
        raise ConsumerDefinitionError(
            f"@on({event.__name__}) {where}: the handler must be `async def`",
            fix=f"async def {getattr(fn, '__name__', 'handler')}(event: "
            f"{event.__name__}) -> None",
        )
    params = list(inspect.signature(fn).parameters.values())
    if len(params) != 1 or params[0].kind not in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        raise ConsumerDefinitionError(
            f"@on({event.__name__}) {where}: the handler takes exactly one "
            "parameter, the event",
            fix=f"async def handler(event: {event.__name__}) -> None",
        )
    try:
        hints = typing.get_type_hints(fn)
    except (NameError, TypeError, AttributeError):  # unresolvable forward ref
        hints = {}
    annotation = hints.get(params[0].name, params[0].annotation)
    if annotation is inspect.Parameter.empty:
        return
    if annotation is event or annotation == event.__name__:
        return
    raise ConsumerDefinitionError(
        f"@on({event.__name__}) {where}: the parameter is annotated "
        f"{annotation!r}, not {event.__name__}",
        fix=f"annotate the parameter as {event.__name__}",
    )


def on(
    event: type[E],
    *,
    name: str | None = None,
    durable: bool = True,
    required: bool = True,
    dead_letter: bool = True,
    start_from: Literal["earliest", "latest"] | datetime = "earliest",
    group: str | None = None,
    database: str = "default",
    tenant: str | None = "derive",
) -> Callable[[Callable[[E], Awaitable[None]]], Callable[[E], Awaitable[None]]]:
    """Register the decorated coroutine as a consumer of ``event`` (§5.4).

    ::

        @on(NoteCreated)
        async def count_note(event: NoteCreated) -> None:
            await counters.increment(event.tenant_id)

    Each delivery runs in its own unit (``db()``), claimed once per envelope
    id with ``durable=True``. ``tenant="derive"`` binds the tenant from a
    ``tenant_id: UUID`` field; ``tenant="<field>"`` names another UUID field;
    ``tenant=None`` binds none. ``name`` defaults to the function name.
    """
    if not (isinstance(event, type) and issubclass(event, Event)) or not is_concrete(
        event
    ):
        raise ConsumerDefinitionError(
            f"@on({event!r}): expects an Event class with a channel",
            fix="pass the event class: @on(NoteCreated)",
        )
    # The kernel reads the tenant from the payload, so ``bind`` is the
    # field's wire name.
    if tenant == "derive":
        bind = (
            wire_name(event, "tenant_id") if _uuid_field(event, "tenant_id") else None
        )
    elif tenant is None:
        bind = None
    elif _uuid_field(event, tenant):
        bind = wire_name(event, tenant)
    else:
        raise ConsumerDefinitionError(
            f"@on({event.__name__}, tenant={tenant!r}): {event.__name__} has no "
            f"`{tenant}: UUID` field",
            fix="name a UUID field of the event, or tenant=None",
        )
    origin = start_from.isoformat() if isinstance(start_from, datetime) else start_from

    def decorate(fn: Callable[[E], Awaitable[None]]) -> Callable[[E], Awaitable[None]]:
        _check_handler(event, fn)
        spec = ConsumerSpec(
            event=event,
            handler=fn,
            name=name or str(getattr(fn, "__name__", "consumer")),
            durable=durable,
            required=required,
            dead_letter=dead_letter,
            start_from=origin,
            group=group,
            database=database,
            bind_tenant_from=bind,
        )
        declared = list(getattr(fn, CONSUMERS_ATTR, ()))
        declared.append(spec)
        setattr(fn, CONSUMERS_ATTR, tuple(declared))
        return fn

    return decorate


def consumer_callback(
    spec: ConsumerSpec,
) -> Callable[[Any, EventEnvelope], Awaitable[None]]:
    """The L2 handler ``async (session, envelope)`` of an ``@on`` consumer:
    decodes ``envelope["payload"]`` into the event (``from_wire``: unknown
    keys ignored, aliased fields read by their wire name) and attaches
    ``EventMeta``."""
    event_cls = spec.event
    handler = spec.handler

    async def callback(session: Any, envelope: EventEnvelope) -> None:
        event = from_wire(event_cls, envelope.get("payload") or {})
        event._sandloom_meta = EventMeta(
            id=str(envelope.get("id", "")),
            channel=str(envelope.get("channel", event_cls.channel)),
            schema_version=str(
                envelope.get("schema_version", event_cls.channel.rsplit(".", 1)[-1])
            ),
            trace_id=cast("str | None", envelope.get("trace_id")),
            emitted_at=str(envelope.get("emitted_at", "")),
            tenant_id=current_tenant_id_optional(),
        )
        await handler(event)

    # Named after the handler (no ``__wrapped__``: the kernel sees the
    # ``(session, envelope)`` signature it calls).
    callback.__module__ = handler.__module__
    callback.__name__ = getattr(handler, "__name__", "callback")
    callback.__qualname__ = getattr(handler, "__qualname__", callback.__name__)
    callback.__doc__ = handler.__doc__
    return callback


__all__ = [
    "CONSUMERS_ATTR",
    "ConsumerSpec",
    "Event",
    "EventMeta",
    "EventRole",
    "consumer_callback",
    "event_role",
    "from_wire",
    "is_concrete",
    "on",
    "prepare",
    "resolve_role",
    "to_wire",
    "wire_name",
    "wire_schema",
]
