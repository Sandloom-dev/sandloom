"""Pure event-schema compatibility; the generator and offline diffs share it."""

from __future__ import annotations

import decimal
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from sandloom.core.contracts.schema_registry import DEFAULTED


def schema_problems(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> list[str]:
    """Breaking differences on the same ``.vN`` (spec §5.2, G-DECL-01): only
    adding an optional field is compatible, at every depth -- ``$ref`` /
    ``$defs`` are resolved, so a referenced enum or a nested DTO follows the
    same additive-only rule as a top-level field, and a recursive DTO is
    compared once per ``($ref, $ref)`` pair on a descent.

    Producers and consumers of one ``.vN`` deploy independently, so the rule
    holds BOTH directions (round 6): a reader of ``new`` accepts every
    payload a producer of ``old`` may still send, and a reader of ``old``
    accepts every payload a producer of ``new`` may send. A validation
    constraint (bounds, sizes, ``multipleOf``, ``pattern``, ``uniqueItems``,
    forbidden extra fields) is therefore never added, tightened, loosened,
    removed or changed, and a declared field is never removed (an old
    reader relies on it, open model or not) nor loses its default (round 8:
    the serialization schema lists a defaulted field as required either
    way, yet a producer from before the field omits it; the ``DEFAULTED``
    mark tells them apart). Every subschema applicator
    follows the same rule:
    dictionary keys (``propertyNames``) and regex-keyed values
    (``patternProperties``; a new key pattern fails), tuple positions,
    ``contains``, ``then`` / ``else``, ``dependent*`` and ``unevaluated*``;
    a changed ``not`` / ``if`` fails. ``required`` and an added property are
    compared in every subschema, with or without ``properties`` (round 4):
    only a DTO may gain an optional field, since only its producer sends
    nothing but its declared fields; in a dictionary, or below an overlaying
    applicator, an added property is compared with the schema that governed
    that key. A keyword beside a ``$ref`` is compared on its own. The
    differential test (``test_sdk_schema_differential``) holds the rule to
    the validator, both ways: a change it accepts rejects no payload of the
    old schema under the new one, nor of the new schema under the old one;
    on real Event classes it holds the rule to the real readers
    (``from_wire``) too, and the wire schema to what its reader accepts.

    Each direction is one comparison of a producer's schema with a reader's
    (``_SchemaDiff``); a problem the second (new producer, old reader) finds
    at a path the first already reported is not repeated."""
    if old is None:
        return []
    backward = _SchemaDiff(old, new)
    problems = backward.problems()
    forward = _SchemaDiff(new, old, forward=True)
    forward.problems()
    problems += [
        text
        for path, text in forward.found
        if path not in backward.paths and text not in problems
    ]
    return problems


#: Keywords of a scalar's shape (an ``enum`` is compared as a set).
_SHAPE_KEYS = ("type", "format", "const")
#: Keywords whose subschemas are compared branch by branch.
_BRANCH_KEYS = ("anyOf", "oneOf", "allOf", "prefixItems")
#: Container keywords: present on one side only is a type change.
_CONTAINERS = (("items", "[]"), ("additionalProperties", "{}"))
#: Applicators whose absence accepts anything: one the new schema adds is
#: compared against ``{}``, so it fails exactly when it narrows something.
_OPTIONAL_SUBSCHEMAS = (
    ("propertyNames", "{keys}"),
    ("then", "[then]"),
    ("else", "[else]"),
    ("unevaluatedItems", "[unevaluated]"),
    ("unevaluatedProperties", "{unevaluated}"),
)
#: Applicators whose subschema is not monotone in the accepted values (a
#: narrower ``not`` accepts more, a moved ``if`` reroutes then/else): any
#: change fails, except removing one (a ``not`` gone, or an ``if`` left
#: without a ``then`` / ``else``) -- that only loosens.
_OPAQUE_SUBSCHEMAS = ("not", "if")
#: Applicators that overlay an instance another keyword shapes (every
#: branch of ``allOf``, ``then`` / ``else``, ``contains``,
#: ``dependentSchemas``). Below one, any key may be present -- the producer
#: shaped the instance elsewhere -- so the "an added property is an
#: optional field no old producer sent" rule does not hold there.
_OVERLAY_KEYS = frozenset({"allOf", "then", "else", "contains", "dependentSchemas"})
#: Keywords that annotate and never validate: a ``$ref`` beside only these
#: is a bare reference.
_ANNOTATIONS = frozenset(
    {
        "$ref",
        "$comment",
        "title",
        "description",
        "default",
        DEFAULTED,
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)
#: Keywords that ``false`` closes (no further item / property accepted).
_CLOSABLE = (
    "items",
    "additionalProperties",
    "unevaluatedItems",
    "unevaluatedProperties",
)


class _SchemaDiff:
    """One direction of the comparison of two JSON schemas, ``$ref``
    resolved against each document's own ``$defs``: everything a reader of
    the second (``new``) rejects that a producer of the first (``old``) may
    send.

    ``forward=False`` compares the old producer with the new reader;
    ``forward=True`` is called with the schemas swapped -- the NEW producer
    (its ``old`` side) and the OLD reader (its ``new`` side) -- and words its
    problems from the change's point of view (``loosened``, ``old readers``).
    A field the producer declares and the reader does not is "removed" only
    backward: forward, it is a field the change adds."""

    def __init__(
        self, old: Mapping[str, Any], new: Mapping[str, Any], *, forward: bool = False
    ) -> None:
        self.roots = (old, new)
        self.forward = forward
        #: ``(path, problem)`` in the order found.
        self.found: list[tuple[str, str]] = []
        self.paths: set[str] = set()
        #: The ``($ref old, $ref new)`` pairs being compared on the current
        #: descent: a recursive DTO meets its own pair again below itself,
        #: where everything it could report is already being reported.
        self.active: set[tuple[str, str]] = set()

    def problems(self) -> list[str]:
        self.compare(self.roots[0], self.roots[1], "")
        return [text for _path, text in self.found]

    def _report(self, path: str, text: str) -> None:
        self.found.append((path, f"field {path!r} {text}"))
        self.paths.add(path)

    def _resolve(self, spec: Mapping[str, Any], side: int) -> Mapping[str, Any]:
        """Follow bare ``$ref`` hops; stop at a target that carries its own
        constraints beside a ``$ref`` (``compare`` splits that one)."""
        node = spec
        while _bare_ref(node):
            target: Any = self.roots[side]
            for part in node["$ref"][2:].split("/"):
                target = target.get(part) if isinstance(target, Mapping) else None
            if not isinstance(target, Mapping):
                return node
            node = target
        return node

    def compare(
        self,
        old: Mapping[str, Any],
        new: Mapping[str, Any],
        path: str,
        overlay: bool = False,
    ) -> None:
        """Report every way ``new`` accepts less than ``old`` at ``path``.

        ``overlay``: the node is below an applicator that overlays an
        instance shaped elsewhere (``_OVERLAY_KEYS``)."""
        if _constrained_ref(old) or _constrained_ref(new):
            # ``{"$ref": X, "minimum": 1}`` is ``X`` AND its siblings: each
            # part is compared on its own (a side without ``$ref`` is all
            # reference part), so neither is lost to the resolution.
            self.compare(_ref_part(old), _ref_part(new), path, overlay)
            self.compare(_sibling_part(old), _sibling_part(new), path, True)
            return
        refs = (str(old.get("$ref", "")), str(new.get("$ref", "")))
        if not all(refs):
            self._compare(old, new, path, overlay)
            return
        if refs in self.active:  # a recursive DTO, already being compared
            return
        self.active.add(refs)
        try:
            self._compare(old, new, path, overlay)
        finally:
            self.active.discard(refs)

    def _compare(
        self,
        old: Mapping[str, Any],
        new: Mapping[str, Any],
        path: str,
        overlay: bool,
    ) -> None:
        old, new = self._resolve(old, 0), self._resolve(new, 1)
        if _constrained_ref(old) or _constrained_ref(new):
            self.compare(old, new, path, overlay)
            return
        problem = _shape_problem(old, new, flip=self.forward)
        if problem is not None:
            self._report(path, problem)
            return
        verb = "loosened" if self.forward else "narrowed"
        for what in _narrowed(old, new, flip=self.forward):
            self._report(path, f"{verb} {what}")
        for a, b, at, below in (
            *_applied(old, new, path, overlay),
            *_keyed(old, new, path, overlay),
        ):
            self.compare(a, b, at, below)
        self._required(old, new, path, overlay)
        if "properties" in old or "properties" in new:
            self._fields(old, new, path, overlay)

    def _required(
        self,
        old: Mapping[str, Any],
        new: Mapping[str, Any],
        path: str,
        overlay: bool,
    ) -> None:
        """``required`` narrows any node, with or without ``properties`` (a
        dictionary, a ``dependentSchemas`` / ``then`` / ``allOf`` subschema):
        a name the reader requires and the producer does not fails, except a
        defaulted field (``DEFAULTED``) the producer's model does not declare
        (the reader fills it)."""
        old_props = old.get("properties") or {}
        new_props = new.get("properties") or {}
        old_required = set(old.get("required") or [])
        new_required = set(new.get("required") or [])
        prefix = f"{path}." if path else ""
        for field_name in sorted(new_required - old_required):
            # A defaulted field is listed as required in a serialization
            # schema (always emitted), yet optional to whoever constructs it:
            # its reader fills the default when an old producer omits it
            # (what an open model's old producer sent under that key is
            # checked against the field by ``_fields``). A default of any
            # kind -- literal or factory -- is marked ``DEFAULTED`` by the
            # wire schema; a factory default has no ``default`` keyword.
            if (
                _model(old, overlay)
                and field_name not in old_props
                and new_props.get(field_name, {}).get(DEFAULTED) is True
            ):
                continue
            if not self.forward:
                text = "is required now (only optional fields may be added)"
            elif field_name in old_props:
                text = "is optional now (old readers require it)"
            else:
                text = "is no longer required (old readers require it)"
            self._report(prefix + field_name, text)
        if not self.forward and _model(old, overlay) and _model(new, overlay):
            # A default removed (round 8): the serialization schema lists the
            # field as required on both sides, yet a producer from before
            # the field was added -- still deployed on this ``.vN`` -- omits
            # it, and only a defaulted reader fills it. (Adding a default is
            # compatible: every producer of the old schema sends the field.)
            for field_name in sorted(old_required & new_required):
                if (
                    old_props.get(field_name, {}).get(DEFAULTED) is True
                    and new_props.get(field_name, {}).get(DEFAULTED) is not True
                ):
                    self._report(
                        prefix + field_name,
                        "lost its default (a producer from before the field omits it)",
                    )
        for field_name in sorted(old_required - new_required):
            if field_name in new_props:
                self._report(
                    prefix + field_name,
                    "is required now (only optional fields may be added)"
                    if self.forward
                    else "is optional now (readers rely on it)",
                )

    def _fields(
        self,
        old: Mapping[str, Any],
        new: Mapping[str, Any],
        path: str,
        overlay: bool,
    ) -> None:
        """Per-property comparison. A property on one side only is compared
        with the schema that governs that key on the other side
        (``_governing``): only a closed DTO (``_struct``) may gain an
        optional field no old producer sent. A model's field is never
        removed, open to extra keys or not: an old reader relies on it
        (round 6). Forward, a field only the (new) producer declares is one
        the change adds: the old reader's governing schema must accept it."""
        old_props = dict(old.get("properties") or {})
        new_props = dict(new.get("properties") or {})
        prefix = f"{path}." if path else ""
        for field_name, spec in old_props.items():
            at = prefix + field_name
            if field_name in new_props:
                self.compare(spec, new_props[field_name], at, overlay)
                continue
            if not self.forward and _model(old, overlay):
                self._report(at, "was removed")
                continue
            governing = _governing(new, field_name)
            if governing is None:
                self._report(
                    at,
                    "was added where old readers forbid other keys"
                    if self.forward
                    else "was removed",
                )
            for schema in governing or ():
                if schema:  # ``{}``: the reader accepts any value there
                    self.compare(spec, schema, at, overlay)
        for field_name, spec in new_props.items():
            if field_name in old_props or _struct(old, overlay):
                continue
            for schema in _governing(old, field_name) or ():
                self.compare(schema, spec, prefix + field_name, overlay)


def _bare_ref(node: Mapping[str, Any]) -> bool:
    """A local ``$ref`` with annotations at most beside it."""
    ref = node.get("$ref")
    return isinstance(ref, str) and ref.startswith("#/") and not _constrained_ref(node)


def _constrained_ref(node: Mapping[str, Any]) -> bool:
    """A ``$ref`` with a validation keyword beside it (2020-12 applies both)."""
    return "$ref" in node and any(key not in _ANNOTATIONS for key in node)


def _ref_part(node: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"$ref": node["$ref"]} if "$ref" in node else node


def _sibling_part(node: Mapping[str, Any]) -> Mapping[str, Any]:
    return {k: v for k, v in node.items() if k != "$ref"} if "$ref" in node else {}


def _struct(node: Mapping[str, Any], overlay: bool) -> bool:
    """A closed DTO: declared ``properties`` that shape the instance (not an
    overlay), and nothing said of other keys (pydantic's default) or
    ``additionalProperties: false`` -- its producer sends only the declared
    fields. A model open to extra keys (``extra="allow"``:
    ``additionalProperties`` true or a schema) or with key patterns is an
    open object: its producer may send any other key, so a field it newly
    declares must accept whatever that key could hold (``_governing``)."""
    return (
        not overlay
        and "properties" in node
        and node.get("additionalProperties", False) is False
        and not node.get("patternProperties")
    )


def _model(node: Mapping[str, Any], overlay: bool) -> bool:
    """A model, closed or open: declared ``properties`` that shape the
    instance. Its reader fills a field's default when the key is absent."""
    return not overlay and "properties" in node


def _governing(node: Mapping[str, Any], key: str) -> list[Mapping[str, Any]] | None:
    """The schemas ``node`` applies to an undeclared property ``key``: the
    matching ``patternProperties``, else ``additionalProperties`` (``{}``
    when it is absent or ``true``); ``None`` when the key is forbidden."""
    matching: list[Mapping[str, Any]] = []
    for pattern, spec in (node.get("patternProperties") or {}).items():
        try:
            matched = re.search(pattern, key) is not None
        except re.error:
            matched = True  # unknown dialect: assume it applies
        if matched and isinstance(spec, Mapping):
            matching.append(spec)
    if matching:
        return matching
    extra = node.get("additionalProperties", True)
    if extra is False:
        return None
    return [extra] if isinstance(extra, Mapping) else [{}]


Pair = tuple[Mapping[str, Any], Mapping[str, Any], str, bool]


def _applied(
    old: Mapping[str, Any], new: Mapping[str, Any], path: str, overlay: bool
) -> list[Pair]:
    """The ``(old, new, path, overlay)`` subschema pairs of the positional
    and the single-subschema applicators (an absent optional one is ``{}``)."""
    pairs: list[Pair] = []
    for key in _BRANCH_KEYS:
        branches = zip(old.get(key) or (), new.get(key) or (), strict=True)
        below = overlay or key in _OVERLAY_KEYS
        for index, (a, b) in enumerate(branches):
            at = f"{path}[{index}]" if key == "prefixItems" else path
            pairs.append((a, b, at, below))
    for key, suffix in (
        *_CONTAINERS,
        *_OPTIONAL_SUBSCHEMAS,
        ("contains", "[contains]"),
    ):
        # A container or ``contains`` present on one side only is reported
        # by ``_shape_problem`` / ``_narrowed`` (``contains: {}`` still
        # demands one element).
        absent: dict[str, Any] | None = (
            {} if (key, suffix) in _OPTIONAL_SUBSCHEMAS else None
        )
        a, b = old.get(key, absent), new.get(key)
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            pairs.append((a, b, f"{path}{suffix}", overlay or key in _OVERLAY_KEYS))
    return pairs


def _keyed(
    old: Mapping[str, Any], new: Mapping[str, Any], path: str, overlay: bool
) -> list[Pair]:
    """The subschema pairs of ``patternProperties`` (shared patterns; a new
    one is a shape problem; the keys of a removed one fall back to the new
    ``additionalProperties``) and ``dependentSchemas`` (a new one against
    ``{}``)."""
    patterns = old.get("patternProperties") or {}
    new_patterns = new.get("patternProperties") or {}
    pairs: list[Pair] = [
        (patterns[pattern], spec, f"{path}{{{pattern}}}", overlay)
        for pattern, spec in new_patterns.items()
        if pattern in patterns
    ]
    extra = new.get("additionalProperties")
    if isinstance(extra, Mapping):
        pairs += [
            (spec, extra, f"{path}{{{pattern}}}", overlay)
            for pattern, spec in patterns.items()
            if pattern not in new_patterns and isinstance(spec, Mapping)
        ]
    dependent = old.get("dependentSchemas") or {}
    pairs += [
        (dependent.get(name, {}), spec, f"{path}[dependentSchemas.{name}]", True)
        for name, spec in (new.get("dependentSchemas") or {}).items()
    ]
    return pairs


def _shape_problem(
    old: Mapping[str, Any], new: Mapping[str, Any], *, flip: bool = False
) -> str | None:
    """What changed in the node's own shape (not its children), if anything
    (``flip``: spelled ``new -> old``, as ``_narrowed``)."""
    if "enum" in old or "enum" in new:
        before, after = _values(old.get("enum")), _values(new.get("enum"))
        if before != after:
            return f"changed its values: {_arrow(before, after, flip)}"
    if any(old.get(k) != new.get(k) for k in _SHAPE_KEYS) or any(
        len(old.get(k) or ()) != len(new.get(k) or ()) for k in _BRANCH_KEYS
    ):
        return "changed type"
    for key, _suffix in _CONTAINERS:
        if isinstance(old.get(key), Mapping) != isinstance(new.get(key), Mapping):
            return "changed type"
    for key in _OPAQUE_SUBSCHEMAS:
        live = key in new and (key == "not" or "then" in new or "else" in new)
        if live and old.get(key) != new.get(key):
            return f"changed its {key} schema"
    # A key pattern the old schema lacks puts the keys it matches under a
    # schema no old producer was held to (a removed pattern only loosens).
    before = sorted(old.get("patternProperties") or {})
    after = sorted(new.get("patternProperties") or {})
    # A removed one hands its keys to ``additionalProperties`` (compared in
    # ``_keyed``); a closed one rejects them.
    removed = set(before) - set(after)
    if set(after) - set(before) or (
        removed and new.get("additionalProperties") is False
    ):
        return f"changed its key patterns: {_arrow(before, after, flip)}"
    return None


def _arrow(before: Any, after: Any, flip: bool) -> str:
    return f"{after} -> {before}" if flip else f"{before} -> {after}"


#: ``(keyword, exclusive keyword)`` of each numeric bound.
_LOWER = ("minimum", "exclusiveMinimum")
_UPPER = ("maximum", "exclusiveMaximum")
#: Size keywords: a larger minimum / a smaller maximum accepts fewer values.
_MIN_SIZES = ("minLength", "minItems", "minProperties", "minContains")
_MAX_SIZES = ("maxLength", "maxItems", "maxProperties", "maxContains")


def _narrowed(
    old: Mapping[str, Any], new: Mapping[str, Any], *, flip: bool = False
) -> list[str]:
    """Every validation keyword of ``new`` that accepts fewer values than
    ``old`` (G-DECL-01): a reader of ``new`` would reject a payload a
    producer of ``old`` may still send. ``flip``: spelled ``new -> old`` (the
    forward comparison, called with the schemas swapped, reports what the
    change loosened)."""

    def change(before: Any, after: Any) -> str:
        return f"({_arrow(before, after, flip)})"

    out = [
        what
        for what in (
            _bound(old, new, _LOWER, 1, change),
            _bound(old, new, _UPPER, -1, change),
        )
        if what is not None
    ]
    for key in _MIN_SIZES + _MAX_SIZES:
        before, after = old.get(key), new.get(key)
        if after is None or before == after:
            continue
        tighter = before is None or (
            after > before if key in _MIN_SIZES else after < before
        )
        if tighter:
            out.append(f"{key} {change(_text(before), after)}")
    before, after = old.get("multipleOf"), new.get("multipleOf")
    if (
        after is not None
        and before != after
        and (before is None or not _divides(after, before))
    ):
        out.append(f"multipleOf {change(_text(before), after)}")
    before, after = old.get("pattern"), new.get("pattern")
    if after is not None and before != after:
        out.append(f"pattern {change(_text(before), after)}")
    return out + _narrowed_structure(old, new, change)


def _narrowed_structure(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    change: Callable[[Any, Any], str],
) -> list[str]:
    """The structural keywords of :func:`_narrowed`: ``uniqueItems``, a
    closed container, ``contains`` and ``dependentRequired``."""
    out: list[str] = []
    if new.get("uniqueItems") is True and old.get("uniqueItems") is not True:
        out.append(f"uniqueItems {change('false', 'true')}")
    out += [
        f"{key} {change('allowed', 'forbidden')}"
        for key in _CLOSABLE
        if new.get(key) is False and old.get(key) is not False
    ]
    if "contains" in new and "contains" not in old:
        out.append(f"contains {change('none', 'schema')}")
    old_dependent = old.get("dependentRequired") or {}
    for name, required in (new.get("dependentRequired") or {}).items():
        before = sorted(old_dependent.get(name) or [])
        if set(required) - set(before):
            out.append(f"dependentRequired {name} {change(before, sorted(required))}")
    return out


def _bound(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    keys: tuple[str, str],
    sign: int,
    change: Callable[[Any, Any], str],
) -> str | None:
    """A numeric bound (``sign`` 1: lower, -1: upper) that ``new`` tightens.

    A bound is ``(value, exclusive)``; ``minimum`` and ``exclusiveMinimum``
    are compared as one, so ``ge=0 -> gt=-1`` is not a tightening while
    ``ge=0 -> gt=0`` and ``ge=0 -> ge=1`` are."""
    before, after = _limit(old, keys, sign), _limit(new, keys, sign)
    if after is None or before == after:
        return None
    if before is not None:
        (b_value, b_excl), (a_value, a_excl) = before, after
        if sign * a_value < sign * b_value or (
            a_value == b_value and b_excl and not a_excl
        ):
            return None  # loosened
    label = "lower bound" if sign > 0 else "upper bound"
    return f"{label} {change(_spell(before, keys), _spell(after, keys))}"


def _limit(
    spec: Mapping[str, Any], keys: tuple[str, str], sign: int
) -> tuple[Any, bool] | None:
    """The effective ``(value, exclusive)`` bound of ``spec`` (the tighter of
    the inclusive and the exclusive keyword when both are present)."""
    candidates = [
        (spec[key], key == keys[1]) for key in keys if spec.get(key) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: (sign * c[0], c[1]))


def _spell(limit: tuple[Any, bool] | None, keys: tuple[str, str]) -> str:
    if limit is None:
        return "none"
    return f"{keys[1] if limit[1] else keys[0]} {limit[0]}"


def _divides(divisor: Any, value: Any) -> bool:
    """``value`` is an integer multiple of ``divisor`` (exact for decimals)."""
    quotient = decimal.Decimal(str(value)) / decimal.Decimal(str(divisor))
    return quotient == quotient.to_integral_value()


def _text(value: Any) -> str:
    return "none" if value is None else str(value)


def _values(enum: Any) -> list[Any]:
    """An ``enum`` keyword as a sorted list (its order is not part of it)."""
    return sorted(enum or [], key=lambda v: json.dumps(v, sort_keys=True))
