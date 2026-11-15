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
    #: A ``<name>-v<version>`` tag push may release it: true only where the
    #: ``pypi`` environment's deployment-tag rules allow that tag.
    tag_release: bool = False

    @property
    def import_name(self) -> str:
        return self.name.replace("-", "_")

    @property
    def suffix(self) -> str:
        """The name after ``sandloom-`` (empty for the kernel)."""
        return self.name.removeprefix("sandloom").removeprefix("-")


#: The list feeds shell commands, ``pip`` requirement strings and a ``python -c``
#: program, so every free string in it has a closed shape; nothing else loads.
NAME = re.compile(r"sandloom(?:-[a-z0-9]+)*")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ENTRY_POINT = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*")
TARGET = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*"
)
EXTRA = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ROUTE = re.compile(r"/[A-Za-z0-9_\-/{}:.]*")
TOP_LEVEL = {"_comment", "packages"}
REQUIRED = {"name", "kind"}


def _shape(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _drivers(name: str, drivers: Any) -> None:
    if not isinstance(drivers, dict):
        raise ValueError(f"{name}: `drivers` is an object of entry points")
    for entry, spec in drivers.items():
        if not _shape(entry, ENTRY_POINT):
            raise ValueError(f"{name}: bad driver entry-point name {entry!r}")
        if not isinstance(spec, dict) or set(spec) != {"target", "provider"}:
            raise ValueError(f"{name}.{entry}: exactly `target` and `provider`")
        if not _shape(spec["target"], TARGET):
            raise ValueError(f"{name}.{entry}: target is `module.path:Name`")
        if not _shape(spec["provider"], IDENTIFIER):
            raise ValueError(f"{name}.{entry}: provider is an identifier")


def _console(name: str, console: Any) -> None:
    if not isinstance(console, dict) or set(console) != {"extra", "route", "with"}:
        raise ValueError(f"{name}: console is exactly `extra`, `route`, `with`")
    if not _shape(console["extra"], EXTRA):
        raise ValueError(f"{name}: console extra is a lowercase extra name")
    if not _shape(console["route"], ROUTE):
        raise ValueError(f"{name}: console route is a URL path")
    with_ = console["with"]
    if not isinstance(with_, list) or not all(_shape(n, NAME) for n in with_):
        raise ValueError(f"{name}: console `with` is a list of package names")


def _package(raw: Any) -> Package:
    if not isinstance(raw, dict):
        raise ValueError("packages.json: each package is an object")
    known = set(Package.__dataclass_fields__)
    if set(raw) - known:
        raise ValueError(
            f"{raw.get('name')}: unknown fields {sorted(set(raw) - known)}"
        )
    if REQUIRED - set(raw):
        raise ValueError(f"{raw.get('name')}: needs {sorted(REQUIRED - set(raw))}")
    name = raw["name"]
    if not _shape(name, NAME):
        raise ValueError(f"{name!r}: a distribution is named sandloom[-<name>]")
    if raw["kind"] not in KINDS:
        raise ValueError(f"{name}: kind must be one of {KINDS}")
    if (raw["kind"] == "kernel") != (name == "sandloom"):
        raise ValueError(f"{name}: only the kernel is named exactly `sandloom`")
    for flag in ("standard", "tag_release"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise ValueError(f"{name}: `{flag}` is true or false")
    if (raw["kind"] == "module") != ("module" in raw):
        raise ValueError(f"{name}: `module` is for kind module, and required")
    if "module" in raw and not _shape(raw["module"], IDENTIFIER):
        raise ValueError(f"{name}: `module` is a Python identifier")
    if (raw["kind"] == "driver") != bool(raw.get("drivers")):
        raise ValueError(f"{name}: `drivers` is for kind driver, and required")
    if "drivers" in raw:
        _drivers(name, raw["drivers"])
    if "console" in raw:
        _console(name, raw["console"])
    return Package(**raw)


def load(path: Path = LIST) -> tuple[Package, ...]:
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or set(document) != TOP_LEVEL:
        raise ValueError(f"packages.json: top level is exactly {sorted(TOP_LEVEL)}")
    if not isinstance(document["packages"], list):
        raise ValueError("packages.json: `packages` is a list")
    packages = tuple(_package(item) for item in document["packages"])
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
