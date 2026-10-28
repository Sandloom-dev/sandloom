"""The contract wire codec (spec §8.2; G-CONTRACT-05, G-CONTRACT-16).

Hypothesis generates a wire annotation together with values of it: the JSON
scalars, ``UUID`` / ``datetime`` / ``date`` / ``Decimal``, an ``Enum``, a
``Literal``, a pydantic model, the containers (``list``, ``tuple[X, ...]``,
``set``, ``frozenset``, ``dict`` with string and non-string keys),
``Optional`` and AMBIGUOUS unions (``UUID | str``, ``date | str``,
``Decimal | str``, ``int | str``). Pinned:

* codec 1 (the tagged tree) round-trips every value exactly, types included
  (a ``UUID`` stays a ``UUID`` and a UUID-looking ``str`` stays a ``str``),
  also through msgpack as the RPC transport carries it;
* local equals remote: what an in-process call observes
  (``WirePlan.local``) is what a remote peer decodes;
* codec 1 equals codec 2 for every fully typed annotation;
* a non-wire annotation is refused at registration (``annotation_problem``),
  wherever it sits in the type.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from uuid import UUID

import msgpack
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from sandloom.core.contract_wire import (
    CODEC_TYPED,
    annotation_problem,
    decode,
    encode,
    roundtrip,
    wire_plan,
)

LAWS = settings(max_examples=300, deadline=None)


class Color(Enum):
    RED = "red"
    BLUE = "blue"


class Item(BaseModel):
    id: UUID
    title: str
    tags: list[str]
    due: date | None = None
    amount: Decimal | None = None


Pair = tuple[Any, st.SearchStrategy[Any]]

_text = st.text(max_size=8)
#: msgpack (the RPC transport) carries 64-bit integers; a larger int is not a
#: portable wire value (see test_an_int_beyond_64_bits_does_not_cross_rpc).
_ints = st.integers(-(2**63), 2**63 - 1)
_floats = st.floats(allow_nan=False, allow_infinity=False)
_decimals = st.decimals(allow_nan=False, allow_infinity=False, places=4)
_datetimes = st.datetimes(timezones=st.one_of(st.none(), st.just(UTC)))

#: Hashable scalars: set elements and dict keys.
KEYS: list[Pair] = [
    (str, _text),
    (int, _ints),
    (UUID, st.uuids()),
    (date, st.dates()),
    (Color, st.sampled_from(Color)),
]
SCALARS: list[Pair] = [
    *KEYS,
    (float, _floats),
    (bool, st.booleans()),
    (type(None), st.none()),
    (datetime, _datetimes),
    (Decimal, _decimals),
    (Literal["a", "b", 1], st.sampled_from(["a", "b", 1])),
    # ambiguous unions: codec 1 keeps the runtime type (spec §5.3, Q3)
    (UUID | str, st.one_of(st.uuids(), st.uuids().map(str), _text)),
    (date | str, st.one_of(st.dates(), st.dates().map(str), _text)),
    (Decimal | str, st.one_of(_decimals, _text)),
    (int | str, st.one_of(_ints, _ints.map(str))),
    (
        Item,
        st.builds(
            Item,
            id=st.uuids(),
            title=_text,
            tags=st.lists(_text, max_size=3),
            due=st.none() | st.dates(),
            amount=st.none() | _decimals,
        ),
    ),
]


def _of(origin: Any, *args: Any) -> Any:
    """``origin[args]`` built at run time (a generated annotation)."""
    return origin[args[0] if len(args) == 1 else args]


def _container(child: Pair) -> st.SearchStrategy[Pair]:
    ann, values = child
    options: list[Pair] = [
        (_of(list, ann), st.lists(values, max_size=3)),
        (_of(tuple, ann, ...), st.lists(values, max_size=3).map(tuple)),
        (_of(dict, str, ann), st.dictionaries(_text, values, max_size=3)),
        (_of(dict, UUID, ann), st.dictionaries(st.uuids(), values, max_size=2)),
        (ann | None, st.none() | values),
    ]
    return st.sampled_from(options)


def _keyed() -> st.SearchStrategy[Pair]:
    def build(key: Pair) -> list[Pair]:
        ann, values = key
        return [
            (_of(set, ann), st.sets(values, max_size=3)),
            (_of(frozenset, ann), st.frozensets(values, max_size=3)),
            (_of(dict, ann, int), st.dictionaries(values, _ints, max_size=3)),
        ]

    return st.sampled_from([p for key in KEYS for p in build(key)])


pairs: st.SearchStrategy[Pair] = st.recursive(
    st.one_of(st.sampled_from(SCALARS), _keyed()),
    lambda children: children.flatmap(_container),
    max_leaves=4,
)
typed_values = pairs.flatmap(lambda p: st.tuples(st.just(p[0]), p[1]))


def same(a: Any, b: Any) -> bool:
    """Equal values of the same runtime types, recursively."""
    if type(a) is not type(b):
        return False
    if isinstance(a, BaseModel):
        return all(same(getattr(a, f), getattr(b, f)) for f in type(a).model_fields)
    if isinstance(a, list | tuple):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, datetime):
        return bool(a == b and a.tzinfo == b.tzinfo)
    return bool(a == b)


def _over_msgpack(wire: Any) -> Any:
    packed = msgpack.packb(wire, use_bin_type=True)
    return msgpack.unpackb(packed, raw=False, strict_map_key=False)


@LAWS
@given(typed_values)
def test_codec_1_round_trips_every_wire_value(typed: tuple[Any, Any]) -> None:
    annotation, value = typed
    assert annotation_problem(annotation) is None
    assert same(roundtrip(value, annotation), value)
    assert same(decode(_over_msgpack(encode(value)), annotation), value)


@LAWS
@given(typed_values)
def test_local_equals_remote(typed: tuple[Any, Any]) -> None:
    annotation, value = typed
    plan = wire_plan(annotation)
    remote = decode(_over_msgpack(encode(value)), annotation)
    assert same(plan.local(value), remote)


@LAWS
@given(typed_values)
def test_codec_1_equals_codec_2_when_fully_typed(typed: tuple[Any, Any]) -> None:
    annotation, value = typed
    plan = wire_plan(annotation)
    codec, wire = plan.to_wire(value, typed=True)
    via_negotiated = plan.decode(_over_msgpack(wire), codec)
    tagged = decode(_over_msgpack(encode(value)), annotation)
    assert same(via_negotiated, tagged)
    if codec == CODEC_TYPED:
        assert plan.typed


@given(st.one_of(st.integers(min_value=2**63), st.integers(max_value=-(2**63) - 1)))
@settings(max_examples=20, deadline=None)
def test_an_int_beyond_64_bits_does_not_cross_rpc(value: int) -> None:
    """Local == remote beyond 64 bits: the codec tags the int (``bigint``),
    the typed codec falls back to it, msgpack carries the tag."""
    from sandloom.core.rpc.wire import _pack, _unpack

    local = wire_plan(int).local(value)
    assert decode(_unpack(_pack(encode(value))), int) == local


NON_WIRE: list[Any] = [
    object,
    bytes,
    complex,
    Callable[[], int],
    type,
    Literal[1.5],
    dict[list[int], int],
]


@LAWS
@given(st.sampled_from(NON_WIRE), pairs)
def test_a_non_wire_annotation_is_refused_wherever_it_sits(
    bad: Any, wrapper: Pair
) -> None:
    assert annotation_problem(bad) is not None
    wrapped = wrapper[0]
    for annotation in (list[bad], dict[str, bad], bad | None, _of(tuple, wrapped, bad)):
        assert annotation_problem(annotation) is not None, annotation
