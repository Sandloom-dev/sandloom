"""The closed set of event wire shapes and the event reader (spec §5.2).

An event payload is plain data. :func:`check_wire_shapes` walks an Event
class's core schema -- what pydantic actually validates, so a custom type or
an ``Annotated`` marker is judged by its effect -- and refuses, at class
definition (SANDLOOM-B015), every shape outside the closed set below, naming the
field path and the allowed alternative:

* scalars: ``str``, ``int``, ``float`` (finite: ``allow_inf_nan=False``,
  the Event base's default), ``bool``, ``None``, ``UUID``,
  ``datetime`` / ``date`` / ``time`` / ``timedelta``, ``Decimal``, an
  ``Enum`` (``StrEnum``, ``IntEnum``) and a ``Literal``, with their
  constraints; ``Any`` is an opaque JSON value (read as it is);
* containers: ``X | None``, ``list[T]``, ``tuple[T, ...]`` and fixed tuples,
  ``set`` / ``frozenset`` of scalars, ``dict[str, T]``;
* a pydantic ``BaseModel`` DTO without its own ``__init__`` / ``__new__``,
  not a ``RootModel`` nor an unparametrised generic, without a computed
  field or any serializer of its own (``SerializeAsAny`` included), whose
  every field has one non-empty wire name -- published under it, looked up
  first by its reader (``alias=`` is both), unique in its model, and not
  another field's name when the model also reads by name -- and whose
  excluded fields have a default; its ``model_config`` sets only the
  options in ``_CONFIG_ALLOWED`` (none changes the wire form or how it is
  read: ``ser_json_*``, ``coerce_numbers_to_str``, ``use_enum_values``...
  are refused); ``extra`` may be anything;
* unions: ``X | None``, a union of ``Literal`` / ``Enum`` members none of
  which reads another member's published value (so the reader returns the
  member the producer held, whatever the union mode or strictness), or a
  union of DTOs discriminated by a ``Literal`` tag field
  (``Field(discriminator="kind")``);
* validators: ``mode="after"`` anywhere (it sees the validated value);
  ``mode="before"`` / ``"wrap"`` only below which no DTO is validated (one
  that sees a raw object could build a DTO with its strict validator, or
  reject a newer producer's key); never ``mode="plain"``.

:func:`compile_reader` builds the reader of a class in that set: a copy of
its core schema in which every model forbidding extra keys ignores them and
nothing is strict, whose ``model`` nodes name a *factory* class instead of
the DTO -- pydantic-core builds a model node of a complete pydantic class
from that class's own (strict) validator, but a factory is no pydantic
class: the node is compiled from the copy, and the factory's ``__new__``
allocates an instance of the real class, which pydantic-core fills
(``__dict__``, fields set, extra, private state) and post-inits as it fills
any model. No class is touched, so readers compile concurrently, once per
class, at definition. :func:`self_check` verifies that contract with
pydantic-core on a canary before the first reader is used.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import enum
import re
import threading
import typing
import uuid
from collections.abc import Callable, Iterator
from typing import Any, ClassVar, cast

import pydantic
import pydantic_core
from pydantic import BaseModel, ConfigDict, PrivateAttr
from pydantic_core import SchemaValidator, core_schema

#: Core-schema keys whose value is data or a reference, never a subschema.
_DATA_KEYS = frozenset(
    {"metadata", "default", "cls", "ref", "schema_ref", "expected", "members"}
)
#: Core-schema keys whose value maps names (or tags) to subschemas.
_NAMED = frozenset({"fields", "choices"})
#: Core-schema types of the wire scalars.
_SCALARS = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "none",
        "uuid",
        "datetime",
        "date",
        "time",
        "timedelta",
        "decimal",
        "enum",
        "literal",
    }
)
_ANY: dict[str, Any] = {"type": "any"}
#: Validators that keep the inner schema (their function sees the value
#: before / around it; ``function-after`` sees the validated value).
_RAW_VALIDATORS = frozenset({"function-before", "function-wrap"})

FIX_TYPES = (
    "use str, int, float, bool, None, UUID, datetime, date, time, timedelta, "
    "Decimal, an Enum, a Literal, Any (an opaque JSON value), a BaseModel DTO, "
    "or X | None, list, tuple, set / frozenset (of scalars) and dict[str, ...] "
    "of them (docs: concepts/events.md, What an event field may be)"
)
_FIX_UNION = (
    'give every DTO branch a tag field (kind: Literal["a"]) and declare '
    'Annotated[A | B, Field(discriminator="kind")]; a plain union is X | None '
    "or a union of Literal / Enum values"
)
_FIX_SCALAR_UNION = (
    "use one type, or a discriminated DTO; a union of scalars may only be "
    "X | None or Literal / Enum members none of which reads another's value "
    '(Literal["a", "b"] | Literal[1], Color | Literal["none"])'
)
#: Core-schema types a scalar union's members may have.
_ENUMERATED = frozenset({"literal", "enum", "none"})

#: The ``ConfigDict`` keys an event or a DTO in it may set, with the values
#: allowed (``None``: any): none changes the wire form or how it is read.
#: Validation-only options (``str_strip_whitespace``, ``strict``...) apply
#: alike to the producer and, the reader being a copy of the class's
#: schema, to every read; a wire name is checked field by field.
_CONFIG_ALLOWED: dict[str, tuple[Any, ...] | None] = dict.fromkeys(
    (
        "title",
        "model_title_generator",
        "field_title_generator",
        "json_schema_extra",
        "use_attribute_docstrings",
        "json_schema_serialization_defaults_required",
        "extra",
        "frozen",
        "alias_generator",
        "populate_by_name",
        "validate_by_name",
        "validate_by_alias",
        "serialize_by_alias",
        "loc_by_alias",
        "str_to_lower",
        "str_to_upper",
        "str_strip_whitespace",
        "str_min_length",
        "str_max_length",
        "strict",
        "validate_assignment",
        "validate_default",
        "validate_return",
        "revalidate_instances",
        "from_attributes",
        "arbitrary_types_allowed",
        "ignored_types",
        "protected_namespaces",
        "hide_input_in_errors",
        "validation_error_cause",
        "cache_strings",
        "defer_build",
        "plugin_settings",
    )
) | {"allow_inf_nan": (False,)}
#: Known keys that change the wire form or its reading: allowed only at
#: pydantic's default value. Any other key is refused (the set is closed).
_CONFIG_DEFAULTS: dict[str, Any] = {
    "ser_json_timedelta": "iso8601",
    "ser_json_temporal": "iso8601",
    "val_temporal_unit": "infer",
    "ser_json_bytes": "utf8",
    "val_json_bytes": "utf8",
    "ser_json_inf_nan": "null",
    "coerce_numbers_to_str": False,
    "regex_engine": "rust-regex",
    "polymorphic_serialization": False,
    "use_enum_values": False,
    "json_encoders": None,
    "json_schema_mode_override": None,
    "schema_generator": None,
    "url_preserve_empty_path": False,
}


def _overlap(shown: str) -> str:
    return (
        f"`{shown}` is a union of scalars whose members can read the same "
        "published value, so the reader may return another member than the "
        'producer held ("123" read as 123, a UUID as a str)'
    )


def _member_reader(node: dict[str, Any]) -> SchemaValidator:
    """A lenient validator of one scalar union member."""
    return SchemaValidator(cast("core_schema.CoreSchema", _lenient(node, {}, {})))


def _wire_values(member: dict[str, Any]) -> list[Any]:
    """The JSON values a ``literal`` / ``enum`` / ``none`` member publishes."""
    if member["type"] == "none":
        return [None]
    if member["type"] == "enum":
        return [m.value for m in member.get("members", ())]
    return [v.value if isinstance(v, enum.Enum) else v for v in member["expected"]]


def _reads(reader: SchemaValidator, value: Any) -> bool:
    try:
        reader.validate_python(value)
    except (ValueError, TypeError):
        return False
    return True


def _config_problem(config: dict[str, Any]) -> tuple[str, Any] | None:
    """The first ``model_config`` key (and its value) outside the whitelist."""
    for key, value in config.items():
        if key in _CONFIG_ALLOWED:
            allowed = _CONFIG_ALLOWED[key]
            if allowed is None or value in allowed:
                continue
        elif key in _CONFIG_DEFAULTS and value == _CONFIG_DEFAULTS[key]:
            continue
        return key, value
    return None


class ShapeError(Exception):
    """An event field outside the closed set: ``where`` (the field path),
    ``problem`` and ``fix``."""

    def __init__(self, where: str, problem: str, fix: str) -> None:
        super().__init__(f"{where}: {problem}")
        self.where = where
        self.problem = problem
        self.fix = fix


def _children(node: dict[str, Any]) -> Iterator[Any]:
    """The subschemas of the core-schema node ``node``: every key but data
    keys, the values of a name -> schema mapping (a model's ``fields``, a
    tagged union's ``choices``: a field may be named ``default``)."""
    for key, value in node.items():
        if key in _DATA_KEYS:
            continue
        if key in _NAMED and isinstance(value, dict):
            yield from value.values()
        else:
            yield value


def _refs(node: Any, out: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every node of the core schema ``node`` that carries a ``ref``."""
    if isinstance(node, list | tuple):
        for child in node:
            _refs(child, out)
    elif isinstance(node, dict):
        if isinstance(node.get("ref"), str):
            out.setdefault(node["ref"], node)
        for child in _children(node):
            _refs(child, out)
    return out


#: A dotted qualifier (``typing.``, ``pkg.mod.``, ``f.<locals>.``) in a repr.
_QUALIFIER = re.compile(r"\b(?:[A-Za-z_]\w*\.|<locals>\.)+(?=[A-Za-z_])")


def _type_text(annotation: Any) -> str:
    """``annotation`` as written, without module qualifiers."""
    if isinstance(annotation, type) and not typing.get_args(annotation):
        return annotation.__name__
    return _QUALIFIER.sub("", repr(annotation)).replace("NoneType", "None")


class _Shapes:
    """One walk of an Event class's core schema (see the module docstring)."""

    #: The check of each core-schema type that is not a scalar (below).
    _HANDLERS: ClassVar[dict[str, _Handler]]

    def __init__(self, event: type[BaseModel]) -> None:
        self.event = event
        self.defs = _refs(event.__pydantic_core_schema__, {})
        self.seen: set[str] = set()
        #: the core config of the model whose fields are being checked
        self.config: dict[str, Any] = {}

    def run(self) -> None:
        schema = cast("dict[str, Any]", self.event.__pydantic_core_schema__)
        self.visit(schema, self.event.__qualname__, "")

    # -- helpers --------------------------------------------------------- #

    def _resolve(self, node: dict[str, Any]) -> dict[str, Any]:
        while node.get("type") == "definition-ref":
            node = self.defs[node["schema_ref"]]
        if node.get("type") == "definitions":
            return self._resolve(node["schema"])
        return node

    def _unwrapped(self, node: dict[str, Any]) -> dict[str, Any]:
        """``node`` without validators, defaults and references around it."""
        node = self._resolve(node)
        while node.get("type") in (*_RAW_VALIDATORS, "function-after", "default"):
            node = self._resolve(node["schema"])
        return node

    def _structured(self, node: Any, seen: set[str] | None = None) -> bool:
        """A DTO is validated somewhere in ``node``."""
        seen = set() if seen is None else seen
        if isinstance(node, list | tuple):
            return any(self._structured(child, seen) for child in node)
        if not isinstance(node, dict):
            return False
        kind = node.get("type")
        if kind in ("model", "model-fields", "dataclass", "typed-dict"):
            return True
        if kind == "definition-ref":
            ref = node["schema_ref"]
            if ref in seen:
                return False
            seen.add(ref)
            return self._structured(self.defs[ref], seen)
        return any(
            self._structured(child, seen)
            for child in _children({**node, "serialization": None})
        )

    def _is_scalar(self, node: dict[str, Any]) -> bool:
        node = self._unwrapped(node)
        if node.get("type") == "nullable":
            return self._is_scalar(node["schema"])
        if node.get("type") == "union":  # of scalars (checked on its own)
            return all(
                self._is_scalar(c[0] if isinstance(c, tuple | list) else c)
                for c in node["choices"]
            )
        return node.get("type") in _SCALARS

    # -- the walk -------------------------------------------------------- #

    def visit(self, node: dict[str, Any], where: str, shown: str) -> None:
        """Check ``node``: the value at path ``where``, of the field type
        ``shown`` (its annotation as written, for messages)."""
        self._visit(node, where, shown)
        if node.get("serialization") is not None:  # SerializeAsAny included
            raise ShapeError(
                where,
                f"`{shown or where}` has a custom serializer, so the published value "
                "is not what its reader reads",
                "drop the serializer (field_serializer, model_serializer, "
                "PlainSerializer, SerializeAsAny): an event's wire form is its "
                "fields' own values; publish a derived value as a field of its own",
            )

    def _visit(self, node: dict[str, Any], where: str, shown: str) -> None:
        kind = node.get("type")
        if kind in _SCALARS:
            self._scalar(node, where, shown)
        elif kind in ("nullable", "default", "function-after", "definitions"):
            self.visit(node["schema"], where, shown)
        elif kind != "any":  # Any: an opaque JSON value, read as it is
            handler = self._HANDLERS.get(str(kind), _Shapes._unknown)
            handler(self, node, where, shown)

    def _ref(self, node: dict[str, Any], where: str, shown: str) -> None:
        ref = node["schema_ref"]
        if ref not in self.seen:
            self.seen.add(ref)
            self.visit(self.defs[ref], where, shown)

    def _plain(self, node: dict[str, Any], where: str, shown: str) -> None:
        raise ShapeError(
            where,
            f'`{shown}` has a plain validator (mode="plain" / PlainValidator): '
            "it replaces the field's validation, so neither the reader nor "
            "the wire schema knows its shape",
            'type the field and check it with a mode="after" validator',
        )

    def _list(self, node: dict[str, Any], where: str, shown: str) -> None:
        self.visit(node.get("items_schema") or _ANY, f"{where}[]", shown)

    def _set(self, node: dict[str, Any], where: str, shown: str) -> None:
        if not self._is_scalar(node.get("items_schema") or _ANY):
            raise ShapeError(
                where,
                f"`{shown}` is a set of non-scalar values",
                "use list[T] for DTOs and containers",
            )
        self._list(node, where, shown)

    def _tuple(self, node: dict[str, Any], where: str, shown: str) -> None:
        for i, item in enumerate(node["items_schema"]):
            self.visit(item, f"{where}[{i}]", shown)

    def _dict(self, node: dict[str, Any], where: str, shown: str) -> None:
        keys = self._unwrapped(node.get("keys_schema") or _ANY)
        if keys.get("type") != "str":
            raise ShapeError(
                where,
                f"`{shown}` has keys that are not str (JSON object keys are strings)",
                "use dict[str, T] (dict[str, Any] for an opaque JSON object)",
            )
        self.visit(node.get("values_schema") or _ANY, f"{where}{{}}", shown)

    def _class(self, node: dict[str, Any], where: str, shown: str) -> None:
        what = "a dataclass" if node["type"] == "dataclass" else "a TypedDict"
        raise ShapeError(
            where,
            f"`{shown}` is {what}: event payloads are pydantic DTOs",
            "declare it as a pydantic BaseModel",
        )

    def _unknown(self, node: dict[str, Any], where: str, shown: str) -> None:
        raise ShapeError(
            where,
            f"`{shown}` is not a wire type (core schema {node.get('type')!r})",
            FIX_TYPES,
        )

    def _scalar(self, node: dict[str, Any], where: str, shown: str) -> None:
        kind = node["type"]
        if kind in ("float", "decimal"):
            # pydantic-core: the field's flag, else its model's, else the
            # type's default (a float may be NaN / inf, a Decimal may not)
            finite = node.get(
                "allow_inf_nan", self.config.get("allow_inf_nan", kind == "float")
            )
            if finite:
                raise ShapeError(
                    where,
                    f"`{shown}` may be NaN or infinite, which JSON cannot carry "
                    "(the transport publishes null, which its reader refuses)",
                    "make it finite: Field(allow_inf_nan=False), or "
                    "model_config = ConfigDict(allow_inf_nan=False) on its DTO "
                    "(an event's own fields are finite already)",
                )
        if kind == "str" and node.get("coerce_numbers_to_str"):
            raise ShapeError(
                where,
                f"`{shown}` sets coerce_numbers_to_str: a number is read as a "
                "str, so a reader does not see what the wire schema states",
                "drop it: publish a str, or type the field as a number",
            )
        if kind == "enum":
            values = [member.value for member in node.get("members", ())]
        elif node["type"] == "literal":
            values = [
                v.value if isinstance(v, enum.Enum) else v
                for v in node.get("expected", ())
            ]
        else:
            return
        if not all(v is None or isinstance(v, str | int | float) for v in values):
            raise ShapeError(
                where,
                f"`{shown}` has a value that is not a JSON scalar",
                "use str, int, float, bool or None values",
            )

    def _raw_validator(self, node: dict[str, Any], where: str, shown: str) -> None:
        if self._structured(node["schema"]):
            mode = "before" if node["type"] == "function-before" else "wrap"
            raise ShapeError(
                where,
                f'a mode="{mode}" validator on `{shown or "the model"}` sees the '
                "raw payload (a newer producer's keys included) and could "
                "build a DTO with its strict validator or reject an added key",
                'use a mode="after" validator: it sees the validated value',
            )
        self.visit(node["schema"], where, shown)

    def _union(self, node: dict[str, Any], where: str, shown: str) -> None:
        choices = [c[0] if isinstance(c, tuple | list) else c for c in node["choices"]]
        values = [c for c in choices if self._unwrapped(c).get("type") != "none"]
        if len(values) == 1:  # X | None, however pydantic spells it
            self.visit(values[0], where, shown)
            return
        if not all(self._is_scalar(c) for c in choices):
            raise ShapeError(
                where,
                f"`{shown}` is a union of a DTO, a container or Any without a "
                "discriminator: a reader cannot tell which branch a payload "
                "is, and one branch can take another's data (a newer key "
                "ignored, a value lost)",
                _FIX_UNION,
            )
        for choice in choices:
            self.visit(choice, where, shown)
        self._disjoint(choices, where, shown)

    def _disjoint(self, choices: list[dict[str, Any]], where: str, shown: str) -> None:
        """A union of scalars round-trips only when its members are
        ``Literal`` / ``Enum`` values (or ``None``) and no member reads
        another member's published value: the reader then returns the
        member the producer held, whatever the union mode or strictness."""
        members = [(c, self._unwrapped(c)) for c in choices]
        if any(m.get("type") not in _ENUMERATED for _, m in members):
            raise ShapeError(where, _overlap(shown), _FIX_SCALAR_UNION)
        readers = [_member_reader(self._resolve(c)) for c, _ in members]
        for i, (_, member) in enumerate(members):
            for value in _wire_values(member):
                for j, reader in enumerate(readers):
                    if j != i and _reads(reader, value):
                        raise ShapeError(
                            where,
                            f"{_overlap(shown)}: the published value {value!r} "
                            "is read by another member",
                            _FIX_SCALAR_UNION,
                        )

    def _tagged_union(self, node: dict[str, Any], where: str, shown: str) -> None:
        if not isinstance(node.get("discriminator"), str):
            raise ShapeError(
                where,
                f"`{shown}` is discriminated by a callable or a path",
                "name a Literal tag field present in every branch: "
                'Field(discriminator="kind")',
            )
        for tag, choice in node["choices"].items():
            if self._unwrapped(choice).get("type") != "model":
                raise ShapeError(
                    where,
                    f"`{shown}`: the branch tagged {tag!r} is not a DTO",
                    _FIX_UNION,
                )
            self.visit(choice, f"{where}<{tag}>", shown)

    def _model(self, node: dict[str, Any], where: str) -> None:
        cls = node["cls"]
        name = cls.__name__
        if node.get("root_model"):
            raise ShapeError(
                where,
                f"{name} is a RootModel",
                "use its inner type, or a BaseModel with a named field",
            )
        if node.get("custom_init") or cls.__new__ is not BaseModel.__new__:
            method = "__init__" if node.get("custom_init") else "__new__"
            raise ShapeError(
                where,
                f"{name} overrides {method}: the reader builds DTOs from "
                "their fields and never calls it, so a delivered value would "
                "differ from a constructed one (and a strict model inside it "
                "would refuse a newer producer's key)",
                f"move {name}.{method}'s logic to a "
                '@model_validator(mode="after") or a classmethod constructor',
            )
        parameters = getattr(cls, "__pydantic_generic_metadata__", {}).get("parameters")
        if parameters:
            raise ShapeError(
                where,
                f"{name} is a generic DTO used without its type parameters",
                f"parametrise it: {name}[...]",
            )
        problem = _config_problem(dict(getattr(cls, "model_config", {})))
        if problem is not None:
            key, value = problem
            owner = "" if cls is self.event else f"{name}'s "
            raise ShapeError(
                where,
                f"{owner}model_config sets {key}={value!r}, which changes how "
                "the payload is published or read (an event and its DTOs may "
                "only set the options events.md lists)",
                f"drop it from {name}.model_config: an event's wire form is "
                "pydantic's default JSON of its fields",
            )
        inner = node["schema"]
        if inner.get("type") in _RAW_VALIDATORS:
            self._raw_validator(inner, where, "")
            return
        if inner.get("type") == "function-after":
            inner = inner["schema"]
        outer, self.config = self.config, node.get("config") or {}
        try:
            self._fields(cls, inner, self.config, where)
        finally:
            self.config = outer

    def _fields(
        self,
        cls: type[BaseModel],
        node: dict[str, Any],
        config: dict[str, Any],
        where: str,
    ) -> None:
        if node.get("type") != "model-fields":
            raise ShapeError(where, f"{cls.__name__} is not a plain DTO", FIX_TYPES)
        if node.get("computed_fields"):
            names = [f["property_name"] for f in node["computed_fields"]]
            raise ShapeError(
                where,
                f"{cls.__name__} has computed fields {names}: they are "
                "published but never read back",
                "make each a regular field the producer sets",
            )
        self._wire_names(node, config, where)
        fields = getattr(cls, "__pydantic_fields__", {})
        for name, field in node["fields"].items():
            info = fields.get(name)
            shown = _type_text(info.annotation) if info is not None else name
            at = f"{where}.{name}"
            excluded = field.get("serialization_exclude") or (
                "serialization_exclude_if" in field
            )
            if excluded and field["schema"].get("type") != "default":
                raise ShapeError(
                    at,
                    "is never published (exclude) yet has no default, so no "
                    "reader can read the payload",
                    "give it a default, or publish it",
                )
            self.visit(field["schema"], at, shown)
        extras = node.get("extras_schema")
        if isinstance(extras, dict):
            self.visit(extras, f"{where}.<extra>", "the extra keys' type")

    def _wire_names(
        self, node: dict[str, Any], config: dict[str, Any], where: str
    ) -> None:
        """Every field of the model-fields ``node`` has one wire name of its
        own (see :meth:`_alias`), and none is another field's name when the
        model also reads fields by name."""
        by_alias = config.get("validate_by_alias", True)
        by_name = config.get("validate_by_name", False)
        wire: dict[str, str] = {}  # wire name -> field name
        for name, field in node["fields"].items():
            at = f"{where}.{name}"
            key = self._alias(field, name, at, by_alias=by_alias, by_name=by_name)
            if key in wire:
                raise ShapeError(
                    at,
                    f"has the wire name {key!r} of field {wire[key]}: one key "
                    "carries one value, so a delivery gives both fields the "
                    "same one",
                    "give each field its own alias (one wire name per field; "
                    "an alias_generator must map distinct names to distinct keys)",
                )
            wire[key] = name
        if by_name:  # a field is also read by its name when its key is absent
            for name in node["fields"]:
                owner = wire.get(name)
                if owner is not None and owner != name:
                    raise ShapeError(
                        f"{where}.{name}",
                        f"its name is the wire name of field {owner}: the model "
                        "also reads fields by name, so a payload without this "
                        f"field's own key would fill it from {owner}'s",
                        f"rename {name}, or give {owner} another alias",
                    )

    def _alias(
        self,
        field: dict[str, Any],
        name: str,
        where: str,
        *,
        by_alias: bool,
        by_name: bool,
    ) -> str:
        """The field's wire name: a non-empty string under which it is
        published and which its reader looks up first."""
        reads = field.get("validation_alias")
        if reads is not None and not isinstance(reads, str):
            raise ShapeError(
                where,
                "has an AliasPath / AliasChoices: it is published under one "
                "key and read from others",
                f'give it one wire name: Field(alias="{name}")',
            )
        writes = field.get("serialization_alias")
        writes = name if writes is None else writes  # "" is a name, not absent
        first: str | None = reads if reads is not None and by_alias else name
        if reads is not None and not by_alias and not by_name:
            first = None
        if "" in (writes, first):
            raise ShapeError(
                where,
                "has the empty wire name: it would be published under the "
                'key "", which no reader looks up',
                f'give it a wire name: Field(alias="{name}"), or no alias',
            )
        if first != writes:
            raise ShapeError(
                where,
                f"is published as {writes!r} but its reader looks up "
                f"{first!r} first, so a delivery does not round-trip",
                f'give it one wire name for both directions: Field(alias="{writes}")',
            )
        return writes


_Handler = Callable[[_Shapes, dict[str, Any], str, str], None]
_Shapes._HANDLERS = {
    "definition-ref": _Shapes._ref,
    "function-before": _Shapes._raw_validator,
    "function-wrap": _Shapes._raw_validator,
    "function-plain": _Shapes._plain,
    "list": _Shapes._list,
    "set": _Shapes._set,
    "frozenset": _Shapes._set,
    "tuple": _Shapes._tuple,
    "dict": _Shapes._dict,
    "union": _Shapes._union,
    "tagged-union": _Shapes._tagged_union,
    "model": lambda self, node, where, _shown: self._model(node, where),
    "dataclass": _Shapes._class,
    "typed-dict": _Shapes._class,
}


def check_wire_shapes(event: type[BaseModel]) -> None:
    """Raise :class:`ShapeError` for the first field of ``event`` (a
    complete class) outside the closed set of wire shapes."""
    _Shapes(event).run()


# --------------------------------------------------------------------------- #
# Contract DTOs: the same closed set, and what the generated copy can carry
# --------------------------------------------------------------------------- #

#: The ``default_factory`` a generated copy can write.
_FACTORIES = (list, dict, set, frozenset, tuple)
#: Field-level metadata the copy writes as ``Field(...)`` keywords (by class
#: name: annotated_types / pydantic metadata).
_METADATA = frozenset(
    {
        "Gt",
        "Ge",
        "Lt",
        "Le",
        "MultipleOf",
        "MinLen",
        "MaxLen",
        "Interval",
        "Len",
        "Strict",
        "AllowInfNan",
        "_PydanticGeneralMetadata",
        "FieldInfo",
    }
)
_WRITABLE = (
    type(None),
    bool,
    int,
    float,
    str,
    enum.Enum,
    uuid.UUID,
    decimal.Decimal,
    _dt.date,
    _dt.time,
    _dt.timedelta,
)


def _writable(value: Any) -> bool:
    """A default the generated copy can write as source."""
    if isinstance(value, list | tuple | set | frozenset):
        return all(_writable(v) for v in value)
    if isinstance(value, dict):
        return all(_writable(k) and _writable(v) for k, v in value.items())
    return isinstance(value, _WRITABLE) and not isinstance(value, BaseModel)


def _metadata_problem(items: Any, where: str) -> None:
    for item in items:
        kind = type(item).__name__
        if kind == "StringConstraints":
            transforms = ("strip_whitespace", "to_upper", "to_lower", "ascii_only")
            if any(getattr(item, t, None) for t in transforms):
                raise ShapeError(
                    where,
                    "a StringConstraints transformation (strip_whitespace, "
                    "to_upper, to_lower, ascii_only) is not a wire constraint",
                    "normalise the value in the provider; keep the length and "
                    "pattern constraints (Field(max_length=..., pattern=...))",
                )
            continue
        if kind not in _METADATA:
            raise ShapeError(
                where,
                f"the constraint {item!r} cannot be copied into the generated DTO",
                "use Field(...) constraints: gt / ge / lt / le, multiple_of, "
                "min_length / max_length, pattern, strict, max_digits / "
                "decimal_places, allow_inf_nan",
            )
        if kind == "FieldInfo":
            _metadata_problem(item.metadata, where)
            if item.discriminator is not None and not isinstance(
                item.discriminator, str
            ):
                raise ShapeError(
                    where,
                    "a callable discriminator cannot be copied",
                    'name a Literal tag field: Field(discriminator="kind")',
                )


def _annotated_metadata(annotation: Any, where: str) -> None:
    """``Annotated`` metadata at any depth of a field's annotation."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Annotated:
        _metadata_problem(args[1:], where)
        _annotated_metadata(args[0], where)
        return
    for arg in args:
        if arg is not Ellipsis and not isinstance(arg, str | int | float | bool):
            _annotated_metadata(arg, where)


def _copyable_model(cls: type[BaseModel], where: str) -> list[type]:
    """Refuse what the generated copy of ``cls`` could not carry; return
    the DTO classes its fields name (checked in turn)."""
    if getattr(cls, "__pydantic_generic_metadata__", {}).get("origin") is not None:
        raise ShapeError(
            where,
            f"{cls.__name__} is a parametrised generic DTO",
            "declare a concrete model with the item type spelled out",
        )
    named: list[type] = []
    for name, info in cls.model_fields.items():
        at = f"{where}.{name}"
        factory = info.default_factory
        if factory is not None and factory not in _FACTORIES:
            raise ShapeError(
                at,
                f"default_factory={getattr(factory, '__name__', factory)!r} cannot "
                "be copied into the generated DTO",
                "use a literal default, or default_factory=list / dict / set / "
                "frozenset / tuple",
            )
        if (
            factory is None
            and not _writable(info.default)
            and (info.default is not pydantic_core.PydanticUndefined)
        ):
            raise ShapeError(
                at,
                f"its default {info.default!r} cannot be written into the "
                "generated DTO",
                "use a JSON scalar, an Enum member, a UUID / Decimal / date / "
                "datetime / time / timedelta, or a container of them",
            )
        if info.exclude_if is not None:
            raise ShapeError(
                at, "exclude_if is a callable", "exclude the field always, or never"
            )
        _metadata_problem(info.metadata, at)
        _annotated_metadata(info.annotation, at)
        named += _dto_classes(info.annotation)
    return named


def _dto_classes(annotation: Any) -> list[type]:
    """The models / Enums ``annotation`` names directly (not their fields)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel | enum.Enum):
        return [annotation]
    out: list[type] = []
    for arg in typing.get_args(annotation):
        if arg is not Ellipsis and not isinstance(arg, str | int | float | bool):
            out += _dto_classes(arg)
    return out


def check_contract_dto(cls: type, where: str) -> None:
    """Raise :class:`ShapeError` when the contract DTO ``cls`` (a pydantic
    model or an Enum a provider's signature names, at ``where``) is outside
    the closed set of wire shapes or cannot be copied by ``sandloom
    generate``. The closed set is the event one; a validator is allowed and
    stays the provider's (the copy does not carry it). Every model / Enum a
    field names is checked too, except classes of ``sandloom.shared`` and
    generated copies (referenced, never copied)."""
    _check_dto(cls, where, set())


#: Packages whose classes are referenced as they are, never copied.
_SHARED_PREFIX = "sandloom.shared"
_COPY_ATTR = "__sandloom_dto__"


def _skipped(cls: type) -> bool:
    module = cls.__module__
    return (
        isinstance(cls.__dict__.get(_COPY_ATTR), str)
        or module == _SHARED_PREFIX
        or module.startswith(_SHARED_PREFIX + ".")
    )


def _check_dto(cls: type, where: str, seen: set[type]) -> None:
    if cls in seen or _skipped(cls):
        return
    seen.add(cls)
    if issubclass(cls, enum.Enum):
        if issubclass(cls, enum.Flag):
            raise ShapeError(where, f"{cls.__name__} is a Flag enum", "use an Enum")
        for member in cls:
            if not (
                member.value is None or isinstance(member.value, str | int | float)
            ):
                raise ShapeError(
                    f"{where}.{member.name}",
                    f"value {member.value!r} is not a JSON scalar",
                    "use str, int, float, bool or None values",
                )
        return
    if not issubclass(cls, BaseModel):
        return
    _Shapes(cls).run()
    for named in _copyable_model(cls, where):
        _check_dto(named, f"{where}.{named.__name__}", seen)


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #


def _factory(cls: type[BaseModel]) -> type:
    """A class whose ``__new__`` allocates an instance of ``cls`` (its
    ``__init__`` never runs: pydantic-core fills the instance)."""
    allocate = object.__new__

    def __new__(_factory: type) -> Any:
        return allocate(cls)

    return type(
        cls.__name__,
        (),
        {
            "__new__": __new__,
            "__module__": cls.__module__,
            "__qualname__": cls.__qualname__,
        },
    )


def _lenient(
    node: Any, factories: dict[type, type], configs: dict[type, dict[str, Any]]
) -> Any:
    """A copy of the core schema ``node`` for the reader (see
    :func:`compile_reader`); data keys are shared."""
    if isinstance(node, list):
        return [_lenient(child, factories, configs) for child in node]
    if isinstance(node, tuple):
        return tuple(_lenient(child, factories, configs) for child in node)
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "strict":
            continue
        if key in _DATA_KEYS:
            out[key] = value
        elif key in _NAMED and isinstance(value, dict):
            out[key] = {k: _lenient(v, factories, configs) for k, v in value.items()}
        else:
            out[key] = _lenient(value, factories, configs)
    if out.get("extra_behavior") == "forbid":
        out["extra_behavior"] = "ignore"
    if out.get("type") == "model":
        _lenient_model(out, factories, configs)
    return out


def _lenient_model(
    node: dict[str, Any],
    factories: dict[type, type],
    configs: dict[type, dict[str, Any]],
) -> None:
    """Point the (copied) model node ``node`` at the factory of its class,
    its config ignoring extra keys."""
    cls = node["cls"]
    config = dict(node.get("config") or {})
    if config.get("extra_fields_behavior") == "forbid":
        config["extra_fields_behavior"] = "ignore"
    node["config"] = configs[cls] = config
    factory = factories.get(cls)
    if factory is None:
        factory = factories[cls] = _factory(cls)
    node["cls"] = factory


def compile_reader(cls: type[BaseModel]) -> SchemaValidator:
    """The reader of ``cls`` (a complete class in the closed set): a model
    forbidding extra keys ignores them at every depth, nothing is strict,
    validators run once and every value is an instance of its real class."""
    configs: dict[type, dict[str, Any]] = {}
    schema = _lenient(cls.__pydantic_core_schema__, {}, configs)
    return SchemaValidator(
        cast("core_schema.CoreSchema", schema),
        cast("core_schema.CoreConfig", configs.get(cls) or {"title": cls.__name__}),
    )


#: Set once the self-check passed in this process.
_CHECKED = threading.Event()
_CHECK_LOCK = threading.Lock()


def _canary() -> Iterator[str]:
    """The reader's contract with pydantic-core, on a canary; yields each
    broken expectation."""

    class Leaf(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        x: int

    class Root(BaseModel):
        model_config = ConfigDict(extra="forbid")
        _state: int = PrivateAttr(default=7)
        leaf: Leaf
        leaves: list[Leaf] = []
        note: str = ""

    try:
        got = compile_reader(Root).validate_python(
            {"leaf": {"x": 1, "new": 1}, "leaves": [{"x": "2", "new": 2}], "new": 0}
        )
    except (ValueError, TypeError, pydantic_core.SchemaError) as exc:
        yield f"a nested unknown key or a lax value was refused ({exc})"
        return
    if type(got) is not Root or type(got.leaf) is not Leaf:
        yield f"the reader returned {type(got).__name__}, not the real classes"
        return
    if got != Root(leaf=Leaf(x=1), leaves=[Leaf(x=2)]):
        yield "the read value differs from the constructed one"
    if got.model_fields_set != {"leaf", "leaves"}:
        yield f"fields set {got.model_fields_set}"
    if got.__pydantic_private__ != {"_state": 7}:
        yield f"private state {got.__pydantic_private__}"
    try:
        Root.model_validate({"leaf": {"x": 1, "new": 1}})
    except pydantic.ValidationError:
        pass
    else:
        yield "compiling the reader made the class itself lenient"


def self_check() -> None:
    """Verify the reader's contract with pydantic-core once per process
    (before the first reader is stored); a pydantic upgrade that breaks it
    fails loudly, naming the versions."""
    if _CHECKED.is_set():
        return
    with _CHECK_LOCK:
        if _CHECKED.is_set():
            return
        broken = list(_canary())
        if broken:
            raise RuntimeError(
                "sandloom: the event reader's self-check failed under pydantic "
                f"{pydantic.VERSION} / pydantic-core {pydantic_core.__version__}: "
                + "; ".join(broken)
                + ". The reader builds DTOs through factory classes in model "
                "nodes (sandloom/sdk/_decl/_event_wire.py); pin the pydantic "
                "version the framework was released with"
            )
        _CHECKED.set()


__all__ = [
    "FIX_TYPES",
    "ShapeError",
    "check_contract_dto",
    "check_wire_shapes",
    "compile_reader",
    "self_check",
]
