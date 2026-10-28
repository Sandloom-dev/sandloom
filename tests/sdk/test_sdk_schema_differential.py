"""Differential check of the event-schema compatibility rule (G-DECL-01).

On the same ``.vN`` producers and consumers deploy independently, so
``schema_problems(old, new) == []`` claims BOTH directions: a reader of
``new`` accepts every payload a producer of ``old`` may still send (an old
producer, a new consumer), and a reader of ``old`` accepts every payload a
producer of ``new`` may send (a new producer, an old consumer). These tests
hold it to that claim: a corpus of schema pairs -- real pydantic-emitted
wire schemas, each mutated with every narrowing and loosening keyword at
every subschema position and under every applicator, field removals and
models open to extra keys included -- is compared, and for each pair the
rule calls compatible, payloads sampled from ``old`` are validated by the
framework validator (``compile_payload_validator``) of ``new`` and payloads
sampled from ``new`` by that of ``old``: one a producer may send and the
other side's reader rejects is an escape.

The producer model the rule encodes is part of the oracle: a DTO producer
sends only its declared fields (so an added optional field is compatible),
while a dictionary, a model open to extra keys (``extra="allow"``:
``additionalProperties`` true or a schema), or a node below an overlaying
applicator, may hold any key. The spec's one deliberate exception -- a new
defaulted field (literal or factory, marked ``DEFAULTED``) is listed as
``required`` by the serialization schema, yet the reader fills it -- is not
mutated here; it is checked with the real readers (``from_wire``) below and
in ``test_sdk_generated``.
"""

from __future__ import annotations

import copy
import enum
import functools
import json
import pathlib
import random
import re
import types
import typing
import zlib
from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import (
    AliasChoices,
    AliasPath,
    AnyUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    FailFast,
    Field,
    Json,
    PlainSerializer,
    PlainValidator,
    RootModel,
    SerializeAsAny,
    StringConstraints,
    Tag,
    computed_field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic.dataclasses import dataclass as pydantic_dataclass
from typing_extensions import TypedDict

from sandloom.core.cli.generated import schema_problems
from sandloom.core.contracts.payload_validation import compile_payload_validator
from sandloom.core.contracts.schema_registry import DEFAULTED

Schema = dict[str, Any]
Mutation = tuple[str, Callable[[Schema], None]]

# --------------------------------------------------------------------------- #
# Base schemas: what pydantic emits for real event payloads
# --------------------------------------------------------------------------- #


class _Line(BaseModel):
    sku: typing.Annotated[str, StringConstraints(max_length=4)]
    qty: typing.Annotated[int, Field(ge=1)]
    note: str | None = None


class _Node(BaseModel):
    name: str
    children: list[_Node] = []


class _Open(BaseModel):
    """Open to extra keys of any value (``additionalProperties: true``)."""

    model_config = ConfigDict(extra="allow")
    name: str
    level: int = 0
    #: required, any value: its removal is the round-6 regression (the new
    #: producer omits it, the old reader requires it)
    metadata: typing.Any


class _OpenTyped(BaseModel):
    """Open to extra keys of one type (``additionalProperties: {schema}``)."""

    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, int] = Field(init=False)
    name: str


def _bases() -> dict[str, Schema]:
    from sandloom.sdk import Event

    class Order(Event, channel="sdkdiff.probe.order.v1"):
        total: typing.Annotated[float, Field(ge=0)]
        lines: list[_Line]
        values: dict[str, int]
        kind: Literal["a", "b"]
        pair: tuple[int, str]
        parent: _Line | None = None
        count: int = 0

    class Bag(Event, channel="sdkdiff.probe.bag.v1"):
        extra: dict[str, typing.Any]
        tags: set[str]
        keyed: dict[typing.Annotated[str, StringConstraints(max_length=3)], float]
        step: typing.Annotated[int, Field(multiple_of=2, le=10)]

    class Tree(Event, channel="sdkdiff.probe.tree.v1"):
        root: _Node

    class Loose(Event, channel="sdkdiff.probe.loose.v1"):
        info: _Open
        counts: _OpenTyped
        maybe: _Open | None = None

    return {
        "order": Order.wire_schema(),
        "bag": Bag.wire_schema(),
        "tree": Tree.wire_schema(),
        "loose": Loose.wire_schema(),
    }


BASES = _bases()

# --------------------------------------------------------------------------- #
# Positions and mutations
# --------------------------------------------------------------------------- #

Pointer = tuple[str | int, ...]


def _positions(schema: Schema) -> Iterator[Pointer]:
    """Every subschema node of ``schema`` (``$defs`` included, ``$ref`` not
    followed: its target is visited under ``$defs``)."""

    def walk(node: Any, at: Pointer) -> Iterator[Pointer]:
        if not isinstance(node, dict):
            return
        yield at
        for key in ("properties", "$defs", "patternProperties", "dependentSchemas"):
            for name, child in (node.get(key) or {}).items():
                yield from walk(child, (*at, key, name))
        for key in ("items", "additionalProperties", "contains", "then", "else"):
            yield from walk(node.get(key), (*at, key))
        for key in ("prefixItems", "anyOf", "oneOf", "allOf"):
            for index, child in enumerate(node.get(key) or ()):
                yield from walk(child, (*at, key, index))

    yield from walk(schema, ())


def _at(schema: Schema, pointer: Pointer) -> Schema:
    node: Any = schema
    for part in pointer:
        node = node[part]
    return typing.cast(Schema, node)


def _kind(node: Schema) -> str:
    kind = node.get("type")
    if "$ref" in node:
        return "ref"
    if kind == "object" or "properties" in node or "additionalProperties" in node:
        return "object"
    if kind == "array":
        return "array"
    if kind in ("integer", "number"):
        return "number"
    if kind == "string":
        return "string"
    return "any"


def _set(**keywords: Any) -> Callable[[Schema], None]:
    def apply(node: Schema) -> None:
        node.update(copy.deepcopy(keywords))

    return apply


def _require(name: str) -> Callable[[Schema], None]:
    def apply(node: Schema) -> None:
        required = node.get("required", [])
        node["required"] = required if name in required else [*required, name]

    return apply


def _declare(name: str, spec: Schema) -> Callable[[Schema], None]:
    """Declare one more property beside the node's own (an optional field
    added to a model, ``promo``: a key its open producers may already send)."""

    def apply(node: Schema) -> None:
        node["properties"] = {**node.get("properties", {}), name: copy.deepcopy(spec)}

    return apply


def _require_optional(node: Schema) -> None:
    optional = [p for p in node.get("properties", {}) if p not in node["required"]]
    node["required"] = [*node["required"], *optional[:1]]


#: Constraint subschemas placed under every applicator.
_CONSTRAINTS: dict[str, Schema] = {
    "required": {"required": ["total"]},
    "minProperties": {"minProperties": 2},
    "maxProperties": {"maxProperties": 1},
    "property-bound": {"properties": {"total": {"minimum": 5}}},
    "property-type": {"properties": {"total": {"type": "string"}}},
    "dependentRequired": {"dependentRequired": {"promo": ["total"]}},
    "closed": {"additionalProperties": False},
    "propertyNames": {"propertyNames": {"maxLength": 3}},
    "minimum": {"minimum": 3},
    "maxLength": {"maxLength": 1},
    "minItems": {"minItems": 2},
    "type": {"type": "string"},
    "enum": {"enum": [1]},
    "nested-required": {"dependentSchemas": {"promo": {"required": ["total"]}}},
    "empty": {},
}

_DIRECT: dict[str, list[Mutation]] = {
    "object": [
        ("required+total", _require("total")),
        ("required+promo", _require("promo")),
        ("minProperties", _set(minProperties=2)),
        ("maxProperties", _set(maxProperties=1)),
        ("closed", _set(additionalProperties=False)),
        ("open", _set(additionalProperties=True)),
        ("open-typed", _set(additionalProperties={"type": "integer"})),
        ("unevaluated-closed", _set(unevaluatedProperties=False)),
        ("dependentRequired", _set(dependentRequired={"promo": ["total"]})),
        ("propertyNames", _set(propertyNames={"maxLength": 3})),
        ("property-added", _set(properties={"promo": {"type": "integer"}})),
        ("property-declared", _declare("promo", {"type": "integer", "default": 0})),
        ("property-declared-any", _declare("promo", {"default": None})),
        (
            "property-bounded",
            _set(properties={"promo": {"type": "integer", "minimum": 5}}),
        ),
        ("pattern-added", _set(patternProperties={"^p": {"minimum": 5}})),
    ],
    "array": [
        ("minItems", _set(minItems=2)),
        ("maxItems", _set(maxItems=1)),
        ("uniqueItems", _set(uniqueItems=True)),
        ("contains", _set(contains={"type": "integer", "minimum": 3})),
        ("unevaluated-closed", _set(unevaluatedItems=False)),
    ],
    "number": [
        ("minimum", _set(minimum=1)),
        ("maximum", _set(maximum=3)),
        ("exclusiveMinimum", _set(exclusiveMinimum=0)),
        ("exclusiveMaximum", _set(exclusiveMaximum=4)),
        ("multipleOf", _set(multipleOf=3)),
        ("enum", _set(enum=[0, 1])),
        ("const", _set(const=1)),
    ],
    "string": [
        ("minLength", _set(minLength=2)),
        ("maxLength", _set(maxLength=1)),
        ("pattern", _set(pattern="^a")),
        ("enum", _set(enum=["a"])),
        ("const", _set(const="a")),
    ],
    "ref": [
        ("ref+minProperties", _set(minProperties=3)),
        ("ref+required", _require("promo")),
        ("ref+property", _set(properties={"name": {"maxLength": 1}})),
    ],
    "any": [("type", _set(type="string")), ("minimum", _set(minimum=3))],
}


def _wrapped(constraint: Schema) -> list[Mutation]:
    """``constraint`` under every applicator (and two applicators deep)."""
    c = copy.deepcopy(constraint)
    if_promo = {"required": ["promo"]}
    return [
        ("allOf", _set(allOf=[c])),
        ("then", _set(**{"if": if_promo, "then": c})),
        ("else", _set(**{"if": if_promo, "else": c})),
        ("dependentSchemas", _set(dependentSchemas={"promo": c})),
        ("dependentSchemas.allOf", _set(dependentSchemas={"promo": {"allOf": [c]}})),
        ("allOf.then", _set(allOf=[{"if": if_promo, "then": c}])),
        ("contains", _set(contains=c)),
        ("not", _set(**{"not": c})),
    ]


def _loosenings(node: Schema) -> list[Mutation]:
    """Remove each validation keyword, each ``required`` name and each
    declared property."""
    out: list[Mutation] = []
    for key in list(node):
        if key in ("title", "description", "default", DEFAULTED, "$defs"):
            continue

        def drop(target: Schema, key: str = key) -> None:
            target.pop(key, None)

        out.append((f"-{key}", drop))
    if node.get(DEFAULTED) is True:

        def undefault(target: Schema) -> None:
            # the field's default removed (round 8): readers of the result
            # require a key a producer from before the field omits
            target.pop(DEFAULTED, None)
            target.pop("default", None)

        out.append(("-default", undefault))
    for name in node.get("required") or ():

        def unrequire(target: Schema, name: str = name) -> None:
            target["required"] = [n for n in target["required"] if n != name]

        out.append((f"-required.{name}", unrequire))
    for name in node.get("properties") or ():

        def remove(target: Schema, name: str = name) -> None:
            # a field removed (its other direction: a field added, required
            # when it was): on a closed DTO and on a model open to extras
            target["properties"] = {
                n: s for n, s in target["properties"].items() if n != name
            }
            if "required" in target:
                target["required"] = [n for n in target["required"] if n != name]

        out.append((f"-property.{name}", remove))
    return out


def _mutations(node: Schema) -> list[Mutation]:
    kind = _kind(node)
    out = list(_DIRECT.get(kind, ()))
    if kind == "object" and "required" in node:
        out.append(("required+optional", _require_optional))
    for name, constraint in _CONSTRAINTS.items():
        out += [(f"{where}:{name}", m) for where, m in _wrapped(constraint)]
    return out + _loosenings(node)


def _mutated(schema: Schema, pointer: Pointer, mutation: Callable[..., None]) -> Schema:
    out = copy.deepcopy(schema)
    mutation(_at(out, pointer))
    return out


def _corpus() -> Iterator[tuple[str, Schema, Schema]]:
    """``(label, old, new)``: each mutation in both directions (a defaulted
    field's removal one way only: its other direction is the spec's
    defaulted-field exception)."""
    for base_name, base in BASES.items():
        for pointer in _positions(base):
            node = _at(base, pointer)
            for name, mutation in _mutations(node):
                changed = _mutated(base, pointer, mutation)
                label = f"{base_name}:/{'/'.join(map(str, pointer))}:{name}"
                yield f"{label} (narrow)", base, changed
                removed = name.removeprefix("-property.")
                if removed != name and DEFAULTED in node["properties"][removed]:
                    continue
                yield f"{label} (loosen)", changed, base


# --------------------------------------------------------------------------- #
# Payloads a producer of the old schema may send
# --------------------------------------------------------------------------- #

_KEYS = ("promo", "total", "a", "p1", "abcd", "x")
_STRINGS = ("", "a", "b", "ab", "abc", "promo", "total", "a1", "xyzzy")
_INTEGERS = (-2, -1, 0, 1, 2, 3, 4, 5, 6, 9, 10)
#: Strings of each format a producer emits (the framework validator does not
#: check formats; the rule compares them as types, the reader parses them).
_FORMATS: dict[Any, tuple[str, ...]] = {
    "uuid": ("00000000-0000-0000-0000-000000000001", str(UUID(int=7))),
    "date-time": ("2026-01-01T00:00:00Z", "2026-10-27T12:30:00+02:00"),
    "date": ("2026-01-01", "1999-12-31"),
    "time": ("01:00:00", "23:59:59.5"),
    "duration": ("P1D", "PT1H30M", "PT0S"),
    "decimal": ("0", "1.5", "-2.25", "10"),
}
_ANY: tuple[Any, ...] = (
    None,
    True,
    0,
    1,
    5,
    0.5,
    "a",
    "abc",
    [],
    [1],
    {},
    {"promo": 1},
    {"promo": 1, "total": 1},
    {"total": "x"},
)


@functools.lru_cache(maxsize=65536)
def _validator(text: str) -> Any:
    return compile_payload_validator(json.loads(text))


class _Producer:
    """Random payloads shaped by a schema the way a producer shapes them: a
    DTO with its declared fields only; a dictionary, or a model open to
    extra keys (``additionalProperties`` true or a schema), with undeclared
    keys too."""

    def __init__(self, root: Schema, rng: random.Random) -> None:
        self.root = root
        self.defs = root.get("$defs") or {}
        self.rng = rng
        #: Compiled validators by node identity (the documents stay alive).
        self.validators: dict[int, Any] = {}

    def _target(self, node: Schema) -> Schema:
        while isinstance(node.get("$ref"), str):
            ref = node["$ref"].rsplit("/", 1)[-1]
            node = {**self.defs[ref], **{k: v for k, v in node.items() if k != "$ref"}}
        return node

    def valid(self, node: Schema, depth: int) -> Any:
        """A value of ``node``; retried until ``node`` (with the document's
        ``$defs``) accepts it, so a whole payload is valid often."""
        validator = self.validators.get(id(node))
        if validator is None:
            standalone = {**node, "$defs": self.defs} if self.defs else node
            validator = self.validators[id(node)] = _validator(
                json.dumps(standalone, sort_keys=True)
            )
        value = self.value(node, depth)
        for _ in range(4 if depth < 2 else 1):
            if validator.error(value) is None:
                return value
            value = self.value(node, depth)
        return value

    def value(self, node: Schema, depth: int) -> Any:
        rng = self.rng
        node = self._target(node)
        if "const" in node:
            return copy.deepcopy(node["const"])
        if "enum" in node:
            return copy.deepcopy(rng.choice(node["enum"]))
        branches = node.get("anyOf") or node.get("oneOf")
        if branches:
            return self.value(rng.choice(branches), depth)
        kind = node.get("type")
        if isinstance(kind, list):
            kind = rng.choice(kind)
        if kind is None:
            kind = {"object": "object", "array": "array"}.get(_kind(node), "any")
        if kind == "object":
            return self._object(node, depth)
        if kind == "array":
            return self._array(node, depth)
        if kind == "integer":
            return rng.choice(_INTEGERS)
        if kind == "number":
            return rng.choice((*_INTEGERS, 0.5, 2.5))
        if kind == "string":
            return rng.choice(_FORMATS.get(node.get("format"), _STRINGS))
        if kind == "boolean":
            return rng.random() < 0.5
        if kind == "null":
            return None
        return copy.deepcopy(rng.choice(_ANY))

    def _object(self, node: Schema, depth: int) -> dict[str, Any]:
        rng = self.rng
        out: dict[str, Any] = {}
        props = node.get("properties") or {}
        required = set(node.get("required") or ())
        for name, spec in props.items():
            if name in required or (depth < 3 and rng.random() < 0.5):
                out[name] = self.valid(spec, depth + 1)
        extra = node.get("additionalProperties", True)
        patterns = node.get("patternProperties") or {}
        is_dto = (
            "properties" in node and "additionalProperties" not in node and not patterns
        )
        if is_dto or extra is False:
            return out
        for key in rng.sample(_KEYS, rng.randint(0, 3)):
            if key in props:
                continue
            spec = next(
                (s for p, s in patterns.items() if re.search(p, key)),
                extra if isinstance(extra, dict) else {},
            )
            out[key] = self.valid(spec, depth + 1)
        return out

    def _array(self, node: Schema, depth: int) -> list[Any]:
        rng = self.rng
        out = [self.valid(s, depth + 1) for s in node.get("prefixItems") or ()]
        items = node.get("items")
        if isinstance(items, dict):
            low = node.get("minItems", 0)
            high = max(low, min(node.get("maxItems", 2), 2 if depth < 3 else 0))
            out += [self.valid(items, depth + 1) for _ in range(rng.randint(low, high))]
        if node.get("uniqueItems"):  # a set: its producer sends no duplicate
            out = list({json.dumps(v, sort_keys=True): v for v in out}.values())
        return out


def _reader(schema: Schema) -> Schema:
    """What a reader of ``schema`` accepts: ``schema`` with every defaulted
    property (``DEFAULTED``) dropped from ``required`` -- the reader fills
    it -- at every depth (round 8)."""
    out = copy.deepcopy(schema)

    def relax(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict) and isinstance(node.get("required"), list):
                node["required"] = [
                    n
                    for n in node["required"]
                    if not (isinstance(props.get(n), dict) and props[n].get(DEFAULTED))
                ]
            for child in node.values():
                relax(child)
        elif isinstance(node, list):
            for child in node:
                relax(child)

    relax(out)
    return out


def _escape(
    old: Schema, new: Schema, seed: int, samples: int, *, omitting: bool = True
) -> Any | None:
    """A payload a producer of ``old`` may send that a reader of ``new``
    rejects, if one of ``samples`` sampled from ``old`` is.

    Readers fill defaulted fields (``_reader``). ``omitting``: the producer
    may omit a defaulted field, as one from before the field was added did
    (round 8) -- true when ``old`` is the earlier schema; a producer of the
    later one emits every field (an earlier schema that requires a field
    without a default was always sent it)."""
    sampled = _reader(old) if omitting else old
    producer = _Producer(sampled, random.Random(seed))
    before = _validator(json.dumps(_reader(old), sort_keys=True))
    after = _validator(json.dumps(_reader(new), sort_keys=True))
    for _ in range(samples):
        payload = producer.valid(sampled, 0)
        if before.error(payload) is None and after.error(payload) is not None:
            return payload
    return None


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

CORPUS = list(_corpus())


def test_the_corpus_covers_every_applicator_position() -> None:
    """The corpus is not vacuous: every base, every kind of position and
    hundreds of mutations, with narrowings the sampler can witness."""
    assert len(CORPUS) > 5000
    witnessed = [
        label
        for label, old, new in CORPUS[::7]
        if schema_problems(old, new) and _escape(old, new, 0, 30) is not None
    ]
    assert len(witnessed) > 100
    for where in ("allOf", "then", "else", "dependentSchemas", "contains"):
        assert any(f":{where}:" in label for label in witnessed), where


class _MetaOld(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str


class _MetaNew(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    score: int = 0


class _MetaAny(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    score: typing.Any = None


def test_a_field_declared_on_an_open_model_must_accept_its_old_extras() -> None:
    """(G-DECL-01, round 5) a model allowing extras is an open object: an old
    producer may send ``score="free text"`` as an extra key, so declaring an
    optional ``score: int = 0`` narrows what the reader accepts and is
    reported; declaring one that accepts anything is compatible."""
    from sandloom.sdk import Event

    class Old(Event, channel="sdkdiff.probe.open_old.v1"):
        info: _MetaOld

    class New(Event, channel="sdkdiff.probe.open_new.v1"):
        info: _MetaNew

    class Any_(Event, channel="sdkdiff.probe.open_any.v1"):
        info: _MetaAny

    old, new = Old.wire_schema(), New.wire_schema()
    payload = Old(info=_MetaOld(name="n", score="free text")).model_dump(mode="json")
    assert compile_payload_validator(old).error(payload) is None
    assert compile_payload_validator(new).error(payload) is not None
    assert schema_problems(old, new) == ["field 'info.score' changed type"]
    assert schema_problems(old, Any_.wire_schema()) == []


def _escapes(old: Schema, new: Schema, seed: int, samples: int) -> Any | None:
    """A payload one side's producer may send and the other side's reader
    rejects: ``("old->new", payload)`` (an old producer, a new reader) or
    ``("new->old", payload)`` (a new producer, an old reader)."""
    payload = _escape(old, new, seed, samples)
    if payload is not None:
        return ("old->new", payload)
    payload = _escape(new, old, seed, samples, omitting=False)
    if payload is not None:
        return ("new->old", payload)
    return None


@pytest.mark.parametrize("base_name", sorted(BASES))
def test_a_change_the_rule_accepts_rejects_no_payload_either_way(
    base_name: str,
) -> None:
    """G-DECL-01 differential, both directions: for every pair of the corpus
    the rule calls compatible, no sampled payload of the old schema fails
    the new one, and no sampled payload of the new schema fails the old
    one."""
    escapes = []
    for label, old, new in CORPUS:
        if not label.startswith(f"{base_name}:") or schema_problems(old, new):
            continue
        found = _escapes(old, new, zlib.crc32(label.encode()), 40)
        if found is not None:
            escapes.append(f"{label}: {found[0]} {json.dumps(found[1])}")
    assert escapes == []


class _MetaRequired(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    metadata: typing.Any


def test_a_field_removed_from_an_open_model_breaks_its_old_readers() -> None:
    """(G-DECL-01, round 6) removing a required field from a model open to
    extra keys: an old producer's ``metadata`` still validates for the new
    reader (an extra), but the NEW producer omits it and the OLD reader
    requires it. Both directions matter on the same ``.vN``."""
    from sandloom.sdk import Event

    class Old(Event, channel="sdkdiff.probe.open_req_old.v1"):
        info: _MetaRequired

    class New(Event, channel="sdkdiff.probe.open_req_new.v1"):
        info: _MetaOld

    old, new = Old.wire_schema(), New.wire_schema()
    payload = New(info=_MetaOld(name="n")).model_dump(mode="json")
    assert compile_payload_validator(old).error(payload) is not None
    assert schema_problems(old, new) == ["field 'info.metadata' was removed"]


def test_a_loosened_constraint_breaks_its_old_readers() -> None:
    """(G-DECL-01, round 6) ``le=10 -> le=20``: a new producer sends 15, an
    old reader rejects it -- a loosening is a breaking change too."""
    old = {"type": "object", "properties": {"v": {"type": "integer", "maximum": 10}}}
    new = {"type": "object", "properties": {"v": {"type": "integer", "maximum": 20}}}
    assert compile_payload_validator(old).error({"v": 15}) is not None
    assert schema_problems(old, new) == [
        "field 'v' loosened upper bound (maximum 10 -> maximum 20)"
    ]


def _apply_all(schema: Schema, steps: list[tuple[Pointer, Mutation]]) -> Schema:
    """``schema`` with each step applied whose position still exists."""
    out = copy.deepcopy(schema)
    for pointer, (_name, mutation) in steps:
        try:
            node = _at(out, pointer)
        except (KeyError, IndexError, TypeError):
            continue  # an earlier step removed it
        mutation(node)
    return out


@st.composite
def _chained(draw: st.DrawFn) -> tuple[Schema, Schema]:
    """``(old, new)``: a base with one to four random mutations, and the base
    with a random subset of them (often a compatible loosening) plus maybe
    one more -- tightenings and loosenings composed at random positions."""
    base = BASES[draw(st.sampled_from(sorted(BASES)))]
    positions = list(_positions(base))

    def step() -> tuple[Pointer, Mutation]:
        pointer = draw(st.sampled_from(positions))
        return pointer, draw(st.sampled_from(_mutations(_at(base, pointer))))

    steps = [step() for _ in range(draw(st.integers(1, 4)))]
    kept = [s for s in steps if draw(st.booleans())]
    extra = [step()] if draw(st.integers(0, 3)) == 0 else []
    return _apply_all(base, steps), _apply_all(base, kept + extra)


@settings(
    max_examples=300,
    derandomize=True,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(pair=_chained(), seed=st.integers(0, 2**16))
def test_composed_changes_the_rule_accepts_reject_no_payload_either_way(
    pair: tuple[Schema, Schema], seed: int
) -> None:
    """G-DECL-01 differential, composed: random chains of mutations, both
    directions."""
    old, new = pair
    if schema_problems(old, new):
        return
    assert _escapes(old, new, seed, 30) is None


# --------------------------------------------------------------------------- #
# Fields added with a default factory (round 7)
# --------------------------------------------------------------------------- #


class _Part(BaseModel):
    sku: str


class _Holder(BaseModel):
    """A nested DTO emitted like an event: its defaulted fields required."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    name: str


class _HolderAdded(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    name: str
    tags: list[str] = Field(default_factory=list)
    attrs: dict[str, int] = Field(default_factory=dict)
    flags: set[str] = Field(default_factory=set)
    part: _Part = Field(default_factory=lambda: _Part(sku="none"))


class _Plain(BaseModel):
    name: str


class _PlainAdded(BaseModel):
    name: str
    tags: list[str] = Field(default_factory=list)
    attrs: dict[str, int] = Field(default_factory=dict)
    flags: set[str] = Field(default_factory=set)
    part: _Part = Field(default_factory=lambda: _Part(sku="none"))


def _factory_pair() -> tuple[Any, Any]:
    from sandloom.sdk import Event

    class Before(Event, channel="sdkdiff.probe.factory.v1"):
        x: int
        holder: _Holder
        plain: _Plain

    class After(Event, channel="sdkdiff.probe.factory.v1"):
        x: int
        holder: _HolderAdded
        plain: _PlainAdded
        tags: list[str] = Field(default_factory=list)
        attrs: dict[str, int] = Field(default_factory=dict)
        flags: set[str] = Field(default_factory=set)
        part: _Part = Field(default_factory=lambda: _Part(sku="none"))

    return Before, After


def test_a_field_added_with_a_default_factory_is_compatible_both_ways() -> None:
    """(G-DECL-01, round 7) a field added with a default factory (list,
    dict, set, a model) is an optional field (spec §5.2), top-level and in
    nested DTOs: the wire schema lists it as required (always emitted) with
    no literal ``default``, so the rule recognizes a default of any kind by
    the framework annotation. Both directions hold with the real readers: a
    new reader fills the field in an old payload, an old reader ignores it
    in a new payload (and the old schema accepts that payload)."""
    from sandloom.sdk._decl.events import from_wire, to_wire

    before, after = _factory_pair()
    old, new = before.wire_schema(), after.wire_schema()
    assert "tags" in new["required"]
    assert "default" not in new["properties"]["tags"]
    assert (
        new["properties"]["tags"][DEFAULTED] is True
    )  # the SDK's mark is the kernel's
    assert "tags" in new["$defs"]["_HolderAdded"]["required"]
    assert schema_problems(old, new) == []

    old_payload = to_wire(before(x=1, holder=_Holder(name="h"), plain=_Plain(name="p")))
    filled = from_wire(after, old_payload)
    assert (filled.tags, filled.attrs, filled.flags) == ([], {}, set())
    assert filled.part == _Part(sku="none")
    assert (filled.holder.tags, filled.plain.attrs) == ([], {})

    new_payload = to_wire(
        after(
            x=1,
            holder=_HolderAdded(name="h", tags=["a"]),
            plain=_PlainAdded(name="p", flags={"f"}),
            tags=["t"],
            part=_Part(sku="s"),
        )
    )
    assert compile_payload_validator(old).error(new_payload) is None
    assert from_wire(before, new_payload).x == 1


def test_a_field_added_without_a_default_is_still_required_now() -> None:
    """(G-DECL-01, round 7) the exemption is the default, not the type: the
    same field added without a default fails, top-level and nested."""
    from sandloom.sdk import Event

    before, _after = _factory_pair()

    class _HolderBare(BaseModel):
        model_config = ConfigDict(json_schema_serialization_defaults_required=True)
        name: str
        flags: set[str]

    class Bare(Event, channel="sdkdiff.probe.factory.v1"):
        x: int
        holder: _HolderBare
        plain: _Plain
        tags: list[str]

    assert schema_problems(before.wire_schema(), Bare.wire_schema()) == [
        "field 'tags' is required now (only optional fields may be added)",
        "field 'holder.flags' is required now (only optional fields may be added)",
    ]


# --------------------------------------------------------------------------- #
# Round 8: removed defaults, strict nested DTOs, the real readers
# --------------------------------------------------------------------------- #


def test_removing_a_default_breaks_a_producer_from_before_the_field() -> None:
    """(G-DECL-01, round 8) ``tags: list[str] = Field(default_factory=list)``
    becoming ``tags: list[str]`` leaves the serialization schema's
    ``required`` unchanged (a defaulted field is always emitted), yet a
    producer from before ``tags`` was added -- still deployed on the same
    ``.vN`` -- sends ``{"x": 1}`` and the new reader rejects it. Losing the
    default is breaking, literal or factory, top level or nested."""
    from sandloom.sdk import Event
    from sandloom.sdk._decl.events import from_wire

    class _Inner(BaseModel):
        model_config = ConfigDict(json_schema_serialization_defaults_required=True)
        name: str
        level: int = 0

    class _InnerBare(BaseModel):
        model_config = ConfigDict(json_schema_serialization_defaults_required=True)
        name: str
        level: int

    class Old(Event, channel="sdkdiff.probe.undefault.v1"):
        x: int
        tags: list[str] = Field(default_factory=list)
        count: int = 0
        inner: _Inner

    class New(Event, channel="sdkdiff.probe.undefault.v1"):
        x: int
        tags: list[str]
        count: int
        inner: _InnerBare

    old, new = Old.wire_schema(), New.wire_schema()
    assert old["required"] == new["required"]  # the serialization schema hides it
    with pytest.raises(ValueError):
        from_wire(New, {"x": 1, "inner": {"name": "n"}})
    assert schema_problems(old, new) == [
        "field 'count' lost its default (a producer from before the field omits it)",
        "field 'tags' lost its default (a producer from before the field omits it)",
        "field 'inner.level' lost its default (a producer from before the "
        "field omits it)",
    ]
    # adding a default to a required field is compatible (both ways)
    assert schema_problems(new, old) == []


def test_the_registry_refuses_a_removed_default() -> None:
    """(G-DECL-01, round 8) the live registry reads the same mark: a field
    that loses its default is newly required for readers -- top level and
    in a nested DTO (``$defs``) -- and the change is breaking."""
    from sandloom.core.contracts.validators import classify_schema_change
    from sandloom.sdk import Event

    class _Inner(BaseModel):
        model_config = ConfigDict(json_schema_serialization_defaults_required=True)
        name: str
        tags: list[str] = Field(default_factory=list)

    def bare() -> type[BaseModel]:
        class _Inner(BaseModel):  # the same DTO (``$defs`` name), default gone
            model_config = ConfigDict(json_schema_serialization_defaults_required=True)
            name: str
            tags: list[str]

        return _Inner

    _InnerBare = bare()

    class Old(Event, channel="sdkdiff.probe.undefault_reg.v1"):
        x: int
        count: int = 0
        inner: _Inner

    class New(Event, channel="sdkdiff.probe.undefault_reg.v1"):
        x: int
        count: int
        inner: _Inner

    class NewNested(Event, channel="sdkdiff.probe.undefault_reg.v1"):
        x: int
        count: int = 0
        inner: _InnerBare  # type: ignore[valid-type]

    channel = "sdkdiff.probe.undefault_reg.v1"
    top = classify_schema_change(Old.wire_schema(), New.wire_schema(), channel, 1)
    assert (top.added_required, top.breaking) == (["count"], True)
    nested = classify_schema_change(
        Old.wire_schema(), NewNested.wire_schema(), channel, 1
    )
    assert nested.breaking is True
    assert nested.added_required == ["$defs._Inner.tags"]
    back = classify_schema_change(New.wire_schema(), Old.wire_schema(), channel, 1)
    assert back.breaking is False


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class _StrictAdded(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    tags: list[str] = Field(default_factory=list)
    level: int = 0


def test_an_old_strict_nested_reader_ignores_a_newer_producers_field() -> None:
    """(G-DECL-01, round 8, design (a)) a nested DTO declared
    ``extra="forbid"`` gains ``tags`` (factory default): the gate accepts it
    (spec §5.2: the same ``.vN`` may gain an optional field), so the OLD
    reader must accept the new payload -- ``from_wire`` ignores unknown keys
    at every depth, as at the top level, and the wire schema says so (no
    ``additionalProperties: false`` anywhere); construction stays strict."""
    from sandloom.sdk import Event
    from sandloom.sdk._decl.events import from_wire, to_wire

    class Old(Event, channel="sdkdiff.probe.strict.v1"):
        dto: _Strict
        dtos: list[_Strict]
        maybe: _Strict | None = None

    class New(Event, channel="sdkdiff.probe.strict.v1"):
        dto: _StrictAdded
        dtos: list[_StrictAdded]
        maybe: _StrictAdded | None = None

    old, new = Old.wire_schema(), New.wire_schema()
    assert "additionalProperties" not in json.dumps(old)
    assert schema_problems(old, new) == []
    added = _StrictAdded(name="n", tags=["t"], level=2)
    payload = to_wire(New(dto=added, dtos=[added], maybe=added))
    assert compile_payload_validator(old).error(payload) is None
    read = from_wire(Old, payload)
    assert read.dto == read.dtos[0] == read.maybe == _Strict(name="n")
    with pytest.raises(ValueError):
        _Strict(name="n", tags=[])  # type: ignore[call-arg]
    # a real error below an ignored key is still reported
    bad = {**payload, "dto": {"name": 5, "tags": ["t"]}}
    with pytest.raises(ValueError, match=r"dto\.name"):
        from_wire(Old, bad)

    class Open(Event, channel="sdkdiff.probe.strict.v1"):
        dto: _Strict
        info: _MetaOld  # extra="allow": its extras are data, kept

    read_open = from_wire(
        Open, {"dto": {"name": "n", "x": 1}, "info": {"name": "i", "k": 2}}
    )
    assert read_open.info.model_extra == {"k": 2}


def test_a_user_cannot_forge_the_default_mark() -> None:
    """(G-DECL-01, round 8) the ``DEFAULTED`` mark is framework-owned: a
    ``json_schema_extra`` carrying it -- on a required field, in a nested
    DTO, or as a callable -- is refused at class definition (SANDLOOM-B015), so
    the mark always equals "this field has a default"."""
    from sandloom.sdk import Event
    from sandloom.sdk.errors import EventDefinitionError

    with pytest.raises(EventDefinitionError, match="x-sandloom-default") as info:

        class Top(Event, channel="sdkdiff.probe.forged.v1"):
            tags: list[str] = Field(json_schema_extra={DEFAULTED: True})

    assert info.value.sandloom_code == "SANDLOOM-B015"

    class _Forged(BaseModel):
        level: int = Field(json_schema_extra=lambda s: s.update({DEFAULTED: True}))

    with pytest.raises(EventDefinitionError, match="x-sandloom-default"):

        class Nested(Event, channel="sdkdiff.probe.forged.v1"):
            inner: _Forged

    class _Model(BaseModel):
        model_config = ConfigDict(json_schema_extra={DEFAULTED: False})
        name: str

    with pytest.raises(EventDefinitionError, match="x-sandloom-default"):

        class OnModel(Event, channel="sdkdiff.probe.forged.v1"):
            inner: _Model


# ---- the real readers --------------------------------------------------- #


class _Lax(BaseModel):
    name: str


class _LaxAdded(BaseModel):
    name: str
    tags: list[str] = Field(default_factory=list)


class _LaxBare(BaseModel):
    name: str
    tags: list[str]


class _StrictBare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    tags: list[str]
    level: int


class _Emitted(BaseModel):
    """A nested DTO emitted like an event: defaulted fields required."""

    model_config = ConfigDict(
        extra="forbid", json_schema_serialization_defaults_required=True
    )
    name: str
    level: int = 0


class _EmittedBare(BaseModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_serialization_defaults_required=True
    )
    name: str
    level: int


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str


class _LooseAny(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    score: typing.Any = None


def _event(name: str, fields: dict[str, tuple[Any, ...]]) -> type[Any]:
    """An Event class ``name`` of the probe channel with ``fields``
    (``name: (annotation,)`` or ``(annotation, default)``)."""
    from sandloom.sdk import Event

    namespace: dict[str, Any] = {
        "__annotations__": {k: v[0] for k, v in fields.items()},
        "__module__": __name__,
    }
    namespace.update({k: v[1] for k, v in fields.items() if len(v) > 1})
    return types.new_class(
        name,
        (Event,),
        {"channel": "sdkdiff.probe.reader.v1"},
        lambda ns: ns.update(namespace),
    )


# Round 10: the closed set of wire shapes (``sandloom.sdk._decl._event_wire``).
# Every allowed shape -- each scalar, each container, DTOs strict / lax /
# open, nested, recursive, aliased, validated, discriminated -- in the version
# before and after an added optional field (at the top level next to a
# scalar, inside every DTO otherwise), checked both directions through the
# real readers; every refused shape raises SANDLOOM-B015 at definition.
_FORBID = ConfigDict(extra="forbid")


class _Color(enum.StrEnum):
    red = "red"
    blue = "blue"


class _Level(enum.IntEnum):
    low = 1
    high = 2


class _Mode(enum.Enum):
    on = "on"
    off = "off"


class _Leaf(BaseModel):
    model_config = _FORBID
    name: str


class _LeafAdded(_Leaf):
    rank: int = 0


class _Mail(BaseModel):
    model_config = _FORBID
    kind: Literal["mail"]
    email: str


class _MailAdded(_Mail):
    cc: list[str] = Field(default_factory=list)


class _Text(BaseModel):
    model_config = _FORBID
    kind: Literal["text"]
    phone: str


class _TextAdded(_Text):
    carrier: str = ""


class _Aliased(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, validate_by_name=True
    )
    first_name: str
    user_ref: str = Field("", alias="userRef")


class _AliasedAdded(_Aliased):
    last_seen: int = 0


class _Tree(BaseModel):
    model_config = _FORBID
    name: str
    children: list[_Tree] = Field(default_factory=list)


class _TreeAdded(BaseModel):
    model_config = _FORBID
    name: str
    children: list[_TreeAdded] = Field(default_factory=list)
    weight: int = 0


class _Checked(BaseModel):
    """After validators (they see validated values), frozen, and a field
    never published (``exclude``) that has a default."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    secret: str = Field("", exclude=True)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _after(self) -> _Checked:
        return self


class _CheckedAdded(_Checked):
    note: str = ""


def _tagged(mail: type[Any], text: type[Any]) -> Any:
    return typing.Annotated[mail | text, Field(discriminator="kind")]


class _Outer(BaseModel):
    """Two levels of DTOs: every container of a nested strict DTO."""

    model_config = _FORBID
    leaf: _Leaf
    leaves: list[_Leaf] = Field(default_factory=list)
    keyed: dict[str, _Leaf] = Field(default_factory=dict)
    tagged: _tagged(_Mail, _Text) | None = None  # type: ignore[valid-type]


class _OuterAdded(BaseModel):
    model_config = _FORBID
    leaf: _LeafAdded
    leaves: list[_LeafAdded] = Field(default_factory=list)
    keyed: dict[str, _LeafAdded] = Field(default_factory=dict)
    tagged: _tagged(_MailAdded, _TextAdded) | None = None  # type: ignore[valid-type]
    level: int = 0


_TAGS: tuple[Any, ...] = (list[str], Field(default_factory=list))
_COUNT: tuple[Any, ...] = (int, 0)

#: Every allowed scalar and container of scalars: ``(label, annotation)``.
_ALLOWED_SCALARS: list[tuple[str, Any]] = [
    ("str", str),
    ("int", int),
    ("float", float),
    ("bool", bool),
    ("optional", int | None),
    ("uuid", UUID),
    ("datetime", datetime),
    ("date", date),
    ("time", time),
    ("timedelta", timedelta),
    ("decimal", Decimal),
    ("str-enum", _Color),
    ("int-enum", _Level),
    ("enum", _Mode),
    ("literal", Literal["a", "b"]),
    ("any", typing.Any),
    ("scalar-union", Literal["a", "b"] | Literal[1, 2]),
    ("enum-union", _Color | _Level | None),
    ("constrained-int", typing.Annotated[int, Field(ge=0, le=10)]),
    ("constrained-str", typing.Annotated[str, StringConstraints(max_length=3)]),
    ("list", list[int]),
    ("tuple-variadic", tuple[int, ...]),
    ("tuple-fixed", tuple[int, str]),
    ("set", set[str]),
    ("frozenset", frozenset[int]),
    ("dict", dict[str, int]),
    ("dict-opaque", dict[str, typing.Any]),
    ("optional-list", list[UUID] | None),
]


def _dto_shapes(leaf: Any, mail: Any, text: Any) -> list[tuple[str, Any]]:
    """Every allowed container of a DTO, around ``leaf`` / ``mail | text``."""
    tagged = _tagged(mail, text)
    return [
        ("dto", leaf),
        ("dto-optional", leaf | None),
        ("dto-list", list[leaf]),
        ("dto-tuple-variadic", tuple[leaf, ...]),
        ("dto-tuple-fixed", tuple[leaf, int]),
        ("dto-dict", dict[str, leaf]),
        ("dto-deep", dict[str, list[leaf | None]]),
        ("dto-fail-fast", typing.Annotated[list[leaf], FailFast()]),
        ("discriminated", tagged),
        ("discriminated-optional", tagged | None),
        ("discriminated-list", list[tagged]),  # type: ignore[valid-type]
        ("discriminated-dict", dict[str, tagged]),  # type: ignore[valid-type]
    ]


#: ``(label, old fields, new fields, compatible)``: two versions of one
#: ``.vN`` and the verdict the gate must give.
_READER_SPEC: list[tuple[str, dict[str, Any], dict[str, Any], bool]] = [
    *(
        (f"scalar-{label}", {"v": (tp,)}, {"v": (tp,), "count": _COUNT}, True)
        for label, tp in _ALLOWED_SCALARS
    ),
    *(
        (label, {"d": (old,)}, {"d": (new,)}, True)
        for (label, old), (_, new) in zip(
            _dto_shapes(_Leaf, _Mail, _Text),
            _dto_shapes(_LeafAdded, _MailAdded, _TextAdded),
            strict=True,
        )
    ),
    ("add-literal", {"x": (int,)}, {"x": (int,), "count": _COUNT}, True),
    ("add-factory", {"x": (int,)}, {"x": (int,), "tags": _TAGS}, True),
    ("add-strict", {"d": (_Strict,)}, {"d": (_StrictAdded,)}, True),
    ("add-lax", {"d": (_Lax,)}, {"d": (_LaxAdded,)}, True),
    ("add-open-any", {"d": (_Loose,)}, {"d": (_LooseAny,)}, True),
    ("add-aliased", {"d": (_Aliased,)}, {"d": (_AliasedAdded,)}, True),
    ("add-recursive", {"d": (_Tree,)}, {"d": (_TreeAdded,)}, True),
    ("add-nested", {"d": (list[_Outer],)}, {"d": (list[_OuterAdded],)}, True),
    ("add-validated", {"d": (_Checked,)}, {"d": (_CheckedAdded,)}, True),
    ("add-default", {"x": (int,)}, {"x": (int, 0)}, True),
    ("add-required", {"x": (int,)}, {"x": (int,), "y": (int,)}, False),
    ("remove-field", {"x": (int,), "y": (int,)}, {"x": (int,)}, False),
    ("retype-decimal-to-str", {"x": (Decimal,)}, {"x": (str,)}, False),
    ("retype-uuid-to-str", {"x": (UUID,)}, {"x": (str,)}, False),
    ("retype-enum", {"x": (_Color,)}, {"x": (Literal["red"],)}, False),
    (
        "undefault-literal",
        {"x": (int,), "count": _COUNT},
        {"x": (int,), "count": (int,)},
        False,
    ),
    (
        "undefault-factory",
        {"x": (int,), "tags": _TAGS},
        {"x": (int,), "tags": (list[str],)},
        False,
    ),
    ("undefault-nested-lax", {"d": (_LaxAdded,)}, {"d": (_LaxBare,)}, False),
    ("undefault-nested-strict", {"d": (_StrictAdded,)}, {"d": (_StrictBare,)}, False),
    ("undefault-nested-emitted", {"d": (_Emitted,)}, {"d": (_EmittedBare,)}, False),
    (
        "undefault-in-list",
        {"d": (list[_Emitted],)},
        {"d": (list[_EmittedBare],)},
        False,
    ),
]
READER_PAIRS = [
    (label, _event(f"Old{i}", old), _event(f"New{i}", new), compatible)
    for i, (label, old, new, compatible) in enumerate(_READER_SPEC)
]


def _reads(cls: type[Any], payload: Any) -> bool:
    from pydantic import ValidationError

    from sandloom.sdk._decl.events import from_wire

    try:
        from_wire(cls, payload)
    except ValidationError:
        return False
    return True


def _reader_escape(
    producer: type[Any], reader: type[Any], seed: int, *, omitting: bool
) -> Any | None:
    """A payload the real ``producer`` may send (its own schema and its own
    reader accept it; ``omitting``: a defaulted field may be absent, as from
    a producer before the field) that the real ``reader`` rejects."""
    schema = producer.wire_schema()
    sampled = _reader(schema) if omitting else schema
    sampler = _Producer(sampled, random.Random(seed))
    accepts = _validator(json.dumps(_reader(schema), sort_keys=True))
    for _ in range(60):
        payload = sampler.valid(sampled, 0)
        if (
            accepts.error(payload) is None
            and _reads(producer, payload)
            and not _reads(reader, payload)
        ):
            return payload
    return None


@pytest.mark.parametrize(
    ("label", "old", "new", "compatible"),
    READER_PAIRS,
    ids=[p[0] for p in READER_PAIRS],
)
def test_the_gate_agrees_with_the_real_readers(
    label: str, old: type[Any], new: type[Any], compatible: bool
) -> None:
    """(G-DECL-01, round 8) the differential with the REAL readers
    (``from_wire`` of the old and the new class, not only the JSON-schema
    validator): a pair the gate accepts has no payload an old producer --
    or one from before a defaulted field -- sends that the new reader
    rejects, nor one a new producer sends that the old reader rejects; a
    pair the gate refuses has such a payload, found by the sampler."""
    problems = schema_problems(old.wire_schema(), new.wire_schema())
    seed = zlib.crc32(label.encode())
    escape = _reader_escape(old, new, seed, omitting=True)
    if escape is None:
        escape = _reader_escape(new, old, seed, omitting=False)
    if compatible:
        assert (problems, escape) == ([], None)
    else:
        assert problems != []
        assert escape is not None


def _with_unknown_keys(value: Any, rng: random.Random) -> Any:
    """``value`` with an undeclared key added to every object at any depth
    (a newer producer's field the schema leaves open). The added value is
    like one of its siblings' (a dictionary's values are typed: a value of
    another type is not an added field but an invalid payload, which the
    lax reader may still coerce -- ``True`` read as ``1`` -- while no
    producer sends it: SANDLOOM-R004 validates every payload against the wire
    schema); an empty object gets a nested one."""
    if isinstance(value, dict):
        out = {k: _with_unknown_keys(v, rng) for k, v in value.items()}
        added = copy.deepcopy(rng.choice(list(out.values()))) if out else {"zz": 1}
        out[f"zz_new_{rng.randint(0, 9)}"] = added
        return out
    if isinstance(value, list):
        return [_with_unknown_keys(v, rng) for v in value]
    return value


_READER_CLASSES = [
    (p[0], side, cls)
    for p in READER_PAIRS
    for side, cls in (("old", p[1]), ("new", p[2]))
]


@pytest.mark.parametrize(
    "cls",
    [c[2] for c in _READER_CLASSES],
    ids=[f"{c[0]}-{c[1]}" for c in _READER_CLASSES],
)
def test_the_wire_schema_is_what_the_real_reader_accepts(cls: type[Any]) -> None:
    """(G-DECL-01, round 8) the schema is the truth: a payload sampled from
    the wire schema (a reader's view: defaulted fields optional), with an
    undeclared key added at every depth -- the schema leaves every object
    open -- is accepted by the schema validator exactly when the class's
    real reader (``from_wire``) accepts it. A schema/reader mismatch (round
    8: a nested ``extra="forbid"`` DTO rejected keys its schema allowed)
    fails here generically."""
    schema = _reader(cls.wire_schema())
    validator = _validator(json.dumps(schema, sort_keys=True))
    rng = random.Random(zlib.crc32(cls.__name__.encode()))
    sampler = _Producer(schema, rng)
    mismatches = []
    read = 0
    for _ in range(40):
        payload = _with_unknown_keys(sampler.valid(schema, 0), rng)
        reads = _reads(cls, payload)
        read += reads
        if (validator.error(payload) is None) != reads:
            mismatches.append(payload)
    assert mismatches == []
    assert read >= 10  # not vacuous: most samples are valid payloads


# ---- the refused shapes ------------------------------------------------- #


class _Lax2(BaseModel):
    name: str = ""


class _WithInit(BaseModel):
    x: int = 0

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class _WithNew(BaseModel):
    x: int = 0

    def __new__(cls, *args: Any, **kwargs: Any) -> _WithNew:
        return super().__new__(cls)


class _Before(BaseModel):
    x: int = 0

    @model_validator(mode="before")
    @classmethod
    def _raw(cls, data: Any) -> Any:
        return data


class _Wrap(BaseModel):
    x: int = 0

    @model_validator(mode="wrap")
    @classmethod
    def _around(cls, data: Any, handler: Any) -> Any:
        return handler(data)


class _FieldBefore(BaseModel):
    leaf: _Leaf

    @field_validator("leaf", mode="before")
    @classmethod
    def _raw(cls, value: Any) -> Any:
        return value


class _Serialized(BaseModel):
    x: int = 0

    @field_serializer("x")
    def _out(self, value: int) -> str:
        return str(value)


class _ModelSerialized(BaseModel):
    x: int = 0

    @model_serializer
    def _out(self) -> dict[str, Any]:
        return {"y": self.x}


class _Computed(BaseModel):
    x: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def double(self) -> int:
        return 2 * self.x


class _Root(RootModel[list[int]]):
    pass


_T = typing.TypeVar("_T")


class _Page(BaseModel, typing.Generic[_T]):
    items: list[_T] = Field(default_factory=list)


@pydantic_dataclass
class _Point:
    x: int


class _Typed(TypedDict):
    x: int


class _Pair(typing.NamedTuple):
    x: int


class _Bytes(enum.Enum):
    raw = b"x"


class _Nested(BaseModel):
    """A refused shape deep inside the payload: the path names it."""

    item: _Leaf | _Lax2


class _Floating(BaseModel):
    """A DTO float that may be NaN / infinite (round 11)."""

    x: float = 0.0


class _Milliseconds(BaseModel):
    """A configuration that changes the wire encoding (round 11)."""

    model_config = ConfigDict(ser_json_temporal="milliseconds")
    span: timedelta = timedelta(0)


def _kind_of(value: Any) -> str | None:
    kind = (
        value.get("kind") if isinstance(value, dict) else getattr(value, "kind", None)
    )
    return kind if kind in ("mail", "text") else None


#: ``(label, annotation, the path and problem the error names)``.
_REFUSED: list[tuple[str, Any, str]] = [
    ("union-dto-dict", _Leaf | dict[str, Any], r"Ev\.d: .* without a discriminator"),
    ("union-dto-dto", _Leaf | _Lax2, r"Ev\.d: .* without a discriminator"),
    (
        "union-left-to-right",
        typing.Annotated[_Leaf | _Lax2, Field(union_mode="left_to_right")],
        r"Ev\.d: .* without a discriminator",
    ),
    ("union-dto-scalar", _Leaf | int, r"without a discriminator"),
    ("union-container-scalar", list[int] | str, r"without a discriminator"),
    ("union-any-container", dict[str, int] | list[int], r"without a discriminator"),
    (
        "union-nested",
        dict[str, list[_Nested]],
        r"Ev\.d\{\}\[\]\.item: .* discriminator",
    ),
    (
        "callable-discriminator",
        typing.Annotated[
            typing.Annotated[_Mail, Tag("mail")] | typing.Annotated[_Text, Tag("text")],
            Discriminator(_kind_of),
        ],
        r"discriminated by a callable",
    ),
    ("dataclass", _Point, r"is a dataclass"),
    ("typed-dict", list[_Typed], r"Ev\.d\[\]: .*is a TypedDict"),
    ("root-model", _Root, r"_Root is a RootModel"),
    ("generic-unparametrised", _Page, r"generic DTO used without"),
    ("custom-init", _WithInit, r"_WithInit overrides __init__"),
    ("custom-new", list[_WithNew], r"_WithNew overrides __new__"),
    ("model-before", _Before, r'mode="before"'),
    ("model-wrap", _Wrap, r'mode="wrap"'),
    ("field-before-dto", _FieldBefore, r'Ev\.d\.leaf: a mode="before"'),
    (
        "annotated-before-dto",
        typing.Annotated[list[_Leaf], BeforeValidator(lambda v: v)],
        r'mode="before"',
    ),
    ("plain-validator", typing.Annotated[int, PlainValidator(int)], r"plain validator"),
    ("field-serializer", _Serialized, r"custom serializer"),
    ("model-serializer", _ModelSerialized, r"custom serializer"),
    (
        "plain-serializer",
        typing.Annotated[int, PlainSerializer(str)],
        r"custom serializer",
    ),
    ("computed-field", _Computed, r"computed fields \['double'\]"),
    ("bytes", bytes, r"not a wire type \(core schema 'bytes'\)"),
    ("url", AnyUrl, r"not a wire type \(core schema 'url'\)"),
    ("json", Json[int], r"not a wire type"),
    ("named-tuple", _Pair, r"not a wire type"),
    ("path", pathlib.Path, r"not a wire type"),
    ("non-str-keys", dict[int, str], r"keys that are not str"),
    ("opaque-dict-keys", dict, r"keys that are not str"),
    ("set-of-tuples", set[tuple[int, int]], r"set of non-scalar values"),
    ("bytes-literal", Literal[b"x"], r"not a JSON scalar"),
    ("bytes-enum", _Bytes, r"not a JSON scalar"),
    # round 11: a union of scalars that can read each other's values
    ("union-int-str", int | str, r"Ev\.d: .*union of scalars"),
    ("union-uuid-str", UUID | str, r"union of scalars"),
    ("union-float-int", float | int, r"union of scalars"),
    ("union-datetime-str", datetime | str, r"union of scalars"),
    ("union-nested", list[dict[str, int | str]], r"Ev\.d\[\]\{\}: .*union of scalars"),
    (
        "union-literal-overlap",
        Literal[1] | Literal[True],
        r"the published value .* is read by another member",
    ),
    # round 11: what changes the wire encoding
    ("serialize-as-any", SerializeAsAny[_Lax2], r"custom serializer"),
    (
        "float-may-be-nan",
        typing.Annotated[float, Field(allow_inf_nan=True)],
        r"may be NaN or infinite",
    ),
    ("dto-float-may-be-nan", _Floating, r"Ev\.d\.x: .*may be NaN or infinite"),
    (
        "coerce-numbers-to-str",
        typing.Annotated[str, Field(coerce_numbers_to_str=True)],
        r"coerce_numbers_to_str",
    ),
    (
        "dto-config",
        _Milliseconds,
        r"Ev\.d: _Milliseconds's model_config sets ser_json_temporal",
    ),
]


class _AliasOnly(BaseModel):
    value: int = Field(0, serialization_alias="wireValue")


#: Field declarations refused for their wire name or their exclusion.
_REFUSED_FIELDS: list[tuple[str, tuple[Any, ...], str]] = [
    (
        "serialization-alias-only",
        (int, Field(0, serialization_alias="wireValue")),
        r"Ev\.d: is published as 'wireValue' but its reader looks up 'd'",
    ),
    (
        "validation-alias-only",
        (int, Field(0, validation_alias="wireValue")),
        r"published as 'd' but its reader looks up 'wireValue'",
    ),
    (
        "alias-choices",
        (int, Field(0, validation_alias=AliasChoices("d", "dee"))),
        r"AliasPath / AliasChoices",
    ),
    (
        "alias-path",
        (int, Field(0, validation_alias=AliasPath("d", 0))),
        r"AliasPath / AliasChoices",
    ),
    ("nested-alias-only", (_AliasOnly,), r"Ev\.d\.value: is published as 'wireValue'"),
    (
        "excluded-without-default",
        (int, Field(exclude=True)),
        r"Ev\.d: is never published \(exclude\) yet has no default",
    ),
    (
        "empty-serialization-alias",
        (int, Field(0, serialization_alias="")),
        r"Ev\.d: has the empty wire name",
    ),
    ("empty-alias", (int, Field(0, alias="")), r"Ev\.d: has the empty wire name"),
]


@pytest.mark.parametrize(
    ("annotation", "problem"),
    [(a, p) for _, a, p in _REFUSED] + [(f, p) for _, f, p in _REFUSED_FIELDS],
    ids=[r[0] for r in _REFUSED] + [r[0] for r in _REFUSED_FIELDS],
)
def test_every_refused_shape_raises_at_definition(
    annotation: Any, problem: str
) -> None:
    """(G-DECL-01, round 10) an event payload is plain data: every shape
    outside the closed set -- an undiscriminated union holding a DTO or a
    container (round 10, item 2), a callable discriminator, a dataclass, a
    ``TypedDict``, a ``RootModel``, an unparametrised generic, a custom
    ``__init__`` / ``__new__`` (item 3), a before / wrap validator over a
    DTO, a plain validator, a custom serializer, a computed field, a wire
    name that does not round-trip (item 4), an excluded field without a
    default, a non-wire scalar, non-str keys, a set of non-scalars, a
    non-JSON literal -- is refused when the class is defined (SANDLOOM-B015),
    naming the field path, the problem and the fix."""
    from sandloom.sdk.errors import EventDefinitionError

    fields = annotation if isinstance(annotation, tuple) else (annotation,)
    with pytest.raises(EventDefinitionError, match=problem) as info:
        _event("Ev", {"d": fields})
    assert info.value.sandloom_code == "SANDLOOM-B015"
    assert "\n  fix: " in str(info.value)


def test_every_bundled_event_is_in_the_closed_set() -> None:
    """(round 10) the bundled modules' events define (their readers are
    built at definition); the example apps' are checked by their own
    suites in CI (examples job)."""
    import sandloom_billing.events
    import sandloom_identity.events
    import sandloom_tenancy.events
    from sandloom.sdk import Event
    from sandloom.sdk._decl.events import _READERS, is_concrete

    modules = (
        sandloom_billing.events,
        sandloom_identity.events,
        sandloom_tenancy.events,
    )
    events = [
        value
        for module in modules
        for value in vars(module).values()
        if isinstance(value, type) and issubclass(value, Event) and is_concrete(value)
    ]
    assert len(events) > 30
    assert all(event in _READERS for event in events)
