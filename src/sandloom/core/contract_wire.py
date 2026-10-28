"""The single wire codec of service-contract calls (local AND internal RPC).

A contract call must have identical observable semantics in a monolith and
across processes. Both paths therefore push every argument and every result
through THIS codec — the in-process path round-trips through the wire form
too — and validate it against the method's annotations, so a local caller
sees exactly what a remote caller sees.

Wire types (checked at registration by :func:`annotation_problem`):

* JSON-native scalars ``str`` / ``int`` / ``float`` / ``bool`` and ``None``;
* ``UUID``, ``datetime``, ``date`` and ``Decimal`` (string-encoded, exact
  round-trip — never a lossy float);
* ``Enum`` subclasses (encoded by value) and ``Literal[...]`` of scalars;
* pydantic ``BaseModel`` subclasses whose fields are themselves wire types;
* ``list`` / ``tuple`` / ``set`` / ``frozenset`` / ``dict`` of wire types
  (dict keys: a wire scalar), unions/``Optional`` of them, ``Any``.

Encoding is a tagged tree of msgpack-native values: ``{"$sandloom_type": <tag>,
"value": ...}`` for the non-native types (uuid, datetime, date, decimal,
tuple, set, frozenset, and ``map`` for a dict whose keys are not all plain
strings). Decoding first untags (:func:`decode_untyped`), then validates /
coerces against an annotation with a cached pydantic ``TypeAdapter``
(:func:`decode`). A remote caller that does not have the provider's Python
types receives a portable :func:`type_descriptor` from the provider's describe
endpoint and rebuilds the annotation with :func:`annotation_from_descriptor`.

That tagged tree is **codec 1**. **Codec 2** (:class:`WirePlan`) is
annotation-driven: for a FULLY TYPED annotation (:func:`is_fully_typed`) the
value travels as ``TypeAdapter(annotation).dump_python(mode="json")`` and is
rebuilt with ``validate_python`` — both in pydantic-core, no Python-level tree
walk, no tags. A value that does not exactly fit its annotation falls back to
codec 1 (so coercion and errors never change), and immutable exact-type values
skip the round trip entirely on the in-process path. Over RPC the codec is
negotiated per call (see :mod:`sandloom.core.rpc`).
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import sys
import types
import typing
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin
from uuid import UUID

import orjson
from pydantic import BaseModel, TypeAdapter

from sandloom.core.descriptors.contract_refs import dto_copies

WIRE_TAG = "$sandloom_type"
WIRE_VALUE = "value"

_SCALARS: dict[type, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    UUID: "uuid",
    datetime: "datetime",
    date: "date",
    Decimal: "decimal",
}
_SCALAR_BY_NAME = {name: tp for tp, name in _SCALARS.items()}


class ContractTypeError(TypeError):
    """A contract signature uses a type that cannot cross the wire.

    Raised at REGISTRATION time, so a non-portable contract never deploys
    (it would work locally and fail — or silently change type — remotely).
    """


class ContractWireError(TypeError):
    """A runtime value is not a contract wire type (see the module doc)."""


# --------------------------------------------------------------------------- #
# Registration-time checks
# --------------------------------------------------------------------------- #
def annotation_problem(  # noqa: C901
    annotation: Any, *, _seen: frozenset[int] = frozenset()
) -> str | None:
    """``None`` if ``annotation`` is a wire type, else a human-readable reason."""
    if annotation is inspect.Parameter.empty:
        return "missing type annotation"
    if annotation is Any or annotation is None or annotation is types.NoneType:
        return None
    if annotation in _SCALARS:
        return None
    if get_origin(annotation) is None and inspect.isclass(annotation):
        if issubclass(annotation, Enum):
            return None
        if issubclass(annotation, BaseModel):
            if id(annotation) in _seen:
                return None
            seen = _seen | {id(annotation)}
            for field_name, field in annotation.model_fields.items():
                problem = annotation_problem(field.annotation, _seen=seen)
                if problem is not None:
                    return f"{annotation.__name__}.{field_name}: {problem}"
            return None
        if annotation in (list, tuple, set, frozenset, dict):
            return None  # bare container == container of Any
        return (
            f"{annotation.__module__}.{annotation.__qualname__} is not a contract "
            "wire type (use a pydantic model, an Enum or a JSON-native type)"
        )
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is typing.Annotated:
        return annotation_problem(args[0], _seen=_seen)
    if origin is Literal:
        for value in args:
            if not isinstance(value, (str, int, bool, type(None))):
                return f"Literal value {value!r} is not a wire scalar"
        return None
    if origin in (list, set, frozenset):
        return annotation_problem(args[0], _seen=_seen) if args else None
    if origin is tuple:
        for arg in args:
            if arg is Ellipsis:
                continue
            problem = annotation_problem(arg, _seen=_seen)
            if problem is not None:
                return problem
        return None
    if origin is dict:
        if not args:
            return None
        key, value = args
        if key is not Any and not _is_key_annotation(key):
            return f"dict key type {key!r} must be a wire scalar or Enum"
        return annotation_problem(value, _seen=_seen)
    if origin in (Union, types.UnionType):
        for arg in args:
            problem = annotation_problem(arg, _seen=_seen)
            if problem is not None:
                return problem
        return None
    return (
        f"{annotation!r} is not a contract wire type (use list/tuple/set/dict, "
        "Optional/Union, Literal, a pydantic model, an Enum or a JSON-native type)"
    )


def nominal_types(annotation: Any) -> list[type]:  # noqa: C901
    """Every pydantic model / Enum class an annotation names (recursively).

    Walks containers, unions, ``Annotated`` and model fields (cycle-safe), in
    first-seen order. These are the types a remote caller can only rebuild
    when it imports the SAME class (see :func:`annotation_from_descriptor`);
    the registration-time DTO locality check inspects them.
    """
    found: list[type] = []
    seen: set[int] = set()

    def walk(ann: Any) -> None:  # noqa: C901
        if ann is None or ann is Any or ann is inspect.Parameter.empty:
            return
        origin = get_origin(ann)
        if origin is None and inspect.isclass(ann):
            if issubclass(ann, (Enum, BaseModel)):
                if id(ann) in seen:
                    return
                seen.add(id(ann))
                found.append(ann)
                if issubclass(ann, BaseModel):
                    for field in ann.model_fields.values():
                        walk(field.annotation)
            return
        if origin is Literal:
            return
        args = get_args(ann)
        if origin is typing.Annotated:
            walk(args[0])
            return
        for arg in args:
            if arg is not Ellipsis:
                walk(arg)

    walk(annotation)
    return found


def _is_key_annotation(annotation: Any) -> bool:
    if annotation in _SCALARS:
        return True
    if inspect.isclass(annotation) and issubclass(annotation, Enum):
        return True
    origin = get_origin(annotation)
    if origin is Literal:
        return True
    if origin in (Union, types.UnionType):
        return all(_is_key_annotation(a) for a in get_args(annotation))
    return False


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #
def _tag(name: str, value: Any) -> dict[str, Any]:
    return {WIRE_TAG: name, WIRE_VALUE: value}


def _canonical(item: Any) -> bytes:
    return orjson.dumps(item, option=orjson.OPT_SORT_KEYS)


#: The integer range the RPC transport carries natively (msgpack: int64 /
#: uint64). An int outside it is tagged ``bigint`` (codec v1), and the typed
#: codec (v2) falls back to v1 for a value holding one.
_INT64_MIN = -(2**63)
_UINT64_MAX = 2**64 - 1


def encode(value: Any) -> Any:  # noqa: C901
    """Encode ``value`` into the tagged wire tree (or :class:`ContractWireError`)."""
    if value is None or (
        isinstance(value, (bool, str)) and not isinstance(value, Enum)
    ):
        return value
    if isinstance(value, Enum):
        return encode(value.value)
    if isinstance(value, int):
        if _INT64_MIN <= value <= _UINT64_MAX:
            return value
        # beyond what the RPC transport (msgpack) packs: tagged, so a local
        # and a remote call observe the same value
        return _tag("bigint", str(value))
    if isinstance(value, float):
        return value
    if isinstance(value, UUID):
        return _tag("uuid", str(value))
    if isinstance(value, datetime):
        return _tag("datetime", value.isoformat())
    if isinstance(value, date):
        return _tag("date", value.isoformat())
    if isinstance(value, Decimal):
        return _tag("decimal", str(value))
    if isinstance(value, BaseModel):
        # under each field's wire name: what the receiver's model reads
        return encode(value.model_dump(mode="python", by_alias=True))
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, tuple):
        return _tag("tuple", [encode(item) for item in value])
    if isinstance(value, (set, frozenset)):
        # Canonical (sorted) order: digests and payloads are stable across
        # processes regardless of hash randomization.
        items = sorted((encode(item) for item in value), key=_canonical)
        return _tag("frozenset" if isinstance(value, frozenset) else "set", items)
    if isinstance(value, dict):
        if all(isinstance(k, str) and not isinstance(k, Enum) for k in value) and (
            WIRE_TAG not in value
        ):
            return {k: encode(v) for k, v in value.items()}
        pairs = []
        for key, item in value.items():
            if not isinstance(key, (str, int, bool, UUID, date, Decimal, Enum)):
                raise ContractWireError(
                    f"dict key of type {type(key).__name__} is not a wire scalar"
                )
            pairs.append([encode(key), encode(item)])
        pairs.sort(key=lambda pair: _canonical(pair[0]))
        return _tag("map", pairs)
    raise ContractWireError(
        f"value of type {type(value).__module__}.{type(value).__qualname__} is not "
        "a contract wire type"
    )


def decode_untyped(wire: Any) -> Any:  # noqa: C901
    """Untag a wire tree into plain Python values (no annotation applied)."""
    if isinstance(wire, list):
        return [decode_untyped(item) for item in wire]
    if isinstance(wire, dict):
        tag = wire.get(WIRE_TAG)
        if tag is not None and len(wire) == 2 and WIRE_VALUE in wire:
            raw = wire[WIRE_VALUE]
            if tag == "uuid":
                return UUID(str(raw))
            if tag == "datetime":
                return datetime.fromisoformat(str(raw))
            if tag == "date":
                return date.fromisoformat(str(raw))
            if tag == "decimal":
                return Decimal(str(raw))
            if tag == "bigint":
                return int(str(raw))
            if tag == "tuple":
                return tuple(decode_untyped(item) for item in raw)
            if tag == "set":
                return {_hashable(decode_untyped(item)) for item in raw}
            if tag == "frozenset":
                return frozenset(_hashable(decode_untyped(item)) for item in raw)
            if tag == "map":
                return {_hashable(decode_untyped(k)): decode_untyped(v) for k, v in raw}
        return {key: decode_untyped(item) for key, item in wire.items()}
    return wire


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


@functools.lru_cache(maxsize=2048)
def _cached_adapter(annotation: Any) -> TypeAdapter[Any]:
    return TypeAdapter(annotation)


def type_adapter(annotation: Any) -> TypeAdapter[Any]:
    """A (cached) pydantic ``TypeAdapter`` for ``annotation``."""
    try:
        return _cached_adapter(annotation)
    except TypeError:  # unhashable annotation (e.g. exotic Annotated metadata)
        return TypeAdapter(annotation)


def validate(value: Any, annotation: Any) -> Any:
    """Coerce an UNTAGGED value to ``annotation`` (pydantic, lax mode)."""
    if annotation is Any or annotation is inspect.Parameter.empty:
        return value
    return type_adapter(annotation).validate_python(value)


def decode(wire: Any, annotation: Any = Any) -> Any:
    """Untag ``wire`` and validate it against ``annotation``."""
    return validate(decode_untyped(wire), annotation)


def roundtrip(value: Any, annotation: Any = Any) -> Any:
    """``decode(encode(value), annotation)`` — what a remote peer would see."""
    return decode(encode(value), annotation)


def digest(value: Any) -> str:
    """Stable SHA-256 of a value's canonical wire form (sets/maps sorted)."""
    return hashlib.sha256(_canonical(encode(value))).hexdigest()


# --------------------------------------------------------------------------- #
# Typed codec (wire codec v2): annotation-driven, on pydantic-core
# --------------------------------------------------------------------------- #
#: The tagged tree above (works without any annotation).
CODEC_TAGGED = 1
#: Annotation-driven JSON-native values: ``TypeAdapter(ann).dump_python(
#: mode="json")`` on the sender, ``validate_python`` on the receiver — both in
#: pydantic-core (C), no Python-level tree walk. Used only for FULLY TYPED
#: annotations (:func:`is_fully_typed`): there the annotation alone restores
#: every type, so no tag is needed. ``Any`` positions keep the tagged tree.
CODEC_TYPED = 2
#: Codecs this process speaks (advertised in the RPC describe document).
SUPPORTED_CODECS: tuple[int, ...] = (CODEC_TAGGED, CODEC_TYPED)

#: Immutable wire scalars whose validation against their own annotation is the
#: identity: a value of EXACTLY this type needs no round trip at all. (Not
#: ``Decimal``: pydantic rejects a non-finite one, so it must be validated.)
_IMMUTABLE_SCALARS: frozenset[type] = frozenset(
    {str, int, float, bool, UUID, datetime, date}
)


def _json_kind(annotation: Any) -> str:  # noqa: C901
    """The JSON shape a fully typed annotation dumps to (union disambiguation)."""
    if annotation is None or annotation is types.NoneType:
        return "null"
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation in _SCALARS:
        return "string"  # str / UUID / datetime / date / Decimal
    origin = get_origin(annotation)
    if origin is typing.Annotated:
        return _json_kind(get_args(annotation)[0])
    if origin is Literal:
        return "literal"  # dumps to its own (identical) value
    if origin in (list, set, frozenset, tuple):
        return "array"
    if origin is dict:
        return "object"
    if inspect.isclass(annotation):
        if issubclass(annotation, Enum):
            # Encoded by value: may collide with that value's own kind.
            return "string-enum"
        if issubclass(annotation, BaseModel):
            return "object"
    return "unknown"


def _model_fully_typed(model: type[BaseModel], seen: frozenset[int]) -> bool:
    if id(model) in seen:
        return True  # recursive model: its fields are checked once
    config = getattr(model, "model_config", {}) or {}
    if config.get("extra") == "allow":
        return False  # extras carry no annotation
    decorators = getattr(model, "__pydantic_decorators__", None)
    if decorators is not None and (
        decorators.field_serializers or decorators.model_serializers
    ):
        return False  # custom (json-mode) serializers may not round-trip
    nested = seen | {id(model)}
    return all(
        _fully_typed(field.annotation, nested) for field in model.model_fields.values()
    )


def _fully_typed(annotation: Any, seen: frozenset[int] = frozenset()) -> bool:  # noqa: C901
    if annotation is Any or annotation is inspect.Parameter.empty:
        return False
    if annotation is None or annotation is types.NoneType:
        return True
    if annotation in _SCALARS:
        return True
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is None and inspect.isclass(annotation):
        if issubclass(annotation, Enum):
            return True
        if issubclass(annotation, BaseModel):
            return _model_fully_typed(annotation, seen)
        return False  # bare containers == containers of Any
    if origin is typing.Annotated:
        return _fully_typed(args[0], seen)
    if origin is Literal:
        return True
    if origin in (list, set, frozenset):
        return bool(args) and _fully_typed(args[0], seen)
    if origin is tuple:
        items = [a for a in args if a is not Ellipsis]
        return bool(items) and all(_fully_typed(a, seen) for a in items)
    if origin is dict:
        return (
            len(args) == 2
            and _is_key_annotation(args[0])
            and _fully_typed(args[0], seen)
            and _fully_typed(args[1], seen)
        )
    if origin in (Union, types.UnionType):
        if not all(_fully_typed(a, seen) for a in args):
            return False
        # Members whose JSON forms could collide (``UUID | str`` both dump to a
        # string, ``ModelA | ModelB`` both to an object) cannot be told apart
        # by the annotation: keep the tagged codec for them.
        kinds = [_json_kind(a) for a in args]
        concrete = [k for k in kinds if k != "null"]
        return (
            "unknown" not in kinds
            and len(concrete) == len(set(concrete))
            and ("string-enum" not in concrete or len(concrete) == 1)
        )
    return False


@functools.lru_cache(maxsize=4096)
def _fully_typed_cached(annotation: Any) -> bool:
    return _fully_typed(annotation)


def is_fully_typed(annotation: Any) -> bool:
    """Whether ``annotation`` alone restores every value type (codec v2-safe).

    ``False`` for ``Any`` anywhere (incl. bare ``list``/``dict``), models with
    ``extra="allow"`` or custom serializers, and unions whose members dump to
    the same JSON shape (``UUID | str``) — those keep the tagged codec.
    """
    try:
        return _fully_typed_cached(annotation)
    except TypeError:  # unhashable annotation metadata
        return _fully_typed(annotation)


def _immutable_types(annotation: Any) -> frozenset[type] | None:  # noqa: C901
    """Exact runtime types that are returned unchanged for ``annotation``.

    ``None`` when values of this annotation may be mutable (must be copied).
    """
    if annotation is None or annotation is types.NoneType:
        return frozenset({types.NoneType})
    if annotation in _IMMUTABLE_SCALARS:
        return frozenset({annotation})
    origin = get_origin(annotation)
    if origin is typing.Annotated:
        args = get_args(annotation)
        # Constraints (Field(ge=...)) must still be validated: no passthrough.
        return _immutable_types(args[0]) if len(args) == 1 else None
    if origin in (Union, types.UnionType):
        found: set[type] = set()
        for arg in get_args(annotation):
            member = _immutable_types(arg)
            if member is None:
                return None
            found |= member
        # ``int | float``-style unions coerce across members: keep it exact.
        if len(found - {types.NoneType}) > 1:
            return None
        return frozenset(found)
    if origin is None and inspect.isclass(annotation):
        if issubclass(annotation, Enum):
            return frozenset({annotation})
        if (
            issubclass(annotation, BaseModel)
            and (getattr(annotation, "model_config", {}) or {}).get("frozen")
            and is_fully_typed(annotation)
            and all(
                _immutable_types(field.annotation) is not None
                for field in annotation.model_fields.values()
            )
        ):
            return frozenset({annotation})
    return None


class WirePlan:
    """How values of one annotation cross the contract boundary (cached).

    Built once per parameter / return annotation. ``local`` is what an
    in-process call does (observably what a remote peer would get back);
    ``to_wire``/``from_wire`` are the RPC sender/receiver halves.
    """

    __slots__ = ("annotation", "ints", "typed", "passthrough", "_adapter")

    def __init__(self, annotation: Any) -> None:
        self.annotation = annotation
        self.typed = is_fully_typed(annotation)
        self.passthrough = _immutable_types(annotation)
        self._adapter: TypeAdapter[Any] | None = None
        #: the typed dump may hold an int (range-checked, see ``dump_typed``)
        self.ints = self.typed and _may_hold_int(annotation)

    @property
    def adapter(self) -> TypeAdapter[Any]:
        adapter = self._adapter
        if adapter is None:
            adapter = self._adapter = type_adapter(self.annotation)
        return adapter

    def is_passthrough(self, value: Any) -> bool:
        """``value`` is immutable and exactly of a type the annotation keeps."""
        passthrough = self.passthrough
        return passthrough is not None and type(value) in passthrough

    def dump_typed(self, value: Any) -> Any:
        """Codec v2 form of ``value``; raises :class:`ContractWireError` when
        ``value`` is not exactly of the annotated type (caller falls back to
        the tagged codec, which then validates like before)."""
        try:
            wire = self.adapter.dump_python(
                value, mode="json", by_alias=True, warnings="error"
            )
        except Exception as exc:  # serializer mismatch
            raise ContractWireError(str(exc)) from exc
        if self.ints:
            try:
                # orjson carries exactly msgpack's integer range: an int
                # beyond 64 bits sends the value through the tagged codec
                orjson.dumps(wire)
            except TypeError as exc:
                raise ContractWireError(str(exc)) from exc
        return wire

    def to_wire(self, value: Any, *, typed: bool) -> tuple[int, Any]:
        """``(codec, wire)``: codec v2 when ``typed`` and the value fits."""
        if typed and self.typed:
            try:
                return CODEC_TYPED, self.dump_typed(value)
            except ContractWireError:
                pass
        return CODEC_TAGGED, encode(value)

    def from_wire(self, wire: Any, codec: int) -> Any:
        """Untagged value of ``wire`` (validation is the caller's next step)."""
        if codec == CODEC_TYPED and self.typed:
            return wire
        return decode_untyped(wire)

    def decode(self, wire: Any, codec: int) -> Any:
        """Rebuild a received value (``from_wire`` + validation)."""
        value = self.from_wire(wire, codec)
        if self.annotation is Any or self.annotation is inspect.Parameter.empty:
            return value
        return self.adapter.validate_python(value)

    def local(self, value: Any) -> Any:
        """What the peer of an in-process call observes (validated copy).

        Immutable exact-type values pass through untouched; fully typed values
        round-trip through pydantic-core (codec v2); anything else through the
        tagged tree. Raises :class:`ContractWireError` (non-wire value) or
        ``pydantic.ValidationError`` (value does not match the annotation).
        """
        return self.local_and_wire(value)[0]

    def local_and_wire(self, value: Any) -> tuple[Any, Any]:
        """``(local(value), typed wire | None)``: the codec v2 form the copy
        was rebuilt from, when it took that path (else ``None``). Rebuilding
        from that wire (:meth:`from_typed_wire`) yields another equal copy
        without serializing again."""
        if self.is_passthrough(value):
            return value, None
        if self.typed:
            try:
                wire = self.dump_typed(value)
            except ContractWireError:
                pass
            else:
                return self.adapter.validate_python(wire), wire
        return validate(decode_untyped(encode(value)), self.annotation), None

    def from_typed_wire(self, wire: Any) -> Any:
        """A fresh copy rebuilt from a :meth:`local_and_wire` wire."""
        return self.adapter.validate_python(wire)


#: Annotations whose typed dump never holds a JSON integer.
_NO_INT_SCALARS: frozenset[Any] = frozenset(
    {str, float, bool, UUID, datetime, date, Decimal, type(None), None}
)


def _may_hold_int(annotation: Any, seen: frozenset[int] = frozenset()) -> bool:
    """Whether the typed dump of ``annotation`` may contain an ``int``
    (conservative: anything not provably int-free may)."""
    if annotation in _NO_INT_SCALARS:
        return False
    origin = get_origin(annotation)
    if origin is Literal:
        return any(
            isinstance(a, int) and not isinstance(a, bool) for a in get_args(annotation)
        )
    if origin is not None:
        return any(_may_hold_int(a, seen) for a in get_args(annotation))
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        if id(annotation) in seen:
            return False
        inner = seen | {id(annotation)}
        return any(
            _may_hold_int(f.annotation, inner) for f in annotation.model_fields.values()
        )
    if inspect.isclass(annotation) and issubclass(annotation, Enum):
        return any(isinstance(m.value, int) for m in annotation)
    return True


def wire_plan(annotation: Any) -> WirePlan:
    """A (cached) :class:`WirePlan` for ``annotation``."""
    try:
        return _cached_plan(annotation)
    except TypeError:  # unhashable annotation metadata
        return WirePlan(annotation)


@functools.lru_cache(maxsize=4096)
def _cached_plan(annotation: Any) -> WirePlan:
    return WirePlan(annotation)


# --------------------------------------------------------------------------- #
# Portable type descriptors (provider -> remote caller)
# --------------------------------------------------------------------------- #
def _ref(tp: type) -> str:
    return f"{tp.__module__}:{tp.__qualname__}"


def type_descriptor(  # noqa: C901
    annotation: Any, *, _seen: frozenset[int] = frozenset()
) -> dict[str, Any]:
    """JSON-able description of a wire annotation (see annotation_from_descriptor).

    A pydantic model also lists its ``fields`` (name -> ``{"type", "required"}``)
    so a peer can judge a model change structurally (an added optional field
    is compatible, a new required one is not — :mod:`sandloom.core.contract_compat`);
    a recursive reference omits them.
    """
    if annotation is Any or annotation is inspect.Parameter.empty:
        return {"k": "any"}
    if annotation is None or annotation is types.NoneType:
        return {"k": "none"}
    if annotation in _SCALARS:
        return {"k": "scalar", "t": _SCALARS[annotation]}
    if get_origin(annotation) is None and inspect.isclass(annotation):
        if issubclass(annotation, Enum):
            return {
                "k": "enum",
                "ref": _ref(annotation),
                "values": [encode(member.value) for member in annotation],
            }
        if issubclass(annotation, BaseModel):
            schema = orjson.dumps(
                annotation.model_json_schema(), option=orjson.OPT_SORT_KEYS
            )
            desc: dict[str, Any] = {
                "k": "model",
                "ref": _ref(annotation),
                "schema_hash": hashlib.sha256(schema).hexdigest()[:16],
            }
            if id(annotation) not in _seen:
                seen = _seen | {id(annotation)}
                desc["fields"] = {
                    name: {
                        "type": type_descriptor(field.annotation, _seen=seen),
                        "required": field.is_required(),
                    }
                    for name, field in annotation.model_fields.items()
                }
            return desc
        if annotation in (list, set, frozenset):
            return {"k": annotation.__name__, "item": {"k": "any"}}
        if annotation is tuple:
            return {"k": "tuple", "item": {"k": "any"}, "variadic": True}
        if annotation is dict:
            return {"k": "dict", "key": {"k": "any"}, "value": {"k": "any"}}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is typing.Annotated:
        return type_descriptor(args[0], _seen=_seen)
    if origin is Literal:
        return {"k": "literal", "values": list(args)}
    if origin in (list, set, frozenset):
        return {
            "k": origin.__name__,
            "item": (type_descriptor(args[0], _seen=_seen) if args else {"k": "any"}),
        }
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return {
                "k": "tuple",
                "item": type_descriptor(args[0], _seen=_seen),
                "variadic": True,
            }
        return {"k": "tuple", "items": [type_descriptor(a, _seen=_seen) for a in args]}
    if origin is dict:
        key, value = args if args else (Any, Any)
        return {
            "k": "dict",
            "key": type_descriptor(key, _seen=_seen),
            "value": type_descriptor(value, _seen=_seen),
        }
    if origin in (Union, types.UnionType):
        return {"k": "union", "of": [type_descriptor(a, _seen=_seen) for a in args]}
    raise ContractTypeError(f"{annotation!r} has no wire descriptor")


def _resolve_ref(ref: str) -> Any:
    """The class named by ``ref`` IF its module is already imported (no
    import); else a loaded generated copy of it (``sandloom generate`` writes
    the copies of a provider's own DTOs into the shared DTO package)."""
    module_name, _, qualname = ref.partition(":")
    module = sys.modules.get(module_name)
    if module is None:
        copies = dto_copies(ref)
        return copies[0] if copies else None
    obj: Any = module
    for part in qualname.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def annotation_from_descriptor(desc: dict[str, Any]) -> Any:  # noqa: C901
    """Rebuild an annotation from :func:`type_descriptor` output.

    A pydantic model / Enum resolves to the caller's class when its module is
    already imported in the caller process (the DTO lives in a package both
    sides import, e.g. ``sandloom.shared``), else to a loaded generated copy of
    the provider's class; otherwise it degrades to plain data
    (``dict[str, Any]`` / ``Literal`` of the enum values) — never an import
    with side effects driven by a peer.
    """
    kind = desc.get("k")
    if kind == "none":
        return type(None)
    if kind == "scalar":
        return _SCALAR_BY_NAME.get(str(desc.get("t")), Any)
    if kind == "enum":
        resolved = _resolve_ref(str(desc.get("ref", "")))
        if inspect.isclass(resolved) and issubclass(resolved, Enum):
            return resolved
        values = tuple(decode_untyped(v) for v in desc.get("values") or ())
        return Literal[values] if values else Any
    if kind == "model":
        resolved = _resolve_ref(str(desc.get("ref", "")))
        if inspect.isclass(resolved) and issubclass(resolved, BaseModel):
            return resolved
        return dict[str, Any]
    if kind == "literal":
        values = tuple(desc.get("values") or ())
        return Literal[values] if values else Any
    if kind in ("list", "set", "frozenset"):
        item = annotation_from_descriptor(desc.get("item") or {"k": "any"})
        container = {"list": list, "set": set, "frozenset": frozenset}[str(kind)]
        return container[item]  # type: ignore[index]
    if kind == "tuple":
        if desc.get("variadic"):
            return tuple[annotation_from_descriptor(desc.get("item") or {}), ...]  # type: ignore[misc]
        items = tuple(annotation_from_descriptor(d) for d in desc.get("items") or ())
        return tuple[items] if items else tuple[()]  # type: ignore[valid-type]
    if kind == "dict":
        return dict[  # type: ignore[misc]
            annotation_from_descriptor(desc.get("key") or {}),
            annotation_from_descriptor(desc.get("value") or {}),
        ]
    if kind == "union":
        members = tuple(annotation_from_descriptor(d) for d in desc.get("of") or ())
        return Union[members] if members else Any  # noqa: UP007
    return Any


__all__ = [
    "ContractTypeError",
    "ContractWireError",
    "WIRE_TAG",
    "WIRE_VALUE",
    "annotation_from_descriptor",
    "annotation_problem",
    "CODEC_TAGGED",
    "CODEC_TYPED",
    "SUPPORTED_CODECS",
    "WirePlan",
    "decode",
    "decode_untyped",
    "digest",
    "encode",
    "is_fully_typed",
    "nominal_types",
    "roundtrip",
    "type_adapter",
    "type_descriptor",
    "validate",
    "wire_plan",
]
