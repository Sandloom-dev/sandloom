"""Module-declared generated files: the kernel collects data, the module renders.

ADR-0012 decision 4 (A-M2): a module declares a file on its registration; the
kernel hands the renderer data of the composed app, never a module.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.cli import generation_input
from sandloom.core.descriptors import GenerationInput, ModuleDescriptor


async def _listing() -> list[str]:
    return []


def _app(*descriptors: Any, failed: tuple[str, ...] = ()) -> FastAPI:
    app = FastAPI()
    _listing.__module__ = "demo.catalog.api"
    app.add_api_route("/api/v1/catalog", _listing, methods=["GET"])
    app.state.module_summary = SimpleNamespace(
        loaded=list(descriptors), failed=list(failed)
    )
    return app


def _module(name: str, **files: Any) -> ModuleDescriptor:
    return ModuleDescriptor(name=name, version="0.1.0", generated_files=dict(files))


def _resources(monkeypatch: pytest.MonkeyPatch, **declared: Any) -> list[str]:
    """``<package>.admin`` of each package in ``declared``; absent otherwise."""
    imported: list[str] = []

    def import_module(name: str) -> Any:
        imported.append(name)
        package = name.removesuffix(".admin")
        if package not in declared:
            raise ModuleNotFoundError(name=name)
        return SimpleNamespace(RESOURCES=declared[package])

    monkeypatch.setattr(generation_input.importlib, "import_module", import_module)
    return imported


def test_the_renderer_gets_data_of_the_composed_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resources(monkeypatch, **{"demo.catalog": [{"name": "items"}]})
    seen: list[GenerationInput] = []

    def render(source: GenerationInput) -> str:
        seen.append(source)
        return "rendered\n"

    app = _app(_module("demo.console", **{"console.json": render}), _module("demo.catalog"))
    [item] = generation_input.planned(app, Path("/shared"))
    assert item == (Path("/shared/console.json"), "rendered\n", [])
    [source] = seen
    assert [(m.package, m.name, m.admin_resources) for m in source.modules] == [
        ("demo.console", "console", None),
        ("demo.catalog", "catalog", [{"name": "items"}]),
    ]
    assert source.routes[("/api/v1/catalog", "GET")] == "demo.catalog.api"
    assert "/api/v1/catalog" in source.openapi["paths"]


def test_without_a_declaration_nothing_is_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = _resources(monkeypatch)
    assert generation_input.planned(_app(_module("demo.catalog")), Path(".")) == []
    assert imported == []


def test_a_broken_declaration_is_never_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(name: str) -> None:
        raise ModuleNotFoundError(name="missing_dependency_inside_declaration")

    monkeypatch.setattr(generation_input.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        generation_input.collect(_app(_module("demo.catalog")))


def test_a_partially_failed_composition_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resources(monkeypatch)
    app = _app(_module("demo.console", **{"c.json": lambda s: ""}), failed=("x",))
    [item] = generation_input.declared(app)
    assert "partially failed composition" in item.problems[0]


@pytest.mark.parametrize(
    "files, problem",
    [
        ({"../escape.json": lambda s: ""}, "not a plain file name"),
        ({"events": lambda s: ""}, "kernel's own generated files"),
        ({"c.json": lambda s: 1}, "returned no text"),
    ],
)
def test_a_bad_declaration_is_a_problem_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, Any], problem: str
) -> None:
    _resources(monkeypatch)
    [item] = generation_input.declared(_app(_module("demo.console", **files)))
    assert problem in item.problems[0]


def test_a_renderer_that_refuses_names_its_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resources(monkeypatch)

    def refuse(source: GenerationInput) -> str:
        raise ValueError("/api/v1/notes: operation is not owned")

    [item] = generation_input.declared(_app(_module("demo.console", **{"c.json": refuse})))
    assert item.problems == ["demo.console: /api/v1/notes: operation is not owned"]


def test_two_modules_declaring_one_file_both_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resources(monkeypatch)
    app = _app(
        _module("demo.one", **{"c.json": lambda s: "1"}),
        _module("demo.two", **{"c.json": lambda s: "2"}),
    )
    files = generation_input.declared(app)
    assert [f.problems for f in files] == [["c.json is declared by demo.one and demo.two"]] * 2


def test_admin_export_renders_the_declared_file_and_checks_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core import composition
    from sandloom.core.cli.admin import main

    _resources(monkeypatch)
    app = _app(_module("demo.console", **{"console.json": lambda s: "{}\n"}))
    monkeypatch.setattr(composition, "load_composition_app", lambda: app)
    output = tmp_path / "admin.json"  # the only declared file, under any name
    assert main(["export", "--output", str(output)]) == 0
    assert output.read_text() == "{}\n"
    assert main(["export", "--output", str(output), "--check"]) == 0
    output.write_text("[]")
    assert main(["export", "--output", str(output), "--check"]) == 1


def test_admin_export_without_a_declaring_module_says_what_to_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandloom.core import composition
    from sandloom.core.cli.admin import main

    _resources(monkeypatch)
    monkeypatch.setattr(
        composition, "load_composition_app", lambda: _app(_module("demo.catalog"))
    )
    assert main(["export", "--output", str(tmp_path / "m.json")]) == 1
    assert "compose the console module" in capsys.readouterr().err
