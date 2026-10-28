"""`sandloom docs recipes` ships the how-tos an application needs (no docs/ in an app)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.sdk._prim.docs import docs_main
from sandloom.sdk._prim.recipes import _RECIPES


def test_recipes_are_listed_and_printed(capsys: pytest.CaptureFixture[str]) -> None:
    assert docs_main(["recipes"]) == 0
    listed = capsys.readouterr().out
    for name in _RECIPES:
        assert name in listed
        assert docs_main(["recipe", name]) == 0
        assert capsys.readouterr().out.startswith(f"# {name}:")


def test_an_unknown_recipe_lists_the_known_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert docs_main(["recipe", "nope"]) == 1
    assert "org-gate" in capsys.readouterr().out


def test_the_app_agents_map_points_at_the_recipes() -> None:
    from sandloom.core.cli.project import _AGENTS_HEADER  # the app AGENTS.md template

    text = _AGENTS_HEADER.template
    assert "sandloom docs recipes" in text
    assert "## Recipes" not in text  # recipes live in sandloom docs, not in the map


#: The recipes ``tools/smoke/recipes_check.py`` runs in a generated app.
VERIFIED = (
    "test-service-or-task",
    "call-contract",
    "projection-from-platform-events",
    "event-v2",
    "periodic-task",
)


def test_the_recipes_check_runs_every_verified_recipe() -> None:
    import ast
    import importlib.util
    import re

    from sandloom.sdk._prim.recipes import recipe_parts

    spec = importlib.util.spec_from_file_location(
        "recipes_check",
        Path(__file__).resolve().parents[2] / "tools/smoke/recipes_check.py",
    )
    assert spec is not None and spec.loader is not None
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    assert set(check.MODULES) == set(VERIFIED)
    for name in VERIFIED:
        steps = recipe_parts(name)
        files = [(what, body) for kind, what, body in steps if kind == "file"]
        assert files, name
        for path, body in files:
            text = check.fill(body, check.MODULES[name])
            assert not re.search(r"<[a-zA-Z]+>", text), (name, path)
            ast.parse(text, filename=path)  # a whole, parseable file
        for kind, command, _ in steps:
            if kind == "command":
                assert command.startswith(
                    ("uv run sandloom add ", "uv run sandloom migrate new ")
                )


def test_recipe_parts_keep_the_text_order() -> None:
    from sandloom.sdk._prim.recipes import recipe_parts

    steps = recipe_parts("projection-from-platform-events")
    order = [what for _kind, what, _body in steps]
    model = order.index("<module>/models/member.py")
    migrate = next(
        i for i, w in enumerate(order) if w.startswith("uv run sandloom migrate")
    )
    assert model < migrate < order.index("<module>/projections.py")


def test_the_split_recipe_commands_are_the_real_cli() -> None:
    """Every ``sandloom add deploy`` line of the ``split`` recipe parses with the
    command it names (the recipe cannot drift from the CLI)."""
    import shlex

    from sandloom.core.cli.scaffold_deploy import _parser

    text = _RECIPES["split"][1]
    lines = [
        shlex.split(line.split("#", 1)[0])
        for line in text.splitlines()
        if line.strip().startswith("uv run sandloom add deploy")
    ]
    assert len(lines) >= 5
    for argv in lines:
        args = _parser().parse_args(argv[4:])
        assert args.modules or args.refresh or args.check, argv
    assert "uv run sandloom keys dev-env --out deploy/.env.microservices" in text
