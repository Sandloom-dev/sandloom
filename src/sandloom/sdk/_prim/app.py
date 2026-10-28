"""The application's module list, derived (spec §5.7, ledger G-MODULE-19).

An application loads every subpackage of ``<app>.modules`` that defines
``register``, read statically (nothing is imported to find them), so adding a
module edits no list. ``SANDLOOM_MODULES`` still selects a subset::

    app = create_app(default_modules=app_modules("helpdesk.modules"))
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


def _defines_register(init: Path) -> bool:
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "register"
        ):
            return True
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == "register" for t in targets):
            return True
    return False


def app_modules(package: str) -> list[str]:
    """The dotted names of ``package``'s module packages, sorted by name.

    A subpackage counts when its ``__init__.py`` defines ``register`` (a
    ``define_module(...)`` assignment or a ``def register``); packages whose
    name starts with ``_`` are skipped.
    """
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise ModuleNotFoundError(f"{package!r} is not an importable package")
    found: set[str] = set()
    for location in spec.submodule_search_locations:
        for init in sorted(Path(location).glob("*/__init__.py")):
            name = init.parent.name
            if name.startswith(("_", ".")) or not name.isidentifier():
                continue
            if _defines_register(init):
                found.add(f"{package}.{name}")
    return sorted(found)


__all__ = ["app_modules"]
