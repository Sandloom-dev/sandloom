"""Ownership of declared objects (spec §5.7).

Classes and functions are owned by the package their ``__module__`` lies in.
Instances (``module_router()`` routers, ``Contract(...)`` references,
``Saga`` and ``Projection`` definitions) record their owner at construction,
taken from the constructing frame's module, because an instance's
``__module__`` is its class's module.
"""

from __future__ import annotations

import functools
import sys
from importlib import metadata

#: The attribute an L1 instance records its owning module in.
OWNER_ATTR = "sandloom_owner"

#: The conventional submodules discovery imports, in order (spec §5.7).
CONVENTIONAL: tuple[str, ...] = (
    "models",
    "api",
    "events",
    "consumers",
    "contracts",
    "sagas",
    "projections",
    "tasks",
)


def caller_module(depth: int) -> str:
    """The ``__name__`` of the module ``depth`` frames above the caller."""
    frame = sys._getframe(depth + 1)
    # ``Saga[Input](...)`` constructs through ``typing``'s generic alias,
    # which adds frames of its own: the owner is the first frame outside it.
    while frame.f_back is not None and frame.f_globals.get("__name__") == "typing":
        frame = frame.f_back
    name = frame.f_globals.get("__name__")
    return name if isinstance(name, str) else "__main__"


def within(module: str, package: str) -> bool:
    """``module`` is ``package`` or lies inside it."""
    return module == package or module.startswith(package + ".")


def module_package(module: str) -> str:
    """The business-module package a defining module belongs to, by layout.

    The package that holds the conventional submodule ``module`` lives in
    (``app.modules.notes.api.routes`` -> ``app.modules.notes``); for a module
    outside the conventional layout, its parent package. Discovery replaces
    this guess with the package it walks (the authoritative owner).

    A conventional NAME is not enough: a business module may itself be named
    like a submodule (``app.modules.tasks``). A segment that names a loaded
    package defining ``register`` is that business module, not a submodule.
    """
    parts = module.split(".")
    for index in range(1, len(parts)):
        if parts[index] not in CONVENTIONAL:
            continue
        named = sys.modules.get(".".join(parts[: index + 1]))
        if named is not None and _is_business_package(named):
            continue  # ``tasks`` here is a business module of that name
        return ".".join(parts[:index])
    itself = sys.modules.get(module)
    if itself is not None and _is_business_package(itself):
        return module  # the business package itself (``app.modules.tasks``)
    return module.rsplit(".", 1)[0] if "." in module else module


def _is_business_package(module: object) -> bool:
    """A loaded package that defines ``register`` (a business module)."""
    return hasattr(module, "__path__") and hasattr(module, "register")


@functools.cache
def _entry_point_names() -> dict[str, str]:
    """``{import path: module name}`` of the installed ``sandloom.modules``
    entry points (the kernel's rule, ``sandloom.core.descriptors.module_names``,
    read here with the standard library: L1 imports no kernel module)."""
    return {
        str(ep.value).split(":", 1)[0].strip(): ep.name
        for ep in metadata.entry_points().select(group="sandloom.modules")
    }


def short_name(package: str) -> str:
    """The module's name: its ``sandloom.modules`` entry-point name, else the
    last segment of its package (``app.modules.notes`` -> ``notes``)."""
    return _entry_point_names().get(package) or package.rsplit(".", 1)[-1]


__all__ = [
    "CONVENTIONAL",
    "OWNER_ATTR",
    "caller_module",
    "module_package",
    "short_name",
    "within",
]
