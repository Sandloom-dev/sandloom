"""A generated app's `sandloom.*` imports must resolve in the INSTALLED package layout.

`sandloom new --no-batteries` once generated imports that shipped in a
distribution the minimal app did not depend on (the platform root, the SDK in a
separate wheel). Every import must resolve in `sandloom` plus the module
packages chosen with `--with`.

This test builds the generated project's import graph and checks it against
the file lists of exactly the distributions the generated `pyproject.toml`
depends on (resolved transitively through `packages/*/pyproject.toml`
and their `tool.sandloom.sources` maps) — the layout a `pip install` produces,
not this monorepo's source tree.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli.project import generate_project

BACKEND = Path(__file__).resolve().parents[4]
PACKAGES = BACKEND / "packages"
pytestmark = pytest.mark.skipif(
    not PACKAGES.is_dir(), reason="package definitions not in this checkout"
)

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


def _req_name(requirement: str) -> str:
    match = _REQ_NAME.match(requirement)
    assert match, requirement
    return match.group(1).lower().replace("_", "-")


def _package_defs() -> dict[str, tuple[Path, dict[str, Any]]]:
    defs: dict[str, tuple[Path, dict[str, Any]]] = {}
    for pyproject in PACKAGES.glob("*/pyproject.toml"):
        data = tomllib.loads(pyproject.read_text())
        defs[data["project"]["name"].lower()] = (pyproject.parent, data)
    return defs


def _closure(requirements: list[str]) -> set[str]:
    """Transitive set of Sandloom distributions installed for ``requirements``."""
    defs = _package_defs()
    seen: set[str] = set()
    stack = [_req_name(r) for r in requirements]
    while stack:
        name = stack.pop()
        if name in seen or name not in defs:
            continue  # third-party (fastapi, redis, ...) — not our layout
        seen.add(name)
        stack.extend(_req_name(r) for r in defs[name][1]["project"]["dependencies"])
    return seen


def _shipped_files(distributions: set[str]) -> set[str]:
    """``sandloom/...`` paths (posix) the given wheels install."""
    defs = _package_defs()
    files: set[str] = set()
    for name in distributions:
        root, data = defs[name]
        config = data["tool"]["sandloom"]
        excluded = config.get("exclude", {}).get("paths", [])
        for source, dest in config["sources"].items():
            src = (root / source).resolve()
            if src.is_file():
                files.add(dest)
            else:
                for path in src.rglob("*.py"):
                    files.add(f"{dest}/{path.relative_to(src).as_posix()}")
        files = {
            f
            for f in files
            if not any(f == x or f.startswith(x + "/") for x in excluded)
        }
    return files


def _sandloom_imports(project: Path) -> set[str]:
    modules: set[str] = set()
    for path in project.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(
                    a.name for a in node.names if a.name.startswith("sandloom")
                )
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mod = node.module or ""
                if mod == "sandloom" or mod.startswith("sandloom."):
                    modules.add(mod)
                    # `from sandloom.core import di` may name a submodule.
                    modules.update(f"{mod}.{a.name}" for a in node.names)
    return modules


def _resolves(module: str, shipped: set[str]) -> bool:
    path = module.replace(".", "/")
    return f"{path}.py" in shipped or f"{path}/__init__.py" in shipped


def _is_attribute(module: str, shipped: set[str]) -> bool:
    """``from sandloom.core.di import DbSession``: the parent module ships."""
    parent = module.rsplit(".", 1)[0]
    return _resolves(parent, shipped)


def _dependencies(project: Path) -> list[str]:
    data = tomllib.loads((project / "pyproject.toml").read_text())
    return list(data["project"]["dependencies"])


@pytest.mark.parametrize(
    ("batteries", "expected_dist"),
    [
        ([], "sandloom"),
        (["identity", "tenancy", "billing"], "sandloom-billing"),
    ],
)
def test_generated_imports_ship_in_its_dependencies(
    tmp_path: Path, batteries: list[str], expected_dist: str
) -> None:
    generate_project("probe", tmp_path, with_batteries=batteries)
    project = tmp_path / "probe"
    installed = _closure(_dependencies(project))
    assert expected_dist in installed, installed
    shipped = _shipped_files(installed)

    missing = sorted(
        m
        for m in _sandloom_imports(project)
        if not _resolves(m, shipped) and not _is_attribute(m, shipped)
    )
    assert not missing, (
        f"generated project imports modules its dependencies {sorted(installed)} "
        f"do not ship: {missing}"
    )


def test_minimal_app_uses_the_kernel_factory(tmp_path: Path) -> None:
    generate_project("probe", tmp_path)
    project = tmp_path / "probe"
    installed = _closure(_dependencies(project))
    assert installed == {"sandloom"}  # no module package
    shipped = _shipped_files(installed)
    assert _resolves("sandloom.core.app_factory", shipped)
    assert "sandloom.main" not in _sandloom_imports(project)
