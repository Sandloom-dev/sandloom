"""Declared interface focus and exact source landmarks, never an inferred call graph."""

from __future__ import annotations

import ast
from pathlib import Path
from urllib.parse import unquote

from sandloom.core.cli.inspection import files, queries
from sandloom.core.cli.inspection.model import Doc, InspectionError
from sandloom.core.cli.inspection.read import Reader


def _matches(reader: Reader, path: str, names: list[str], selected: Doc) -> bool:
    if not path.endswith(".py") or "tests" in Path(path).parts:
        return False
    try:
        tree = ast.parse(files.source(reader.root, path))
    except (UnicodeError, SyntaxError):
        return False
    except InspectionError as exc:
        if exc.kind != "file_too_large":
            raise
        return False
    if selected["id"].startswith("route:"):
        # Fallback for older indexes without endpoint locations; equal names only.
        handler = selected.get("handler", "").rsplit(".", 1)[-1]
        return any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == handler
            for n in ast.walk(tree)
        )
    constants = set(names)
    if selected["id"].startswith("saga:"):
        constants.add(selected["saga_type"].rsplit(".", 1)[-1])
    identifiers = {n.replace(".", "_").replace("-", "_").upper() for n in names}
    return any(
        (
            isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n.value in constants
        )
        or (isinstance(n, ast.Name) and n.id in identifiers)
        for n in ast.walk(tree)
    )


def scope(reader: Reader, module: Doc, selector: str, candidates: list[str]) -> Doc:
    selected = queries.item(reader, selector)
    if module["id"] not in queries.owners(reader, selected):
        raise InspectionError(
            "wrong_owner", "Focus must belong to or be used by the selected module"
        )
    interfaces = {selected["id"]}
    interfaces |= {
        e["source"]
        for e in reader.doc["edges"]
        if e["target"] == selected["id"]
        and e["kind"] in {"consumes_event", "projects_event"}
    }
    whole_module = selected["id"].startswith(("module:", "capability:"))
    for step in selected.get("steps", []):
        interfaces |= {
            "contract:" + step[key].rsplit(".", 1)[0]
            for key in ("call", "compensate")
            if step.get(key)
        }
    names = [
        unquote(i.split(":", 1)[1])
        for i in interfaces
        if i.startswith(("contract:", "event:"))
    ]
    wires = (
        module["wire_paths"]
        if whole_module
        else [
            p
            for p in module["wire_paths"]
            if any(
                name in p or name.replace(".", "_").replace("-", "_") in p
                for name in names
            )
        ]
    )
    landmarks = [selected.get("source")]
    for step in selected.get("steps", []):
        landmarks += [step.get("source"), step.get("compensation_source")]
    paths = {location["path"] for location in landmarks if location}
    exact_route = selected["id"].startswith("route:") and selected.get("source")
    paths |= {
        p
        for p in candidates
        if whole_module or (not exact_route and _matches(reader, p, names, selected))
    }
    return {
        "item": selected,
        "candidates": sorted(paths.intersection(candidates)),
        "wires": reader.wire_paths({**module, "wire_paths": wires}),
        "all_interfaces": whole_module,
        "channels": {
            i.removeprefix("event:") for i in interfaces if i.startswith("event:")
        },
        "limits": (
            "Declared interfaces and source landmarks only. Handler dependencies "
            "are not inferred. Open helpers/tests directly; normal quality gates apply."
        ),
    }
