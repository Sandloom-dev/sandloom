"""The files ``sandloom test`` generates (spec §5.2, §5.3, §5.8; SANDLOOM-T005/T007).

Stubs are rendered from the provider and pass the format gate; compatibility
is decided against the committed version before anything is written.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import typing
import uuid
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from sandloom.core.cli import generated
from sandloom.core.cli.generated import (
    Generated,
    apply,
    render_annotation,
    render_stub,
    schema_problems,
    stub_names,
    stub_problems,
)

FIXTURE_STUB = (
    Path(__file__).parent
    / "l1_modules"
    / "sdkfix_shared"
    / "contracts"
    / "sdkfix_stats_quota_v1.py"
)


def test_the_fixture_stub_is_the_generated_one() -> None:
    """The committed stub equals the regenerated one (T007 in miniature)."""
    from sdkfix_stats.contracts import Quota

    assert render_stub("sdkfix_stats.quota.v1", Quota) == FIXTURE_STUB.read_text()


def test_stub_names() -> None:
    assert stub_names("billing.credits.v1") == (
        "billing_credits_v1",
        "BillingCreditsV1",
        "BILLING_CREDITS_V1",
    )


class Dto(BaseModel):
    x: int


@pytest.mark.parametrize(
    ("annotation", "text"),
    [
        (int, "int"),
        (uuid.UUID, "UUID"),
        (dt.datetime | None, "datetime | None"),
        (list[decimal.Decimal], "list[Decimal]"),
        (dict[str, Any], "dict[str, Any]"),
        (Literal["a", "b"], 'Literal["a", "b"]'),
        (tuple[int, ...], "tuple[int, ...]"),
        (typing.Optional[str], "str | None"),  # noqa: UP045 - the typing form itself
        (Dto, "Dto"),
    ],
)
def test_annotations_render_as_source(annotation: Any, text: str) -> None:
    imports = generated._Imports()
    assert render_annotation(annotation, imports) == text


def test_stub_compatibility_is_additive() -> None:
    old = "class P(Protocol):\n    async def a(self, x: int) -> int: ...\n"
    added = old + "    async def b(self) -> bool: ...\n"
    assert stub_problems(old, added) == []
    assert stub_problems(None, old) == []
    changed = "class P(Protocol):\n    async def a(self, x: str) -> int: ...\n"
    assert stub_problems(old, changed) == [
        "method 'a' changed: (self, x: int) -> int -> (self, x: str) -> int"
    ]
    assert stub_problems(added, old) == ["method 'b' was removed"]


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def test_only_an_optional_field_may_be_added_on_the_same_version() -> None:
    old = _schema({"a": {"type": "string"}}, ["a"])
    assert schema_problems(None, old) == []
    assert (
        schema_problems(
            old, _schema({"a": {"type": "string"}, "b": {"type": "integer"}}, ["a"])
        )
        == []
    )
    assert schema_problems(old, _schema({}, [])) == ["field 'a' was removed"]
    assert schema_problems(old, _schema({"a": {"type": "integer"}}, ["a"])) == [
        "field 'a' changed type"
    ]
    assert schema_problems(
        old, _schema({"a": {"type": "string"}, "b": {"type": "integer"}}, ["a", "b"])
    ) == ["field 'b' is required now (only optional fields may be added)"]
    assert schema_problems(old, _schema({"a": {"type": "string"}}, [])) == [
        "field 'a' is optional now (readers rely on it)"
    ]


class _Status(str, enum.Enum):
    open = "open"
    closed = "closed"


class _Renamed(str, enum.Enum):
    open = "open"
    archived = "archived"


class _Widened(str, enum.Enum):
    open = "open"
    closed = "closed"
    archived = "archived"


class _Line(BaseModel):
    qty: int


class _LineStr(BaseModel):
    qty: str


class _LineNoted(BaseModel):
    qty: int
    note: str | None = None


class _LineRequired(BaseModel):
    qty: int
    note: str


def _event_schema(status: type, line: type) -> dict[str, Any]:
    """The wire schema of an event with a ``$defs`` enum and nested DTO."""
    from sandloom.sdk import Event

    class Probe(Event, channel="sdkgen.probe.changed.v1"):
        status: status  # type: ignore[valid-type]
        lines: list[line]  # type: ignore[valid-type]
        main: line | None = None  # type: ignore[valid-type]

    return Probe.wire_schema()


def test_a_referenced_enum_or_nested_type_change_is_incompatible() -> None:
    """G-DECL-01 at every depth: ``$ref``/``$defs`` are resolved, so a
    referenced enum's values and a nested DTO's fields follow the additive
    rule like top-level fields."""
    old = _event_schema(_Status, _Line)
    assert "$defs" in old  # the shapes below are referenced, not inlined
    assert schema_problems(old, _event_schema(_Status, _Line)) == []
    assert schema_problems(old, _event_schema(_Renamed, _Line)) == [
        "field 'status' changed its values: ['closed', 'open'] -> ['archived', 'open']"
    ]
    assert schema_problems(old, _event_schema(_Widened, _Line)) == [
        "field 'status' changed its values: ['closed', 'open'] -> "
        "['archived', 'closed', 'open']"
    ]
    assert schema_problems(old, _event_schema(_Status, _LineStr)) == [
        "field 'lines[].qty' changed type",
        "field 'main.qty' changed type",
    ]
    assert schema_problems(old, _event_schema(_Status, _LineRequired)) == [
        "field 'lines[].note' is required now (only optional fields may be added)",
        "field 'main.note' is required now (only optional fields may be added)",
    ]
    assert schema_problems(_event_schema(_Status, _LineNoted), old) == [
        "field 'lines[].note' was removed",
        "field 'main.note' was removed",
    ]


def test_an_added_optional_field_passes_at_every_depth() -> None:
    """An added defaulted field is optional to a reader even though the wire
    schema lists it as required (always emitted): it passes, top-level and
    nested."""
    from sandloom.sdk import Event

    class Before(Event, channel="sdkgen.probe.added.v1"):
        lines: list[_Line]

    class After(Event, channel="sdkgen.probe.added.v1"):
        lines: list[_LineNoted]
        comment: str | None = None

    assert schema_problems(Before.wire_schema(), After.wire_schema()) == []


class _Node(BaseModel):
    name: str
    children: list[_Node] = []


class _NodeRetyped(BaseModel):
    name: int
    children: list[_NodeRetyped] = []


class _Folder(BaseModel):
    title: str
    files: list[_File] = []


class _File(BaseModel):
    size: int
    parent: _Folder | None = None


class _FileRetyped(BaseModel):
    size: str
    parent: _FolderOfRetyped | None = None


class _FolderOfRetyped(BaseModel):
    title: str
    files: list[_FileRetyped] = []


for _model in (_Node, _NodeRetyped, _Folder, _File, _FileRetyped, _FolderOfRetyped):
    _model.model_rebuild()


def _tree_schema(node: type, folder: type) -> dict[str, Any]:
    """An event whose payload holds a self-recursive and a mutually recursive
    DTO (both ``$ref`` cycles through ``$defs``)."""
    from sandloom.sdk import Event

    class Tree(Event, channel="sdkgen.probe.tree.v1"):
        root: node  # type: ignore[valid-type]
        folder: folder  # type: ignore[valid-type]

    return Tree.wire_schema()


def test_a_recursive_dto_compares_without_recursing_forever() -> None:
    """G-DECL-01: the recursion guard keys on the ``($ref old, $ref new)``
    pair under comparison, not the ever-growing field path, so a recursive
    DTO compared with itself is compatible (no ``RecursionError``) and a
    change inside the cycle is still found."""
    tree = _tree_schema(_Node, _Folder)
    assert schema_problems(tree, _tree_schema(_Node, _Folder)) == []
    assert schema_problems(tree, _tree_schema(_NodeRetyped, _Folder)) == [
        "field 'root.name' changed type"
    ]
    assert schema_problems(tree, _tree_schema(_Node, _FolderOfRetyped)) == [
        "field 'folder.files[].size' changed type"
    ]


def _bounded(**constraints: Any) -> dict[str, Any]:
    """The wire schema of an event with one constrained field per keyword."""
    from pydantic import Field

    from sandloom.sdk import Event

    def field(name: str, default: Any = ...) -> Any:
        return Field(default, **constraints.get(name, {}))

    class Bounded(Event, channel="sdkgen.probe.bounded.v1"):
        value: int = field("value")
        ratio: float = field("ratio")
        code: str = field("code")
        tags: list[str] = field("tags")

    return Bounded.wire_schema()


@pytest.mark.parametrize(
    ("before", "after", "problem"),
    [
        (
            {"value": {"ge": 0}},
            {"value": {"ge": 1}},
            "lower bound (minimum 0 -> minimum 1)",
        ),
        ({}, {"value": {"ge": 0}}, "lower bound (none -> minimum 0)"),
        (
            {"value": {"ge": 0}},
            {"value": {"gt": 0}},
            "lower bound (minimum 0 -> exclusiveMinimum 0)",
        ),
        (
            {"value": {"le": 9}},
            {"value": {"le": 8}},
            "upper bound (maximum 9 -> maximum 8)",
        ),
        (
            {"value": {"lt": 9}},
            {"value": {"lt": 8}},
            "upper bound (exclusiveMaximum 9 -> exclusiveMaximum 8)",
        ),
        ({}, {"value": {"lt": 9}}, "upper bound (none -> exclusiveMaximum 9)"),
        (
            {"value": {"multiple_of": 2}},
            {"value": {"multiple_of": 3}},
            "multipleOf (2 -> 3)",
        ),
        ({}, {"value": {"multiple_of": 2}}, "multipleOf (none -> 2)"),
        (
            {"ratio": {"multiple_of": 0.5}},
            {"ratio": {"multiple_of": 1.0}},
            "multipleOf (0.5 -> 1.0)",
        ),
        (
            {"code": {"min_length": 1}},
            {"code": {"min_length": 2}},
            "minLength (1 -> 2)",
        ),
        (
            {"code": {"max_length": 9}},
            {"code": {"max_length": 8}},
            "maxLength (9 -> 8)",
        ),
        ({}, {"code": {"max_length": 9}}, "maxLength (none -> 9)"),
        (
            {"code": {"pattern": "^a"}},
            {"code": {"pattern": "^b"}},
            "pattern (^a -> ^b)",
        ),
        ({}, {"code": {"pattern": "^a"}}, "pattern (none -> ^a)"),
        ({"tags": {"min_length": 0}}, {"tags": {"min_length": 1}}, "minItems (0 -> 1)"),
        ({"tags": {"max_length": 5}}, {"tags": {"max_length": 4}}, "maxItems (5 -> 4)"),
    ],
)
def test_a_tightened_constraint_is_incompatible(
    before: dict[str, Any], after: dict[str, Any], problem: str
) -> None:
    """G-DECL-01: ``Field(ge=0)`` -> ``Field(ge=1)`` on the same channel makes
    the new reader reject ``{value: 0}`` an old producer still sends."""
    (name,) = after
    assert schema_problems(_bounded(**before), _bounded(**after)) == [
        f"field {name!r} narrowed {problem}"
    ]


@pytest.mark.parametrize(
    ("before", "after", "problem"),
    [
        (
            {"value": {"ge": 1}},
            {"value": {"ge": 0}},
            "lower bound (minimum 1 -> minimum 0)",
        ),
        ({"value": {"ge": 0}}, {}, "lower bound (minimum 0 -> none)"),
        (
            {"value": {"gt": 0}},
            {"value": {"ge": 0}},
            "lower bound (exclusiveMinimum 0 -> minimum 0)",
        ),
        (
            {"value": {"ge": 0}},
            {"value": {"gt": -1}},
            "lower bound (minimum 0 -> exclusiveMinimum -1)",
        ),
        (
            {"value": {"le": 8}},
            {"value": {"le": 9}},
            "upper bound (maximum 8 -> maximum 9)",
        ),
        (
            {"value": {"lt": 8}},
            {"value": {"le": 8}},
            "upper bound (exclusiveMaximum 8 -> maximum 8)",
        ),
        (
            {"value": {"multiple_of": 4}},
            {"value": {"multiple_of": 2}},
            "multipleOf (4 -> 2)",
        ),
        (
            {"ratio": {"multiple_of": 1.0}},
            {"ratio": {"multiple_of": 0.5}},
            "multipleOf (1.0 -> 0.5)",
        ),
        ({"value": {"multiple_of": 2}}, {}, "multipleOf (2 -> none)"),
        (
            {"code": {"min_length": 2}},
            {"code": {"min_length": 1}},
            "minLength (2 -> 1)",
        ),
        (
            {"code": {"max_length": 8}},
            {"code": {"max_length": 9}},
            "maxLength (8 -> 9)",
        ),
        ({"code": {"pattern": "^a"}}, {}, "pattern (^a -> none)"),
        ({"tags": {"max_length": 4}}, {}, "maxItems (4 -> none)"),
    ],
)
def test_a_loosened_constraint_is_incompatible_too(
    before: dict[str, Any], after: dict[str, Any], problem: str
) -> None:
    """Round 6 (G-DECL-01, both directions): ``Field(ge=1)`` -> ``Field(ge=0)``
    makes an OLD reader reject ``{value: 0}`` a NEW producer sends; a
    loosening is as breaking as a tightening on the same channel."""
    (name,) = before
    assert schema_problems(_bounded(**before), _bounded(**after)) == [
        f"field {name!r} loosened {problem}"
    ]


def test_an_unchanged_constraint_is_compatible() -> None:
    same = _bounded(value={"ge": 0})
    assert schema_problems(same, _bounded(value={"ge": 0})) == []


def test_a_changed_format_or_const_is_incompatible() -> None:
    old = _schema({"a": {"type": "string"}, "k": {"const": "x"}}, ["a", "k"])
    for changed in (
        {"a": {"type": "string", "format": "uuid"}, "k": {"const": "x"}},
        {"a": {"type": "string"}, "k": {"const": "y"}},
    ):
        problems = schema_problems(old, _schema(changed, ["a", "k"]))
        assert len(problems) == 1 and problems[0].endswith("changed type")


def test_unique_items_and_forbidden_extras_narrow_a_nested_dto() -> None:
    from sandloom.sdk import Event

    class AsList(BaseModel):
        ids: list[int]

    class AsSet(BaseModel):
        ids: set[int]

    def schema(item: type) -> dict[str, Any]:
        class Holder(Event, channel="sdkgen.probe.holder.v1"):
            item: item  # type: ignore[valid-type]

        return Holder.wire_schema()

    assert schema_problems(schema(AsList), schema(AsSet)) == [
        "field 'item.ids' narrowed uniqueItems (false -> true)"
    ]
    assert schema_problems(schema(AsSet), schema(AsList)) == [
        "field 'item.ids' loosened uniqueItems (true -> false)"
    ]
    # A schema that forbids extra properties (hand-written or validation
    # mode; pydantic's serialization schema never emits it).
    extras = _schema({"a": {"type": "string"}}, ["a"])
    closed = {**extras, "additionalProperties": False}
    assert schema_problems(extras, closed) == [
        "field '' narrowed additionalProperties (allowed -> forbidden)"
    ]
    assert schema_problems(closed, extras) == [
        "field '' loosened additionalProperties (forbidden -> allowed)"
    ]


def _keyed(key: Any, value: Any = int) -> dict[str, Any]:
    """The wire schema of an event with one ``dict[key, value]`` field."""
    from sandloom.sdk import Event

    class Keyed(Event, channel="sdkgen.probe.keyed.v1"):
        values: dict[key, value]  # type: ignore[valid-type]

    return Keyed.wire_schema()


def _key(**constraints: Any) -> Any:
    return typing.Annotated[str, StringConstraints(**constraints)]


def test_a_tightened_dictionary_key_constraint_is_incompatible() -> None:
    """Round 3 (G-DECL-01): ``propertyNames`` is traversed, so a key
    ``min_length=1 -> 2`` fails although the old producer may still send
    ``{"values": {"x": 1}}``; a loosened or removed key constraint passes."""
    old, new = _keyed(_key(min_length=1)), _keyed(_key(min_length=2))
    with pytest.raises(ValidationError):
        TypeAdapter(dict[_key(min_length=2), int]).validate_python({"x": 1})
    assert schema_problems(old, new) == [
        "field 'values{keys}' narrowed minLength (1 -> 2)"
    ]
    assert schema_problems(_keyed(str), old) == [
        "field 'values{keys}' narrowed minLength (none -> 1)"
    ]
    assert schema_problems(_keyed(_key(max_length=5)), _keyed(_key(max_length=4))) == [
        "field 'values{keys}' narrowed maxLength (5 -> 4)"
    ]
    assert schema_problems(new, old) == [
        "field 'values{keys}' loosened minLength (2 -> 1)"
    ]
    assert schema_problems(old, _keyed(str)) == [
        "field 'values{keys}' loosened minLength (1 -> none)"
    ]


def test_a_regex_keyed_dictionary_is_compared_per_pattern() -> None:
    """Round 3 (G-DECL-01): ``patternProperties`` is traversed: a value-type
    change under a pattern key fails, an added or changed pattern key fails,
    a removed one only loosens."""
    old = _keyed(_key(pattern="^a"), int)
    assert schema_problems(old, _keyed(_key(pattern="^a"), int)) == []
    assert schema_problems(old, _keyed(_key(pattern="^a"), str)) == [
        "field 'values{^a}' changed type"
    ]
    assert schema_problems(old, _keyed(_key(pattern="^b"), int)) == [
        "field 'values' changed its key patterns: ['^a'] -> ['^b']"
    ]
    assert schema_problems(_keyed(str), old) == ["field 'values' changed type"]
    bounded = _keyed(_key(pattern="^a"), typing.Annotated[int, Field(ge=0)])
    assert schema_problems(old, bounded) == [
        "field 'values{^a}' narrowed lower bound (none -> minimum 0)"
    ]
    two = {
        "type": "object",
        "patternProperties": {"^a": {"type": "integer"}, "^b": {"type": "string"}},
    }
    one = {"type": "object", "patternProperties": {"^a": {"type": "integer"}}}
    assert schema_problems(one, two) == [
        "field '' changed its key patterns: ['^a'] -> ['^a', '^b']"
    ]
    assert schema_problems(two, one) == [
        "field '' changed its key patterns: ['^a', '^b'] -> ['^a']"
    ]


def _array(**keywords: Any) -> dict[str, Any]:
    return {"type": "array", **keywords}


def _object(**keywords: Any) -> dict[str, Any]:
    return {"type": "object", **keywords}


_IF_A = {"required": ["a"]}


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (
            _array(prefixItems=[{"type": "integer"}]),
            _array(prefixItems=[{"type": "integer", "minimum": 0}]),
            "field '[0]' narrowed lower bound (none -> minimum 0)",
        ),
        (
            _array(),
            _array(contains={"type": "integer"}),
            "field '' narrowed contains (none -> schema)",
        ),
        (
            _array(contains={"type": "integer"}),
            _array(contains={"type": "integer", "maximum": 3}),
            "field '[contains]' narrowed upper bound (none -> maximum 3)",
        ),
        (
            _array(contains={}, minContains=1),
            _array(contains={}, minContains=2),
            "field '' narrowed minContains (1 -> 2)",
        ),
        (
            _array(),
            _array(**{"not": {"type": "string"}}),
            "field '' changed its not schema",
        ),
        (
            _object(**{"if": _IF_A, "then": {}}),
            _object(**{"if": {"required": ["b"]}, "then": {}}),
            "field '' changed its if schema",
        ),
        (
            _object(**{"if": _IF_A}),
            _object(**{"if": _IF_A, "then": {"minProperties": 2}}),
            "field '[then]' narrowed minProperties (none -> 2)",
        ),
        (
            _object(**{"if": _IF_A, "else": {"maxProperties": 3}}),
            _object(**{"if": _IF_A, "else": {"maxProperties": 2}}),
            "field '[else]' narrowed maxProperties (3 -> 2)",
        ),
        (
            _object(),
            _object(dependentSchemas={"a": {"minProperties": 2}}),
            "field '[dependentSchemas.a]' narrowed minProperties (none -> 2)",
        ),
        (
            _object(dependentRequired={"a": ["b"]}),
            _object(dependentRequired={"a": ["b", "c"]}),
            "field '' narrowed dependentRequired a (['b'] -> ['b', 'c'])",
        ),
        (
            _object(),
            _object(unevaluatedProperties=False),
            "field '' narrowed unevaluatedProperties (allowed -> forbidden)",
        ),
        (
            _object(unevaluatedProperties={"type": "integer"}),
            _object(unevaluatedProperties={"type": "string"}),
            "field '{unevaluated}' changed type",
        ),
        (
            _array(),
            _array(unevaluatedItems=False),
            "field '' narrowed unevaluatedItems (allowed -> forbidden)",
        ),
        (
            _array(),
            _array(items=False),
            "field '' narrowed items (allowed -> forbidden)",
        ),
    ],
)
def test_every_applicator_follows_the_narrowing_rule(
    old: dict[str, Any], new: dict[str, Any], problem: str
) -> None:
    """Round 3 (G-DECL-01): every JSON-schema applicator pydantic (or a
    hand-written schema) can carry is traversed with the narrowing rules."""
    assert schema_problems(old, new) == [problem]


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (
            _array(contains={"type": "integer"}),
            _array(),
            "field '' loosened contains (schema -> none)",
        ),
        (
            _array(contains={"type": "integer", "maximum": 3}),
            _array(contains={"type": "integer"}),
            "field '[contains]' loosened upper bound (maximum 3 -> none)",
        ),
        (
            _object(dependentSchemas={"a": {"minProperties": 2}}),
            _object(),
            "field '[dependentSchemas.a]' loosened minProperties (2 -> none)",
        ),
        (
            _object(dependentRequired={"a": ["b", "c"]}),
            _object(dependentRequired={"a": ["b"]}),
            "field '' loosened dependentRequired a (['b', 'c'] -> ['b'])",
        ),
        (
            _object(unevaluatedProperties=False),
            _object(),
            "field '' loosened unevaluatedProperties (forbidden -> allowed)",
        ),
        (
            _object(**{"if": _IF_A, "then": {"minProperties": 2}}),
            _object(**{"if": _IF_A}),
            "field '[then]' loosened minProperties (2 -> none)",
        ),
    ],
)
def test_a_loosened_applicator_is_incompatible_too(
    old: dict[str, Any], new: dict[str, Any], problem: str
) -> None:
    """Round 6 (G-DECL-01, both directions): an old reader rejects what a
    new producer of a loosened applicator may send."""
    assert schema_problems(old, new) == [problem]


def test_an_unchanged_applicator_is_compatible() -> None:
    negated = _array(**{"not": {"type": "string"}})
    assert schema_problems(negated, _array(**{"not": {"type": "string"}})) == []


def _with_values(**keywords: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(old, new)``: a ``dict[str, int]`` event field before and after
    ``keywords`` are added to its schema."""
    old = _keyed(str)
    new = _keyed(str)
    new["properties"]["values"].update(keywords)
    return old, new


@pytest.mark.parametrize(
    ("keywords", "values", "problem"),
    [
        (
            {"dependentSchemas": {"promo": {"required": ["total"]}}},
            {"promo": 1},
            "field 'values[dependentSchemas.promo].total' is required now "
            "(only optional fields may be added)",
        ),
        (
            {"required": ["total"]},
            {"promo": 1},
            "field 'values.total' is required now (only optional fields may be added)",
        ),
        (
            {"if": {}, "then": {"required": ["total"]}},
            {"promo": 1},
            "field 'values' changed its if schema",
        ),
        (
            {"properties": {"promo": {"type": "integer", "minimum": 5}}},
            {"promo": 1},
            "field 'values.promo' narrowed lower bound (none -> minimum 5)",
        ),
        (
            {"dependentSchemas": {"promo": {"properties": {"total": {"minimum": 5}}}}},
            {"promo": 1, "total": 1},
            "field 'values[dependentSchemas.promo].total' narrowed lower bound "
            "(none -> minimum 5)",
        ),
    ],
)
def test_a_constraint_on_a_node_without_properties_is_compared(
    keywords: dict[str, Any], values: dict[str, int], problem: str
) -> None:
    """Round 4 (G-DECL-01): ``required`` (and an added ``properties`` entry)
    narrows a node that has no ``properties`` of its own -- a dictionary or a
    subschema applicator -- so an old producer's ``{"values": {"promo": 1}}``
    that a new reader rejects is caught; removing the constraint fails too
    (round 6: an old reader would reject a new producer's payload)."""
    from sandloom.core.contracts.payload_validation import compile_payload_validator

    old, new = _with_values(**keywords)
    payload = {"values": values}
    assert compile_payload_validator(old).error(payload) is None
    assert compile_payload_validator(new).error(payload) is not None
    assert schema_problems(old, new) == [problem]
    # removing it breaks an old reader: a new producer may send the payload
    assert compile_payload_validator(old).error(payload) is None
    assert schema_problems(new, old) != []


def test_a_constraint_beside_a_ref_and_a_removed_key_pattern_are_compared() -> None:
    """Round 4 (G-DECL-01, found by the differential test): a keyword beside
    a ``$ref`` is not lost to the resolution, and the keys of a removed
    ``patternProperties`` entry fall back to ``additionalProperties``."""
    from sandloom.sdk import Event

    class Line(BaseModel):
        sku: str

    class Order(Event, channel="sdkgen.probe.order.v1"):
        lines: list[Line]

    old = Order.wire_schema()
    new = Order.wire_schema()
    new["properties"]["lines"]["items"]["minProperties"] = 3
    assert schema_problems(old, new) == [
        "field 'lines[]' narrowed minProperties (none -> 3)"
    ]
    assert schema_problems(new, old) == [
        "field 'lines[]' loosened minProperties (3 -> none)"
    ]
    patterned = _keyed(str)
    patterned["properties"]["values"]["patternProperties"] = {"^p": {}}
    assert schema_problems(patterned, _keyed(str)) == [
        "field 'values{^p}' changed type",
        # round 6, the other direction: the old reader still applies ``^p``
        "field 'values' changed its key patterns: ['^p'] -> []",
    ]


def test_apply_writes_in_dev_and_fails_stale_in_ci(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    target = shared / "contracts" / "x_y_v1.py"
    item = Generated(target, "content\n", "SANDLOOM-T007")
    assert apply([item], write=False, shared_dir=shared) == 1  # CI: stale fails
    assert not target.exists()
    assert apply([item], write=True, shared_dir=shared) == 0
    assert target.read_text() == "content\n"
    assert (shared / "__init__.py").is_file()
    assert (shared / "contracts" / "__init__.py").is_file()
    assert (
        apply(
            [Generated(target, "content\n", "SANDLOOM-T007")],
            write=False,
            shared_dir=shared,
        )
        == 0
    )


def test_a_stale_file_asks_for_sandloom_generate_and_is_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``sandloom test`` (``write=False``) never writes: a stale or missing
    generated file -- a hand-edited DTO copy included -- fails, listed under
    "generated files are stale: run `sandloom generate`"."""
    shared = tmp_path / "shared"
    copy = shared / "contracts" / "m_dto.py"
    copy.parent.mkdir(parents=True)
    copy.write_text("# hand-edited\n")
    stub = shared / "contracts" / "m_x_v1.py"
    files = [
        Generated(copy, "generated\n", "SANDLOOM-C012"),
        Generated(stub, "stub\n", "SANDLOOM-T007"),
    ]
    assert apply(files, write=False, shared_dir=shared, root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "generated files are stale: run `sandloom generate` (2 file(s)):" in out
    assert "shared/contracts/m_dto.py [SANDLOOM-C012] (differs)" in out
    assert "shared/contracts/m_x_v1.py [SANDLOOM-T007] (missing)" in out
    assert copy.read_text() == "# hand-edited\n" and not stub.exists()
    assert apply(files, write=True, shared_dir=shared, root=tmp_path) == 0
    assert copy.read_text() == "generated\n" and stub.read_text() == "stub\n"


def test_generate_refuses_a_weakened_route_and_names_the_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A weaker route policy is never written by ``sandloom generate``: it
    fails with SANDLOOM-T001, writes nothing, and names ``sandloom routes
    snapshot --write`` (the explicit approval)."""
    routes = tmp_path / "routes.snapshot.json"
    other = tmp_path / "module.snapshot.json"
    weakened = Generated(
        routes, "{}\n", "SANDLOOM-T001", problems=["GET /x: authentication was removed"]
    )
    assert (
        apply(
            [Generated(other, "{}\n", "SANDLOOM-T005"), weakened],
            write=True,
            shared_dir=tmp_path,
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "error[SANDLOOM-T001]" in out
    assert "`sandloom routes snapshot --write`" in out
    assert "sandloom generate: nothing was written" in out
    assert not routes.exists() and not other.exists()


def test_an_incompatible_change_fails_before_anything_is_written(
    tmp_path: Path,
) -> None:
    target = tmp_path / "events" / "x.y.v1.json"
    fine = Generated(tmp_path / "other.json", "{}\n", "SANDLOOM-T005")
    broken = Generated(
        target, "{}\n", "SANDLOOM-T005", problems=["field 'a' was removed"]
    )
    assert apply([fine, broken], write=True, shared_dir=tmp_path) == 1
    assert not target.exists() and not (tmp_path / "other.json").exists()


def test_plan_generates_the_l1_module_files(tmp_path: Path) -> None:
    """``plan`` over a booted L1 module: its snapshot, the producer schema
    export and the provider stub, all fresh on first run."""
    from types import SimpleNamespace

    import sdkfix_stats
    from fastapi import FastAPI

    from sandloom.core.facade import CoreFacade
    from sandloom.core.loader.manifest import AppManifest, module_manifest
    from sandloom.core.module_api import ModuleAPI, ModuleRegistrationDraft
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    draft = ModuleRegistrationDraft()
    descriptor = sdkfix_stats.register(ModuleAPI(core, draft=draft))
    draft.commit(core)
    manifest = AppManifest.of(
        [module_manifest(descriptor, draft, package="sdkfix_stats")]
    )
    app = SimpleNamespace(state=SimpleNamespace(app_manifest=manifest, core=core))
    shared = tmp_path / "shared"
    files = generated.plan(app, shared)
    targets = {f.path.relative_to(f.path.parents[1]).as_posix(): f for f in files}
    assert set(targets) == {
        "events/sdkfix_stats.counter.bumped.v1.json",
        "sdkfix_stats/module.snapshot.json",
        "sdkfix_stats/routes.snapshot.json",
        "contracts/sdkfix_stats_quota_v1.py",
    }
    stub = targets["contracts/sdkfix_stats_quota_v1.py"]
    assert stub.content == FIXTURE_STUB.read_text()
    snapshot = targets["sdkfix_stats/module.snapshot.json"]
    assert '"sdkfix_notes.note.created.v1"' in snapshot.content  # the consumer
    assert all(not f.problems for f in files)


def test_a_route_that_loses_its_authentication_is_incompatible() -> None:
    import json

    old = json.dumps(
        {"routes": [{"method": "GET", "path": "/x", "auth": "authenticated"}]}
    )
    weaker = {"routes": [{"method": "GET", "path": "/x", "auth": "public"}]}
    same = {"routes": [{"method": "GET", "path": "/x", "auth": "authenticated"}]}
    assert generated.route_problems(old, weaker) == [
        "GET /x: authentication was removed"
    ]
    assert generated.route_problems(old, same) == []
    assert generated.route_problems(None, weaker) == []


_STRICT = {
    "method": "POST",
    "path": "/x",
    "auth": "authenticated",
    "policy": {
        "effect": "write",
        "session": "live, re-check before commit",
        "risk": {"admin": ["sandloom.shared.guards.platform.require_platform_admin"]},
        "idempotency": "explicit durable, fail-closed",
        "audit": "automatic",
        "step_up_max_age_s": 300,
        "authz": ["probe.can_edit"],
    },
}


def _weakened(**policy: Any) -> dict[str, Any]:
    """``_STRICT`` with policy fields replaced (``None`` drops one)."""
    merged = {**_STRICT["policy"], **policy}
    entry = {**_STRICT, "policy": {k: v for k, v in merged.items() if v is not None}}
    return {"routes": [entry]}


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"risk": None}, "risk 'admin' was removed"),
        (
            {"idempotency": "none"},
            "idempotency was removed (explicit durable, fail-closed -> none)",
        ),
        (
            {"idempotency": "explicit durable"},
            "idempotency no longer fails closed "
            "(explicit durable, fail-closed -> explicit durable)",
        ),
        (
            {"idempotency": "explicit cache, fail-closed"},
            "idempotency is no longer durable "
            "(explicit durable, fail-closed -> explicit cache, fail-closed)",
        ),
        (
            {"session": "none"},
            "the session check was weakened (live, re-check before commit -> none)",
        ),
        (
            {"session": "live, re-check if a unit writes"},
            "the session check was weakened "
            "(live, re-check before commit -> live, re-check if a unit writes)",
        ),
        ({"audit": None}, "the automatic audit was dropped"),
        ({"step_up_max_age_s": None}, "step-up was removed"),
        ({"step_up_max_age_s": 3600}, "step-up was relaxed (300 s -> 3600 s)"),
        ({"authz": None}, "authorization probe.can_edit was removed"),
    ],
)
def test_every_weakened_route_policy_is_incompatible(
    change: dict[str, Any], problem: str
) -> None:
    """G-ROUTE-04 / spec §5.8: a weaker policy fails ``sandloom test`` before the
    snapshot is written (approval: ``sandloom routes snapshot --write``)."""
    import json

    old = json.dumps({"routes": [_STRICT]})
    assert generated.route_problems(old, {"routes": [_STRICT]}) == []
    assert generated.route_problems(old, _weakened(**change)) == [f"POST /x: {problem}"]


def test_a_route_made_public_is_incompatible() -> None:
    import json

    old = json.dumps({"routes": [_STRICT]})
    public = {
        "routes": [
            {
                **_STRICT,
                "auth": "public",
                "policy": {
                    "effect": "write",
                    "public_reason": "why",
                    "session": "none",
                },
            }
        ]
    }
    problems = generated.route_problems(old, public)
    assert problems[0] == "POST /x: authentication was removed"
    assert "POST /x: the route became public (why)" in problems


def test_a_stronger_route_policy_is_compatible() -> None:
    import json

    weak = _weakened(
        risk=None, idempotency="none", audit=None, step_up_max_age_s=None, authz=None
    )
    assert generated.route_problems(json.dumps(weak), {"routes": [_STRICT]}) == []


_AUTO_S = "automatic S"
_AUTO_B = "automatic B (echoes the caller's input)"
_DURABLE = "explicit durable, fail-closed"
_CACHE_CLOSED = "explicit cache, fail-closed"
_CACHE_OPEN = "explicit cache"
_X_CLOSED = "explicit in-flight, fail-closed"
_X_OPEN = "explicit in-flight"
_NONE_REASON = "none (streams the response)"


@pytest.mark.parametrize(
    ("before", "after", "lost"),
    [
        (_AUTO_S, _X_CLOSED, ["is no longer durable"]),
        (_AUTO_B, _X_CLOSED, ["is no longer durable"]),
        (_AUTO_S, _CACHE_CLOSED, ["is no longer durable"]),
        (_AUTO_S, _CACHE_OPEN, ["no longer fails closed", "is no longer durable"]),
        (_AUTO_S, _X_OPEN, ["no longer fails closed", "is no longer durable"]),
        (_AUTO_S, "none", ["was removed"]),
        (_AUTO_B, _NONE_REASON, ["was removed"]),
        (_DURABLE, _X_CLOSED, ["is no longer durable"]),
        (_CACHE_CLOSED, _CACHE_OPEN, ["no longer fails closed"]),
        (_CACHE_CLOSED, _X_OPEN, ["no longer fails closed"]),
        (_X_CLOSED, _X_OPEN, ["no longer fails closed"]),
        (_X_CLOSED, _CACHE_OPEN, ["no longer fails closed"]),
        (_X_CLOSED, _NONE_REASON, ["was removed"]),
        (_CACHE_OPEN, "none", ["was removed"]),
        (_AUTO_S, "explicit mystery", ["changed to an unknown mode"]),
    ],
)
def test_every_idempotency_mode_transition_that_loses_a_property_is_flagged(
    before: str, after: str, lost: list[str]
) -> None:
    """G-ROUTE-04: each snapshot spelling maps to properties (present,
    fail-closed, durable); losing any one of them is a weakening, whatever
    the spelling (``automatic S`` -> ``explicit in-flight, fail-closed``
    loses durable replay protection)."""
    import json

    old = json.dumps({"routes": [_weakened(idempotency=before)["routes"][0]]})
    problems = generated.route_problems(old, _weakened(idempotency=after))
    change = f"({before} -> {after})"
    assert problems == [f"POST /x: idempotency {what} {change}" for what in lost]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("none", _X_OPEN),
        (_NONE_REASON, _AUTO_S),
        (_X_OPEN, _X_CLOSED),
        (_X_CLOSED, _CACHE_CLOSED),
        (_CACHE_CLOSED, _AUTO_S),
        (_X_CLOSED, _DURABLE),
        (_AUTO_S, _AUTO_B),
        (_AUTO_B, _AUTO_S),
        (_AUTO_S, _DURABLE),
        (_DURABLE, _AUTO_S),
        ("none", _NONE_REASON),
        (_NONE_REASON, "none"),
    ],
)
def test_an_idempotency_mode_that_keeps_every_property_is_compatible(
    before: str, after: str
) -> None:
    import json

    old = json.dumps({"routes": [_weakened(idempotency=before)["routes"][0]]})
    assert generated.route_problems(old, _weakened(idempotency=after)) == []


def test_every_snapshot_idempotency_spelling_has_properties() -> None:
    """The weakening model knows every spelling the snapshot writer emits
    (no spelling falls through to "unknown")."""
    from sandloom.core.cli.route_weakening import idempotency_props
    from sandloom.core.route_snapshots import _GUARD_KINDS

    for spelling in (
        "none",
        _NONE_REASON,
        _AUTO_S,
        _AUTO_B,
        *(
            f"explicit {kind}{closed}"
            for kind in _GUARD_KINDS.values()
            for closed in ("", ", fail-closed")
        ),
    ):
        assert idempotency_props(spelling) is not None, spelling


def test_sandloom_test_refuses_a_weakened_route_snapshot_and_the_write_approves_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``sandloom test`` (dev) never overwrites a snapshot with a weaker policy;
    ``sandloom routes snapshot --write`` is the explicit approval and prints what
    it approves (the reviewed diff records it)."""
    import json

    from sandloom.core.cli import routes as routes_cli

    target = tmp_path / "routes.snapshot.json"
    target.write_text(json.dumps({"routes": [_STRICT]}))
    weaker = _weakened(audit=None)
    item = Generated(target, json.dumps(weaker), "SANDLOOM-T001")
    item.problems = generated.route_problems(item.current, weaker)
    assert apply([item], write=True, shared_dir=tmp_path) == 1
    assert json.loads(target.read_text()) == {"routes": [_STRICT]}
    out = capsys.readouterr().out
    assert "POST /x: the automatic audit was dropped" in out
    assert "sandloom routes snapshot --write" in out

    approved = routes_cli.weakenings(target, weaker)
    assert approved == ["POST /x: the automatic audit was dropped"]


def test_a_generated_stub_is_ruff_formatted(tmp_path: Path) -> None:
    """String defaults and ``Literal`` values are double-quoted, so a stub
    passes ``ruff format --check`` as generated (WS-C report)."""
    import subprocess
    import sys

    from sandloom.sdk.contracts import contract_method

    class Defaults:
        @classmethod
        @contract_method
        async def note(
            cls, reason: str = "it's", kind: Literal["a", "b"] = "a", n: int = 2
        ) -> str:
            return reason

    stub = render_stub("sdkgen.defaults.v1", Defaults)
    assert 'reason: str = "it\'s"' in stub
    assert 'kind: Literal["a", "b"] = "a"' in stub and "n: int = 2" in stub
    path = tmp_path / "sdkgen_defaults_v1.py"
    path.write_text(stub)
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "--no-cache", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


class _StubDtoWithAVeryLongNameNumberOne(BaseModel):
    a: int


class _StubDtoWithAVeryLongNameNumberTwo(BaseModel):
    b: int


class _StubDtoWithAVeryLongNameNumberThree(BaseModel):
    c: int


def _stub_providers() -> dict[str, tuple[str, type]]:
    """Representative providers, each at a ``ruff format`` line-length edge:
    ``{case: (contract name, provider)}``."""
    from sandloom.sdk.contracts import contract_method

    class DocumentedAt88:
        """A provider docstring.

        Second paragraph, kept as written."""

        # flat signature + ":" is exactly 88 columns (+ ": ..." would be 92)
        @classmethod
        @contract_method
        async def send(
            cls, recipient_id: uuid.UUID, template: str, operation_key: str
        ) -> uuid.UUID:
            """Record ``template`` for ``recipient_id``; the message id."""
            return recipient_id

        @classmethod
        @contract_method
        async def short(cls, n: int) -> int:
            """One line.

            And a second paragraph."""
            return n

    class Undocumented:
        # flat signature + ": ..." is exactly 88 columns
        @classmethod
        @contract_method
        async def send(
            cls, recipient_id: uuid.UUID, template: str, operation: str
        ) -> uuid.UUID:
            return recipient_id

        # one column more: the parameters move to their own line
        @classmethod
        @contract_method
        async def sendx(
            cls, recipient_id: uuid.UUID, template: str, operation: str
        ) -> uuid.UUID:
            return recipient_id

        @classmethod
        @contract_method
        async def tiny(cls) -> None:
            return None

    class Exploded:
        @classmethod
        @contract_method
        async def charge(
            cls,
            account_id: uuid.UUID,
            amount: decimal.Decimal,
            currency: Literal["EUR", "USD", "GBP"],
            *,
            operation_key: str,
            memo: str | None = None,
            when: dt.datetime | None = None,
        ) -> list[_StubDtoWithAVeryLongNameNumberOne]:
            """Charge the account; one line per parameter."""
            return []

        @classmethod
        @contract_method
        async def lookup(
            cls, first: _StubDtoWithAVeryLongNameNumberTwo, *, limit: int = 50
        ) -> dict[str, _StubDtoWithAVeryLongNameNumberThree]:
            return {}

    class NoMethods:
        """Only a docstring."""

    return {
        "documented-at-88": ("notifications.mail.v1", DocumentedAt88),
        "undocumented-at-88": ("sdkgen.undocumented.v1", Undocumented),
        "exploded-and-long-imports": (
            "payments_and_settlements.charges_with_a_long_name.v1",
            Exploded,
        ),
        "no-methods": ("sdkgen.empty.v1", NoMethods),
        "declaration-head-overflows": (
            "settlements_reconciliation.ledger_entries_import.v1",
            NoMethods,
        ),
        "declaration-value-overflows-too": (
            "settlements_reconciliation_and_reporting.ledger_entries_import_batch.v12",
            NoMethods,
        ),
    }


@pytest.mark.parametrize("case", sorted(_stub_providers()))
def test_a_rendered_stub_is_what_ruff_format_writes(case: str) -> None:
    """``render_stub`` emits ``ruff format``'s own layout (default settings,
    as every generated app's pyproject): formatting a generated stub is a
    no-op, at each line-length edge of the signature, the imports and the
    reference declaration."""
    import subprocess
    import sys

    contract, provider = _stub_providers()[case]
    stub = render_stub(contract, provider)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--isolated",
            "--stdin-filename",
            "stub.py",
            "-",
        ],
        input=stub,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == stub
