"""The default module set every composition root of this checkout composes.

Both roots, the lean ``sandloom.main`` and the standard
``sandloom_standard.main`` (ADR-0012 decision 3), take their default module
set from :func:`default_modules`, so a framework checkout composes the same
modules whichever root a process names. This module builds no app and reads
no environment: the standard root imports it without importing the lean root.
``SANDLOOM_MODULES`` still chooses an explicit subset.
"""

from sandloom.core.descriptors.module_names import installed_modules

#: Modules of a framework checkout that no installed distribution registers
#: (``sandloom add module`` in the framework tree appends here). Empty in a
#: release: installed modules are discovered.
LOCAL_MODULES: list[str] = []


def default_modules() -> list[str]:
    """Every installed module's entry-point name (sorted), then LOCAL_MODULES."""
    installed = sorted({module.name for module in installed_modules()})
    return [*installed, *(m for m in LOCAL_MODULES if m not in installed)]


__all__ = [
    "LOCAL_MODULES",
    "default_modules",
]
