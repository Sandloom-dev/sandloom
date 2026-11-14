"""The ``kernel-never-imports-driverkit`` import-linter contract stays exhaustive.

The kernel (``sandloom.core`` but its built-in drivers, and ``sandloom.shared``)
never imports ``sandloom.driverkit`` (ADR-0010, ADR-0016); only the built-in
drivers (``sandloom.core.drivers``) do, since their files move whole to a
package. import-linter takes no exclusion of a sub-package, so the contract
lists every ``sandloom.core`` child but ``drivers`` as a source. A hand-written
list goes stale: a new core child would be free to import the driverkit until
someone adds it. These tests keep the list equal to the core's children, and
cover what the list cannot, ``sandloom/core/__init__.py`` (``sandloom.core``
itself is no source: it would pull in ``drivers``).
"""

from __future__ import annotations

import ast
import configparser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
IMPORTLINTER = REPO / ".importlinter"
CORE = REPO / "src" / "sandloom" / "core"
CONTRACT = "importlinter:contract:kernel-never-imports-driverkit"

#: ``sandloom.core`` children the contract deliberately leaves out: the
#: built-in drivers, which import the driverkit by ADR-0016. (``core/tests`` is
#: no package: it has no ``__init__.py`` and ships nowhere.)
EXCLUDED_CHILDREN = frozenset({"drivers"})

#: The sources outside ``sandloom.core`` the contract names.
OTHER_SOURCES = frozenset({"sandloom.shared"})


def core_children(core: Path) -> set[str]:
    """The importable children of ``sandloom.core``: its modules and its
    packages (a directory with an ``__init__.py``)."""
    children: set[str] = set()
    for entry in core.iterdir():
        if entry.is_file() and entry.suffix == ".py" and entry.stem != "__init__":
            children.add(entry.stem)
        elif entry.is_dir() and (entry / "__init__.py").exists():
            children.add(entry.name)
    return children


def contract_sources(path: Path = IMPORTLINTER) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(path)
    raw = parser[CONTRACT]["source_modules"]
    return [line.strip() for line in raw.splitlines() if line.strip()]


def source_drift(
    sources: list[str], core: Path, excluded: frozenset[str] = EXCLUDED_CHILDREN
) -> tuple[list[str], list[str], list[str]]:
    """``(missing, stale, duplicated)`` sources against the core's children."""
    expected = {f"sandloom.core.{name}" for name in core_children(core) - excluded}
    expected |= OTHER_SOURCES
    given = set(sources)
    duplicated = sorted({s for s in sources if sources.count(s) > 1})
    return sorted(expected - given), sorted(given - expected), duplicated


def driverkit_imports(source: str) -> list[int]:
    """The line numbers of the imports of ``sandloom.driverkit`` in ``source``
    (any form: ``import``, ``from``, relative from a package that names it)."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [module] + [f"{module}.{alias.name}" for alias in node.names]
            if module == "sandloom":
                names += [f"sandloom.{alias.name}" for alias in node.names]
        else:
            continue
        if any(
            n == "sandloom.driverkit" or n.startswith("sandloom.driverkit.")
            for n in names
        ):
            lines.append(node.lineno)
    return lines


def test_the_contract_sources_are_exactly_the_cores_children_but_the_drivers() -> None:
    missing, stale, duplicated = source_drift(contract_sources(), CORE)
    assert not missing, (
        f"{missing} would be free to import sandloom.driverkit: add them to the "
        "source_modules of kernel-never-imports-driverkit in .importlinter"
    )
    assert not stale, f"{stale} are no sandloom.core child: remove them"
    assert not duplicated, f"{duplicated} are listed twice"


def test_the_core_package_init_does_not_import_the_driverkit() -> None:
    """``sandloom.core`` is no contract source (its init would reach
    ``drivers`` through it), so this scan is its guard."""
    init = CORE / "__init__.py"
    assert driverkit_imports(init.read_text(encoding="utf-8")) == []


def test_a_new_core_child_is_found_missing(tmp_path: Path) -> None:
    core = tmp_path / "core"
    (core / "drivers").mkdir(parents=True)
    (core / "drivers" / "__init__.py").write_text("")
    (core / "brand_new").mkdir()
    (core / "brand_new" / "__init__.py").write_text("")
    (core / "alone.py").write_text("")
    (core / "tests").mkdir()  # no __init__: not a package
    (core / "__init__.py").write_text("")
    sources = ["sandloom.shared", "sandloom.core.alone"]
    assert source_drift(sources, core) == (["sandloom.core.brand_new"], [], [])
    assert source_drift(
        [*sources, "sandloom.core.brand_new", "sandloom.core.gone"], core
    ) == (
        [],
        ["sandloom.core.gone"],
        [],
    )
    assert source_drift(
        [*sources, "sandloom.core.brand_new", "sandloom.core.alone"], core
    ) == ([], [], ["sandloom.core.alone"])


def test_a_listed_drivers_source_is_stale() -> None:
    """The drivers are excluded on purpose: listing them breaks the built-ins."""
    sources = [*contract_sources(), "sandloom.core.drivers"]
    assert source_drift(sources, CORE)[1] == ["sandloom.core.drivers"]


@pytest.mark.parametrize(
    "code",
    [
        "import sandloom.driverkit\n",
        "import sandloom.driverkit.kits\n",
        "from sandloom.driverkit import x\n",
        "from sandloom.driverkit.kits import x\n",
        "from sandloom import driverkit\n",
        "def f():\n    from sandloom.driverkit import x\n",
    ],
)
def test_the_init_scan_sees_every_import_form(code: str) -> None:
    assert driverkit_imports(code)


@pytest.mark.parametrize(
    "code",
    ["import sandloom.core\n", "from sandloom.core import x\n", "from . import y\n"],
)
def test_the_init_scan_ignores_other_imports(code: str) -> None:
    assert driverkit_imports(code) == []
