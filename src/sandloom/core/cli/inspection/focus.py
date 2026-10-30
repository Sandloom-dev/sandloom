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
    if selected["id"].startswith(("route:", "module:")):
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
            selected["id"].startswith("module:")
            or Path(path).name in {"__init__.py", "schemas.py"}
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
        "all_interfaces": selected["id"].startswith(("route:", "module:")),
        "channels": {
            i.removeprefix("event:") for i in interfaces if i.startswith("event:")
        },
        "limits": (
            "Source matches are navigation hints, not a complete call "
            "graph. Add module-local helpers/tests explicitly with --file; "
            "normal quality gates still apply."
        ),
    }
