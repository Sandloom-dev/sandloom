"""``sandloom generate`` writes the console's manifest, only for a console that exists."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.cli import generated


def _app(*modules: str) -> Any:
    entries = [SimpleNamespace(name=name) for name in modules]
    return SimpleNamespace(
        state=SimpleNamespace(app_manifest=SimpleNamespace(modules=entries))
    )


def test_without_the_console_there_is_no_manifest(tmp_path: Path) -> None:
    assert generated._admin_manifest(_app("sandloom_identity"), tmp_path) == []


def test_the_manifest_is_stable_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(generated, "compile_app", lambda app: {"version": 1, "a": "é"})
    [item] = generated._admin_manifest(_app("sandloom_admin"), tmp_path)
    assert item.path == tmp_path / "admin-manifest.json"
    assert item.code == "SANDLOOM-T016"
    assert item.stale
    assert item.content == '{\n  "a": "é",\n  "version": 1\n}\n'
    assert json.loads(item.content)["version"] == 1


def test_a_bad_declaration_is_a_problem_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(app: Any) -> None:
        raise ValueError("/api/v1/notes: operation is not owned")

    monkeypatch.setattr(generated, "compile_app", refuse)
    [item] = generated._admin_manifest(_app("sandloom_admin"), tmp_path)
    assert "not owned" in item.problems[0]
