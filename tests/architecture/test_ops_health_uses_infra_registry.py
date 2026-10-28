"""Ops health checks must probe configured infra providers, not raw defaults."""

from __future__ import annotations

import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "src" / "sandloom" / "main.py"
FORBIDDEN_DEFAULT_ACCESSORS = {
    "get_default_engine",
    "get_default_client",
    "get_default_storage",
}


def _forbidden_default_accessor_usage(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in FORBIDDEN_DEFAULT_ACCESSORS:
                    violations.append(f"Line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_DEFAULT_ACCESSORS:
                violations.append(f"Line {node.lineno}: {func.id}()")
            elif (
                isinstance(func, ast.Attribute)
                and func.attr in FORBIDDEN_DEFAULT_ACCESSORS
            ):
                violations.append(f"Line {node.lineno}: .{func.attr}()")
    return violations


def test_ops_health_does_not_use_driver_default_accessors() -> None:
    assert not _forbidden_default_accessor_usage(MAIN)


def test_analyzer_detects_default_accessor_usage(tmp_path: Path) -> None:
    sample = tmp_path / "main.py"
    sample.write_text(
        "from sandloom.core.drivers.sqlalchemy_database import get_default_engine\n"
        "async def health():\n"
        "    get_default_engine()\n"
    )
    found = _forbidden_default_accessor_usage(sample)
    assert len(found) == 2, found
