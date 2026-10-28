"""``sandloom add module``'s module wiring and agent contract, and the harness's
disposable-database guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.cli import add
from sandloom.core.cli.scaffold_wiring import module_contract


def _layout(root: Path) -> add.Layout:
    return add.Layout(
        modules_dir=root,
        modules_package="myapp.modules",
        shared_dir=root / "shared",
        shared_package="myapp.shared",
        source_root=root,
    )


def test_disposable_database_guard() -> None:
    """The harness refuses to reset anything but an obvious TEST database."""
    import os

    from sandloom.core.testing.database import assert_disposable_test_database

    saved = os.environ.pop("ENVIRONMENT", None)
    intended = os.environ.pop("SANDLOOM_TEST_DATABASE_URL", None)
    try:
        with pytest.raises(RuntimeError, match="Refusing"):
            assert_disposable_test_database("postgresql+asyncpg://u:p@db/production")
        assert_disposable_test_database("postgresql+asyncpg://u:p@db/app_test")
        os.environ["ENVIRONMENT"] = "test"
        with pytest.raises(RuntimeError, match="Refusing"):
            assert_disposable_test_database("postgresql+asyncpg://u:p@db/anything")
    finally:
        os.environ.pop("ENVIRONMENT", None)
        if saved is not None:
            os.environ["ENVIRONMENT"] = saved
        if intended is not None:
            os.environ["SANDLOOM_TEST_DATABASE_URL"] = intended


def test_reset_refuses_a_database_other_than_the_explicitly_named_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SANDLOOM_TEST_DATABASE_URL pins the ONE database this run may reset."""
    from sandloom.core.testing.database import assert_disposable_test_database

    monkeypatch.setenv(
        "SANDLOOM_TEST_DATABASE_URL", "postgresql+asyncpg://u:p@db/sandloom_test_mine"
    )
    with pytest.raises(RuntimeError, match="sandloom_test_mine"):
        assert_disposable_test_database("postgresql+asyncpg://u:p@db/sandloom_test")
    assert_disposable_test_database("postgresql+asyncpg://u:p@db/sandloom_test_mine")
    # its pytest-xdist worker databases are fine too
    assert_disposable_test_database(
        "postgresql+asyncpg://u:p@db/sandloom_test_mine_gw3"
    )


def test_wiring_registers_the_module_in_framework_and_app_lists(
    tmp_path: Path,
) -> None:
    from sandloom.core.cli.scaffold_wiring import wire_module

    # framework tree: backend/{pyproject.toml, .env.example, src/sandloom/main.py}
    backend = tmp_path / "repo"
    (backend / "src" / "sandloom").mkdir(parents=True)
    (backend / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (backend / "src" / "sandloom" / "main.py").write_text(
        'LOCAL_MODULES: list[str] = ["gadgets"]\n'
    )
    (backend / ".env.example").write_text(
        'SANDLOOM_MODULES=["sandloom_identity","sandloom_tenancy"]\n'
    )
    deploy = tmp_path / "repo" / "deploy"
    (deploy / "compose").mkdir(parents=True)
    (deploy / "compose/monolith.yml").write_text(
        "x-env: &env\n  SANDLOOM_MODULES: '[\"sandloom_identity\"]'\n"
    )
    edited = wire_module("quotas", backend / "src" / "quotas")
    assert {p.name for p in edited} == {
        "main.py",
        ".env.example",
        "monolith.yml",
    }
    assert (
        'LOCAL_MODULES: list[str] = ["gadgets", "quotas"]'
        in (backend / "src" / "sandloom" / "main.py").read_text()
    )
    assert (
        (backend / ".env.example")
        .read_text()
        .startswith(
            'SANDLOOM_MODULES=["sandloom_identity","sandloom_tenancy","quotas"]'
        )
    )
    assert wire_module("quotas", backend / "src" / "quotas") == []

    # application: SANDLOOM_MODULES in every env file and the deploy profile
    app = tmp_path / "shop"
    (app / "deploy").mkdir(parents=True)
    (app / "pyproject.toml").write_text("[project]\nname = 'shop'\n")
    for name in (".env", ".env.example", ".env.test"):
        (app / name).write_text('X=1\nSANDLOOM_MODULES=["shop.modules.a"]\n')
    (app / "deploy" / "docker-compose.yml").write_text(
        "  SANDLOOM_MODULES: '[\"shop.modules.a\"]'\n"
    )
    edited = wire_module("shop.modules.b", app / "shop" / "modules" / "b")
    assert len(edited) == 4
    assert (
        'SANDLOOM_MODULES=["shop.modules.a","shop.modules.b"]'
        in (app / ".env.test").read_text()
    )


@pytest.mark.parametrize("name", ["q", "quotas", "customer_notifications"])
def test_generated_module_passes_the_format_gate(tmp_path: Path, name: str) -> None:
    """The templates wrap name-dependent lines the way `ruff format` does, so
    a fresh module passes the project's format hook whatever its name."""
    import subprocess
    import sys

    created = add.add_module(name, _layout(tmp_path))
    files = [
        str(p) for p in created if p.suffix == ".py" and "migrations" not in p.parts
    ]
    check = [sys.executable, "-m", "ruff", "format", "--check", "--line-length=88"]
    proc = subprocess.run(
        [*check, *files],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_add_module_ships_the_module_contract(tmp_path: Path) -> None:
    """A module generated where no ancestor carries the contract gets it."""
    add.add_module("parcels", _layout(tmp_path))
    base = tmp_path / "parcels"
    assert (base / "AGENTS.md").read_text() == module_contract()
    assert (base / "CLAUDE.md").read_text() == "@AGENTS.md\n"


def test_add_module_does_not_duplicate_an_inherited_contract(tmp_path: Path) -> None:
    """Under an AGENTS.md that carries the contract (the framework tree, an
    ``sandloom new`` app) the module gets no second copy to load."""
    (tmp_path / "AGENTS.md").write_text("# app\n\n" + module_contract())
    add.add_module("parcels", _layout(tmp_path / "pkg"))
    assert not (tmp_path / "pkg" / "parcels" / "AGENTS.md").exists()
    assert not (tmp_path / "pkg" / "parcels" / "CLAUDE.md").exists()


# --------------------------------------------------------------------------- #
# The generated skeleton (C-MODULE-01, G-MODULE-18)
# --------------------------------------------------------------------------- #


def _non_blank(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if ln.strip()]


def test_add_module_generates_a_compliant_parseable_skeleton(tmp_path: Path) -> None:
    import ast

    created = add.add_module("payments", _layout(tmp_path))
    base = tmp_path / "payments"
    rel = {p.relative_to(base).as_posix() for p in created}
    assert {
        "__init__.py",
        "models/__init__.py",
        "models/item.py",
        "schemas.py",
        "services.py",
        "api.py",
        "events.py",
        "migrations/env.py",
        "migrations/versions/0001_payments_baseline.py",
        "tests/test_payments.py",
    } <= rel
    for path in created:
        if path.suffix == ".py":
            ast.parse(path.read_text())  # parseable
    api = (base / "api.py").read_text()
    assert "sqlalchemy" not in api and ".models" not in api  # ORM-free API
    assert 'channel="payments.item.created.v1"' in (base / "events.py").read_text()
    model = (base / "models" / "item.py").read_text()
    assert "(TenantScoped, Base)" in model
    # namespace-portable: the module never names its own dotted package
    for path in created:
        if path.suffix == ".py" and "migrations" not in path.parts:
            assert "myapp.modules.payments" not in path.read_text(), path
    assert "define_module(__name__" in (base / "__init__.py").read_text()


@pytest.mark.parametrize("bad", ["Bad", "1x", "with-dash", "class", ""])
def test_add_module_refuses_bad_names_and_overwrites(tmp_path: Path, bad: str) -> None:
    with pytest.raises(add.GeneratorError):
        add.add_module(bad, _layout(tmp_path))
    add.add_module("dupes", _layout(tmp_path))
    with pytest.raises(add.GeneratorError, match="already exists"):
        add.add_module("dupes", _layout(tmp_path))


_REGISTER_PROBE = """
from fastapi import FastAPI

from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry
import gen_probe.widgets as module

core = CoreFacade(app=FastAPI())
core.register_schema_registry(InMemorySchemaRegistry())
with core.module_registration("gen_probe.widgets") as api:
    desc = module.register(api)
assert isinstance(desc, ModuleDescriptor), desc
assert desc.name == "gen_probe.widgets", desc.name
paths = {getattr(r, "path", "") for r in core.app.routes}
assert "/api/v1/widgets/items" in paths, paths
assert core.get_event_schema("widgets.item.created.v1") is not None
print("ok")
"""


def test_add_module_register_is_declarative(tmp_path: Path) -> None:
    """register is built by define_module and returns a ModuleDescriptor.

    Runs in a subprocess: importing the module registers its example model on
    the shared ``Base.metadata``, which must not leak into this suite.
    """
    import os
    import subprocess
    import sys

    pkg = tmp_path / "gen_probe"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    where = add.Layout(
        modules_dir=pkg,
        modules_package="gen_probe",
        shared_dir=pkg / "shared",
        shared_package="gen_probe.shared",
        source_root=tmp_path,
    )
    add.add_module("widgets", where)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path])}
    proc = subprocess.run(
        [sys.executable, "-c", _REGISTER_PROBE],
        cwd=Path(__file__).resolve().parents[4],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0 and "ok" in proc.stdout, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    ("package", "has_conftest"), [("", True), ("myapp.modules", False)]
)
def test_add_module_boilerplate_is_minimal(
    tmp_path: Path, package: str, has_conftest: bool
) -> None:
    """The framework absorbs per-module boilerplate (pinned sizes): conftest
    absent (apps load the plugin) or a one-line fixture import (framework
    tree), a short env.py, a declarative __init__.py, and no kernel-internal
    import anywhere in the module."""
    from sandloom.core.archcheck.analyzers import (
        find_kernel_internal_test_imports,
        find_legacy_infra_imports,
    )

    if not package:  # the framework tree: top-level modules next to sandloom
        (tmp_path / "sandloom").mkdir()
    where = add.Layout(
        modules_dir=tmp_path,
        modules_package=package,
        shared_dir=tmp_path / "shared",
        shared_package=f"{package or 'sandloom'}.shared",
        source_root=tmp_path,
    )
    created = add.add_module("parcels", where)
    base = tmp_path / "parcels"
    conftest = base / "tests" / "conftest.py"
    assert conftest.exists() is has_conftest
    if has_conftest:
        assert len(conftest.read_text().splitlines()) <= 3
    assert len(_non_blank(base / "migrations" / "env.py")) <= 3
    assert len(_non_blank(base / "__init__.py")) <= 8
    for path in created:
        if path.suffix != ".py":
            continue
        assert find_kernel_internal_test_imports(path) == [], path
        assert find_legacy_infra_imports(path) == [], path
