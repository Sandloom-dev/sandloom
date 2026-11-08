"""The kernel stays module-agnostic: it imports no business-module package.

Invariant I-02 of ``design/kernel-boundary/requirements.md``: shipped kernel
code (``sandloom.core``, ``sandloom.sdk``, ``sandloom.shared``,
``sandloom.main``) imports no business module, statically, function-locally,
or dynamically by a literal or module-level constant name. ``shared`` is the
one layer every business module may import, so an import of a module from
it would also be a transitive coupling channel (module A -> shared ->
module B); for ``shared`` the ``sandloom.<m>`` subpackages other than the
kernel's (``sandloom.driverkit``) count too.

The business modules are discovered, never listed: every ``sandloom_*``
package of the source layout and every ``sandloom_*`` package an installed
distribution registers in the ``sandloom.modules`` entry-point group. Today's
violations sit in ``kernel_module_imports_allowlist.json`` (shared with
``tools/packaging/distribution_imports.py``), which only shrinks.

Scope (I-02): kernel tests (``*/tests/*``, ``conftest.py``) are not shipped
and are not scanned. ``if TYPE_CHECKING:`` imports count. The module loader
importing a package an application declares (``SANDLOOM_MODULES``, an entry
point) is a computed name, which this scan neither sees nor refuses.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "packaging"))

import distribution_imports as di  # noqa: E402

_SRC = REPO / "src"
_SANDLOOM = _SRC / "sandloom"
#: The kernel's own packages; every other ``sandloom.<m>`` is foreign to shared.
_KERNEL_PACKAGES = ("core", "shared", "sdk")
#: The shipped kernel (I-02), relative to the ``sandloom`` package.
_KERNEL = ("core", "sdk", "shared", "main.py")
_ENTRY_POINT_GROUP = "sandloom.modules"


def business_modules(src: Path = _SRC) -> set[str]:
    """Every ``sandloom_*`` business-module package: the source layout's and
    the installed ones (``sandloom.modules`` entry points)."""
    found = {
        d.name
        for d in src.iterdir()
        if d.is_dir()
        and d.name.startswith("sandloom_")
        and (d / "__init__.py").exists()
    }
    for entry in metadata.entry_points(group=_ENTRY_POINT_GROUP):
        top = entry.value.split(":")[0].split(".")[0].strip()
        if top.startswith("sandloom_"):
            found.add(top)
    return found


def _foreign_subpackages(sandloom: Path = _SANDLOOM) -> set[str]:
    """``sandloom.<m>`` subpackages that are not the kernel's (shared must
    not import them either)."""
    return {
        f"sandloom.{d.name}"
        for d in sandloom.iterdir()
        if d.is_dir()
        and (d / "__init__.py").exists()
        and not d.name.startswith(("_", "."))
        and d.name not in _KERNEL_PACKAGES
    }


def _imported_modules(path: Path, root: Path) -> list[tuple[int, str]]:
    """``(line, module)`` of every import of one file: absolute, relative
    (resolved against its package), inside ``TYPE_CHECKING`` or a function,
    and dynamic imports of a literal or module-level string constant."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants = di.string_constants(tree)
    package = path.relative_to(root).with_suffix("").parts[:-1]
    names: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = list(package)[: len(package) - (node.level - 1)]
                if node.module:
                    anchor += node.module.split(".")
                names.extend(
                    (node.lineno, ".".join([*anchor, alias.name]))
                    for alias in node.names
                )
                if anchor:
                    names.append((node.lineno, ".".join(anchor)))
            elif node.module:
                names.append((node.lineno, node.module))
                names.extend(
                    (node.lineno, f"{node.module}.{alias.name}") for alias in node.names
                )
        elif isinstance(node, ast.Call):
            target = di.call_target(node, constants)
            if target is not None:
                names.append((node.lineno, target))
    return names


def _kernel_files(sandloom: Path) -> list[Path]:
    files: list[Path] = []
    for part in _KERNEL:
        path = sandloom / part
        files.extend([path] if path.is_file() else sorted(path.rglob("*.py")))
    return [
        f
        for f in files
        if "tests" not in f.relative_to(sandloom).parts and f.name != "conftest.py"
    ]


def _matches(imported: str, forbidden: set[str]) -> bool:
    return any(imported == f or imported.startswith(f + ".") for f in forbidden)


def kernel_violations(
    sandloom: Path, modules: set[str], allowlist: Mapping[str, tuple[str, ...]]
) -> list[str]:
    """Every kernel import of a business module (and, from shared, of a
    foreign ``sandloom.<m>``) not in ``allowlist``, then every allowlist
    entry that matches no import (the list only shrinks)."""
    root = sandloom.parent
    foreign = _foreign_subpackages(sandloom)
    used: set[tuple[str, str]] = set()
    problems: list[str] = []
    for path in _kernel_files(sandloom):
        rel = path.relative_to(root)
        dotted = ".".join(rel.with_suffix("").parts)
        dotted = dotted.removesuffix(".__init__")
        in_shared = rel.parts[1] == "shared"
        forbidden = modules | foreign if in_shared else modules
        for line, imported in _imported_modules(path, root):
            if not _matches(imported, forbidden):
                continue
            if imported in allowlist.get(dotted, ()):
                used.add((dotted, imported))
                continue
            problems.append(f"{rel}:{line}: imports {imported}")
    problems.extend(
        f"allowlist entry {module} -> {target} matches no kernel import: remove it "
        f"(the list only shrinks)"
        for module, targets in sorted(allowlist.items())
        for target in targets
        if (module, target) not in used
    )
    return problems


def testbusiness_modules_are_discovered() -> None:
    """The scan's set is not hard-coded and covers the bundled modules
    (an empty set would make every test below pass vacuously)."""
    found = business_modules()
    assert {
        "sandloom_admin",
        "sandloom_billing",
        "sandloom_identity",
        "sandloom_tenancy",
    } <= found


def test_shared_does_not_import_business_modules() -> None:
    """I-02 for ``sandloom.shared``: no ``sandloom_*`` module and no foreign
    ``sandloom.<m>`` (G-KERNEL-10)."""
    problems = [
        p
        for p in kernel_violations(_SANDLOOM, business_modules(), {})
        if p.startswith("sandloom/shared/")
    ]
    assert not problems, (
        "sandloom.shared must not import business modules:\n" + "\n".join(problems)
    )


def test_the_kernel_imports_no_business_module_beyond_the_allowlist() -> None:
    """I-02 for the whole shipped kernel, with the shrink-only allowlist
    (B-I1 is its one entry today)."""
    problems = kernel_violations(_SANDLOOM, business_modules(), di.load_allowlist())
    assert not problems, (
        "the kernel (sandloom.core, sdk, shared, main) must not import a business "
        f"module; a listed debt that is gone leaves {di.ALLOWLIST.name}:\n"
        + "\n".join(problems)
    )


def _plant(tmp_path: Path, files: dict[str, str]) -> Path:
    sandloom = tmp_path / "sandloom"
    for package in (
        "",
        "core",
        "core/cli",
        "sdk",
        "shared",
        "shared/authz",
        "driverkit",
    ):
        (sandloom / package).mkdir(parents=True, exist_ok=True)
        (sandloom / package / "__init__.py").write_text("")
    (sandloom / "main.py").write_text("")
    for rel, source in files.items():
        (sandloom / rel).write_text(source)
    return sandloom


def test_the_scan_catches_planted_violations(tmp_path: Path) -> None:
    """Each spelling of a kernel import of a module fails the scan (I-02)."""
    sandloom = _plant(
        tmp_path,
        {
            "core/static.py": "import sandloom_billing\n",
            "core/cli/local.py": "def cmd():\n    from sandloom_identity.api import x\n",
            "sdk/typing_only.py": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n    from sandloom_tenancy import y\n"
            ),
            "main.py": "import importlib\nimportlib.import_module('sandloom_admin')\n",
            "core/constant.py": (
                "import importlib\nPOLICY = 'sandloom_identity.password_policy'\n"
                "def f():\n    return importlib.import_module(POLICY)\n"
            ),
            "shared/authz/relative.py": "from ...driverkit import z\n",
            "shared/from_root.py": "from sandloom import driverkit\n",
            "core/fine.py": "import json\nfrom sandloom.shared import authz\n",
        },
    )
    modules = {
        "sandloom_admin",
        "sandloom_billing",
        "sandloom_identity",
        "sandloom_tenancy",
    }
    problems = kernel_violations(sandloom, modules, {})
    flagged = {p.split(":")[0] for p in problems}
    assert flagged == {
        "sandloom/core/static.py",
        "sandloom/core/cli/local.py",
        "sandloom/sdk/typing_only.py",
        "sandloom/main.py",
        "sandloom/core/constant.py",
        "sandloom/shared/authz/relative.py",
        "sandloom/shared/from_root.py",
    }, problems


def test_core_may_import_driverkit_here_import_linter_forbids_it(
    tmp_path: Path,
) -> None:
    """Foreign ``sandloom.<m>`` subpackages are this scan's concern for
    shared only; the ``kernel-never-imports-sdk`` contract covers core."""
    sandloom = _plant(tmp_path, {"core/x.py": "from sandloom.driverkit import z\n"})
    assert kernel_violations(sandloom, {"sandloom_billing"}, {}) == []


def test_the_allowlist_tolerates_listed_debt_and_fails_stale_entries(
    tmp_path: Path,
) -> None:
    sandloom = _plant(
        tmp_path,
        {
            "core/cli/pw.py": "import importlib\nimportlib.import_module('sandloom_identity.p')\n"
        },
    )
    listed = {"sandloom.core.cli.pw": ("sandloom_identity.p",)}
    assert kernel_violations(sandloom, {"sandloom_identity"}, listed) == []
    stale = {**listed, "sandloom.core.gone": ("sandloom_billing",)}
    (problem,) = kernel_violations(sandloom, {"sandloom_identity"}, stale)
    assert "allowlist entry sandloom.core.gone -> sandloom_billing" in problem


def test_kernel_tests_are_not_shipped_and_not_scanned(tmp_path: Path) -> None:
    sandloom = _plant(tmp_path, {})
    (sandloom / "core" / "tests").mkdir()
    (sandloom / "core" / "tests" / "test_x.py").write_text("import sandloom_billing\n")
    assert kernel_violations(sandloom, {"sandloom_billing"}, {}) == []
