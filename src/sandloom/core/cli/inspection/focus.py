"""Explicit interface focus, preserving whole wire modules and module ownership."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from sandloom.core.cli.inspection import files, queries
from sandloom.core.cli.inspection.model import Doc, InspectionError
from sandloom.core.cli.inspection.read import Reader


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
    whole_module = selected["id"].startswith(("route:", "module:", "capability:"))
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
    wires = [
        p
        for p in module["wire_paths"]
        if any(
            name in p or name.replace(".", "_").replace("-", "_") in p for name in names
        )
    ]
    # Route internals are opaque: retain every declared wire interface.
    if whole_module:
        wires = module["wire_paths"]
    wire_module = {**module, "wire_paths": wires}
    tokens = set(names) | {name.replace(".", "_").upper() for name in names}
    tokens |= {
        selected.get("handler", ""),
        selected.get("saga_type", ""),
        selected.get("name", ""),
    }
    tokens.discard("")
    paths = []
    for path in candidates:
        text = files.read(reader.root, path)
        if (
            selected["id"].startswith(("module:", "capability:"))
            or path == module["source"] + "/__init__.py"
            or Path(path).name == "schemas.py"
            or any(t in text for t in tokens)
        ):
            paths.append(path)
    for step in selected.get("steps", []):
        for key in ("source", "compensation_source"):
            location = step.get(key)
            if (
                location
                and location["path"] in candidates
                and location["path"] not in paths
            ):
                paths.append(location["path"])
    return {
        "item": selected,
        "candidates": paths,
        "wires": reader.wire_paths(wire_module),
        "all_interfaces": whole_module,
        "channels": {
            i.removeprefix("event:") for i in interfaces if i.startswith("event:")
        },
        "limits": (
            "Source matches are navigation hints, not a complete call "
            "graph. Read the owned files and their helpers/tests directly; "
            "normal quality gates still apply."
        ),
    }
