"""``sandloom generate`` writes the files modules declare, never a name of its own.

The console's manifest is one of them (ADR-0012 decision 4): without a module
that declares it there is none, and the kernel names neither the console nor
its file (B-N1, B-N2).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.cli import generated, generation_input
from sandloom.core.descriptors import ModuleDescriptor


def _app(*descriptors: ModuleDescriptor) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            module_summary=SimpleNamespace(loaded=list(descriptors), failed=[])
        )
    )


def _collect(monkeypatch: pytest.MonkeyPatch) -> None:
    source = SimpleNamespace(openapi={}, routes={}, modules=())
    monkeypatch.setattr(generation_input, "collect", lambda app: source)


def test_without_a_declaring_module_there_is_no_file(tmp_path: Path) -> None:
    app = _app(ModuleDescriptor(name="sandloom_identity", version="1"))
    assert generated._declared_files(app, tmp_path) == []


def test_the_declared_file_is_planned_with_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _collect(monkeypatch)
    console = ModuleDescriptor(
        name="demo.console",
        version="1",
        generated_files={"screens.json": lambda source: '{\n  "a": "é"\n}\n'},
    )
    [item] = generated._declared_files(_app(console), tmp_path)
    assert item.path == tmp_path / "screens.json"
    assert item.code == "SANDLOOM-T016"
    assert item.stale
    assert item.content == '{\n  "a": "é"\n}\n'


def test_a_bad_declaration_is_a_problem_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _collect(monkeypatch)

    def refuse(source: Any) -> str:
        raise ValueError("/api/v1/notes: operation is not owned")

    console = ModuleDescriptor(
        name="demo.console", version="1", generated_files={"screens.json": refuse}
    )
    [item] = generated._declared_files(_app(console), tmp_path)
    assert "not owned" in item.problems[0]


def test_the_kernel_names_neither_the_console_nor_its_file() -> None:
    """B-N1, B-N2: the gate and the file name left the generator."""
    source = Path(generated.__file__).read_text(encoding="utf-8")
    assert '"sandloom_admin"' not in source
    assert "admin-manifest.json" not in source
