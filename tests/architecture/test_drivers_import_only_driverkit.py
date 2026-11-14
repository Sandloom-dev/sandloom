"""Drivers import only the driver kit, never the kernel (ADR-0011, ADR-0016).

A driver that imports ``sandloom.core`` is the fault ADR-0011 fixed: it ties
the driver to kernel internals, so it cannot leave the kernel wheel, and a
kernel refactor breaks it. ``packages/sandloom-s3`` is the model: it imports
``sandloom.driverkit`` and nothing else of Sandloom's.

- **Packages.** Every module shipped from ``packages/*/src`` imports nothing
  from ``sandloom.core``: no allowlist.
- **Built-in drivers.** Every module under ``src/sandloom/core/drivers`` (but
  the kernel's registration table ``bootstrap.py``) and every module a row of
  ``BUILTIN_DRIVER_ROWS`` targets is held to the same rule; the imports they
  still make are listed in ``drivers_core_import_allowlist.json``
  (``file -> [imported kernel module]``), seeded when the rule landed. The list
  only shrinks: an import it does not list fails, while an entry no longer
  imported does not (delete it when the driver moves).

Counted: ``import sandloom.core...``, ``from sandloom.core... import``,
``from sandloom import core``, a relative import that lands in
``sandloom.core``, and ``importlib.import_module`` / ``__import__`` of a
literal ``sandloom.core...`` name, at any depth (function-local and
``TYPE_CHECKING`` imports included: a moved driver has neither).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable
from pathlib import Path

from sandloom.core.drivers.bootstrap import BUILTIN_DRIVER_ROWS

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
DRIVERS = SRC / "sandloom" / "core" / "drivers"
PACKAGES = REPO / "packages"
ALLOWLIST = Path(__file__).with_name("drivers_core_import_allowlist.json")

#: Kernel modules under ``core/drivers`` that are not drivers.
NOT_DRIVERS = frozenset({"__init__.py", "bootstrap.py"})

KERNEL = "sandloom.core"


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(
    module: str, level: int, target: str | None, package: bool
) -> str:
    """The absolute module ``from <level dots><target> import`` names."""
    parts = module.split(".")
    base = parts if package else parts[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join([*base, target] if target else base)


def _is_kernel(name: str) -> bool:
    return name == KERNEL or name.startswith(KERNEL + ".")


def kernel_imports(path: Path, module: str) -> set[str]:
    """The ``sandloom.core`` modules ``path`` (the module ``module``) imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = path.name == "__init__.py"
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if _is_kernel(a.name))
        elif isinstance(node, ast.ImportFrom):
            target = (
                _resolve_relative(module, node.level, node.module, package)
                if node.level
                else node.module or ""
            )
            if _is_kernel(target):
                found.add(target)
            elif target == "sandloom":
                found.update(
                    f"sandloom.{a.name}" for a in node.names if a.name == "core"
                )
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if name in {"import_module", "__import__"} and node.args:
                arg = node.args[0]
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and _is_kernel(arg.value)
                ):
                    found.add(arg.value)
    return found


def _row_modules() -> set[str]:
    return {target.partition(":")[0] for _, _, target, *_ in BUILTIN_DRIVER_ROWS}


def builtin_driver_files() -> list[Path]:
    """Every built-in driver module this checkout has."""
    files = {p for p in DRIVERS.glob("*.py") if p.name not in NOT_DRIVERS}
    for module in _row_modules():
        base = SRC.joinpath(*module.split("."))
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file() and candidate.name not in NOT_DRIVERS:
                files.add(candidate)
    return sorted(files)


def package_files() -> list[Path]:
    """Every module shipped from a ``packages/*/src`` tree."""
    return sorted(PACKAGES.glob("*/src/**/*.py"))


def violations(
    files: Iterable[Path], root: Path, repo: Path, allowed: dict[str, list[str]]
) -> list[str]:
    """``file: module`` for each kernel import ``allowed`` does not list."""
    out: list[str] = []
    for path in files:
        rel = path.relative_to(repo).as_posix()
        listed = set(allowed.get(rel, ()))
        for name in sorted(kernel_imports(path, _module_name(path, root)) - listed):
            out.append(f"{rel}: {name}")
    return out


def _allowlist() -> dict[str, list[str]]:
    data = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def test_packages_import_nothing_from_the_kernel() -> None:
    files = package_files()
    assert files, "no packages/*/src module found"
    found: list[str] = []
    for path in files:
        src = next(p for p in path.parents if p.name == "src")
        found += violations([path], src, REPO, {})
    assert not found, (
        "a driver package imports sandloom.core (import it from "
        "sandloom.driverkit instead; ADR-0011):\n  " + "\n  ".join(found)
    )


def test_builtin_drivers_gain_no_kernel_import() -> None:
    found = violations(builtin_driver_files(), SRC, REPO, _allowlist())
    assert not found, (
        "a built-in driver gained a sandloom.core import; drivers record "
        "metrics through sandloom.driverkit.observe and read options, not "
        "settings (ADR-0016). Import from the driver kit instead; the "
        "allowlist only shrinks:\n  " + "\n  ".join(found)
    )


def test_allowlist_names_only_driver_files() -> None:
    known = {p.relative_to(REPO).as_posix() for p in builtin_driver_files()}
    data = _allowlist()
    assert all(isinstance(v, list) for v in data.values())
    # A file that left (moved to a package) may stay listed until removed,
    # but a typo'd path guards nothing.
    unknown = sorted(set(data) - known - _gone(data))
    assert not unknown, f"allowlist names files that are no drivers: {unknown}"


def _gone(data: dict[str, list[str]]) -> set[str]:
    return {rel for rel in data if not (REPO / rel).exists()}


# ---------------------------------------------------------------- self-test


def _plant(tmp_path: Path, files: dict[str, str]) -> Path:
    src = tmp_path / "src"
    for rel, body in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return src


def test_every_import_form_is_counted(tmp_path: Path) -> None:
    src = _plant(
        tmp_path,
        {
            "sandloom/core/drivers/x.py": (
                "import sandloom.core.config.settings\n"
                "from sandloom.core.effects import RECORD_TTL_SECONDS\n"
                "from sandloom import core\n"
                "from ..infra import errors\n"
                "from . import redis_cache\n"
                "import importlib\n"
                "def f():\n"
                "    from sandloom.core.tasks.enqueue import enqueue_once\n"
                "    importlib.import_module('sandloom.core.worker')\n"
                "from sandloom.driverkit import observe\n"
                "import sandloom.sdk\n"
            ),
        },
    )
    path = src / "sandloom/core/drivers/x.py"
    assert kernel_imports(path, _module_name(path, src)) == {
        "sandloom.core.config.settings",
        "sandloom.core.effects",
        "sandloom.core",
        "sandloom.core.infra",
        "sandloom.core.drivers",
        "sandloom.core.tasks.enqueue",
        "sandloom.core.worker",
    }


def test_a_new_import_bites_and_a_stale_entry_does_not(tmp_path: Path) -> None:
    src = _plant(
        tmp_path,
        {
            "sandloom/core/drivers/y.py": (
                "from sandloom.core.infra.config import InstanceConfig\n"
                "from sandloom.core.config.settings import settings\n"
            ),
        },
    )
    path = src / "sandloom/core/drivers/y.py"
    rel = "src/sandloom/core/drivers/y.py"
    allowed = {rel: ["sandloom.core.infra.config", "sandloom.core.effects"]}
    # settings is new: it bites; the stale effects entry is tolerated.
    assert violations([path], src, tmp_path, allowed) == [
        f"{rel}: sandloom.core.config.settings"
    ]
    allowed[rel].append("sandloom.core.config.settings")
    assert violations([path], src, tmp_path, allowed) == []


def test_a_package_importing_the_kernel_bites(tmp_path: Path) -> None:
    src = _plant(
        tmp_path,
        {
            "sandloom_x/__init__.py": "from sandloom.core.descriptors import HealthResult\n"
        },
    )
    path = src / "sandloom_x/__init__.py"
    assert violations([path], src, tmp_path, {}) == [
        "src/sandloom_x/__init__.py: sandloom.core.descriptors"
    ]
