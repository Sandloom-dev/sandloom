"""Which modules schedule periodic (beat) tasks, read without importing them.

A module's ``tasks.py`` that defines :data:`PERIODIC_HOOK` registers beat
schedules; the worker connects the hook (:mod:`sandloom.core.worker`). Deploy
tooling and the static checks (SANDLOOM-T010) need the answer without importing
the module or building a Celery app. The reading of ``tasks.py`` is the
descriptors' own (:mod:`sandloom.core.descriptors.derived_capabilities`), which
also derives a loaded module's ``taskqueue`` requirement and its ``periodic``
feature.
"""

from __future__ import annotations

import importlib.util

from sandloom.core.descriptors.derived_capabilities import (
    PERIODIC_HOOK,
    defines_periodic_hook,
    tasks_source,
)


def has_periodic_tasks(module: str) -> bool:
    """Does ``<module>/tasks.py`` define the beat hook? (AST; the module
    itself is not imported.)"""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return False
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    path = tasks_source(locations)
    return path is not None and defines_periodic_hook(path)


__all__ = ["PERIODIC_HOOK", "has_periodic_tasks"]
