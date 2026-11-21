"Versioned data, diagnostics and deterministic bytes shared by both tools."

from __future__ import annotations

import hashlib
import json
from typing import Any

Doc = dict[str, Any]
INDEX = ".sandloom/inspection.json"
RECEIPT = ".sandloom/cache/inspection-receipt.json"
LIMIT = 131072


def encode(value: Any) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


#: First line of the tracked index: one JSON value per line, so that two
#: branches that each add a module add different lines and git merges them
#: (``.gitattributes``: ``merge=union`` for the lines that land in the same gap).
LINES = '["@format","sandloom.inspection.lines",1]'


def _line(value: Any) -> str:
    return encode(value)[:-1]


def _ordered(items: list[Any], section: str) -> list[Any]:
    """Items in their one order: by ``id`` when they have one, else by text,
    each record once. Two different items with one id are two versions of one
    record (a merge that kept both), never a document."""
    items = list({_line(item): item for item in items}.values())
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            if item["id"] in seen:
                raise InspectionError(
                    "invalid_artifact",
                    f"Conflicting versions of {section} in the inspection index; "
                    "run sandloom generate",
                    actions=regenerate(),
                )
            seen.add(item["id"])
    return sorted(
        items,
        key=lambda item: (
            item["id"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            else "",
            _line(item),
        ),
    )


def _module_ids(doc: Doc) -> list[str]:
    modules = doc.get("modules")
    if not isinstance(modules, list):
        return []
    return sorted(m["id"] for m in modules if isinstance(m, dict) and "id" in m)


def encode_index(doc: Doc) -> str:
    """The tracked index text: the document as lines, one record each.

    Every list item and every entry of a map is its own line, tagged with its
    section, in one order; a section is declared by a header line. A record
    another module's records never touch is a line no other module's change
    touches. ``scope.module_ids`` is derived from ``modules`` and not stored."""
    doc = dict(doc)
    scope = doc.get("scope")
    if isinstance(scope, dict) and scope.get("module_ids") == _module_ids(doc):
        doc["scope"] = {k: v for k, v in scope.items() if k != "module_ids"}
    lines = [LINES]
    for key in sorted(doc):
        value = doc[key]
        if isinstance(value, list):
            lines.append(_line(["@list", key]))
            lines += [_line([key, item]) for item in _ordered(value, key)]
        elif isinstance(value, dict) and key != "layout" and key != "scope":
            lines.append(_line(["@map", key]))
            lines += [_line([key, name, value[name]]) for name in sorted(value)]
        else:
            lines.append(_line(["@val", key, value]))
    return "\n".join(lines) + "\n"


def _invalid() -> InspectionError:
    return InspectionError(
        "invalid_artifact",
        "The inspection index is not a valid document; run sandloom generate",
        actions=regenerate(),
    )


_TAGS = ("@list", "@map", "@val")


def _rows(text: str) -> list[list[Any]]:
    rows = []
    seen: set[str] = set()
    for line in text.splitlines()[1:]:
        if line in seen:  # `union` repeats the lines both sides share
            continue
        seen.add(line)
        try:
            row = json.loads(line)
        except ValueError:
            raise _invalid() from None
        if not isinstance(row, list) or not row or not isinstance(row[0], str):
            raise _invalid()
        rows.append(row)
    return rows


def _sections(rows: list[list[Any]]) -> tuple[Doc, dict[str, str]]:
    """The declared sections, empty, and the tag of each."""
    doc: Doc = {}
    kinds: dict[str, str] = {}
    for row in rows:
        if row[0] not in _TAGS:
            continue
        if len(row) != (3 if row[0] == "@val" else 2) or not isinstance(row[1], str):
            raise _invalid()
        if row[1] in doc:
            raise _invalid()
        kinds[row[1]] = row[0]
        doc[row[1]] = row[2] if row[0] == "@val" else [] if row[0] == "@list" else {}
    return doc, kinds


def _fill(doc: Doc, kinds: dict[str, str], row: list[Any]) -> None:
    section = doc.get(row[0])
    kind = kinds.get(row[0])
    if kind == "@list" and len(row) == 2:
        section.append(row[1])
    elif (
        kind == "@map"
        and len(row) == 3
        and isinstance(row[1], str)
        and row[1] not in section
    ):
        section[row[1]] = row[2]
    else:
        raise _invalid()


def decode_index(text: str) -> Doc:
    """The document ``encode_index`` wrote, whatever the order of its lines."""
    rows = _rows(text)
    doc, kinds = _sections(rows)
    for row in rows:
        if row[0] not in _TAGS:
            _fill(doc, kinds, row)
    for key, kind in kinds.items():
        if kind == "@list":
            doc[key] = _ordered(doc[key], key)
        elif kind == "@map":
            doc[key] = {name: doc[key][name] for name in sorted(doc[key])}
    scope = doc.get("scope")
    if isinstance(scope, dict) and "module_ids" not in scope:
        doc["scope"] = {**scope, "module_ids": _module_ids(doc)}
    return doc


def digest(value: Any) -> str:
    return sha(encode(value).encode())


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class InspectionError(ValueError):
    "A safe diagnostic: never put rejected source contents in the message."

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        code: int = 1,
        actions: list[Doc] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.exit_code = code
        self.actions = actions or []

    def diagnostic(self) -> Doc:
        return {
            "kind": self.kind,
            "severity": "error",
            "code": "SANDLOOM-T015",
            "message": str(self),
            "next_actions": self.actions,
        }


def regenerate() -> list[Doc]:
    return [
        {
            "action": "generate",
            "argv": ["sandloom", "generate"],
            "side_effects": ["writes_generated_files", "loads_application"],
        }
    ]


def response(
    command: str,
    kind: str,
    data: Any,
    *,
    selection: Doc | None = None,
    freshness: Doc | None = None,
    diagnostics: list[Doc] | None = None,
) -> Doc:
    return {
        "schema": "sandloom.tool_response",
        "schema_version": 1,
        "command": command,
        "ok": kind != "error",
        "kind": kind,
        "selection": selection or {},
        "freshness": freshness,
        "data": data,
        "diagnostics": diagnostics or [],
    }
