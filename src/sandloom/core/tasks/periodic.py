"""Which modules schedule periodic (beat) tasks, read without importing them.

A module's ``tasks.py`` that defines :data:`PERIODIC_HOOK` registers beat
schedules; the worker connects the hook (:mod:`sandloom.core.worker`). Deploy
tooling and the static checks (SANDLOOM-T010) need the answer without importing
the module or building a Celery app. The reading is the descriptors' own
(:mod:`sandloom.core.descriptors.derived_capabilities`), which also derives the
module's ``taskqueue`` requirement and its ``periodic`` feature.
"""

from __future__ import annotations

from sandloom.core.descriptors.derived_capabilities import (
    PERIODIC_HOOK,
    defines_periodic_hook,
    tasks_source,
)


def has_periodic_tasks(module: str) -> bool:
    """Does ``<module>/tasks.py`` define the beat hook? (AST; the module
    itself is not imported.)"""
    path = tasks_source(module)
    return path is not None and defines_periodic_hook(path)


__all__ = ["PERIODIC_HOOK", "has_periodic_tasks"]
