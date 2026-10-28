"""What a ``Contract(REF)`` caller receives: results as its stub names them.

The generated stub's Protocol annotates each method with the generated DTO
copies of the shared DTO package (``sandloom generate``, SANDLOOM-C012). The
runtime decodes a result against the provider's annotations (locally: the
provider's own class; over RPC: the class the describe document resolves
to), so the caller side converts it to the stub's annotation, on both paths:
every caller process holds the same generated class.

The conversion is skipped when the annotation names no model / Enum or the
value already is of the stub's class. A frozen copy whose fields are
immutable scalars is rebuilt from the provider instance's validated field
values (no second validation: they are the same data); anything else is
dumped to JSON and validated against the stub's annotation -- what an RPC
caller does. Converters are built once per ``(Protocol, method)``.
"""

from __future__ import annotations

import datetime as _dt
import enum
import inspect
import threading
import types
import typing
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, TypeAdapter

#: The attribute ``dto_copy`` records a generated copy's origin in
#: (``sandloom.core.descriptors.contract_refs.DTO_COPY_ATTR``).
_COPY_ATTR = "__sandloom_dto__"
_ANY: TypeAdapter[Any] = TypeAdapter(Any)
_SCALARS = frozenset({str, int, float, bool, uuid.UUID, _dt.datetime, _dt.date})
_object_setattr = object.__setattr__

Converter = Callable[[Any], Any]
_NONE = object()
_converters: dict[tuple[type, str], Converter | None] = {}
_lock = threading.Lock()


def _nominal(annotation: Any, seen: set[int] | None = None) -> bool:
    """``annotation`` names a pydantic model or an Enum (at any depth)."""
    seen = set() if seen is None else seen
    if inspect.isclass(annotation):
        return issubclass(annotation, BaseModel | enum.Enum)
    if id(annotation) in seen:
        return False
    seen.add(id(annotation))
    return any(
        _nominal(arg, seen)
        for arg in typing.get_args(annotation)
        if arg is not Ellipsis and not isinstance(arg, str | int | float | bool)
    )


def _scalar_field(annotation: Any) -> bool:
    """An immutable non-Enum scalar (``X | None``, ``Literal`` included)."""
    if annotation in _SCALARS or annotation is type(None):
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return not any(isinstance(a, enum.Enum) for a in typing.get_args(annotation))
    if origin in (typing.Union, types.UnionType):
        return all(_scalar_field(a) for a in typing.get_args(annotation))
    return False


def _fast_class(annotation: Any) -> type[BaseModel] | None:
    """The generated copy ``C`` of ``C`` / ``C | None`` when its values can
    be rebuilt from a provider instance's fields: frozen, closed to extra
    keys, no private state, every field an immutable scalar."""
    members = [annotation]
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
    if len(members) != 1 or not inspect.isclass(members[0]):
        return None
    cls = members[0]
    if not issubclass(cls, BaseModel) or not isinstance(
        cls.__dict__.get(_COPY_ATTR), str
    ):
        return None
    config = cls.model_config
    if not config.get("frozen") or config.get("extra") == "allow":
        return None
    if cls.__private_attributes__:
        return None
    if not all(_scalar_field(f.annotation) for f in cls.model_fields.values()):
        return None
    return cls


def _origin(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _build(annotation: Any) -> Converter | None:
    if annotation is Any or not _nominal(annotation):
        return None
    adapter: TypeAdapter[Any] = TypeAdapter(annotation)
    exact = annotation if inspect.isclass(annotation) else None
    fast = _fast_class(annotation)
    origin = fast.__dict__[_COPY_ATTR] if fast is not None else None
    optional = exact is None
    fields = tuple(fast.model_fields) if fast is not None else ()

    def convert(value: Any) -> Any:
        kind = type(value)
        if kind is exact or (optional and value is None) or kind is fast:
            return value
        if fast is not None and _origin(kind) == origin:
            copy = fast.__new__(fast)
            _object_setattr(copy, "__dict__", dict(value.__dict__))
            # every field set: what a caller reads off the wire
            _object_setattr(copy, "__pydantic_fields_set__", set(fields))
            _object_setattr(copy, "__pydantic_extra__", None)
            _object_setattr(copy, "__pydantic_private__", None)
            return copy
        return adapter.validate_json(_ANY.dump_json(value, by_alias=True))

    return convert


def result_converter(protocol: type | None, method: str) -> Converter | None:
    """The conversion of ``protocol.method``'s results to the stub's return
    annotation, or ``None`` when none is needed (built once)."""
    if protocol is None:
        return None
    key = (protocol, method)
    found = _converters.get(key, _NONE)
    if found is not _NONE:
        return found  # type: ignore[return-value]
    with _lock:
        member = getattr(protocol, method, None)
        try:
            hints = typing.get_type_hints(member) if member is not None else {}
        except (NameError, TypeError, AttributeError):  # unresolvable: as they are
            hints = {}
        built = _build(hints.get("return", Any))
        _converters[key] = built
        return built


async def converted(call: Awaitable[Any], convert: Converter) -> Any:
    """Await ``call`` and convert its result."""
    return convert(await call)


__all__ = ["converted", "result_converter"]
