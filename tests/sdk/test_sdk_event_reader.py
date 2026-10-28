"""The event reader ignores unknown keys at every depth (spec §5.2, G-DECL-01).

An event payload is plain data: the closed set of wire shapes
(``sandloom.sdk._decl._event_wire``) is enforced at class definition (SANDLOOM-B015),
and ``from_wire`` decodes with the class's reader, built at definition: the
class's own validation schema in which every model that forbids extra keys
ignores them (a model open to extra keys keeps them), nothing is strict, and
every value is an instance of its declared class. Round 10 closed the set
instead of chasing edge cases: undiscriminated unions with a DTO, custom
``__init__`` / ``__new__``, wire names that do not round-trip, dataclasses,
``TypedDict`` and raw (before / wrap / plain) validators over a DTO are
refused; the reader no longer marks classes incomplete while it compiles.
"""

from __future__ import annotations

import enum
import json
import threading
import types
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import pytest
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    FailFast,
    Field,
    PrivateAttr,
    SerializeAsAny,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from sandloom.sdk import Event
from sandloom.sdk._decl import _event_wire
from sandloom.sdk._decl.events import _READERS, from_wire, to_wire
from sandloom.sdk.errors import EventDefinitionError

_STRICT = ConfigDict(extra="forbid")


class _A(BaseModel):
    model_config = _STRICT
    x: int = 0


class _ReqA(BaseModel):
    model_config = _STRICT
    x: int


class _Email(BaseModel):
    model_config = _STRICT
    kind: Literal["email"] = "email"
    email: str


class _Sms(BaseModel):
    model_config = _STRICT
    kind: Literal["sms"] = "sms"
    phone: str


class _Item(BaseModel):
    model_config = _STRICT
    sku: str
    qty: int


class _Node(BaseModel):
    model_config = _STRICT
    name: str
    children: list[_Node] = []


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    inner: _A | None = None


def _refused(match: str) -> Any:
    return pytest.raises(EventDefinitionError, match=match)


# ---- round 10: the reviewer's four repros ------------------------------- #


def test_round10_concurrent_first_decodes_cache_a_lenient_reader() -> None:
    """(round 10, item 1) the round-9 reader was built on the first decode
    and flipped ``__pydantic_complete__`` on every class while it compiled,
    with ``model_rebuild()`` outside its lock, so concurrent first decodes
    could cache a strict reader. Now the reader is built at class definition
    and compiling one touches no class: events sharing a strict DTO are
    defined from 8 threads at once while 4 others validate that DTO
    strictly, then 16 threads race the first decode of every class (its
    reader dropped to force a build); every decode ignores the nested key,
    every strict validation refuses it, and the cached readers stay
    lenient."""

    class Built(Event, channel="sdkread.round10.built.v1"):
        item: _ReqA

    assert Built in _READERS  # at definition, not on the first decode
    payload = {"item": {"x": 1, "new": 1}, "items": [{"x": 2, "n": 0}], "new": 0}
    events: list[type[Any]] = []
    failures: list[BaseException] = []

    def run(targets: list[Any]) -> None:
        barrier = threading.Barrier(len(targets))

        def call(target: Any) -> None:
            barrier.wait()
            try:
                target()
            except (AssertionError, ValueError) as exc:  # asserted below
                failures.append(exc)

        threads = [threading.Thread(target=call, args=(t,)) for t in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def define(i: int) -> None:
        class Shared(Event, channel=f"sdkread.round10.shared{i}.v1"):
            item: _ReqA
            items: list[_ReqA] = []

        events.append(Shared)

    def strict() -> None:
        for _ in range(200):
            try:
                _ReqA.model_validate({"x": 1, "new": 1})
            except ValidationError:
                continue
            raise AssertionError("_ReqA became lenient")

    def decode(cls: type[Any]) -> Any:
        def read() -> None:
            for _ in range(20):
                assert from_wire(cls, payload).items == [_ReqA(x=2)]

        return read

    run([lambda i=i: define(i) for i in range(8)] + [strict] * 4)
    assert failures == []
    assert len(events) == 8 and all(e in _READERS for e in events)
    for cls in events:
        del _READERS[cls]
    run([decode(events[i % 8]) for i in range(16)] + [strict] * 4)
    assert failures == []
    models: list[type[BaseModel]] = [*events, _ReqA]
    for model in models:
        assert model.__pydantic_complete__ is True
    for cls in events:
        assert from_wire(cls, payload).item == _ReqA(x=1)  # the cached reader


@pytest.mark.parametrize(
    "annotation",
    [
        _A | dict[str, Any],
        _A | _ReqA,
        Annotated[_A | _ReqA, Field(union_mode="left_to_right")],
        _A | _ReqA | None,
        list[_A | _ReqA],
        dict[str, list[_A | dict[str, int]]],
        _A | int,
        list[int] | str,
    ],
    ids=[
        "model|dict",
        "model|model",
        "left_to_right",
        "optional-union",
        "in-list",
        "nested",
        "model|scalar",
        "list|scalar",
    ],
)
def test_round10_an_undiscriminated_union_with_a_dto_is_refused(
    annotation: Any,
) -> None:
    """(round 10, item 2) ``item: A | dict[str, Any]`` with a strict ``A``:
    strict validation kept the dict, ``from_wire`` picked ``A`` and lost
    ``y`` (also ``A | B`` under ``union_mode="left_to_right"``). A union
    holding a DTO or a container is refused at definition, at any depth,
    with the field path and the fix (a discriminator)."""
    with _refused(r"Ev\.item.*without a discriminator") as info:
        types.new_class(
            "Ev",
            (Event,),
            {"channel": "sdkread.round10.union.v1"},
            lambda ns: ns.update(
                {"__annotations__": {"item": annotation}, "__module__": __name__}
            ),
        )
    assert info.value.sandloom_code == "SANDLOOM-B015"
    assert 'Field(discriminator="kind")' in str(info.value)


def test_round10_a_custom_init_or_new_is_refused_at_any_depth() -> None:
    """(round 10, item 3) a custom ``__init__`` wrapper -- allowed in round 9
    when its model was open to extra keys -- built a strict child with its
    own validator, so a newer key in the child raised ``extra_forbidden``.
    The reader never calls ``__init__``: any DTO that overrides ``__init__``
    or ``__new__`` is refused, nested and in a discriminated union too."""

    class Wrapper(BaseModel):
        child: _ReqA

        def __init__(self, **data: Any) -> None:
            super().__init__(**data)

    class Allocating(BaseModel):
        x: int

        def __new__(cls, *args: Any, **kwargs: Any) -> Allocating:
            return super().__new__(cls)

    class Tagged(BaseModel):
        kind: Literal["t"] = "t"

        def __init__(self, **data: Any) -> None:
            super().__init__(**data)

    with _refused(r"Ev\.item: Wrapper overrides __init__") as info:

        class Ev(Event, channel="sdkread.round10.init.v1"):
            item: Wrapper

    assert "model_validator" in str(info.value)
    with _refused(r"Ev\.items\[\]: Allocating overrides __new__"):

        class Ev(Event, channel="sdkread.round10.init.v1"):  # type: ignore[no-redef]
            items: list[Allocating]

    with _refused(r"Ev\.item<t>: Tagged overrides __init__"):

        class Ev(Event, channel="sdkread.round10.init.v1"):  # type: ignore[no-redef]
            item: Annotated[Tagged | _Email, Field(discriminator="kind")]


def test_round10_a_wire_name_that_does_not_round_trip_is_refused() -> None:
    """(round 10, item 4) ``Field(serialization_alias="wireValue")``:
    ``publish()`` emitted ``{"wireValue": 7}``, the wire schema accepted it
    and the reader raised ``value`` missing. Every field is published under
    the name its reader looks up first: ``alias=`` (both directions) and an
    ``alias_generator`` round-trip; a serialization- or validation-only
    alias, ``AliasChoices`` / ``AliasPath`` and an alias the model does not
    read (``validate_by_alias=False``) are refused, nested too."""
    with _refused(r"Ev\.value: is published as 'wireValue'") as info:

        class Ev(Event, channel="sdkread.round10.alias.v1"):
            value: int = Field(serialization_alias="wireValue")

    assert 'Field(alias="wireValue")' in str(info.value)
    with _refused(
        r"Ev\.value: is published as 'value' but its reader looks up 'wireValue'"
    ):

        class Ev(Event, channel="sdkread.round10.alias.v1"):  # type: ignore[no-redef]
            value: int = Field(validation_alias="wireValue")

    with _refused(r"Ev\.value: has an AliasPath / AliasChoices"):

        class Ev(Event, channel="sdkread.round10.alias.v1"):  # type: ignore[no-redef]
            value: int = Field(validation_alias=AliasChoices("value", "v"))

    class NotByAlias(BaseModel):
        model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
        user_name: str = Field(alias="userName")

    with _refused(r"Ev\.who\.user_name: is published as 'userName'"):

        class Ev(Event, channel="sdkread.round10.alias.v1"):  # type: ignore[no-redef]
            who: NotByAlias

    class Camel(BaseModel):
        model_config = ConfigDict(extra="forbid", alias_generator=to_camel)
        first_name: str
        last_seen: int = 0

    class Fine(Event, channel="sdkread.round10.alias.v1"):
        value: int = Field(alias="wireValue")
        who: Camel

    built = Fine.model_validate({"wireValue": 7, "who": {"firstName": "ann"}})
    assert to_wire(built) == {
        "wireValue": 7,
        "who": {"firstName": "ann", "lastSeen": 0},
    }
    assert from_wire(Fine, {**to_wire(built), "new": 1}) == built


# ---- round 11: the reviewer's four repros and two P3 -------------------- #


class _Color(enum.StrEnum):
    red = "red"
    blue = "blue"


class _Level(enum.IntEnum):
    one = 1
    two = 2


def _define(annotation: Any, *, config: ConfigDict | None = None) -> type[Any]:
    namespace: dict[str, Any] = {
        "__annotations__": {"x": annotation},
        "__module__": __name__,
    }
    if config is not None:
        namespace["model_config"] = config
    return types.new_class(
        "Ev",
        (Event,),
        {"channel": "sdkread.round11.union.v1"},
        lambda ns: ns.update(namespace),
    )


def _round_trip(cls: type[Any], event: Any) -> Any:
    return from_wire(cls, json.loads(json.dumps(to_wire(event), allow_nan=False)))


@pytest.mark.parametrize(
    "annotation",
    [
        int | str,
        Annotated[int | str, Field(union_mode="left_to_right")],
        UUID | str,
        float | int,
        int | float,
        bool | int,
        datetime | str,
        date | datetime,
        Decimal | float,
        _Color | str,
        Literal["1"] | int,
        Literal[1] | Literal[True],
        _Level | Literal["1"],
        list[int | str],
        dict[str, UUID | str] | None,
    ],
    ids=[
        "int|str",
        "left_to_right",
        "uuid|str",
        "float|int",
        "int|float",
        "bool|int",
        "datetime|str",
        "date|datetime",
        "decimal|float",
        "enum|str",
        "literal|int",
        "literal-1|literal-true",
        "intenum|literal-str",
        "in-list",
        "in-dict",
    ],
)
def test_round11_a_union_of_overlapping_scalars_is_refused(annotation: Any) -> None:
    """(round 11, item 1) ``strict=True`` with ``x: Annotated[int | str,
    Field(union_mode="left_to_right")]``: the class keeps ``"123"`` as a str,
    the reader (no strict flag) returned ``int`` 123; a constructed
    ``UUID | str`` read back as a str. A union of scalars is allowed only
    when every member is a ``Literal`` or an ``Enum`` and no member reads
    another member's published value; any other is refused at definition,
    at any depth, with the fix (one type, or a discriminated DTO)."""
    with _refused(r"Ev\.x.*union of scalars") as info:
        _define(annotation, config=ConfigDict(strict=True))
    assert info.value.sandloom_code == "SANDLOOM-B015"
    assert "use one type, or a discriminated DTO" in str(info.value)


@pytest.mark.parametrize(
    ("annotation", "values"),
    [
        (Literal["a", "b"] | Literal[1, 2], ["a", 2]),
        (_Color | Literal["green"], [_Color.blue, "green"]),
        (_Color | _Level, [_Color.red, _Level.two]),
        (Literal[True] | Literal["yes"], [True, "yes"]),
        (Literal["a"] | _Level | None, ["a", _Level.one, None]),
        (int | None, [0, None]),
        (UUID | None, [UUID(int=7), None]),
    ],
    ids=[
        "literals",
        "enum|literal",
        "strenum|intenum",
        "bool|str",
        "opt",
        "x|none",
        "uuid|none",
    ],
)
def test_round11_a_disjoint_literal_union_round_trips(
    annotation: Any, values: list[Any]
) -> None:
    """(round 11, item 1) the unions that stay allowed: ``X | None`` and
    unions of ``Literal`` / ``Enum`` members none of which reads another's
    value; strict or not, every value reads back with its type."""
    for strict in (False, True):
        cls = _define(annotation, config=ConfigDict(strict=strict))
        for value in values:
            event = cls(x=value)
            read = _round_trip(cls, event)
            assert read == event and type(read.x) is type(event.x)


@pytest.mark.parametrize(
    ("field", "problem"),
    [
        (Field(serialization_alias=""), r"Ev\.x: has the empty wire name"),
        (Field(alias=""), r"Ev\.x: has the empty wire name"),
        (Field(validation_alias="", serialization_alias=""), r"empty wire name"),
    ],
    ids=["serialization-alias", "alias", "both"],
)
def test_round11_an_empty_wire_name_is_refused(field: Any, problem: str) -> None:
    """(round 11, item 2) ``Field(serialization_alias="")`` bypassed the
    alias check (``or name`` read ``""`` as absent): ``publish()`` emitted
    ``{"": 7}`` and the delivery raised ``x`` missing. A wire name is a
    non-empty string, an alias generator's too."""
    with _refused(problem) as info:

        class Ev(Event, channel="sdkread.round11.empty.v1"):
            x: int = field

    assert 'Field(alias="x")' in str(info.value)

    class Blank(BaseModel):
        model_config = ConfigDict(alias_generator=lambda name: "")
        y: int

    with _refused(r"Ev\.d\.y: has the empty wire name"):

        class Ev(Event, channel="sdkread.round11.empty.v1"):  # type: ignore[no-redef]
            d: Blank


def test_round11_colliding_wire_names_are_refused() -> None:
    """(round 11, item 3) two fields with ``alias="x"`` passed:
    ``E(a=1, b=2)`` emitted ``{"x": 2}`` and decoded ``a=2, b=2``; an
    alias generator mapping two names to one key did the same. Wire names
    are unique within a model (case-sensitive), and when the model also
    reads fields by name, no field's name is another field's wire name (a
    payload missing ``c`` would fill ``b`` from ``a``'s key)."""
    with _refused(r"Ev\.b: has the wire name 'x' of field a") as info:

        class Ev(Event, channel="sdkread.round11.dup.v1"):
            a: int = Field(alias="x")
            b: int = Field(alias="x")

    assert "one wire name per field" in str(info.value)

    class Camel(BaseModel):
        model_config = ConfigDict(alias_generator=to_camel)
        first_name: str
        firstName: str

    with _refused(
        r"Ev\.d\.firstName: has the wire name 'firstName' of field first_name"
    ):

        class Ev(Event, channel="sdkread.round11.dup.v1"):  # type: ignore[no-redef]
            d: Camel

    with _refused(r"Ev\.b: its name is the wire name of field a"):

        class Ev(Event, channel="sdkread.round11.dup.v1"):  # type: ignore[no-redef]
            a: int = Field(alias="b")
            b: int = Field(0, alias="c")

    class Cased(Event, channel="sdkread.round11.dup.v1"):
        x: int
        X: int

    event = Cased(x=1, X=2)
    assert _round_trip(Cased, event) == event


@pytest.mark.parametrize(
    ("config", "key"),
    [
        (ConfigDict(ser_json_temporal="milliseconds"), "ser_json_temporal"),
        (ConfigDict(ser_json_timedelta="float"), "ser_json_timedelta"),
        (ConfigDict(ser_json_inf_nan="constants"), "ser_json_inf_nan"),
        (ConfigDict(ser_json_bytes="base64"), "ser_json_bytes"),
        (ConfigDict(val_json_bytes="base64"), "val_json_bytes"),
        (ConfigDict(val_temporal_unit="milliseconds"), "val_temporal_unit"),
        (ConfigDict(coerce_numbers_to_str=True), "coerce_numbers_to_str"),
        (ConfigDict(regex_engine="python-re"), "regex_engine"),
        (ConfigDict(polymorphic_serialization=True), "polymorphic_serialization"),
        (
            ConfigDict(json_schema_mode_override="validation"),
            "json_schema_mode_override",
        ),
        (ConfigDict(json_encoders={int: str}), "json_encoders"),
        (ConfigDict(allow_inf_nan=True), "allow_inf_nan"),
        (ConfigDict(use_enum_values=True), "use_enum_values"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_round11_a_config_that_changes_the_wire_form_is_refused(
    config: ConfigDict, key: str
) -> None:
    """(round 11, item 4) ``ConfigDict(ser_json_temporal="milliseconds")``
    was accepted: ``timedelta(seconds=2)`` was published as ``2000.0`` and
    read back as 2000 seconds, ``time(1, 2, 3)`` as ``3723000.0`` and
    refused. A model configuration may only set the keys that change
    neither the wire form nor how it is read (events.md lists them), on the
    event and on every DTO in it."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # json_encoders is deprecated
        with _refused(rf"Ev: model_config sets {key}=") as info:
            _define(timedelta, config=config)
        assert "drop it" in str(info.value)

        class Dto(BaseModel):
            model_config = config
            y: int = 0

        with _refused(rf"Ev\.x: Dto's model_config sets {key}="):
            _define(Dto)


def test_round11_field_options_that_change_the_wire_form_are_refused() -> None:
    """(round 11, item 4) the field-level twins: ``SerializeAsAny`` (the
    value's runtime class is published), ``coerce_numbers_to_str`` and a
    float that may be NaN or infinite (JSON has neither: the transport
    publishes ``null``, which the float reader refuses). An event's floats
    are finite by default; a DTO's need ``allow_inf_nan=False``."""

    class Dto(BaseModel):
        y: int = 0

    with _refused(r"Ev\.x: .*custom serializer"):
        _define(SerializeAsAny[Dto])
    with _refused(r"Ev\.x: .*coerce_numbers_to_str"):
        _define(Annotated[str, Field(coerce_numbers_to_str=True)])
    with _refused(r"Ev\.x: .*may be NaN or infinite"):
        _define(Annotated[float, Field(allow_inf_nan=True)])

    class Floating(BaseModel):
        y: float

    with _refused(r"Ev\.x\.y: .*may be NaN or infinite") as info:
        _define(Floating)
    assert "allow_inf_nan=False" in str(info.value)

    class Finite(BaseModel):
        model_config = ConfigDict(allow_inf_nan=False)
        y: float

    cls = _define(Finite)
    event = cls(x=Finite(y=-0.0))
    assert repr(_round_trip(cls, event).x.y) == "-0.0"
    plain = _define(float)
    with pytest.raises(ValidationError):
        plain(x=float("nan"))


def test_round11_the_allowed_configuration_keys_round_trip() -> None:
    """(round 11, item 4) the whitelisted keys change neither the wire form
    nor its reading: a DTO setting each of them round-trips."""

    class Configured(BaseModel):
        model_config = ConfigDict(
            title="Configured",
            extra="allow",
            frozen=True,
            alias_generator=to_camel,
            validate_by_name=True,
            loc_by_alias=False,
            str_strip_whitespace=True,
            str_to_lower=True,
            str_max_length=20,
            validate_assignment=True,
            strict=True,
            revalidate_instances="always",
            validate_default=True,
            hide_input_in_errors=True,
            allow_inf_nan=False,
            use_attribute_docstrings=True,
            cache_strings=False,
            json_schema_extra={"examples": []},
        )
        first_name: str
        color: _Color = _Color.red
        ratio: float = 0.5

    cls = _define(Configured)
    event = cls(x=Configured(first_name="  ANN ", color=_Color.blue, zz=1))
    assert event.x.first_name == "ann"
    assert to_wire(event)["x"] == {
        "firstName": "ann",
        "color": "blue",
        "ratio": 0.5,
        "zz": 1,
    }
    read = _round_trip(cls, event)
    assert read == event
    assert read.x.model_extra == {"zz": 1}


def test_round11_from_wire_reads_json_data_not_instances() -> None:
    """(round 11, P3) the strict class accepts an existing DTO instance;
    the reader's factory nodes do not. ``from_wire`` decodes a JSON-decoded
    delivery (the envelope's payload: dicts, lists and JSON scalars), where
    no instance can occur, so it refuses one explicitly rather than
    returning it unvalidated."""

    class Ev(Event, channel="sdkread.round11.instance.v1"):
        item: _ReqA

    assert Ev.model_validate({"item": _ReqA(x=1)}).item == _ReqA(x=1)
    with pytest.raises(ValidationError, match="model_type|dictionary"):
        from_wire(Ev, {"item": _ReqA(x=1)})
    assert from_wire(Ev, {"item": {"x": 1}}).item == _ReqA(x=1)


def test_round11_a_rebuilt_class_gets_a_fresh_reader() -> None:
    """(round 11, P3) a configuration change followed by ``model_rebuild``
    left the reader cached at definition stale: the reader is keyed by the
    class's core schema, so a rebuilt class is checked again and read with
    a reader of its new schema."""

    class Ev(Event, channel="sdkread.round11.rebuild.v1"):
        name: str
        span: timedelta = timedelta(0)

    assert from_wire(Ev, {"name": "a", "zz": 1}).model_extra is None
    Ev.model_config["extra"] = "allow"  # the class now keeps extra keys
    Ev.model_rebuild(force=True)
    event = Ev.model_validate({"name": "a", "zz": 1})
    assert to_wire(event) == {"name": "a", "span": "PT0S", "zz": 1}
    read = _round_trip(Ev, event)
    assert read == event and read.model_extra == {"zz": 1}
    Ev.model_config["ser_json_timedelta"] = "float"
    Ev.model_rebuild(force=True)
    assert to_wire(Ev(name="a", span=timedelta(seconds=2)))["span"] == 2.0
    with _refused(r"Ev: model_config sets ser_json_timedelta="):
        from_wire(Ev, {"name": "a", "span": 2.0})


# ---- the shapes the reader reads ---------------------------------------- #


def test_a_discriminated_or_aliased_payload_is_read_by_its_own_paths() -> None:
    """(round 9, item 2) a discriminated ``Email | Sms`` whose ``email``
    field is a string, and an aliased nested model (``loc_by_alias=False``),
    both read a newer producer's key; a wrong tag or a bad value raises."""

    class Aliased(BaseModel):
        model_config = ConfigDict(extra="forbid", loc_by_alias=False)
        user_name: str = Field(alias="userName")

    class Ev(Event, channel="sdkread.discriminated.v1"):
        item: Annotated[_Email | _Sms, Field(discriminator="kind")]
        many: list[Annotated[_Email | _Sms, Field(discriminator="kind")]] = []
        who: Aliased

    read = from_wire(
        Ev,
        {
            "item": {"kind": "email", "email": "a@b.c", "new": 1},
            "many": [{"kind": "sms", "phone": "1", "new": {"a": 1}}],
            "who": {"userName": "ann", "new": 2},
        },
    )
    assert read.item == _Email(email="a@b.c")
    assert read.many == [_Sms(phone="1")]
    assert read.who.user_name == "ann"
    with pytest.raises(ValidationError):
        from_wire(Ev, {"item": {"kind": "fax"}, "who": {"userName": "a"}})
    with pytest.raises(ValidationError):
        from_wire(Ev, {"item": {"kind": "sms", "email": "a"}, "who": {"userName": "a"}})


def test_a_fail_fast_list_of_any_length_reads_every_newer_item() -> None:
    """(round 9, item 3) no iteration bound: a ``FailFast`` list of strict
    items, each with a newer key, reads at any length."""

    class Ev(Event, channel="sdkread.failfast.v1"):
        items: Annotated[list[_Item], FailFast()]

    for n in (1, 9, 50):
        payload = {"items": [{"sku": f"s{i}", "qty": i, "new": i} for i in range(n)]}
        assert [i.qty for i in from_wire(Ev, payload).items] == list(range(n))
    with pytest.raises(ValidationError):
        from_wire(Ev, {"items": [{"sku": "a", "qty": "x", "new": 1}]})


def test_dicts_tuples_and_recursive_models_ignore_unknown_keys() -> None:
    """(G-DECL-01) ``dict[str, Strict]``, a tuple of a strict model and a
    recursive strict model ignore a newer key at every depth."""

    class Ev(Event, channel="sdkread.containers.v1"):
        keyed: dict[str, _Item]
        pair: tuple[_Item, int]
        many: tuple[_Item, ...] = ()
        tree: _Node

    read = from_wire(
        Ev,
        {
            "keyed": {"k": {"sku": "a", "qty": 1, "n": 0}},
            "pair": [{"sku": "b", "qty": 2, "n": 0}, 3],
            "many": [{"sku": "c", "qty": 4, "n": 0}],
            "tree": {
                "name": "r",
                "n": 0,
                "children": [
                    {"name": "c", "n": 1, "children": [{"name": "g", "n": 2}]}
                ],
            },
        },
    )
    assert read.keyed["k"] == _Item(sku="a", qty=1)
    assert read.pair == (_Item(sku="b", qty=2), 3)
    assert read.many == (_Item(sku="c", qty=4),)
    assert read.tree.children[0].children[0] == _Node(name="g")


def test_an_open_model_keeps_its_extras_and_its_strict_children_ignore() -> None:
    """(G-DECL-01) ``extra="allow"`` keeps its extras as data (the top
    level included), a strict model inside it still ignores them."""

    class Ev(Event, channel="sdkread.open.v1"):
        model_config = ConfigDict(extra="allow")
        info: _Open

    read = from_wire(
        Ev, {"info": {"name": "i", "k": 2, "inner": {"x": 1, "n": 0}}, "top": 1}
    )
    assert read.info.model_extra == {"k": 2}
    assert read.info.inner == _A(x=1)
    assert read.model_extra == {"top": 1}


def test_a_strict_dto_reads_the_wire_form_of_every_scalar() -> None:
    """(round 10) the reader is never strict: a DTO declared ``strict=True``
    reads the JSON form its producer publishes (a UUID, datetime or Decimal
    as a string); construction stays strict."""
    from datetime import datetime
    from decimal import Decimal
    from uuid import uuid4

    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        id: UUID
        at: datetime
        amount: Decimal

    class Ev(Event, channel="sdkread.strict.v1"):
        item: Strict

    built = Ev(item=Strict(id=uuid4(), at=datetime(2026, 1, 1), amount=Decimal("1.5")))
    assert from_wire(Ev, to_wire(built)) == built
    with pytest.raises(ValidationError):
        Strict.model_validate(to_wire(built)["item"])


def test_validators_run_once_and_real_errors_are_raised() -> None:
    """(G-DECL-01) after validators -- field and model, nested and on the
    event -- and a before validator on a scalar field run exactly once per
    read, and a real error is raised: below an ignored key, from a validator,
    in a discriminated branch."""
    calls: list[str] = []

    class Counted(BaseModel):
        model_config = _STRICT
        kind: Literal["c"] = "c"
        x: int
        label: str = ""

        @field_validator("label", mode="before")
        @classmethod
        def _raw(cls, value: Any) -> Any:
            calls.append("raw")  # a before validator on a scalar field
            return value

        @field_validator("x")
        @classmethod
        def _field(cls, value: int) -> int:
            calls.append("field")
            if value < 0:
                raise ValueError("negative")
            return value

        @model_validator(mode="after")
        def _after(self) -> Counted:
            calls.append("after")
            return self

    class Ev(Event, channel="sdkread.validators.v1"):
        item: Counted
        either: Annotated[Counted | _Email, Field(discriminator="kind")] | None = None

        @model_validator(mode="after")
        def _top(self) -> Ev:
            calls.append("top")
            return self

    from_wire(Ev, {"item": {"x": 1, "label": "l", "new": 1}, "new": 2})
    assert calls == ["field", "raw", "after", "top"]
    with pytest.raises(ValidationError, match=r"item\.x"):
        from_wire(Ev, {"item": {"x": "bad", "new": 1}})
    with pytest.raises(ValidationError, match="negative"):
        from_wire(Ev, {"item": {"x": -1, "new": 1}})
    with pytest.raises(ValidationError):
        from_wire(Ev, {"item": {"x": 1}, "either": {"kind": "c", "x": -1, "new": 1}})


def test_the_reader_returns_the_real_classes_and_construction_stays_strict() -> None:
    """(G-DECL-01) the decoded event and its nested values are instances of
    the declared classes (equal to producer-built ones, private state
    initialised, the same fields set), and building the reader leaves every
    class strict."""

    class Private(BaseModel):
        model_config = _STRICT
        _cache: int = PrivateAttr(default=3)
        x: int

    class Ev(Event, channel="sdkread.identity.v1"):
        item: _ReqA
        private: Private
        many: list[Annotated[_Email | _Sms, Field(discriminator="kind")]] = []

    built = Ev(item=_ReqA(x=1), private=Private(x=2), many=[_Sms(phone="2")])
    read = from_wire(Ev, {**to_wire(built), "new": 1})
    assert type(read) is Ev and type(read.item) is _ReqA
    assert type(read.many[0]) is _Sms
    assert read == built
    assert read.model_fields_set == built.model_fields_set
    assert read.private.__pydantic_private__ == {"_cache": 3}
    with pytest.raises(Exception, match="available on delivered"):
        read.meta  # noqa: B018 -- private state is initialised
    for cls in (Ev, _ReqA, _Email, _Sms, Private):
        assert cls.__pydantic_complete__ is True
    with pytest.raises(ValidationError):
        _ReqA.model_validate({"x": 1, "new": 1})
    with pytest.raises(ValidationError):
        Ev(item={"x": 1, "new": 1}, private={"x": 2})
    with pytest.raises(ValidationError):
        Ev.model_validate({"item": {"x": 1}, "private": {"x": 2}, "new": 1})


def test_a_forward_reference_is_refused_at_definition() -> None:
    """(round 10) every event is checked, and its reader built, when its
    class is defined: a field type that is not defined then is refused
    there (SANDLOOM-B015), naming the fix."""
    with _refused("not defined yet") as info:

        class Ev(Event, channel="sdkread.forward.v1"):
            item: _NeverDefined  # type: ignore[name-defined]  # noqa: F821

    assert "define every DTO before the event" in str(info.value)


# ---- the reader's contract with pydantic-core --------------------------- #


def test_the_reader_self_check_passes_on_the_pinned_pydantic() -> None:
    """(round 10) the reader relies on one pydantic-core behaviour: a
    ``model`` node instantiates its ``cls`` through ``__new__`` and fills
    the instance (``__dict__``, fields set, extra, private state, post-init)
    -- a factory class there yields the real class, compiled from the
    lenient schema. ``self_check`` verifies it on a canary before the first
    reader is stored; it passes on the pinned version."""
    assert list(_event_wire._canary()) == []
    _event_wire.self_check()


def test_a_broken_reader_contract_fails_loudly_naming_pydantic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(round 10) a pydantic upgrade that breaks the contract (simulated: a
    reader that is the class's own strict validator) fails the self-check
    with an error naming the pydantic and pydantic-core versions."""
    import pydantic

    monkeypatch.setattr(_event_wire, "_CHECKED", threading.Event())
    monkeypatch.setattr(
        _event_wire, "compile_reader", lambda cls: cls.__pydantic_validator__
    )
    with pytest.raises(
        RuntimeError, match=rf"pydantic {pydantic.VERSION} / pydantic-core"
    ):
        _event_wire.self_check()
