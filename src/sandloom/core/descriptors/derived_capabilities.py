"""The infrastructure capabilities a module's code shows it needs.

Read from the imported package's directories and the AST, without importing
any submodule:

* a ``<module>.tasks`` submodule (``tasks.py`` or a ``tasks/`` package) needs
  the ``taskqueue`` capability: the framework worker imports it and runs its
  tasks;
* a ``@periodic`` task in it needs the ``taskqueue`` feature ``periodic``:
  something must run the module's periodic jobs at their slots while the
  deployment runs;
* a task declared ``at_least_once=True`` needs the feature ``redelivery``:
  the driver redelivers it when its worker dies.

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
    "PERIODIC_DECORATOR",
    "PERIODIC_FEATURE",
    "REDELIVERY_FEATURE",
    "TASKQUEUE",
    "DerivedNeed",
    "declared_features",
    "defines_periodic",
    "derived_needs",
    "enqueues_on_request_path",
    "loaded_package_locations",
    "tasks_source",
]

#: The decorator that declares a periodic task in a module's ``tasks.py``.
PERIODIC_DECORATOR = "periodic"

#: The decorators that declare a task (``sandloom.sdk.tasks``).
_TASK_DECORATORS = frozenset({"task", PERIODIC_DECORATOR})

#: The capability a module with tasks needs.
TASKQUEUE = "taskqueue"

#: The ``taskqueue`` feature a module with a ``@periodic`` task needs: its
#: periodic jobs run at their slots in at least one process while the
#: deployment runs. Never granted for supporting ``send`` alone.
PERIODIC_FEATURE = "periodic"

#: The ``taskqueue`` feature a module with an ``at_least_once`` task needs.
REDELIVERY_FEATURE = "redelivery"


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


def _decorator_calls(path: Path) -> list[tuple[str, ast.expr | None]]:
    """``(decorator name, the call or None)`` of every decorator of a
    module-level function of the file, by the name's last segment (``task``,
    ``periodic``). An unreadable or unparsable file has none."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return []
    found: list[tuple[str, ast.expr | None]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            target = call.func if call is not None else decorator
            name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr
                if isinstance(target, ast.Attribute)
                else None
            )
            if name in _TASK_DECORATORS:
                found.append((name, call))
    return found


def defines_periodic(path: Path) -> bool:
    """Does the file declare a ``@periodic`` task at module level? (AST.)"""
    return any(name == PERIODIC_DECORATOR for name, _ in _decorator_calls(path))


def enqueues_on_request_path(module: str) -> bool:
    """Does the imported package ``module`` declare a task that is not
    ``@periodic``? Such a task is enqueued by a request (the beat enqueues only
    ``@periodic`` ones), so the module stays on the request path whatever
    features it requires: ``redelivery`` is background only when every
    ``at_least_once`` task is ``@periodic``. Imports nothing."""
    path = tasks_source(loaded_package_locations(module))
    return path is not None and any(
        name != PERIODIC_DECORATOR for name, _ in _decorator_calls(path)
    )


def declared_features(path: Path) -> frozenset[str]:
    """The ``taskqueue`` features the tasks of the file need: ``periodic`` for a
    ``@periodic`` task, ``redelivery`` for ``at_least_once=True`` on any."""
    features: set[str] = set()
    for name, call in _decorator_calls(path):
        if name == PERIODIC_DECORATOR:
            features.add(PERIODIC_FEATURE)
        if isinstance(call, ast.Call) and any(
            kw.arg == "at_least_once"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in call.keywords
        ):
            features.add(REDELIVERY_FEATURE)
    return frozenset(features)


def _shown(path: Path, module: str) -> str:
    """``<package dir>/tasks.py``: the path from the module's directory."""
    root = module.rsplit(".", 1)[-1]
    return (
        f"{root}/tasks.py" if path.name == "tasks.py" else f"{root}/tasks/__init__.py"
    )


def derived_needs(module: str) -> tuple[DerivedNeed, ...]:
    """What the imported package ``module``'s code needs from the
    infrastructure: ``taskqueue`` when it has tasks, with ``periodic`` for a
    ``@periodic`` task and ``redelivery`` for an ``at_least_once`` one. Imports
    nothing; a package that is not imported (or a plain module) needs nothing
    derivable."""
    path = tasks_source(loaded_package_locations(module))
    if path is None:
        return ()
    shown = _shown(path, module)
    features = declared_features(path)
    if features:
        what = " and ".join(
            f"{'@periodic' if f == PERIODIC_FEATURE else 'at_least_once'}"
            for f in sorted(features)
        )
        return (DerivedNeed(TASKQUEUE, features, f"{what} in {shown}"),)
    return (DerivedNeed(TASKQUEUE, frozenset(), shown),)
