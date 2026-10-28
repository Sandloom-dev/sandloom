"""Import layering across the built distributions (no install needed).

Every ``.py`` file a wheel ships may import, at module level or inside a
function, only:

* the standard library;
* a first-party module (any package a Sandloom wheel or the source tree
  provides) shipped by the same wheel or by a wheel it depends on,
  transitively, extras included;
* a third-party distribution the wheel declares (any extra) or that one of
  its dependencies brings, transitively. A third-party import inside a
  ``try`` that catches ``ImportError`` (an optional dependency) is exempt;
  a first-party import never is.

``if TYPE_CHECKING:`` blocks are skipped. Constant-string dynamic imports
(``importlib.import_module("x")``, ``__import__("x")`` and any call of a
function whose name ends in ``import`` / ``import_module``) count as imports.
The dependency closure of third-party distributions is read from the
metadata of the running environment (the contributor environment has every
dependency installed).
"""

from __future__ import annotations

import ast
import re
import sys
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path, PurePosixPath

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize(name: str) -> str:
    """PEP 503 distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str | None:
    match = _NAME.match(requirement)
    return normalize(match.group(1)) if match else None


@dataclass
class Wheel:
    """What the check needs from one wheel."""

    name: str
    requires: set[str]
    #: ``{dotted module name: source}`` of every shipped ``.py`` file.
    modules: dict[str, str] = field(default_factory=dict)
    #: The modules that are packages (an ``__init__.py``).
    packages: set[str] = field(default_factory=set)
    #: ``(distribution, extras)`` of requirements naming extras
    #: (``celery[redis]``): their extras' requirements count too.
    requires_extras: set[tuple[str, frozenset[str]]] = field(default_factory=set)


def read_wheel(path: Path) -> Wheel:
    """Name, ``Requires-Dist`` (every extra) and Python sources of a wheel."""
    with zipfile.ZipFile(path) as archive:
        (meta,) = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        message = BytesParser().parsebytes(archive.read(meta))
        raw_requires = message.get_all("Requires-Dist", [])
        requires = {
            name for raw in raw_requires if (name := requirement_name(raw)) is not None
        }
        wheel = Wheel(normalize(str(message["Name"])), requires)
        for raw in raw_requires:
            match = re.match(r"\s*([A-Za-z0-9._-]+)\s*\[([^\]]+)\]", raw)
            if match:
                extras = frozenset(e.strip() for e in match.group(2).split(","))
                wheel.requires_extras.add((normalize(match.group(1)), extras))
        for name in archive.namelist():
            if not name.endswith(".py") or ".dist-info/" in name:
                continue
            parts = list(PurePosixPath(name).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
                wheel.packages.add(".".join(parts))
            wheel.modules[".".join(parts)] = archive.read(name).decode("utf-8")
    return wheel


@dataclass(frozen=True)
class Import:
    line: int
    target: str
    optional: bool


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _catches_import_error(handlers: Iterable[ast.ExceptHandler]) -> bool:
    names: set[str] = set()
    for handler in handlers:
        kinds = handler.type
        if kinds is None:
            return True
        for node in kinds.elts if isinstance(kinds, ast.Tuple) else [kinds]:
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    return bool(
        names & {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
    )


def _call_target(node: ast.Call) -> str | None:
    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else ""
    )
    if not (name == "__import__" or name.endswith(("import", "import_module"))):
        return None
    if node.args and isinstance(node.args[0], ast.Constant):
        value = node.args[0].value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][\w.]*", value):
            return value
    return None


def module_imports(source: str, module: str, *, is_package: bool) -> list[Import]:
    """Every runtime import of one module (relative imports resolved)."""
    tree = ast.parse(source)
    package = module.split(".") if is_package else module.split(".")[:-1]
    found: list[Import] = []

    def visit(node: ast.AST, optional: bool) -> None:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.orelse:
                visit(child, optional)
            return
        if isinstance(node, ast.Try | getattr(ast, "TryStar", ast.Try)):
            guarded = optional or _catches_import_error(node.handlers)
            for child in node.body:
                visit(child, guarded)
            for child in [*node.handlers, *node.orelse, *node.finalbody]:
                visit(child, optional)
            return
        if isinstance(node, ast.Import):
            found.extend(Import(node.lineno, a.name, optional) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package[: len(package) - (node.level - 1)]
                base = ".".join([*base_parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            for alias in node.names:
                target = base if alias.name == "*" else f"{base}.{alias.name}"
                found.append(Import(node.lineno, target, optional))
        elif isinstance(node, ast.Call):
            target = _call_target(node)
            if target is not None:
                found.append(Import(node.lineno, target, optional))
        for child in ast.iter_child_nodes(node):
            visit(child, optional)

    visit(tree, False)
    return found


def _provider(target: str, owners: Mapping[str, str]) -> tuple[str, str] | None:
    """``(module, distribution)`` shipping ``target``: the module itself, or
    its parent when ``target`` names an attribute (``from m import name``).
    A shipped grandparent does not count: the submodule is missing."""
    for candidate in (target, target.rpartition(".")[0]):
        if candidate in owners:
            return candidate, owners[candidate]
    return None


def _closure(
    start: str,
    wheels: Mapping[str, Wheel],
    requires_of: Callable[[str, frozenset[str]], Iterable[tuple[str, frozenset[str]]]],
) -> set[str]:
    """Distributions ``start`` may import: its own requirements (a Sandloom
    wheel's every extra), then each third-party requirement's base
    requirements plus the extras requested of it, transitively."""
    seen: set[tuple[str, frozenset[str]]] = set()
    todo: list[tuple[str, frozenset[str]]] = [(start, frozenset())]
    while todo:
        node = todo.pop()
        if node in seen:
            continue
        seen.add(node)
        dist, extras = node
        if dist in wheels:
            todo.extend((name, frozenset()) for name in wheels[dist].requires)
            todo.extend(wheels[dist].requires_extras)
        else:
            todo.extend(requires_of(dist, extras))
    return {dist for dist, _extras in seen}


def _installed_requires(
    dist: str, extras: frozenset[str]
) -> Iterator[tuple[str, frozenset[str]]]:
    """Base requirements of an installed distribution plus those of the
    requested ``extras`` (environment markers evaluated here)."""
    from packaging.requirements import Requirement

    try:
        raw = metadata.requires(dist) or []
    except metadata.PackageNotFoundError:
        return
    for line in raw:
        requirement = Requirement(line)
        marker = requirement.marker
        wanted = marker is None or any(
            marker.evaluate({"extra": extra}) for extra in ("", *extras)
        )
        if wanted:
            yield normalize(requirement.name), frozenset(requirement.extras)


def _installed_top_levels() -> dict[str, set[str]]:
    return {
        top: {normalize(d) for d in dists}
        for top, dists in metadata.packages_distributions().items()
    }


def check(
    wheels: Iterable[Wheel],
    *,
    first_party: Iterable[str] = (),
    top_levels: Mapping[str, set[str]] | None = None,
    requires_of: Callable[
        [str, frozenset[str]], Iterable[tuple[str, frozenset[str]]]
    ] = _installed_requires,
    stdlib: Iterable[str] = sys.stdlib_module_names,
) -> list[str]:
    """Every import of a module that its distribution may not import.

    ``first_party``: top-level packages that are Sandloom's even when no
    wheel ships the imported module (``sandloom`` and the source tree's
    module packages); an import of an unshipped first-party module is a
    violation. ``top_levels``: ``{import name: distributions}`` of third-party
    packages (default: the running environment).
    """
    by_name = {w.name: w for w in wheels}
    owners: dict[str, str] = {}
    for wheel in by_name.values():
        for module in wheel.modules:
            owners[module] = wheel.name
    ours = set(first_party) | {m.split(".")[0] for m in owners}
    tops = _installed_top_levels() if top_levels is None else top_levels
    std = set(stdlib) | {"__future__"}
    problems: list[str] = []
    for wheel in by_name.values():
        allowed = _closure(wheel.name, by_name, requires_of)
        for module, source in sorted(wheel.modules.items()):
            is_package = module in wheel.packages
            for imp in module_imports(source, module, is_package=is_package):
                top = imp.target.split(".")[0]
                where = f"{wheel.name}: {module} (line {imp.line}) imports {imp.target}"
                if top in std:
                    continue
                if top in ours:
                    provided = _provider(imp.target, owners)
                    if provided is None:
                        problems.append(f"{where}, which no distribution ships")
                    elif provided[1] not in allowed:
                        problems.append(
                            f"{where} from {provided[1]}, which {wheel.name} "
                            "does not depend on"
                        )
                    continue
                if imp.optional:
                    continue
                dists = tops.get(top)
                if not dists:
                    problems.append(f"{where}: no installed distribution provides it")
                elif not dists & allowed:
                    problems.append(
                        f"{where} from {', '.join(sorted(dists))}, which "
                        f"{wheel.name} does not declare"
                    )
    return problems


__all__ = [
    "Import",
    "Wheel",
    "check",
    "module_imports",
    "normalize",
    "read_wheel",
    "requirement_name",
]
