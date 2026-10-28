"""Which modules schedule periodic (beat) tasks, read without importing them.

A module's ``tasks.py`` that defines :data:`PERIODIC_HOOK` registers beat
schedules; the worker connects the hook (:mod:`sandloom.core.worker`). Deploy
tooling and the static checks (SANDLOOM-T010) need the answer without importing
the module or building a Celery app.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

#: The hook a module's ``tasks.py`` defines to schedule periodic tasks.
PERIODIC_HOOK = "setup_periodic_tasks"


def has_periodic_tasks(module: str) -> bool:
    """Does ``<module>/tasks.py`` define the beat hook? (AST; the module
    itself is not imported.)"""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return False
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    for location in locations:
        tasks = Path(location) / "tasks.py"
        if tasks.is_file():
            tree = ast.parse(tasks.read_text(encoding="utf-8"))
            return any(
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == PERIODIC_HOOK
                for node in tree.body
            )
    return False


__all__ = ["PERIODIC_HOOK", "has_periodic_tasks"]
