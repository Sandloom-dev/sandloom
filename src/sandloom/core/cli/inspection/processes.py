"""Trusted declaration collection; callbacks are inspected, never executed here."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sandloom.core.cli.inspection.model import Doc
from sandloom.core.descriptors.module_names import module_name
from sandloom.core.route_markers import route_module


def identity(kind: str, name: str) -> str:
    return f"{kind}:{quote(name, safe='._-')}"


def _location(callback: Any, root: Path) -> Doc | None:
    code = getattr(callback, "__code__", None)
    if code is None:
        return None
    path = Path(code.co_filename).resolve()
    if not path.is_relative_to(root):
        return None
    return {"path": path.relative_to(root).as_posix(), "line": code.co_firstlineno}


def route_sources(app: Any, root: Path) -> dict[tuple[str, str, str], Doc | None]:
    result = {}
    for route in getattr(app, "routes", ()):
        owner = route_module(route)
        if owner is None or not getattr(route, "endpoint", None):
            continue
        for method in getattr(route, "methods", ()):
            result[owner, method, route.path] = _location(
                inspect.unwrap(route.endpoint), root
            )
    return result


def collect(core: Any, root: Path, modules: list[Doc]) -> list[Doc]:
    result = []
    for definition, owner in getattr(core, "sagas", ()):
        item = definition.describe()
        item.update(
            id=identity("saga", f"{item['saga_type']}@{item['version']}"),
            owner=identity("module", module_name(owner)),
            evidence_kind="registered_saga_definition",
        )
        for step, declared in zip(item["steps"], definition.steps, strict=True):
            deadline = getattr(declared, "deadline", None)
            step.update(
                compensation_available=declared.compensate is not None,
                deadline_seconds=deadline.total_seconds() if deadline else None,
                refuse_on=sorted(getattr(declared, "refuse_on", ())),
                source=_location(getattr(declared, "apply", None), root),
                compensation_source=_location(declared.compensate, root),
                transaction=(
                    "saga unit; local savepoint"
                    if declared.local
                    else "provider command unit; separate commit"
                ),
            )
        result.append(item)
    for module in modules:
        result.extend(
            {
                "id": identity(
                    "route", f"{module['name']}:{route['method']} {route['path']}"
                ),
                "owner": module["id"],
                "name": f"{route['method']} {route['path']}",
                "policy": route["policy"],
                "evidence_kind": "resolved_route_policy",
                "handler": route["name"],
                "method": route["method"],
                "path": route["path"],
                "source": route.get("source"),
            }
            for route in module["routes"]
            if route["policy"]
        )
    return sorted(result, key=lambda p: p["id"])
