"""The infrastructure capabilities a module's code shows it needs.

Read without importing the module's submodules (``find_spec`` and the AST):

* a ``<module>.tasks`` submodule (``tasks.py`` or a ``tasks/`` package) needs
  the ``taskqueue`` capability: the framework worker imports it and runs its
  tasks;
* a beat hook (:data:`PERIODIC_HOOK`) in it needs the ``taskqueue`` feature
  ``periodic``: something must run the module's periodic jobs at their
  interval while the deployment runs.

:meth:`sandloom.core.descriptors.ModuleDescriptor.effective_capabilities`
merges these into the declared requirements, so the load preflight checks
them for every module, an L1 ``define_module`` or a hand-written L2
``register`` alike; ``sandloom check`` requires the declaration
(SANDLOOM-C033), as it does for database features (SANDLOOM-C030). The
derivation is not complete, and is not meant to be: a module that reaches a
capability another way declares it.
"""

from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PERIODIC_FEATURE",
    "PERIODIC_HOOK",
    "TASKQUEUE",
    "DerivedNeed",
    "defines_periodic_hook",
    "derived_needs",
    "tasks_source",
]

#: The hook a module's ``tasks.py`` defines to schedule periodic tasks.
PERIODIC_HOOK = "setup_periodic_tasks"

#: The capability a module with tasks needs.
TASKQUEUE = "taskqueue"

#: The ``taskqueue`` feature a module with a beat hook needs: its periodic
#: jobs run at their interval in at least one process while the deployment
#: runs. Never granted for supporting ``enqueue`` alone.
PERIODIC_FEATURE = "periodic"


@dataclass(frozen=True)
class DerivedNeed:
    """A capability (and its features) a module's code needs, and where that
    was read (``"sandloom_billing/tasks.py"``)."""

    capability: str
    features: frozenset[str]
    source: str


def tasks_source(module: str) -> Path | None:
    """The file of ``<module>.tasks`` (``tasks.py``, or ``tasks/__init__.py``),
    found without importing it; ``None`` when the module has none or is not
    an importable package."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    for location in list(spec.submodule_search_locations or []):
        for candidate in (
            Path(location) / "tasks.py",
            Path(location) / "tasks" / "__init__.py",
        ):
            if candidate.is_file():
                return candidate
    return None


def defines_periodic_hook(path: Path) -> bool:
    """Does the file define :data:`PERIODIC_HOOK` at module level? (AST; an
    unreadable or unparsable file defines none.)"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return False
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == PERIODIC_HOOK
        for node in tree.body
    )


def _shown(path: Path, module: str) -> str:
    """``<package dir>/tasks.py``: the path from the module's directory."""
    root = module.rsplit(".", 1)[-1]
    return (
        f"{root}/tasks.py" if path.name == "tasks.py" else f"{root}/tasks/__init__.py"
    )


def derived_needs(module: str) -> tuple[DerivedNeed, ...]:
    """What ``module``'s code needs from the infrastructure: ``taskqueue``
    when it has tasks, with ``periodic`` when it has a beat hook. Imports
    nothing of the module beyond what ``find_spec`` needs (its parents)."""
    path = tasks_source(module)
    if path is None:
        return ()
    shown = _shown(path, module)
    if defines_periodic_hook(path):
        return (
            DerivedNeed(
                TASKQUEUE,
                frozenset({PERIODIC_FEATURE}),
                f"{PERIODIC_HOOK} in {shown}",
            ),
        )
    return (DerivedNeed(TASKQUEUE, frozenset(), shown),)
