"""``ModuleDescriptor.consumes``: an incomplete SERVICE_BINDINGS fails at boot.

A module declares the sync contracts it calls; after every module registered,
the loader checks each one is provided locally or bound in
``SERVICE_BINDINGS``. Missing required → ``ContractBindingError`` (startup
aborts); missing optional → warning. The outcome is in the load summary
(``/diagnostics/core``).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import ContractRequirement, ModuleDescriptor
from sandloom.core.facade import CoreFacade
from sandloom.core.loader.plugin_loader import (
    ContractBindingError,
    load_module_topology,
    load_modules_with_summary,
)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    consumes: list[Any] | None = None,
    provides: str | None = None,
) -> None:
    mod = types.ModuleType(name)

    def register(core: Any) -> ModuleDescriptor:
        if provides:
            core.register_service(provides, object())
        return ModuleDescriptor(name=name, version="0", consumes=list(consumes or []))

    mod.register = register  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, mod)


def _core(bindings: dict[str, Any] | None = None) -> CoreFacade:
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    cfg = settings.model_copy(update={"service_bindings": bindings or {}})
    core = CoreFacade(app=FastAPI(), settings_obj=cfg)
    core.register_schema_registry(InMemorySchemaRegistry())
    return core


@pytest.fixture(autouse=True)
def _no_entrypoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "module_load_entrypoints", False)


def test_missing_required_consumed_contract_aborts_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        "cx_shop",
        consumes=[ContractRequirement("cx_wallet.balance.v1", reason="checkout")],
    )
    with pytest.raises(ContractBindingError) as exc:
        load_modules_with_summary(_core(), ["cx_shop"])
    assert "cx_wallet.balance.v1" in str(exc.value)
    assert exc.value.missing == [("cx_shop", "cx_wallet.balance.v1", "checkout")]


def test_topology_read_records_a_missing_required_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploy tooling (``sandloom add deploy``) loads one module to derive the
    bindings it is about to write: the missing required contract is the
    output, not a refusal. The application path stays strict."""
    _install(
        monkeypatch,
        "cx_shop",
        consumes=[ContractRequirement("cx_wallet.balance.v1", reason="checkout")],
    )
    summary = load_module_topology(_core(), ["cx_shop"])
    assert summary.success()
    assert summary.consumes["cx_shop"] == [
        {"contract": "cx_wallet.balance.v1", "required": True, "resolution": "missing"}
    ]
    with pytest.raises(ContractBindingError):
        load_modules_with_summary(_core(), ["cx_shop"])


def test_provider_loaded_later_or_binding_satisfies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, "cx_shop", consumes=["cx_wallet.balance.v1"])
    _install(monkeypatch, "cx_wallet", provides="cx_wallet.balance.v1")
    summary = load_modules_with_summary(_core(), ["cx_shop", "cx_wallet"])
    assert summary.consumes["cx_shop"] == [
        {"contract": "cx_wallet.balance.v1", "required": True, "resolution": "local"}
    ]

    bound = _core({"cx_wallet.balance.v1": {"base_url": "http://wallet:2222"}})
    summary = load_modules_with_summary(bound, ["cx_shop"])
    assert summary.consumes["cx_shop"][0]["resolution"] == "remote"
    assert summary.to_dict()["consumes"]["cx_shop"][0]["resolution"] == "remote"


def test_missing_optional_consumed_contract_only_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install(
        monkeypatch,
        "cx_shop",
        consumes=[ContractRequirement("cx_wallet.balance.v1", required=False)],
    )
    with caplog.at_level("WARNING"):
        summary = load_modules_with_summary(_core(), ["cx_shop"])
    assert summary.consumes["cx_shop"][0]["resolution"] == "missing"
    assert any("cx_wallet.balance.v1" in r.getMessage() for r in caplog.records)


def test_shipped_modules_declare_what_they_consume(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Billing alone (personal-only) boots; it consumes membership optionally.
    The default module set resolves it locally. (The sets are derived: a
    module that gains a required contract needs no edit here.)"""
    from sandloom.core.descriptors.module_names import installed_modules
    from sandloom.core.testing.boot import with_required_providers

    DEFAULT_MODULES = [m.path for m in installed_modules()]

    alone = with_required_providers(["sandloom_billing"], DEFAULT_MODULES)
    with caplog.at_level("WARNING"):
        summary = load_modules_with_summary(_core(), alone)
    membership = {
        "contract": "tenancy.membership.v1",
        "required": False,
        "resolution": "local" if "sandloom_tenancy" in alone else "missing",
    }
    assert membership in summary.consumes["sandloom_billing"]
    full = load_modules_with_summary(_core(), list(DEFAULT_MODULES))
    resolutions = {
        e["contract"]: e["resolution"] for e in full.consumes["sandloom_billing"]
    }
    assert resolutions["tenancy.membership.v1"] == "local"
