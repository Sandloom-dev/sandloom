"""The files a recipe tells an app to write pass the app's own ``sandloom test``
lint as written: ``ruff check --no-fix`` and ``ruff format --check`` under the
ruff configuration ``sandloom new`` generates (the gate never writes)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from string import Template
from types import ModuleType

from sandloom.core.cli.project import _PYPROJECT
from sandloom.sdk._prim.recipes import _RECIPES, recipe_parts

APP = "hub"
REPO = Path(__file__).resolve().parents[2]


def _recipes_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "recipes_check", REPO / "tools/smoke/recipes_check.py"
    )
    assert spec is not None and spec.loader is not None
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    return check


def _app_ruff_config() -> str:
    """The ``[tool.ruff*]`` tables of the generated ``pyproject.toml``."""
    text = Template(_PYPROJECT.template).safe_substitute(app=APP)
    start = text.index("[tool.ruff]")
    return text[start : text.index("[tool.mypy]")]


def _render(root: Path) -> list[str]:
    """Write every recipe's files, placeholders filled, into an app skeleton
    at ``root``; the paths written."""
    check = _recipes_check()
    (root / "pyproject.toml").write_text(_app_ruff_config())
    written: list[str] = []
    for name in _RECIPES:
        files = [(p, b) for kind, p, b in recipe_parts(name) if kind == "file"]
        module = check.MODULES.get(name, "sample")
        for path, body in files:
            target = root / APP / "modules" / check.fill(path, module)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(check.fill(body, module))
            written.append(str(target.relative_to(root)))
    return written


def _ruff(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args, "."],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_recipe_files_pass_ruff_as_written(tmp_path: Path) -> None:
    written = _render(tmp_path)
    assert len(written) >= 15  # every recipe with file blocks was rendered
    lint = _ruff(tmp_path, "check", "--no-fix", "--no-cache")
    fmt = _ruff(tmp_path, "format", "--check", "--no-cache")
    assert lint.returncode == 0, lint.stdout[-3000:]
    assert fmt.returncode == 0, fmt.stdout[-3000:] + fmt.stderr[-1000:]


def test_the_ruff_check_bites_on_a_broken_file(tmp_path: Path) -> None:
    """The gate above fails on a file out of isort order and on an unformatted
    one: it checks something."""
    _render(tmp_path)
    target = tmp_path / APP / "modules" / "broken.py"
    target.write_text("from sandloom.sdk import Contract\nfrom hub import y\n")
    assert _ruff(tmp_path, "check", "--no-fix", "--no-cache").returncode != 0
    target.write_text("x = {'a':1}\n")
    assert _ruff(tmp_path, "format", "--check", "--no-cache").returncode != 0
