"""Every event class that defines round-trips its wire form (G-DECL-01).

The generic guard of the closed set of event wire shapes (round 11). A
hypothesis strategy builds random Event classes from every allowed shape --
each scalar with its edge values, ``X | None``, Literal / Enum unions,
lists, variadic and fixed tuples, sets, ``dict[str, T]``, DTOs nested to
any depth with random model configurations from the whitelist, random
aliases and alias generators, defaults, extra keys, discriminated unions
of DTOs -- and also from the shapes and options the set refuses (unions of
overlapping scalars, empty or colliding wire names, configurations that
change the wire encoding, floats that may be NaN). The property:

* a class either is refused at definition (SANDLOOM-B015), or
* every valid instance of it survives ``to_wire`` -> standard JSON (no NaN)
  -> ``from_wire`` with the same value AND the same type at every depth,
  and the strict class's own ``model_validate_json`` of the same JSON
  agrees with ``from_wire`` (value, type and fields set at every depth).

A class built only from allowed ingredients must define: the set is not
narrowed past what the docs promise.
"""

from __future__ import annotations

import enum
import itertools
import json
import math
import string
import types
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, reject, settings
from hypothesis import strategies as st
from pydantic import (
    AliasGenerator,
    BaseModel,
    ConfigDict,
    Field,
    PydanticUserError,
    StringConstraints,
    ValidationError,
)
from pydantic.alias_generators import to_camel, to_pascal, to_snake

from sandloom.sdk import Event
from sandloom.sdk._decl.events import from_wire, to_wire
from sandloom.sdk.errors import EventDefinitionError

Strategy = st.SearchStrategy[Any]
_COUNTER = itertools.count()
#: what pydantic raises when it refuses a generated class itself
_REFUSED_BY_PYDANTIC = (PydanticUserError, TypeError, ValueError)


# --------------------------------------------------------------------------- #
# Shapes: an annotation, a strategy of valid Python values, and whether the
# shape is in the documented closed set ("safe") or a probe the set may refuse
# --------------------------------------------------------------------------- #


@dataclass
class Shape:
    annotation: Any
    values: Strategy
    safe: bool = True
    hashable: bool = False


class _Color(enum.StrEnum):
    red = "red"
    empty = ""
    snow = "☃"


class _Level(enum.IntEnum):
    low = -1
    zero = 0
    high = 2**40


class _Mode(enum.Enum):
    on = "on"
    off = "off"


class _Ratio(enum.Enum):
    half = 0.5
    whole = 2


_FIXED_TZ = st.builds(
    lambda minutes: timezone(timedelta(minutes=minutes)),
    st.integers(-(24 * 60 - 1), 24 * 60 - 1),
)
_TZ = st.none() | st.just(UTC) | _FIXED_TZ
_FINITE = st.floats(allow_nan=False, allow_infinity=False)
_ANY_FLOAT = st.floats()


def _optional(annotation: Any) -> Any:
    """``annotation | None`` (``None`` stays ``None``)."""
    return None if annotation is None else annotation | None


def _or_none(values: Strategy) -> Strategy:
    """``values``, or ``None`` one time in four."""
    return st.integers(0, 3).flatmap(lambda i: st.none() if i == 0 else values)


def _json_values() -> Strategy:
    leaves = st.none() | st.booleans() | st.integers() | _FINITE | st.text()
    return st.recursive(
        leaves,
        lambda inner: (
            st.lists(inner, max_size=3)
            | st.dictionaries(st.text(max_size=4), inner, max_size=3)
        ),
        max_leaves=6,
    )


def _scalars() -> list[Shape]:
    """Every scalar of the closed set with its edge values."""
    return [
        Shape(str, st.text(), hashable=True),
        Shape(
            Annotated[str, StringConstraints(max_length=3)],
            st.text(max_size=3),
            hashable=True,
        ),
        Shape(int, st.integers(), hashable=True),
        Shape(Annotated[int, Field(ge=0)], st.integers(min_value=0), hashable=True),
        Shape(float, _FINITE, hashable=True),
        Shape(bool, st.booleans(), hashable=True),
        Shape(None, st.none(), hashable=True),
        Shape(UUID, st.uuids(), hashable=True),
        Shape(datetime, st.datetimes(timezones=_TZ), hashable=True),
        Shape(date, st.dates(), hashable=True),
        Shape(time, st.times(timezones=_TZ), hashable=True),
        Shape(timedelta, st.timedeltas(), hashable=True),
        Shape(
            Decimal,
            st.decimals(allow_nan=False, allow_infinity=False),
            hashable=True,
        ),
        Shape(_Color, st.sampled_from(_Color), hashable=True),
        Shape(_Level, st.sampled_from(_Level), hashable=True),
        Shape(_Mode, st.sampled_from(_Mode), hashable=True),
        Shape(_Ratio, st.sampled_from(_Ratio), hashable=True),
        Shape(
            Literal["a", "", "b c"], st.sampled_from(["a", "", "b c"]), hashable=True
        ),
        Shape(Literal[1, -2, 2**70], st.sampled_from([1, -2, 2**70]), hashable=True),
        Shape(Literal[True], st.just(True), hashable=True),
        Shape(Literal[_Color.red], st.just(_Color.red), hashable=True),
        Shape(Any, _json_values()),
        # unions that stay in the set: X | None, disjoint Literal / Enum
        Shape(
            Literal["a", "b"] | Literal[1, 2],
            st.sampled_from(["a", "b", 1, 2]),
            hashable=True,
        ),
        Shape(
            _Color | _Level,
            st.sampled_from([*_Color, *_Level]),
            hashable=True,
        ),
        Shape(
            Literal[True] | Literal["yes"],
            st.sampled_from([True, "yes"]),
            hashable=True,
        ),
    ]


def _probes() -> list[Shape]:
    """Shapes and field options the set may refuse (round 11, items 1, 4):
    a class holding one must be refused or round-trip."""

    def union(a: Shape, b: Shape, *, mode: Any = None) -> Shape:
        annotation: Any = a.annotation | b.annotation
        if mode is not None:
            annotation = Annotated[annotation, Field(union_mode=mode)]
        return Shape(annotation, st.one_of(a.values, b.values), safe=False)

    text = Shape(str, st.text())
    digits = Shape(str, st.text(string.digits, min_size=1))
    integer = Shape(int, st.integers())
    return [
        union(integer, digits),
        union(integer, digits, mode="left_to_right"),
        union(Shape(UUID, st.uuids()), text),
        union(Shape(float, _FINITE), integer),
        union(integer, Shape(float, _FINITE)),
        union(Shape(bool, st.booleans()), integer),
        union(Shape(datetime, st.datetimes()), text),
        union(Shape(date, st.dates()), Shape(datetime, st.datetimes())),
        union(Shape(Decimal, st.decimals(allow_nan=False)), Shape(float, _FINITE)),
        union(
            Shape(_Level, st.sampled_from(_Level)),
            Shape(Literal["0", "2"], st.sampled_from(["0", "2"])),
        ),
        union(Shape(Literal[1], st.just(1)), Shape(Literal[True], st.just(True))),
        Shape(Annotated[float, Field(allow_inf_nan=True)], _ANY_FLOAT, safe=False),
        Shape(Annotated[str, Field(coerce_numbers_to_str=True)], st.text(), safe=False),
    ]


# --------------------------------------------------------------------------- #
# DTOs: random fields, aliases and configurations
# --------------------------------------------------------------------------- #

_NAMES = ["a", "b", "value", "first_name", "firstName", "x", "X", "kind_of", "id"]

#: whitelisted configuration options (round 11): (key, values)
_SAFE_CONFIG: list[tuple[str, list[Any]]] = [
    ("extra", ["forbid", "ignore", "allow"]),
    ("frozen", [True, False]),
    ("strict", [True, False]),
    ("str_strip_whitespace", [True]),
    ("str_to_lower", [True]),
    ("str_to_upper", [True]),
    ("validate_default", [True]),
    ("validate_assignment", [True]),
    ("revalidate_instances", ["always", "subclass-instances"]),
    ("loc_by_alias", [False]),
    ("hide_input_in_errors", [True]),
    ("cache_strings", [False, "keys"]),
    ("title", ["T"]),
    ("allow_inf_nan", [False]),
    ("alias_generator", [to_camel, to_pascal, to_snake]),
]

#: configuration options that change the wire form or its reading
_RISKY_CONFIG: list[tuple[str, list[Any]]] = [
    ("ser_json_temporal", ["milliseconds", "seconds"]),
    ("ser_json_timedelta", ["float"]),
    ("ser_json_inf_nan", ["constants", "strings"]),
    ("coerce_numbers_to_str", [True]),
    ("val_temporal_unit", ["seconds", "milliseconds"]),
    ("polymorphic_serialization", [True]),
    ("allow_inf_nan", [True]),
    ("use_enum_values", [True]),
    (
        "alias_generator",
        [
            lambda name: "",
            lambda name: "k",
            lambda name: name.upper(),
            AliasGenerator(validation_alias=to_camel, serialization_alias=to_camel),
        ],
    ),
]


@dataclass
class _Field:
    name: str
    shape: Shape
    alias: str | None = None
    #: a (validation, serialization) alias pair instead of ``alias=``
    split: tuple[str, str] | None = None
    default: bool = False


def _generated(generator: Any, name: str, side: int) -> str:
    """``generator``'s alias of ``name``: validation (0) or serialization (1)."""
    if isinstance(generator, AliasGenerator):
        fn = generator.validation_alias if side == 0 else generator.serialization_alias
        return str(fn(name))  # type: ignore[misc]
    return str(generator(name))


@dataclass
class _Dto:
    """A model's spec: its fields, configuration, discriminator tag and
    whether it reads fields by name too (the Event base does)."""

    fields: list[_Field]
    config: dict[str, Any] = field(default_factory=dict)
    tag: str | None = None
    by_name: bool = False
    #: an Event's floats are finite by default (its base config)
    finite: bool = False

    def _all(self) -> list[_Field]:
        tag = [_Field("kind", Shape(str, st.just(self.tag)))] if self.tag else []
        return [*self.fields, *tag]

    def key(self, f: _Field, side: int) -> str:
        """The key ``f`` is read by (0) or published under (1)."""
        if f.split is not None:
            return f.split[side]
        if f.alias is not None:
            return f.alias
        generator = self.config.get("alias_generator")
        return f.name if generator is None else _generated(generator, f.name, side)

    @property
    def safe(self) -> bool:
        """Built only from what the closed set allows (round 11 rules)."""
        allowed = dict(_SAFE_CONFIG)
        for key, value in self.config.items():
            if key != "alias_generator" and value not in allowed.get(key, []):
                return False
        fields = self._all()
        reads = [self.key(f, 0) for f in fields]
        writes = [self.key(f, 1) for f in fields]
        if reads != writes or "" in writes or len(set(writes)) != len(writes):
            return False
        if self.by_name and any(
            f.name in writes and f.name != writes[i] for i, f in enumerate(fields)
        ):
            return False
        return all(f.shape.safe for f in fields) and not self.float_unsafe()

    def float_unsafe(self) -> bool:
        finite = self.config.get("allow_inf_nan", not self.finite) is False
        return not finite and any(_has_float(f.shape.annotation) for f in self.fields)


def _has_float(annotation: Any) -> bool:
    if annotation is float:
        return True
    return any(_has_float(a) for a in getattr(annotation, "__args__", ()) or ())


def _namespace(spec: _Dto) -> dict[str, Any]:
    namespace: dict[str, Any] = {
        "__annotations__": {},
        "__module__": __name__,
        "model_config": ConfigDict(**spec.config),  # type: ignore[typeddict-item]
    }
    for f in spec.fields:
        namespace["__annotations__"][f.name] = f.shape.annotation
        kwargs: dict[str, Any] = {}
        if f.split is not None:
            kwargs["validation_alias"], kwargs["serialization_alias"] = f.split
        elif f.alias is not None:
            kwargs["alias"] = f.alias
        if f.default:
            kwargs["default"] = None
        if kwargs:
            namespace[f.name] = Field(**kwargs)
    if spec.tag is not None:
        namespace["__annotations__"]["kind"] = Literal[spec.tag]
        namespace["kind"] = spec.tag
    return namespace


@st.composite
def _fields(
    draw: st.DrawFn, inner: Strategy, *, risky: bool, min_size: int = 0
) -> list[_Field]:
    names = draw(
        st.lists(st.sampled_from(_NAMES), min_size=min_size, max_size=4, unique=True)
    )
    out: list[_Field] = []
    for name in names:
        shape = draw(inner)
        default = draw(st.booleans())
        if default:  # a defaulted field is optional: X | None = None
            optional = _optional(shape.annotation)
            shape = Shape(optional, _or_none(shape.values), safe=shape.safe)
        f = _Field(name, shape, default=default)
        alias = draw(st.integers(0, 9))
        if alias == 0:
            f.alias = draw(st.sampled_from([f"w_{name}", f"{name}é"]))
        elif alias == 1 and risky:
            f.alias = draw(st.sampled_from(["", "w", *_NAMES]))
        elif alias == 2 and risky:
            f.split = (name, draw(st.sampled_from(["", "other", name])))
        out.append(f)
    return out


#: the scalars a configuration probe acts on
_PROBED = [
    Shape(timedelta, st.timedeltas()),
    Shape(time, st.times(timezones=_TZ)),
    Shape(datetime, st.datetimes(timezones=_TZ)),
    Shape(date, st.dates()),
    Shape(float, _ANY_FLOAT),
    Shape(str, st.text(string.digits + " Ab")),
    Shape(_Mode, st.sampled_from(_Mode)),
]


@st.composite
def _configs(draw: st.DrawFn, fields: list[_Field], *, risky: bool) -> dict[str, Any]:
    """A random configuration from the whitelist; in risky mode, half the
    time one option that changes the wire form too, with a field of the
    kind it acts on appended to ``fields``."""
    picks = draw(st.lists(st.sampled_from(_SAFE_CONFIG), max_size=4))
    if risky and draw(st.booleans()):
        picks.append(draw(st.sampled_from(_RISKY_CONFIG)))
        fields.append(_Field("probed", draw(st.sampled_from(_PROBED))))
    config: dict[str, Any] = {}
    for key, values in picks:
        config[key] = draw(st.sampled_from(values))
    if config.get("str_to_lower") and config.get("str_to_upper"):
        del config["str_to_upper"]
    return config


def _model_values(spec: _Dto, cls: type[BaseModel]) -> Strategy:
    """Valid instances of ``cls``, built by field name, as ``E(a=1, b=2)``
    (extra keys too, for a model that keeps or ignores them); a field with
    a default is sometimes omitted."""

    @st.composite
    def build(draw: st.DrawFn) -> Any:
        data: dict[str, Any] = {}
        for f in spec.fields:
            if f.default and draw(st.booleans()):
                continue
            data[f.name] = draw(f.shape.values)
        if spec.tag is not None:
            data["kind"] = spec.tag
        if spec.config.get("extra") in ("allow", "ignore"):
            keys = st.sampled_from(["zz", "zz_é"])
            data.update(draw(st.dictionaries(keys, _json_values(), max_size=2)))
        try:
            return cls.model_validate(data, by_alias=False, by_name=True)
        except ValidationError:
            reject()

    return build()


def _define(make: Callable[[], type[Any]]) -> type[Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return make()


@st.composite
def _dto_shape(
    draw: st.DrawFn, inner: Strategy, *, risky: bool, tag: str | None = None
) -> Shape:
    fields = draw(_fields(inner, risky=risky))
    spec = _Dto(fields, draw(_configs(fields, risky=risky)), tag)
    if not risky and spec.float_unsafe():
        spec.config["allow_inf_nan"] = False
    if not risky and not spec.safe:
        reject()  # e.g. an alias generator mapping two names to one key
    namespace = _namespace(spec)
    try:
        cls = _define(
            lambda: types.new_class(
                f"Dto{next(_COUNTER)}",
                (BaseModel,),
                {},
                lambda ns: ns.update(namespace),
            )
        )
    except _REFUSED_BY_PYDANTIC:
        reject()
    return Shape(cls, _model_values(spec, cls), safe=spec.safe)


@st.composite
def _tagged(draw: st.DrawFn, inner: Strategy, *, risky: bool) -> Shape:
    n = draw(st.integers(2, 3))
    branches = [draw(_dto_shape(inner, risky=risky, tag=f"t{i}")) for i in range(n)]
    union: Any = branches[0].annotation
    for b in branches[1:]:
        union = union | b.annotation
    return Shape(
        Annotated[union, Field(discriminator="kind")],
        st.one_of(*(b.values for b in branches)),
        safe=all(b.safe for b in branches),
    )


#: generic aliases subscripted with runtime annotations
_LIST: Any = list
_TUPLE: Any = tuple
_DICT: Any = dict
_SET: Any = set
_FROZENSET: Any = frozenset


def _containers(inner: Strategy, *, risky: bool) -> Strategy:
    hashable = inner.filter(lambda s: s.hashable)
    return st.one_of(
        inner.map(lambda s: Shape(_optional(s.annotation), _or_none(s.values), s.safe)),
        inner.map(
            lambda s: Shape(_LIST[s.annotation], st.lists(s.values, max_size=3), s.safe)
        ),
        inner.map(
            lambda s: Shape(
                _TUPLE[s.annotation, ...],
                st.lists(s.values, max_size=3).map(tuple),
                s.safe,
            )
        ),
        st.tuples(inner, inner).map(
            lambda p: Shape(
                _TUPLE[p[0].annotation, p[1].annotation],
                st.tuples(p[0].values, p[1].values),
                p[0].safe and p[1].safe,
            )
        ),
        inner.map(
            lambda s: Shape(
                _DICT[str, s.annotation],
                st.dictionaries(st.text(max_size=4), s.values, max_size=3),
                s.safe,
            )
        ),
        hashable.map(
            lambda s: Shape(_SET[s.annotation], st.sets(s.values, max_size=3), s.safe)
        ),
        hashable.map(
            lambda s: Shape(
                _FROZENSET[s.annotation], st.frozensets(s.values, max_size=3), s.safe
            )
        ),
        _dto_shape(inner, risky=risky),
        _tagged(inner, risky=risky),
    )


def _shapes(*, risky: bool) -> Strategy:
    leaves = st.sampled_from(_scalars())
    if risky:  # a probe as often as an allowed scalar
        leaves = leaves | st.sampled_from(_probes())
    return st.recursive(
        leaves,
        lambda inner: _containers(inner, risky=risky),
        max_leaves=5,
    )


@dataclass
class _EventSpec:
    cls: type[Any]
    values: Strategy
    safe: bool


@st.composite
def _events(draw: st.DrawFn, *, risky: bool) -> _EventSpec | None:
    """A random Event class (the Event base reads fields by alias and by
    name, publishes by alias), or ``None`` when it is refused at definition
    -- which a class built only from allowed ingredients never is."""
    fields = draw(_fields(_shapes(risky=risky), risky=risky, min_size=1))
    spec = _Dto(
        fields,
        draw(_configs(fields, risky=risky)),
        by_name=True,
        finite=True,
    )
    if not risky and not spec.safe:
        reject()
    namespace = _namespace(spec)
    try:
        cls = _define(
            lambda: types.new_class(
                f"Ev{next(_COUNTER)}",
                (Event,),
                {"channel": "sdkrt.probe.event.v1"},
                lambda ns: ns.update(namespace),
            )
        )
    except EventDefinitionError as exc:
        assert exc.sandloom_code == "SANDLOOM-B015"
        assert "\n  fix: " in str(exc)
        assert not spec.safe, f"an allowed class was refused: {exc}"
        return None
    except _REFUSED_BY_PYDANTIC:
        reject()
    return _EventSpec(cls, _model_values(spec, cls), spec.safe)


# --------------------------------------------------------------------------- #
# The oracle: same value AND type at every depth
# --------------------------------------------------------------------------- #


def same(a: Any, b: Any, where: str = "event", *, fields_set: bool = False) -> None:
    """Assert ``a`` and ``b`` are the same value of the same type at every
    depth (``fields_set``: and the same fields set in every model)."""
    assert type(a) is type(b), (
        f"{where}: {type(a).__name__} != {type(b).__name__} ({a!r} / {b!r})"
    )
    if isinstance(a, BaseModel):
        for name in type(a).model_fields:
            same(
                getattr(a, name),
                getattr(b, name),
                f"{where}.{name}",
                fields_set=fields_set,
            )
        same(a.model_extra, b.model_extra, f"{where}.<extra>", fields_set=fields_set)
        if fields_set:
            assert a.model_fields_set == b.model_fields_set, f"{where}: fields set"
    elif isinstance(a, dict):
        assert list(a) == list(b), f"{where}: keys {list(a)} != {list(b)}"
        for key in a:
            same(a[key], b[key], f"{where}[{key!r}]", fields_set=fields_set)
    elif isinstance(a, list | tuple):
        assert len(a) == len(b), f"{where}: length"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            same(x, y, f"{where}[{i}]", fields_set=fields_set)
    elif isinstance(a, set | frozenset):
        assert sorted(map(_typed, a)) == sorted(map(_typed, b)), (
            f"{where}: {a!r} != {b!r}"
        )
    else:
        assert _typed(a) == _typed(b), f"{where}: {a!r} != {b!r}"


def _typed(value: Any) -> str:
    """A scalar's exact identity: its type and every bit of its value."""
    if isinstance(value, float):
        return f"float:{value.hex()}" if not math.isnan(value) else "float:nan"
    if isinstance(value, Decimal):
        return f"Decimal:{value!s}"
    if isinstance(value, datetime | time):
        return f"{type(value).__name__}:{value.isoformat()}"
    return f"{type(value).__name__}:{value!r}"


def check_round_trip(cls: type[Any], event: Any) -> None:
    """The property, for one constructed event."""
    payload = to_wire(event)
    try:
        text = json.dumps(payload, allow_nan=False)
    except ValueError as exc:  # NaN / Infinity is not JSON
        raise AssertionError(f"the wire form is not JSON: {payload!r}") from exc
    read = from_wire(cls, json.loads(text))
    same(read, event)
    strict = cls.model_validate_json(text)
    same(read, strict, fields_set=True)


# --------------------------------------------------------------------------- #
# The tests
# --------------------------------------------------------------------------- #

_SETTINGS = settings(
    max_examples=250,
    derandomize=True,
    deadline=None,
    database=None,
    suppress_health_check=list(HealthCheck),
)


@_SETTINGS
@given(data=st.data())
def test_every_event_that_defines_round_trips(data: st.DataObject) -> None:
    """(G-DECL-01, round 11) random classes from the closed set AND from
    the shapes and options it refuses: each is refused at definition
    (SANDLOOM-B015, with a fix) or every valid instance round-trips through
    standard JSON with the same value and type at every depth, and the
    strict class reads the same JSON to the same result. A class built only
    from allowed ingredients always defines."""
    spec = data.draw(_events(risky=True))
    if spec is None:
        return
    event = data.draw(spec.values)
    check_round_trip(spec.cls, event)


def _repro(
    annotations: dict[str, Any],
    defaults: dict[str, Any] | None = None,
    config: ConfigDict | None = None,
) -> type[Any] | None:
    namespace: dict[str, Any] = {"__annotations__": annotations, "__module__": __name__}
    namespace.update(defaults or {})
    if config is not None:
        namespace["model_config"] = config
    try:
        return types.new_class(
            "Repro",
            (Event,),
            {"channel": "sdkrt.probe.repro.v1"},
            lambda ns: ns.update(namespace),
        )
    except EventDefinitionError as exc:
        assert exc.sandloom_code == "SANDLOOM-B015"
        return None


_REPROS: list[tuple[str, Callable[[], type[Any] | None], dict[str, Any]]] = [
    (
        "1-strict-left-to-right",
        lambda: _repro(
            {"x": Annotated[int | str, Field(union_mode="left_to_right")]},
            config=ConfigDict(strict=True),
        ),
        {"x": "123"},
    ),
    ("1-uuid-or-str", lambda: _repro({"x": UUID | str}), {"x": UUID(int=1)}),
    (
        "2-empty-serialization-alias",
        lambda: _repro({"x": int}, {"x": Field(serialization_alias="")}),
        {"x": 7},
    ),
    (
        "3-colliding-aliases",
        lambda: _repro(
            {"a": int, "b": int}, {"a": Field(alias="x"), "b": Field(alias="x")}
        ),
        {"a": 1, "b": 2},
    ),
    (
        "4-ser-json-temporal",
        lambda: _repro(
            {"d": timedelta, "t": time},
            config=ConfigDict(ser_json_temporal="milliseconds"),
        ),
        {"d": timedelta(seconds=2), "t": time(1, 2, 3)},
    ),
]


@pytest.mark.parametrize(
    ("make", "values"), [r[1:] for r in _REPROS], ids=[r[0] for r in _REPROS]
)
def test_the_property_holds_on_the_round11_repros(
    make: Callable[[], type[Any] | None], values: dict[str, Any]
) -> None:
    """(round 11) the reviewer's four repros, through the same property:
    each class is refused at definition or round-trips (on the round-10
    base each one defined and failed)."""
    cls = make()
    if cls is not None:
        check_round_trip(cls, cls(**values))


@_SETTINGS
@given(data=st.data())
def test_every_allowed_event_defines_and_round_trips(data: st.DataObject) -> None:
    """(G-DECL-01, round 11) the documented set is not narrowed: a class
    built only from allowed shapes, options and configurations defines,
    and round-trips."""
    spec = data.draw(_events(risky=False))
    assert spec is not None and spec.safe
    check_round_trip(spec.cls, data.draw(spec.values))
