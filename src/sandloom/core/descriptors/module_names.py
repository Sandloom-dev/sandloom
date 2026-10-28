"""Module names: entry-point discovery and the ambiguity rule (SANDLOOM-B020).

A business module has an **import path** (``sandloom_identity``,
``app.modules.notes``) and a **module name** (``identity``, ``notes``). The
name keys its tables (``<name>_*``), its migration chain
(``alembic_version_<name>``), its event channels and contract names
(``<name>.*``). An installed module package registers itself in the
``sandloom.modules`` entry-point group (``identity = "sandloom_identity"``);
its name is the entry point's. Any other module's name is the last segment of
its import path.

Configuration (``SANDLOOM_MODULES``, ``MODULE_REQUIRED``, deploy profiles)
may name a module by entry-point name or by import path:
:func:`resolve_module_names` turns both into import paths and refuses an
ambiguous name before anything loads.

Standard library only: every layer (the loader, the CLI, ``sandloom check``)
reads the same rule.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import metadata
from typing import Any

#: The entry-point group a module distribution registers its module in.
ENTRY_POINT_GROUP = "sandloom.modules"

__all__ = [
    "ENTRY_POINT_GROUP",
    "AmbiguousModuleError",
    "InstalledModule",
    "installed_modules",
    "module_name",
    "refresh_installed_modules",
    "resolve_module_names",
]


@dataclass(frozen=True, order=True)
class InstalledModule:
    """One ``sandloom.modules`` entry point: module name, import path, and
    the distribution that provides it."""

    name: str
    path: str
    distribution: str


class AmbiguousModuleError(RuntimeError):
    """SANDLOOM-B020: one module name has two providers."""

    code = "SANDLOOM-B020"


def _entry_points() -> tuple[Any, ...]:
    return tuple(metadata.entry_points().select(group=ENTRY_POINT_GROUP))


_lock = threading.Lock()
_cache: tuple[InstalledModule, ...] | None = None


def installed_modules() -> tuple[InstalledModule, ...]:
    """Every installed ``sandloom.modules`` entry point (cached; sorted)."""
    global _cache
    with _lock:
        if _cache is None:
            found: set[InstalledModule] = set()
            for ep in _entry_points():
                value = str(getattr(ep, "value", "") or "").split(":", 1)[0].strip()
                name = str(getattr(ep, "name", "") or "").strip()
                if not value or not name:
                    continue
                dist = getattr(getattr(ep, "dist", None), "name", None) or "?"
                found.add(InstalledModule(name, value, str(dist)))
            _cache = tuple(sorted(found))
        return _cache


def refresh_installed_modules() -> None:
    """Forget the cached entry points (after an install, in tests)."""
    global _cache
    with _lock:
        _cache = None


def _last_segment(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def module_name(path: str) -> str:
    """The module name of import path ``path``.

    The ``sandloom.modules`` entry-point name whose value is ``path``, else
    the last dotted segment (``app.modules.notes`` -> ``notes``).
    """
    for module in installed_modules():
        if module.path == path:
            return module.name
    return _last_segment(path)


def _providers(name: str) -> list[InstalledModule]:
    seen: dict[str, InstalledModule] = {}
    for module in installed_modules():
        if module.name == name:
            seen.setdefault(module.path, module)
    return list(seen.values())


def _label(module: InstalledModule) -> str:
    return f"{module.distribution} ({module.path})"


def _resolve_one(entry: str) -> str:
    providers = _providers(entry)
    if providers:
        if len(providers) > 1:
            _refuse_packages(entry, providers)
        return providers[0].path
    return entry


def _refuse_packages(name: str, providers: Iterable[InstalledModule]) -> None:
    labels = " and ".join(_label(p) for p in sorted(providers))
    raise AmbiguousModuleError(
        f"SANDLOOM-B020: module name {name!r} is provided by {labels}: "
        "uninstall one of the packages (`sandloom explain SANDLOOM-B020`)"
    )


def resolve_module_names(entries: Sequence[str]) -> list[str]:
    """Import paths of the configured ``entries`` (names or import paths).

    Refuses (:class:`AmbiguousModuleError`) a requested module whose name has
    two providers: two installed packages, or an installed package and an
    app-local module (a path no entry point names). Unrequested duplicates are
    not this application's concern and pass.
    """
    resolved = [_resolve_one(entry) for entry in entries]
    installed = installed_modules()
    package_paths = {m.path for m in installed}
    for path in resolved:
        if path in package_paths:
            # Requested by path: the package's name must not have a second
            # provider either.
            name = next(m.name for m in installed if m.path == path)
            providers = _providers(name)
            if len(providers) > 1:
                _refuse_packages(name, providers)
            continue
        providers = _providers(_last_segment(path))
        if providers:
            raise AmbiguousModuleError(
                f"SANDLOOM-B020: module name {_last_segment(path)!r} is provided "
                f"by the installed package {_label(providers[0])} and by the "
                f"local module {path!r}: uninstall the package or remove the "
                "local module (rename it) (`sandloom explain SANDLOOM-B020`)"
            )
    return resolved
