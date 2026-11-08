"""Module-declared generated files: the kernel collects, the module renders.

A module declares a file on its registration (``define_module(generates=...)``:
``ModuleDescriptor.generated_files``, file name -> renderer). ``sandloom generate``
writes it into the app's shared package and ``generate --check`` / ``sandloom
test`` fail when it is stale (SANDLOOM-T016), like every generated file
(ADR-0012 decision 4, A-M2).

The kernel hands each renderer :class:`~sandloom.core.descriptors.GenerationInput`,
data of the composed app: its OpenAPI document, its routes and every loaded
module's ``admin.RESOURCES``, which the kernel imports as the loader (class (a)
row A7). The renderer never receives a module, so the declaring module imports
no other module (H-3). Nothing is collected when no module declares a file.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute

from sandloom.core.descriptors import GenerationInput, GenerationModule
from sandloom.core.descriptors.module_names import module_name

#: What a declaration or a renderer raises for a declaration to fix.
DECLARATION_ERRORS = (ValueError, KeyError, AttributeError, ImportError, TypeError)
#: Names the kernel's own generated files take in the shared package.
_RESERVED = frozenset({"__init__.py", "contracts", "events"})


@dataclass
class DeclaredFile:
    """One file a loaded module declares, rendered (or the problems it has)."""

    package: str
    name: str
    render: Callable[[GenerationInput], str]
    content: str = ""
    problems: list[str] = field(default_factory=list)


def collect(app: Any) -> GenerationInput:
    """The composed app as data: OpenAPI, routes, each loaded module's resources.

    Builds the app's OpenAPI; enters no lifespan and opens no connection. A
    partially failed composition is refused: the file would miss a module.
    """
    summary = app.state.module_summary
    if summary.failed:
        raise ValueError("cannot render generated files for a partially failed composition")
    modules = []
    for descriptor in summary.loaded:
        declaring = descriptor.name + ".admin"
        try:
            submodule = importlib.import_module(declaring)
        except ModuleNotFoundError as exc:
            if exc.name != declaring:
                raise  # An error INSIDE a declaration is never ignored.
            resources = None
        else:
            resources = submodule.RESOURCES
        modules.append(
            GenerationModule(descriptor.name, module_name(descriptor.name), resources)
        )
    routes = {
        (route.path, method): getattr(route.endpoint, "__module__", "") or ""
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods)
    }
    return GenerationInput(
        openapi=app.openapi(), routes=routes, modules=tuple(modules)
    )


def _problem(name: str) -> str | None:
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        return f"{name!r} is not a plain file name"
    if name in _RESERVED:
        return f"{name!r} is a name of the kernel's own generated files"
    return None


def declared(app: Any) -> list[DeclaredFile]:
    """Every file the loaded modules declare, in load order, rendered.

    A bad name, a name two modules declare and a renderer that refuses become
    the file's problems, never a crash."""
    files = [
        DeclaredFile(descriptor.name, name, render)
        for descriptor in app.state.module_summary.loaded
        for name, render in sorted(getattr(descriptor, "generated_files", {}).items())
    ]
    if not files:
        return []
    owners: dict[str, list[str]] = {}
    for item in files:
        owners.setdefault(item.name, []).append(item.package)
        problem = _problem(item.name)
        if problem:
            item.problems.append(f"{item.package}: {problem}")
    for item in files:
        if len(owners[item.name]) > 1:
            item.problems.append(
                f"{item.name} is declared by " + " and ".join(owners[item.name])
            )
    try:
        source = collect(app)
    except DECLARATION_ERRORS as exc:
        for item in files:
            item.problems.append(f"module declarations: {exc}")
        return files
    for item in files:
        if item.problems:
            continue
        try:
            content = item.render(source)
        except DECLARATION_ERRORS as exc:
            item.problems.append(f"{item.package}: {exc}")
            continue
        if not isinstance(content, str):
            item.problems.append(f"{item.package}: the renderer returned no text")
            continue
        item.content = content
    return files


def planned(app: Any, shared_dir: Path) -> list[tuple[Path, str, list[str]]]:
    """``(path, content, problems)`` of each declared file in ``shared_dir``."""
    return [(shared_dir / f.name, f.content, f.problems) for f in declared(app)]
