"""Which modules declare periodic tasks, read without importing them.

A module's ``tasks.py`` that declares a ``@periodic`` task is scheduled by the
driver's host (:mod:`sandloom.core.worker`). Deploy tooling and the static
checks (SANDLOOM-T010) need the answer without importing the module or building
a Celery app. The reading of ``tasks.py`` is the descriptors' own
(:mod:`sandloom.core.descriptors.derived_capabilities`), which also derives a
loaded module's ``taskqueue`` requirement and its ``periodic`` feature.
"""

from __future__ import annotations

import importlib.util

from sandloom.core.descriptors.derived_capabilities import (
    defines_periodic,
    tasks_source,
)


def has_periodic_tasks(module: str) -> bool:
    """Does ``<module>/tasks.py`` declare a ``@periodic`` task? (AST; the
    module itself is not imported.)"""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return False
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    path = tasks_source(locations)
    return path is not None and defines_periodic(path)


__all__ = ["has_periodic_tasks"]
