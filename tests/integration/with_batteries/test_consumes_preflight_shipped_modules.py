"""``ModuleDescriptor.consumes``: an incomplete SERVICE_BINDINGS fails at boot.

A module declares the sync contracts it calls; after every module registered,
the loader checks each one is provided locally or bound in
``SERVICE_BINDINGS``. Missing required → ``ContractBindingError`` (startup
aborts); missing optional → warning. The outcome is in the load summary
(``/diagnostics/core``).

Moved from ``sandloom/core/tests/test_consumes_preflight.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.config.settings import settings
from sandloom.core.facade import CoreFacade
from sandloom.core.loader.plugin_loader import (
    load_modules_with_summary,
)


def _core(bindings: dict[str, Any] | None = None) -> CoreFacade:
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    cfg = settings.model_copy(update={"service_bindings": bindings or {}})
    core = CoreFacade(app=FastAPI(), settings_obj=cfg)
    core.register_schema_registry(InMemorySchemaRegistry())
    return core


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
