"""The infrastructure capabilities a module's code shows it needs.

Read from the imported package's directories and the AST, without importing
any submodule:

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
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PERIODIC_FEATURE",
    "PERIODIC_HOOK",
    "TASKQUEUE",
    "DerivedNeed",
    "defines_periodic_hook",
    "derived_needs",
    "loaded_package_locations",
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


def loaded_package_locations(module: str) -> list[str]:
    """The directories of an already imported package ``module`` (its
    ``__path__``); empty when it is not imported or is a plain module. Imports
    nothing: a descriptor's module is imported before it exists."""
    package = sys.modules.get(module)
    return [str(p) for p in getattr(package, "__path__", None) or []]


def tasks_source(locations: Iterable[str]) -> Path | None:
    """The file of a package's ``tasks`` submodule (``tasks.py``, or
    ``tasks/__init__.py``) in its directories ``locations``, found without
    importing it; ``None`` when it has none."""
    for location in locations:
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
    """What the imported package ``module``'s code needs from the
    infrastructure: ``taskqueue`` when it has tasks, with ``periodic`` when
    it has a beat hook. Imports nothing; a package that is not imported (or
    a plain module) needs nothing derivable."""
    path = tasks_source(loaded_package_locations(module))
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
