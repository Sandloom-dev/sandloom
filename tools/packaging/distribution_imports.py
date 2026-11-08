"""Import layering across the built distributions (no install needed).

Every ``.py`` file a wheel ships may import, at module level or inside a
function, only:

* the standard library;
* a first-party module (any package a Sandloom wheel or the source tree
  provides) shipped by the same wheel or by a wheel its **base**
  requirements bring, transitively, plus the extras such a requirement
  requests (``sandloom[redis]``). The wheel's own extras do not count: an
  extra is not installed with the wheel, so ``sandloom[standard]`` does not
  let the kernel import ``sandloom_identity`` (invariant I-02 of
  ``design/kernel-boundary/requirements.md``: kernel code imports no
  business-module implementation);
* a third-party distribution the wheel declares (any extra) or that one of
  its dependencies brings, transitively. A third-party import inside a
  ``try`` that catches ``ImportError`` (an optional dependency) is exempt;
  a first-party import never is.

``if TYPE_CHECKING:`` blocks are skipped. Dynamic imports count when their
argument is a string literal or a module-level string constant
(``importlib.import_module("x")``, ``import_module(NAME)`` with
``NAME = "x"`` bound once at module level, ``__import__("x")`` and any call
of a function whose name ends in ``import`` / ``import_module``).

First-party violations that exist today are listed, each with its reason, in
``tests/architecture/kernel_module_imports_allowlist.json``; the list only
shrinks: an entry that matches no violation is itself a problem.

The dependency closure of third-party distributions is read from the
metadata of the running environment (the contributor environment has every
dependency installed).
"""

from __future__ import annotations

import ast
import json
import re
import sys
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path, PurePosixPath

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXTRA_MARKER = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")

#: The shrink-only list of first-party imports that break the rule today.
ALLOWLIST = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "architecture"
    / "kernel_module_imports_allowlist.json"
)

Requirement = tuple[str, frozenset[str]]


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
    #: Base requirements (no ``extra ==`` marker).
    requires: set[str]
    #: ``{dotted module name: source}`` of every shipped ``.py`` file.
    modules: dict[str, str] = field(default_factory=dict)
    #: The modules that are packages (an ``__init__.py``).
    packages: set[str] = field(default_factory=set)
    #: ``(distribution, extras)`` of base requirements naming extras
    #: (``celery[redis]``): their extras' requirements count too.
    requires_extras: set[Requirement] = field(default_factory=set)
    #: ``{extra: {(distribution, extras)}}``: the requirements of each of the
    #: wheel's own extras (``sandloom[standard]`` -> the module wheels).
    optional: dict[str, set[Requirement]] = field(default_factory=dict)


def _parse_requirements(
    raw_requires: Iterable[str], provided: Iterable[str]
) -> tuple[set[Requirement], dict[str, set[Requirement]]]:
    """Split ``Requires-Dist`` lines into base requirements and those of
    each extra."""
    from packaging.requirements import Requirement as Parsed

    lines = list(raw_requires)
    extras = {normalize(e) for e in provided}
    extras |= {normalize(e) for raw in lines for e in _EXTRA_MARKER.findall(raw)}
    base: set[Requirement] = set()
    optional: dict[str, set[Requirement]] = {}
    for raw in lines:
        parsed = Parsed(raw)
        node = (normalize(parsed.name), frozenset(normalize(e) for e in parsed.extras))
        marker = parsed.marker
        if marker is None or "extra" not in str(marker):
            base.add(node)
            continue
        for extra in sorted(extras):
            if marker.evaluate({"extra": extra}):
                optional.setdefault(extra, set()).add(node)
    return base, optional


def read_wheel(path: Path) -> Wheel:
    """Name, ``Requires-Dist`` (base and per extra) and Python sources of a
    wheel."""
    with zipfile.ZipFile(path) as archive:
        (meta,) = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        message = BytesParser().parsebytes(archive.read(meta))
        base, optional = _parse_requirements(
            message.get_all("Requires-Dist", []), message.get_all("Provides-Extra", [])
        )
        wheel = Wheel(
            normalize(str(message["Name"])),
            {name for name, _extras in base},
            requires_extras={node for node in base if node[1]},
            optional=optional,
        )
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


def string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level names bound exactly once, to a string literal
    (``NAME = "x"``, ``NAME: Final = "x"``)."""
    values: dict[str, list[object]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        literal = value.value if isinstance(value, ast.Constant) else None
        for target in targets:
            if isinstance(target, ast.Name):
                values.setdefault(target.id, []).append(literal)
    return {
        name: bound[0]
        for name, bound in values.items()
        if len(bound) == 1 and isinstance(bound[0], str)
    }


def call_target(
    node: ast.Call, constants: Mapping[str, str] | None = None
) -> str | None:
    """The module a dynamic-import call names: its first argument as a
    string literal or a module-level string constant, else ``None``."""
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
    if not node.args:
        return None
    arg = node.args[0]
    value: object = None
    if isinstance(arg, ast.Constant):
        value = arg.value
    elif isinstance(arg, ast.Name):
        value = (constants or {}).get(arg.id)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][\w.]*", value):
        return value
    return None


def module_imports(source: str, module: str, *, is_package: bool) -> list[Import]:
    """Every runtime import of one module (relative imports resolved)."""
    tree = ast.parse(source)
    constants = string_constants(tree)
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
            dynamic = call_target(node, constants)
            if dynamic is not None:
                found.append(Import(node.lineno, dynamic, optional))
        for child in ast.iter_child_nodes(node):
            visit(child, optional)

    visit(tree, False)
    return found


def load_allowlist(path: Path = ALLOWLIST) -> dict[str, tuple[str, ...]]:
    """``{importing module: imported modules}`` of the shrink-only list.
    Every entry states its ``reason``."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    allowed: dict[str, tuple[str, ...]] = {}
    for module, entry in entries.items():
        if not str(entry.get("reason", "")).strip() or not entry.get("imports"):
            raise ValueError(f"{path.name}: {module} needs `imports` and a `reason`")
        allowed[module] = tuple(entry["imports"])
    return allowed


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
    requires_of: Callable[[str, frozenset[str]], Iterable[Requirement]],
    *,
    every_extra: bool,
) -> set[str]:
    """Distributions ``start`` may import: its base requirements and the
    extras they request, then each requirement's the same, transitively.
    ``every_extra``: a Sandloom wheel's every own extra counts too (the
    third-party rule); without it only the extras a requirement requests
    (the first-party rule, I-02)."""
    seen: set[Requirement] = set()
    todo: list[Requirement] = [(start, frozenset())]
    while todo:
        node = todo.pop()
        if node in seen:
            continue
        seen.add(node)
        dist, extras = node
        if dist in wheels:
            wheel = wheels[dist]
            todo.extend((name, frozenset()) for name in wheel.requires)
            todo.extend(wheel.requires_extras)
            for extra, requirements in wheel.optional.items():
                if every_extra or extra in extras:
                    todo.extend(requirements)
        else:
            todo.extend(requires_of(dist, extras))
    return {dist for dist, _extras in seen}


def _installed_requires(dist: str, extras: frozenset[str]) -> Iterator[Requirement]:
    """Base requirements of an installed distribution plus those of the
    requested ``extras`` (environment markers evaluated here)."""
    from packaging.requirements import Requirement as Parsed

    try:
        raw = metadata.requires(dist) or []
    except metadata.PackageNotFoundError:
        return
    for line in raw:
        requirement = Parsed(line)
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
        [str, frozenset[str]], Iterable[Requirement]
    ] = _installed_requires,
    stdlib: Iterable[str] = sys.stdlib_module_names,
    allowlist: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    """Every import of a module that its distribution may not import.

    ``first_party``: top-level packages that are Sandloom's even when no
    wheel ships the imported module (``sandloom`` and the source tree's
    module packages); an import of an unshipped first-party module is a
    violation. ``top_levels``: ``{import name: distributions}`` of third-party
    packages (default: the running environment). ``allowlist``:
    ``{importing module: imported modules}`` of first-party violations that
    are tolerated (default: :data:`ALLOWLIST`); an entry that matches no
    violation is reported, so the list only shrinks.
    """
    by_name = {w.name: w for w in wheels}
    owners: dict[str, str] = {}
    for wheel in by_name.values():
        for module in wheel.modules:
            owners[module] = wheel.name
    ours = set(first_party) | {m.split(".")[0] for m in owners}
    tops = _installed_top_levels() if top_levels is None else top_levels
    std = set(stdlib) | {"__future__"}
    tolerated = {
        (module, target)
        for module, targets in (
            load_allowlist() if allowlist is None else allowlist
        ).items()
        for target in targets
    }
    used: set[tuple[str, str]] = set()
    problems: list[str] = []
    for wheel in by_name.values():
        allowed = _closure(wheel.name, by_name, requires_of, every_extra=True)
        depends = _closure(wheel.name, by_name, requires_of, every_extra=False)
        for module, source in sorted(wheel.modules.items()):
            is_package = module in wheel.packages
            for imp in module_imports(source, module, is_package=is_package):
                top = imp.target.split(".")[0]
                where = f"{wheel.name}: {module} (line {imp.line}) imports {imp.target}"
                if top in std:
                    continue
                if top in ours:
                    provided = _provider(imp.target, owners)
                    if provided is not None and provided[1] in depends:
                        continue
                    if (module, imp.target) in tolerated:
                        used.add((module, imp.target))
                    elif provided is None:
                        problems.append(f"{where}, which no distribution ships")
                    else:
                        through_extra = (
                            " (only through an extra, which is not a dependency)"
                            if provided[1] in allowed
                            else ""
                        )
                        problems.append(
                            f"{where} from {provided[1]}, which {wheel.name} "
                            f"does not depend on{through_extra}"
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
    problems.extend(
        f"allowlist entry {module} -> {target} matches no violation: remove it "
        f"from {ALLOWLIST.name} (the list only shrinks)"
        for module, target in sorted(tolerated - used)
    )
    return problems


__all__ = [
    "ALLOWLIST",
    "Import",
    "Wheel",
    "call_target",
    "check",
    "load_allowlist",
    "module_imports",
    "normalize",
    "read_wheel",
    "requirement_name",
    "string_constants",
]
