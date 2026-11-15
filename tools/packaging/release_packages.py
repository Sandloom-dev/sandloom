"""The one list of workspace distributions, read by every packaging tool.

``packages.json`` (next to this file) names each ``packages/<name>`` project and
what differs between kinds of package (entry points, the standard set, a
console). ``sync_release``, ``pypi_release`` and ``check_package_distributions``
take names, order and behaviour from here and never compare a package name, so
a new distribution is one entry in the file and its own ``packages/<name>/``.

Standard library only: ``pypi_release.py`` runs from the reviewed artifact
directory, which carries this file and ``packages.json`` beside it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LIST = Path(__file__).resolve().with_name("packages.json")
KINDS = ("kernel", "root", "module", "driver")


@dataclass(frozen=True)
class Package:
    name: str
    kind: str
    #: ``sandloom.modules`` entry-point name (kind ``module``).
    module: str | None = None
    #: Installed by ``sandloom[standard]`` (the standard composition).
    standard: bool = False
    #: ``{entry-point name: {"target": "pkg:Class", "provider": "<driverkit name>"}}``.
    drivers: dict[str, dict[str, str]] | None = None
    #: A web console served from the import package: ``extra``, ``route``, ``with``.
    console: dict[str, Any] | None = None

    @property
    def import_name(self) -> str:
        return self.name.replace("-", "_")

    @property
    def suffix(self) -> str:
        """The name after ``sandloom-`` (empty for the kernel)."""
        return self.name.removeprefix("sandloom").removeprefix("-")


def _package(raw: dict[str, Any]) -> Package:
    known = set(Package.__dataclass_fields__)
    if set(raw) - known:
        raise ValueError(
            f"{raw.get('name')}: unknown fields {sorted(set(raw) - known)}"
        )
    package = Package(**raw)
    if package.kind not in KINDS:
        raise ValueError(f"{package.name}: kind must be one of {KINDS}")
    if (package.kind == "module") != bool(package.module):
        raise ValueError(f"{package.name}: `module` is for kind module, and required")
    if (package.kind == "driver") != bool(package.drivers):
        raise ValueError(f"{package.name}: `drivers` is for kind driver, and required")
    if package.kind != "kernel" and not package.name.startswith("sandloom-"):
        raise ValueError(f"{package.name}: a distribution is named sandloom-<name>")
    return package


def load(path: Path = LIST) -> tuple[Package, ...]:
    raw = json.loads(path.read_text())["packages"]
    packages = tuple(_package(item) for item in raw)
    names = [p.name for p in packages]
    if len(set(names)) != len(names):
        raise ValueError("packages.json: duplicate package name")
    if [p.kind for p in packages].count("kernel") != 1:
        raise ValueError("packages.json: exactly one kernel package")
    for package in packages:
        for other in (package.console or {}).get("with", []):
            if other not in names:
                raise ValueError(f"{package.name}: console needs unknown {other}")
    return packages


PACKAGES = load()
NAMES = tuple(p.name for p in PACKAGES)


def by_name(name: str) -> Package:
    return next(p for p in PACKAGES if p.name == name)


def kernel() -> Package:
    return next(p for p in PACKAGES if p.kind == "kernel")


def of_kind(kind: str) -> tuple[Package, ...]:
    return tuple(p for p in PACKAGES if p.kind == kind)


def standard_set() -> tuple[Package, ...]:
    return tuple(p for p in PACKAGES if p.standard)


def suffix_pattern(*, extra: tuple[str, ...] = ()) -> str:
    """Regex for ``sandloom`` plus an optional ``-<package>`` suffix of the list."""
    suffixes = sorted(
        (re.escape(s) for s in (*(p.suffix for p in PACKAGES if p.suffix), *extra)),
        key=len,
        reverse=True,
    )
    return r"sandloom(?:-(?:" + "|".join(suffixes) + r"))?"
