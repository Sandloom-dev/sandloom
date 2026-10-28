"""Every entrypoint resolves the SAME composition root (SANDLOOM_COMPOSITION_ROOT).

`sandloom outbox` / `sandloom streams` / `sandloom projection` used to import `sandloom.main`
unconditionally, so a downstream app (`<app>.asgi`) had its dead letters and
projections operated with the PLATFORM's module set. They now share the
resolution helper with `sandloom-migrate` and the Celery worker.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core import composition
from sandloom.core.facade import CoreFacade


def _fake_root(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[Any, Any]:
    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    root = types.ModuleType(name)
    root.app = app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, root)
    monkeypatch.setenv(composition.COMPOSITION_ROOT_ENV, name)
    return app, core


def test_events_cli_uses_composition_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.cli import events

    _app, core = _fake_root(monkeypatch, "probe_events_root")
    assert events._build_core() is core


def test_projections_cli_uses_composition_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli import projections

    app, _core = _fake_root(monkeypatch, "probe_projection_root")
    assert projections._build_app() is app


def test_all_entrypoints_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core import worker
    from sandloom.core.cli import migrate

    _app, core = _fake_root(monkeypatch, "probe_shared_root")
    assert composition.load_composition_core() is core
    assert worker.load_composition_core() is core
    assert migrate._load_core() is core


def test_default_root_is_sandloom_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(composition.COMPOSITION_ROOT_ENV, raising=False)
    import sandloom.main

    assert composition.composition_root_name() == "sandloom.main"
    assert composition.load_composition_core() is sandloom.main.core


def test_root_without_core_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "probe_empty_root", types.ModuleType("x"))
    monkeypatch.setenv(composition.COMPOSITION_ROOT_ENV, "probe_empty_root")
    with pytest.raises(
        composition.CompositionRootError, match="SANDLOOM_COMPOSITION_ROOT"
    ):
        composition.load_composition_core()
    with pytest.raises(
        composition.CompositionRootError, match="SANDLOOM_COMPOSITION_ROOT"
    ):
        composition.load_composition_app()
