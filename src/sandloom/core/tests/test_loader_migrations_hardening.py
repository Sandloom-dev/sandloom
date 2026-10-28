"""Loader dependency handling + migration targeting/locking/CLI resolution."""

from __future__ import annotations

import threading
import time
import types
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import (
    CapabilityRequirement,
    MigrationTargetError,
    ModuleDescriptor,
    ModuleLoadSummary,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.loader import plugin_loader
from sandloom.core.loader.plugin_loader import ModuleLoadError, _load_list
from sandloom.core.migrations.migrations import (
    MigrationDependencyError,
    check_chain_collisions,
    version_table_for,
)
from sandloom.core.migrations.urls import to_sync_url


# --------------------------------------------------------------------------- #
# Loader: depends_on + fail-fast
# --------------------------------------------------------------------------- #
def _fake_modules(
    monkeypatch: pytest.MonkeyPatch, specs: dict[str, dict[str, Any]]
) -> list[str]:
    """Install fake importable modules; spec: {name: {deps, fail, optional}}."""
    calls: list[str] = []

    def make(name: str, spec: dict[str, Any]) -> types.ModuleType:
        mod = types.ModuleType(name)

        def register(core: Any) -> ModuleDescriptor:
            calls.append(name)
            if spec.get("fail"):
                raise RuntimeError(f"{name} boom")
            return ModuleDescriptor(
                name=spec.get("desc_name", name),
                version="1",
                optional=spec.get("optional", False),
                depends_on=list(spec.get("deps", [])),
            )

        mod.register = register  # type: ignore[attr-defined]
        return mod

    modules = {n: make(n, s) for n, s in specs.items()}
    real_import = plugin_loader.importlib.import_module

    def fake_import(name: str) -> Any:
        if name in modules:
            return modules[name]
        return real_import(name)

    monkeypatch.setattr(plugin_loader.importlib, "import_module", fake_import)
    monkeypatch.setattr(settings, "module_fail_fast", False)
    monkeypatch.setattr(settings, "module_required", [])
    return calls


def test_dependent_of_failed_module_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_modules(
        monkeypatch,
        {"x.base": {"fail": True}, "x.child": {"deps": ["x.base"]}},
    )
    summary = ModuleLoadSummary()
    _load_list(["x.base", "x.child"], CoreFacade(app=FastAPI()), summary)
    assert [d.name for d in summary.loaded] == []
    failed = {f.name: f.error for f in summary.failed}
    assert "x.child" in failed and "failed to load" in failed["x.child"]


def test_required_dependent_of_failed_module_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_modules(
        monkeypatch,
        {"x.base": {"fail": True}, "x.child": {"deps": ["base"]}},
    )
    monkeypatch.setattr(settings, "module_required", ["x.child"])
    with pytest.raises(ModuleLoadError, match="x.child"):
        plugin_loader.load_modules_with_summary(
            CoreFacade(app=FastAPI()), ["x.base", "x.child"]
        )


def test_dependency_listed_later_is_deferred(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_modules(monkeypatch, {"x.child": {"deps": ["x.base"]}, "x.base": {}})
    summary = ModuleLoadSummary()
    _load_list(["x.child", "x.base"], CoreFacade(app=FastAPI()), summary)
    assert [d.name for d in summary.loaded] == ["x.base", "x.child"]
    assert summary.failed == []


def test_dependency_not_deployed_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Microservice subset: the dependency simply isn't co-deployed.
    _fake_modules(monkeypatch, {"x.child": {"deps": ["x.elsewhere"]}})
    summary = ModuleLoadSummary()
    _load_list(["x.child"], CoreFacade(app=FastAPI()), summary)
    assert [d.name for d in summary.loaded] == ["x.child"]


def test_dependency_cycle_fails_both(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_modules(monkeypatch, {"x.a": {"deps": ["x.b"]}, "x.b": {"deps": ["x.a"]}})
    summary = ModuleLoadSummary()
    _load_list(["x.a", "x.b"], CoreFacade(app=FastAPI()), summary)
    assert summary.loaded == []
    assert {f.name for f in summary.failed} == {"x.a", "x.b"}


def test_fail_fast_aborts_on_optional_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_modules(monkeypatch, {"x.opt": {"fail": True}, "x.ok": {}})
    monkeypatch.setattr(settings, "module_fail_fast", True)
    with pytest.raises(RuntimeError, match="x.opt boom"):
        _load_list(["x.opt", "x.ok"], CoreFacade(app=FastAPI()), ModuleLoadSummary())


def test_a_module_declared_optional_never_aborts_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_modules(
        monkeypatch,
        {"x.base": {"fail": True}, "x.extra": {"deps": ["base"], "optional": True}},
    )
    monkeypatch.setattr(settings, "module_required", ["x.extra"])
    summary = plugin_loader.load_modules_with_summary(
        CoreFacade(app=FastAPI()), ["x.base", "x.extra"]
    )
    assert {f.name: f.required for f in summary.failed} == {
        "x.base": False,
        "x.extra": False,
    }


def test_apply_module_defaults_only_fills_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "sandloom_modules", [])
    monkeypatch.setattr(settings, "module_required", ["keep.me"])
    plugin_loader.apply_module_defaults(["a.b"], ["a.b"])
    assert settings.sandloom_modules == ["a.b"]
    assert settings.module_required == ["keep.me"]


# --------------------------------------------------------------------------- #
# Descriptor migration target + chain collisions
# --------------------------------------------------------------------------- #
def test_database_instances_single_target() -> None:
    both = ModuleDescriptor(
        name="m",
        version="1",
        capabilities=[
            CapabilityRequirement("database"),
            CapabilityRequirement("database", instance="analytics"),
        ],
    )
    # Uses both, tables in default: the chain is NOT duplicated into analytics.
    assert both.database_instances() == {"default"}

    explicit = ModuleDescriptor(
        name="m",
        version="1",
        migrations_database="analytics",
        capabilities=[CapabilityRequirement("database")],
    )
    assert explicit.database_instances() == {"analytics"}


def test_optional_database_never_hosts_chain() -> None:
    desc = ModuleDescriptor(
        name="m",
        version="1",
        capabilities=[
            CapabilityRequirement("database", instance="reporting", required=False)
        ],
    )
    assert desc.database_instances() == {"default"}


def test_ambiguous_databases_require_explicit_target() -> None:
    desc = ModuleDescriptor(
        name="m",
        version="1",
        capabilities=[
            CapabilityRequirement("database", instance="a"),
            CapabilityRequirement("database", instance="b"),
        ],
    )
    with pytest.raises(MigrationTargetError, match="migrations_database"):
        desc.database_instances()


def test_chain_collision_detected() -> None:
    a = ModuleDescriptor(name="sandloom_billing", version="1", migrations_path="/x")
    b = ModuleDescriptor(
        name="myapp.modules.billing", version="1", migrations_path="/y"
    )
    with pytest.raises(MigrationDependencyError, match="alembic_version_billing"):
        check_chain_collisions([a, b])
    b.migrations_chain = "myapp_billing"
    check_chain_collisions([a, b])
    assert version_table_for(a) == "alembic_version_billing"  # back-compat
    assert version_table_for(b) == "alembic_version_myapp_billing"


def test_list_loading_rejects_a_descriptor_name_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GLM review: two listed packages registering the SAME descriptor name
    # both loaded (only the import name was compared), so one module's
    # descriptor shadowed the other's in every name-keyed lookup.
    _fake_modules(
        monkeypatch,
        {"x.one": {"desc_name": "x.shared"}, "x.two": {"desc_name": "x.shared"}},
    )
    summary = ModuleLoadSummary()
    _load_list(["x.one", "x.two"], CoreFacade(app=FastAPI()), summary)
    assert [d.name for d in summary.loaded] == ["x.shared"]
    failed = {f.name: f.error for f in summary.failed}
    assert "x.two" in failed and "x.shared" in failed["x.two"]


def test_loader_rejects_colliding_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = ModuleLoadSummary()
    summary.loaded.append(
        ModuleDescriptor(name="sandloom_billing", version="1", migrations_path="/x")
    )
    desc = ModuleDescriptor(
        name="myapp.modules.billing", version="1", migrations_path="/y"
    )
    with pytest.raises(plugin_loader.ModuleRegistrationError, match="migration"):
        plugin_loader._check_chain_collision(desc, summary)


# --------------------------------------------------------------------------- #
# Migration lock (real PostgreSQL)
# --------------------------------------------------------------------------- #
def _sync_url() -> str:
    import os
    from pathlib import Path

    from sandloom.core.testing.workers import worker_env_values

    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    raw = os.getenv("DATABASE_URL") or str(
        worker_env_values(env_test).get("DATABASE_URL") or settings.database_url
    )
    return to_sync_url(raw).replace("@localhost:", "@127.0.0.1:")


def test_migration_lock_serialises_concurrent_runs() -> None:
    from sqlalchemy import create_engine, pool, text

    from sandloom.core.migrations import alembic_support
    from sandloom.core.migrations.migrations import MIGRATION_LOCK_KEY, migration_lock

    url = _sync_url()
    probe = create_engine(url, poolclass=pool.NullPool)
    try:
        with probe.connect():
            pass
    except Exception as exc:  # pragma: no cover  # noqa: BLE001 - test Postgres unreachable: skip
        probe.dispose()
        pytest.skip(f"Postgres not reachable: {exc}")
    try:
        with migration_lock([url]):
            assert url in alembic_support.LOCKED_URLS
            with probe.connect() as conn:
                got = conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY}
                ).scalar()
                assert got is False  # another runner would block here
        assert url not in alembic_support.LOCKED_URLS

        # A second runner waits for the first to finish.
        order: list[str] = []
        release = threading.Event()

        def first() -> None:
            with migration_lock([url]):
                order.append("first-acquired")
                release.wait(5)
                order.append("first-released")

        t = threading.Thread(target=first)
        t.start()
        while "first-acquired" not in order:
            time.sleep(0.01)
        timer = threading.Timer(0.3, release.set)
        timer.start()
        with migration_lock([url]):
            order.append("second-acquired")
        t.join()
        assert order == ["first-acquired", "first-released", "second-acquired"]
    finally:
        probe.dispose()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_upgrade_all_rejects_non_head_rev(capsys: pytest.CaptureFixture[str]) -> None:
    from sandloom.core.cli.migrate import main

    assert main(["upgrade-all", "--rev", "abc123"]) == 2
    assert "single chain" in capsys.readouterr().err


def test_cli_uses_composition_root_module_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With SANDLOOM_MODULES unset the CLI must see create_app's default set
    (every installed module)."""
    from sandloom.core.cli import migrate

    fake_root = types.ModuleType("fake_root")
    core = CoreFacade(app=FastAPI())
    core.module_descriptors = [ModuleDescriptor(name="sandloom_identity", version="1")]
    fake_root.core = core  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "fake_root", fake_root)
    monkeypatch.setenv(migrate.COMPOSITION_ROOT_ENV, "fake_root")
    assert migrate._load_core() is core

    # The default root is sandloom.main, i.e. exactly create_app's resolution.
    monkeypatch.delenv(migrate.COMPOSITION_ROOT_ENV)
    import sandloom.main

    assert migrate._load_core() is sandloom.main.core
    names = {d.name for d in sandloom.main.core.module_descriptors}
    from sandloom.core.descriptors.module_names import installed_modules

    standard = {m.path for m in installed_modules()}
    assert standard <= names or settings.sandloom_modules
