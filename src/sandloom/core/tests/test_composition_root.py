"""Every entrypoint resolves the SAME composition root (SANDLOOM_COMPOSITION_ROOT).

`sandloom outbox` / `sandloom streams` / `sandloom projection` used to import `sandloom.main`
unconditionally, so a downstream app (`<app>.asgi`) had its dead letters and
projections operated with the PLATFORM's module set. They now share the
resolution helper with `sandloom-migrate` and the Celery worker.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core import composition
from sandloom.core.config.common import ConfigurationError
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
    # The framework suite composes sandloom_standard.main when it is installed
    # (conftest.py); the settings snapshot took it too.
    monkeypatch.setattr(composition.settings, "sandloom_composition_root", None)
    assert composition.composition_root_name() == "sandloom.main"
    try:
        import sandloom.main
    except ConfigurationError as exc:
        # The lean root assigns no claim authorities: with a token issuer
        # installed (the batteries) it refuses the boot (SANDLOOM-B035) and
        # says what to set, instead of an app whose every login is refused.
        assert "SANDLOOM-B035" in str(exc)
        assert "SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main" in str(exc)
        return
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


# --- the project default (``[tool.sandloom] composition-root``) -------------------


def _project(tmp_path: Path, table: str | bytes) -> Path:
    data = table.encode("utf-8") if isinstance(table, str) else table
    (tmp_path / "pyproject.toml").write_bytes(data)
    return tmp_path


def _no_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(composition.COMPOSITION_ROOT_ENV, raising=False)
    monkeypatch.setattr(composition.settings, "sandloom_composition_root", None)


def test_project_default_is_read_only_for_a_caller_that_gives_its_project_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runtime entrypoints and the pytest suites call without a project
    root: they never boot the project's default."""
    _no_selection(monkeypatch)
    project = _project(tmp_path, '[tool.sandloom]\ncomposition-root = "probe.main"\n')
    assert composition.composition_root_name(project_root=project) == "probe.main"
    assert composition.composition_root_name() == "sandloom.main"


def test_project_default_loses_to_every_explicit_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_selection(monkeypatch)
    project = _project(tmp_path, '[tool.sandloom]\ncomposition-root = "probe.main"\n')
    monkeypatch.setattr(
        composition.settings, "sandloom_composition_root", "dotenv.root"
    )
    assert composition.composition_root_name(project_root=project) == "dotenv.root"
    monkeypatch.setenv(composition.COMPOSITION_ROOT_ENV, "env.root")
    assert composition.composition_root_name(project_root=project) == "env.root"
    assert (
        composition.composition_root_name("arg.root", project_root=project)
        == "arg.root"
    )


@pytest.mark.parametrize(
    "text",
    [
        None,  # no pyproject.toml
        "[project]\nname = 'x'\n",  # no [tool.sandloom]
        "[tool.sandloom]\nsources = {}\n",  # a generated app's table: no key
    ],
)
def test_a_project_without_the_key_keeps_the_lean_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str | None
) -> None:
    _no_selection(monkeypatch)
    if text is not None:
        _project(tmp_path, text)
    assert composition.composition_root_name(project_root=tmp_path) == "sandloom.main"


@pytest.mark.parametrize(
    "text",
    [
        "[tool.sandloom]\ncomposition-root = 3\n",
        '[tool.sandloom]\ncomposition-root = "  "\n',
        '[tool.sandloom]\ncomposition-root = "../evil"\n',
        '[tool.sandloom]\ncomposition-root = ".rel"\n',
        '[tool.sandloom]\ncomposition-root = "/etc/passwd"\n',
        '[tool.sandloom]\ncomposition-root = "a b"\n',
        '[tool.sandloom]\ncomposition-root = "x\\ny"\n',
        '[tool.sandloom]\ncomposition-root = "pkg."\n',
        "[tool.sandloom\n",  # not TOML
        b"[tool.sandloom]\ncomposition-root = '\xff\xfe'\n",  # not UTF-8
        'tool = "x"\n',  # `tool` is not a table
        "[tool]\nsandloom = 3\n",  # `tool.sandloom` is not a table
    ],
    ids=[
        "int",
        "blank",
        "parent-path",
        "relative-dot",
        "absolute-path",
        "whitespace",
        "newline",
        "trailing-dot",
        "not-toml",
        "not-utf8",
        "tool-not-table",
        "sandloom-not-table",
    ],
)
def test_a_malformed_project_default_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str | bytes
) -> None:
    _no_selection(monkeypatch)
    project = _project(tmp_path, text)
    with pytest.raises(composition.CompositionRootError, match="composition-root"):
        composition.composition_root_name(project_root=project)


def test_this_checkouts_project_default_is_the_standard_composition() -> None:
    """The repository's own ``pyproject.toml`` names the root that selects
    drivers, which ``sandloom generate`` and ``sandloom test`` build."""
    root = Path(__file__).resolve().parents[4]
    assert composition.project_composition_root(root) == "sandloom_standard.main"
