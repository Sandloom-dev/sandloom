"""``run_module_migrations``: a module's env.py derives everything from __file__."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sandloom.core.migrations import alembic_support, run_module_migrations


def _module_tree(root: Path, dotted: str) -> Path:
    current = root
    for part in dotted.split("."):
        current = current / part
        current.mkdir()
        (current / "__init__.py").write_text("")
    env = current / "migrations" / "env.py"
    env.parent.mkdir()
    env.write_text("")
    return env


def test_package_is_derived_from_the_env_file(tmp_path: Path) -> None:
    env = _module_tree(tmp_path, "shop.modules.links")
    assert alembic_support.module_package_of(env) == "shop.modules.links"


def test_not_a_package_is_a_clear_error(tmp_path: Path) -> None:
    env = tmp_path / "loose" / "migrations" / "env.py"
    env.parent.mkdir(parents=True)
    env.write_text("")
    with pytest.raises(RuntimeError, match="package="):
        alembic_support.module_package_of(env)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_run(
        table_names: Any = (), model_modules: Any = (), *, table_prefix: Any = None
    ) -> None:
        seen.update(
            table_names=tuple(table_names),
            model_modules=list(model_modules),
            table_prefix=table_prefix,
        )

    monkeypatch.setattr(alembic_support, "run_migrations", fake_run)
    return seen


def test_conventions(tmp_path: Path, captured: dict[str, Any]) -> None:
    env = _module_tree(tmp_path, "shop.modules.links")
    run_module_migrations(str(env))
    assert captured == {
        "table_names": (),
        "model_modules": ["shop.modules.links.models"],
        "table_prefix": "links_",
    }


def test_explicit_overrides(tmp_path: Path, captured: dict[str, Any]) -> None:
    env = _module_tree(tmp_path, "shop.modules.links")
    run_module_migrations(
        env,
        package="other.pkg",
        model_modules=["other.pkg.models.a", "other.pkg.models.b"],
        table_prefix="lnk_",
        table_names=["legacy_table"],
    )
    assert captured == {
        "table_names": ("legacy_table",),
        "model_modules": ["other.pkg.models.a", "other.pkg.models.b"],
        "table_prefix": "lnk_",
    }
