# src/sandloom/core/contracts/validators.py
from __future__ import annotations

import json
import re
from typing import Any, cast

import sandloom.core.contracts.schema_registry as _schema_registry

# Service & channel names MUST be versioned (`domain.component(.sub)*.vN`).
# Unversioned names are never accepted — there is no migration window to support.
_SERVICE_RE = re.compile(r"^(?P<base>([a-z0-9_]+\.)+[a-z0-9_]+)\.v(?P<v>\d+)$")
_CHANNEL_RE = _SERVICE_RE


class ContractValidationError(ValueError):
    """Custom exception for contract validation errors."""


def validate_service_name(name: str) -> None:
    """Validate service naming convention (versioned ``.vN`` required)."""
    if _SERVICE_RE.match(name):
        return
    raise ContractValidationError(
        "Service name must match 'domain.component(.sub)*.vN' (lowercase, digits)."
    )


def validate_channel_name(channel: str) -> None:
    """Validate event channel naming (versioned ``.vN`` required)."""
    if _CHANNEL_RE.match(channel):
        return
    raise ContractValidationError(
        "Channel must match 'domain.component(.sub)*.vN' (lowercase, digits)."
    )


def _list(value: list[str]) -> list[str]:
    return sorted(set(value))


def _json_key(value: Any) -> str:
    """``value`` as canonical JSON: equal JSON values (``1`` and ``1.0``),
    and only those (``true`` is not ``1``), share a key; lists and objects
    included, which are unhashable as Python values."""

    def canonical(item: Any) -> Any:
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, list | tuple):
            return [canonical(v) for v in item]
        if isinstance(item, dict):
            return {k: canonical(v) for k, v in item.items()}
        return item

    return json.dumps(canonical(value), sort_keys=True, separators=(",", ":"))


def _enum_changes(old: Any, new: Any) -> tuple[list[Any], list[Any]]:
    """The ``(added, removed)`` members between two ``enum`` lists, compared
    as JSON values (a member may be any JSON value), each in key order."""
    before = {_json_key(v): v for v in (old if isinstance(old, list) else [])}
    after = {_json_key(v): v for v in (new if isinstance(new, list) else [])}
    added = [after[k] for k in sorted(after.keys() - before.keys())]
    removed = [before[k] for k in sorted(before.keys() - after.keys())]
    return added, removed


def classify_schema_change(  # noqa: C901
    old: dict[str, Any],
    new: dict[str, Any],
    channel: str,
    version: int,
) -> _schema_registry.SchemaChangeReport:
    """Produce a SchemaChangeReport comparing two JSON Schema object roots.

    Object schemas are compared recursively; nested property names are
    reported as dotted paths (e.g. "payload.amount"). Beyond the modeled
    dimensions (properties/required/type/enum — enum values added OR removed
    are breaking: cross-service consumers cannot be upgraded atomically),
    tightening
    ``additionalProperties`` is flagged as breaking, array ``items`` object
    schemas are recursed into, so is each ``$defs`` entry both sides share
    (a nested DTO, reported as ``$defs.<Name>.<field>``; an enum's values),
    a field that loses its ``DEFAULTED`` mark is newly required for readers
    (round 8), and any change to other validation keywords
    (pattern, minimum, oneOf, $ref, ...) sets ``other_changes`` — the checker
    deliberately models a JSON Schema subset and refuses to silently bless
    what it does not understand.
    """

    added_required_acc: list[str] = []
    removed_required_acc: list[str] = []
    added_optional_acc: list[str] = []
    removed_properties_acc: list[str] = []
    type_changed: dict[str, tuple[str | None, str | None]] = {}
    added_enum_values: dict[str, list[Any]] = {}
    removed_enum_values: dict[str, list[Any]] = {}
    flags = {"breaking_extra": False, "other_changes": False}

    # Keywords fully handled by the dimensions above
    _MODELED = {"properties", "required", "type", "enum", "additionalProperties"}

    def _additional_props_tightened(
        old_node: dict[str, Any], new_node: dict[str, Any]
    ) -> bool:
        # true (default) -> false/schema is breaking: previously valid extra
        # fields no longer pass.
        old_ap = old_node.get("additionalProperties", True)
        new_ap = new_node.get("additionalProperties", True)
        return old_ap in (True, None) and new_ap not in (True, None)

    def _unmodeled_keywords_changed(
        old_node: dict[str, Any], new_node: dict[str, Any]
    ) -> bool:
        old_rest = {k: v for k, v in old_node.items() if k not in _MODELED}
        new_rest = {k: v for k, v in new_node.items() if k not in _MODELED}
        return old_rest != new_rest

    def _walk(old_node: dict[str, Any], new_node: dict[str, Any], prefix: str) -> None:  # noqa: C901
        old_props = cast(dict[str, Any], old_node.get("properties", {}))
        new_props = cast(dict[str, Any], new_node.get("properties", {}))
        old_required_set = set(cast(list[str], old_node.get("required", [])))
        new_required_set = set(cast(list[str], new_node.get("required", [])))
        old_prop_names = set(old_props.keys())
        new_prop_names = set(new_props.keys())

        if _additional_props_tightened(old_node, new_node):
            flags["breaking_extra"] = True
            flags["other_changes"] = True

        # A new field marked DEFAULTED (a literal or factory default) is
        # listed as required by the serialization schema (always emitted),
        # yet its reader fills it: an optional addition (spec §5.2).
        defaulted = {
            n
            for n in new_prop_names - old_prop_names
            if cast(dict[str, Any], new_props[n]).get(_schema_registry.DEFAULTED)
            is True
        }
        added_required_acc.extend(
            f"{prefix}{n}" for n in new_required_set - old_required_set - defaulted
        )
        # A default removed (round 8): required on both sides (always
        # emitted), yet a producer from before the field omits it and only a
        # defaulted reader fills it -- newly required for readers.
        added_required_acc.extend(
            f"{prefix}{n}"
            for n in old_required_set & new_required_set
            if cast(dict[str, Any], old_props.get(n, {})).get(
                _schema_registry.DEFAULTED
            )
            is True
            and cast(dict[str, Any], new_props.get(n, {})).get(
                _schema_registry.DEFAULTED
            )
            is not True
        )
        removed_required_acc.extend(
            f"{prefix}{n}" for n in old_required_set - new_required_set
        )
        added_optional_acc.extend(
            f"{prefix}{n}"
            for n in new_prop_names - old_prop_names
            if n not in new_required_set or n in defaulted
        )
        removed_properties_acc.extend(
            f"{prefix}{n}" for n in old_prop_names - new_prop_names
        )

        for name in old_prop_names & new_prop_names:
            path = f"{prefix}{name}"
            old_entry = cast(dict[str, Any], old_props.get(name, {}))
            new_entry = cast(dict[str, Any], new_props.get(name, {}))
            old_type = cast(str | None, old_entry.get("type"))
            new_type = cast(str | None, new_entry.get("type"))

            if old_type != new_type:
                type_changed[path] = (old_type, new_type)

            added, removed = _enum_changes(old_entry.get("enum"), new_entry.get("enum"))
            if added:
                added_enum_values[path] = added
            if removed:
                removed_enum_values[path] = removed

            # Recurse into nested object schemas (same declared type)
            if (
                old_type == "object"
                and new_type == "object"
                and (old_entry.get("properties") or new_entry.get("properties"))
            ):
                _walk(old_entry, new_entry, prefix=f"{path}.")
            elif old_type == "array" and new_type == "array":
                old_items = old_entry.get("items")
                new_items = new_entry.get("items")
                if isinstance(old_items, dict) and isinstance(new_items, dict):
                    _walk(old_items, new_items, prefix=f"{path}[].")
                elif old_items != new_items:
                    flags["other_changes"] = True
            elif _unmodeled_keywords_changed(old_entry, new_entry):
                # pattern/min/max/oneOf/$ref/... changed on a leaf property
                flags["other_changes"] = True

    _walk(old, new, prefix="")
    # Nested DTOs live under ``$defs`` (pydantic's ``$ref`` targets): each
    # definition both sides share follows the same rules (``$defs.<Name>.``).
    old_defs = cast(dict[str, Any], old.get("$defs") or {})
    new_defs = cast(dict[str, Any], new.get("$defs") or {})
    for def_name in sorted(old_defs.keys() & new_defs.keys()):
        old_def, new_def = old_defs[def_name], new_defs[def_name]
        if isinstance(old_def, dict) and isinstance(new_def, dict):
            path = f"$defs.{def_name}"
            if old_def.get("properties") or new_def.get("properties"):
                _walk(old_def, new_def, prefix=f"{path}.")
            elif "enum" in old_def or "enum" in new_def:
                added, removed = _enum_changes(old_def.get("enum"), new_def.get("enum"))
                if added:
                    added_enum_values[path] = added
                if removed:
                    removed_enum_values[path] = removed
            elif old_def != new_def:
                flags["other_changes"] = True
    added_required = _list(added_required_acc)
    removed_required = _list(removed_required_acc)
    added_optional = _list(added_optional_acc)
    removed_properties = _list(removed_properties_acc)

    breaking = bool(
        added_required
        or removed_required
        or removed_properties
        or type_changed
        or removed_enum_values
        # An added enum value is breaking too: a consumer deployed in ANOTHER
        # service (old schema / exhaustive match) rejects or mishandles it and
        # producer + consumers cannot be upgraded atomically. Ship it as vN+1
        # (dual-publish with publish_event_versions).
        or added_enum_values
        or flags["breaking_extra"]
    )

    return _schema_registry.SchemaChangeReport(
        channel=channel,
        version=version,
        added_required=added_required,
        removed_required=removed_required,
        added_optional=added_optional,
        removed_properties=removed_properties,
        type_changed=type_changed,
        added_enum_values=added_enum_values,
        removed_enum_values=removed_enum_values,
        breaking=breaking,
        other_changes=flags["other_changes"],
    )
