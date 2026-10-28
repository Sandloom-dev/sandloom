"""Module names, entry-point discovery and the ambiguity rule (SANDLOOM-B020).

A module has an import path (``sandloom_identity``, ``app.modules.notes``)
and a module name (``identity``, ``notes``): the name of the
``sandloom.modules`` entry point whose value is the path, else the path's last
segment. Configuration names a module by either; an ambiguous name is refused
before anything loads.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sandloom.core.descriptors import module_names
from sandloom.core.descriptors.module_names import (
    AmbiguousModuleError,
    InstalledModule,
    module_name,
    resolve_module_names,
)


@dataclass(frozen=True)
class _EP:
    name: str
    value: str
    dist_name: str

    @property
    def dist(self) -> object:
        return type("D", (), {"name": self.dist_name})()


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch):
    """Replace the installed ``sandloom.modules`` entry points."""

    def install(*eps: _EP) -> None:
        monkeypatch.setattr(
            module_names, "_entry_points", lambda: tuple(eps), raising=True
        )
        module_names.refresh_installed_modules()

    yield install
    module_names.refresh_installed_modules()


def test_the_installed_standard_modules_are_discovered() -> None:
    names = {m.name: m.path for m in module_names.installed_modules()}
    assert names["identity"] == "sandloom_identity"
    assert names["tenancy"] == "sandloom_tenancy"
    assert names["billing"] == "sandloom_billing"
    assert module_name("sandloom_identity") == "identity"


def test_module_name_is_the_entry_point_name_else_the_last_segment(installed) -> None:
    installed(_EP("crm", "acme_crm_module", "acme-crm"))
    assert module_name("acme_crm_module") == "crm"
    assert module_name("app.modules.notes") == "notes"
    assert module_name("notes") == "notes"


def test_entry_point_names_and_import_paths_resolve_alike(installed) -> None:
    installed(_EP("crm", "acme_crm", "acme-crm"))
    assert resolve_module_names(["crm", "app.modules.notes", "acme_crm"]) == [
        "acme_crm",
        "app.modules.notes",
        "acme_crm",
    ]


def test_two_packages_providing_one_requested_name_are_refused(installed) -> None:
    installed(
        _EP("crm", "acme_crm", "acme-crm"),
        _EP("crm", "other_crm", "other-crm"),
    )
    with pytest.raises(AmbiguousModuleError) as caught:
        resolve_module_names(["crm"])
    message = str(caught.value)
    assert "SANDLOOM-B020" in message
    assert "acme-crm (acme_crm)" in message and "other-crm (other_crm)" in message
    assert "uninstall" in message
    # Requested by one of the paths: the name is still ambiguous.
    with pytest.raises(AmbiguousModuleError):
        resolve_module_names(["other_crm"])


def test_an_unrequested_duplicate_does_not_break_other_apps(installed) -> None:
    installed(
        _EP("crm", "acme_crm", "acme-crm"),
        _EP("crm", "other_crm", "other-crm"),
    )
    assert resolve_module_names(["app.modules.notes"]) == ["app.modules.notes"]


def test_a_local_module_named_like_an_installed_one_is_refused(installed) -> None:
    installed(_EP("identity", "sandloom_identity", "sandloom-identity"))
    with pytest.raises(AmbiguousModuleError) as caught:
        resolve_module_names(["app.modules.identity"])
    message = str(caught.value)
    assert "app.modules.identity" in message
    assert "sandloom-identity (sandloom_identity)" in message
    assert "uninstall the package or remove the local module" in message


def test_one_distribution_registering_its_module_twice_is_not_ambiguous(
    installed,
) -> None:
    # The same (name, path) seen twice (an editable install next to a wheel
    # metadata copy) is one provider.
    installed(
        _EP("crm", "acme_crm", "acme-crm"),
        _EP("crm", "acme_crm", "acme-crm"),
    )
    assert resolve_module_names(["crm"]) == ["acme_crm"]
    assert list(module_names.installed_modules()) == [
        InstalledModule("crm", "acme_crm", "acme-crm")
    ]


def test_the_error_code_is_catalogued() -> None:
    from sandloom.core import error_codes

    entry = error_codes.lookup("SANDLOOM-B020")
    assert entry is not None
    assert "uninstall" in entry.fix


def test_the_loader_refuses_an_ambiguous_module_before_loading_anything(
    installed, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI

    from sandloom.core.descriptors import ModuleLoadSummary
    from sandloom.core.facade import CoreFacade
    from sandloom.core.loader import plugin_loader

    installed(_EP("identity", "sandloom_identity", "sandloom-identity"))
    imported: list[str] = []
    monkeypatch.setattr(
        plugin_loader.importlib, "import_module", lambda name: imported.append(name)
    )
    with pytest.raises(AmbiguousModuleError, match="SANDLOOM-B020"):
        plugin_loader.load_modules_with_summary(
            CoreFacade(app=FastAPI()), ["identity", "app.modules.identity"]
        )
    assert imported == []
    assert ModuleLoadSummary  # the refusal happens before any summary exists


def test_configuration_names_installed_modules_by_entry_point_name() -> None:
    from sandloom.core.testing.boot import load_topology

    _core, summary = load_topology(["identity", "tenancy"], exact=True)
    assert [d.name for d in summary.loaded] == ["sandloom_identity", "sandloom_tenancy"]
    assert summary.explicit == ["sandloom_identity", "sandloom_tenancy"]


def test_the_kernel_names_no_module() -> None:
    """G-KERNEL-13: no default module list in the kernel's settings, app
    factory, loader or standard root: installed modules are discovered."""
    import ast
    from pathlib import Path

    from sandloom.core.config.settings import Settings

    root = Path(__file__).resolve().parents[2]  # src/sandloom
    names = {"identity", "tenancy", "billing"}
    paths = {f"sandloom_{n}" for n in names} | {f"sandloom.{n}" for n in names}
    sources = [
        *sorted((root / "core" / "config").glob("*.py")),
        root / "core" / "app_factory.py",
        root / "core" / "loader" / "plugin_loader.py",
        root / "main.py",
    ]
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not (literals & (names | paths)), (source, literals & (names | paths))
    for field in ("sandloom_modules", "module_required"):
        factory = Settings.model_fields[field].default_factory
        assert factory is not None and factory() == []  # type: ignore[call-arg]


@pytest.mark.parametrize("explicit", [[], ["app.modules.notes"]])
def test_auto_discovery_refuses_ambiguous_names_before_any_registration(
    installed, monkeypatch: pytest.MonkeyPatch, explicit: list[str]
) -> None:
    from fastapi import FastAPI

    from sandloom.core.config.settings import settings
    from sandloom.core.facade import CoreFacade
    from sandloom.core.loader import plugin_loader

    installed(_EP("crm", "acme_crm", "acme-crm"), _EP("crm", "other_crm", "other-crm"))
    monkeypatch.setattr(settings, "module_load_entrypoints", True)
    monkeypatch.setattr(settings, "module_whitelist", [])
    monkeypatch.setattr(settings, "module_blacklist", [])
    registered: list[str] = []
    monkeypatch.setattr(
        plugin_loader, "_load_list", lambda *args: registered.append("explicit")
    )
    monkeypatch.setattr(
        plugin_loader, "_load_entry_points", lambda *args: registered.append("auto")
    )
    with pytest.raises(AmbiguousModuleError, match="SANDLOOM-B020"):
        plugin_loader.load_modules_with_summary(CoreFacade(app=FastAPI()), explicit)
    assert registered == []
