"""A module refuses its configuration at registration, in every entry point.

A module raises ``SystemExit(reason)`` from ``register`` / ``setup``; the
loader turns it into the kernel's :class:`ConfigurationError`, prefixed
``INVALID_CONFIGURATION`` with the module's name. Registration runs wherever
the composition is built (the app, worker, beat, ``sandloom migrate``, the
operator CLIs: ``test_boot_configuration_checks``), so the refusal is the
same everywhere. It is never a module load failure that an optional or
aggregated load could carry on past.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI

from sandloom.core.config.common import INVALID_CONFIGURATION, ConfigurationError
from sandloom.core.config.settings import settings
from sandloom.core.facade import CoreFacade
from sandloom.core.loader.plugin_loader import load_modules_with_summary
from sandloom.core.schema.registry import InMemorySchemaRegistry

_NAME = "refusing_module_probe"


def _install(monkeypatch: pytest.MonkeyPatch, refusal: SystemExit) -> None:
    """Load only a synthetic module whose ``register`` mounts a route and
    then refuses with ``refusal``."""
    module = types.ModuleType(_NAME)

    def register(core: Any) -> None:
        router = APIRouter()
        router.add_api_route("/refused-probe", lambda: None)
        core.include_router(router)
        raise refusal

    module.register = register  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _NAME, module)
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    monkeypatch.setattr(settings, "module_fail_fast", False)
    monkeypatch.setattr(settings, "sandloom_modules", [_NAME])


def _core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    return core


def test_module_refusal_is_the_kernel_configuration_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, SystemExit("FOO_KEY is required with ENVIRONMENT=prod"))
    core = _core()
    with pytest.raises(ConfigurationError) as refused:
        load_modules_with_summary(core)
    assert str(refused.value) == (
        f"{INVALID_CONFIGURATION}module {_NAME}: FOO_KEY is required with ENVIRONMENT=prod"
    )
    # Raised even with aggregated (non fail-fast) loading, and nothing of the
    # module's registration is applied.
    paths = {getattr(r, "path", "") for r in core.app.router.routes}
    assert "/refused-probe" not in paths


def test_an_already_prefixed_refusal_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = f"{INVALID_CONFIGURATION}set FOO"
    _install(monkeypatch, SystemExit(message))
    with pytest.raises(ConfigurationError) as refused:
        load_modules_with_summary(_core())
    assert str(refused.value) == message


def test_a_bare_exit_status_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, SystemExit(3))
    with pytest.raises(
        ConfigurationError, match=r"module refusing_module_probe: exit status 3"
    ):
        load_modules_with_summary(_core())
