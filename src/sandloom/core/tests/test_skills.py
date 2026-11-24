"""The skills an app ships stay short routers whose every name exists.

A skill names recipes, ``sandloom`` commands, error codes, sibling skills and
the tier names of the public API; a skill that names one the framework does not
have sends an agent down a dead end. Each check below is a function over the
skill's text, and each has a test that feeds it a broken text.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import pytest

import sandloom
import sandloom.core.cli.add as _add
import sandloom.core.cli.skills as _skills
import sandloom.sdk
import sandloom.sdk.testing
from sandloom.core import error_codes
from sandloom.sdk._prim import recipes

APP_SKILLS = _skills.APP_SKILLS
TEMPLATES = Path(_skills.__file__).resolve().parent / "templates" / "skills"
MAX_LINES = 120
FRONTMATTER = re.compile(
    r"\A---\nname: (?P<name>\S+)\ndescription: (?P<desc>.+)\n---\n"
)


def _text(name: str) -> str:
    return (TEMPLATES / name / "SKILL.md").read_text(encoding="utf-8")


def _recipe_names(text: str) -> set[str]:
    """Recipe names a skill points at: ``recipe `x```, ``recipes `x`, `y```,
    ``sandloom docs recipe x``."""
    found = set(re.findall(r"docs recipe ([a-z][a-z0-9-]*)", text))
    for run in re.findall(
        r"recipes?\s+((?:`[a-z][a-z0-9-]*`[,\s]*(?:and\s+)?)+)", text
    ):
        found.update(re.findall(r"`([a-z][a-z0-9-]*)`", run))
    return found


def _add_subcommands() -> set[str]:
    parser = _add._parser()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            return set(choices)
    raise AssertionError("sandloom add has no subcommands")


def _commands() -> set[str]:
    return {name for _group, entries in sandloom.COMMAND_GROUPS for name, _ in entries}


def _tier2_names() -> set[str]:
    names: set[str] = set()
    for info in pkgutil.iter_modules(sandloom.sdk.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"sandloom.sdk.{info.name}")
        for group in getattr(module, "__after_first__", {}).values():
            names.update(group)
    return names


def name_problems(name: str, text: str) -> list[str]:
    """The recipes, commands, error codes and skills ``text`` (a shipped agent
    file called ``name``) points at that the framework does not have."""
    found: list[str] = []
    for recipe in sorted(_recipe_names(text)):
        if recipes._recipe(recipe) is None:
            found.append(f"{name}: recipe {recipe} does not exist")
    adds = _add_subcommands()
    for sub in re.findall(r"\bsandloom add ([a-z][a-z-]*)", text):
        if sub not in adds:
            found.append(f"{name}: `sandloom add {sub}` does not exist")
    commands = _commands()
    for command in re.findall(r"\bsandloom ([a-z][a-z-]*)", text):
        if command not in commands:
            found.append(f"{name}: `sandloom {command}` does not exist")
    for code in re.findall(r"SANDLOOM-[A-Z]\d{3}", text):
        if error_codes.lookup(code) is None:
            found.append(f"{name}: error code {code} does not exist")
    for sibling in re.findall(r"\bsandloom-[a-z]+(?:-[a-z]+)+", text):
        if sibling not in APP_SKILLS:
            found.append(f"{name}: skill {sibling} does not exist")
    return found


def problems(name: str, text: str) -> list[str]:
    """What is wrong with the skill ``name`` whose text is ``text``."""
    head = FRONTMATTER.match(text)
    if head is None:
        return [f"{name}: no `name` + `description` frontmatter"]
    found: list[str] = []
    if head["name"] != name:
        found.append(f"{name}: frontmatter name is {head['name']}")
    if not head["desc"].startswith("Use when"):
        found.append(f"{name}: the description is not a trigger (`Use when ...`)")
    if len(text.splitlines()) > MAX_LINES:
        found.append(f"{name}: over {MAX_LINES} lines")
    return found + name_problems(name, text)


def test_app_skills_are_the_folders_of_the_templates() -> None:
    on_disk = {p.parent.name for p in TEMPLATES.glob("*/SKILL.md")}
    assert on_disk == set(APP_SKILLS)
    assert len(APP_SKILLS) == len(set(APP_SKILLS))


@pytest.mark.parametrize("name", APP_SKILLS)
def test_every_skill_is_a_short_router_of_names_that_exist(name: str) -> None:
    assert problems(name, _text(name)) == []


#: The other agent files `sandloom new` ships: the module-author contract and
#: the signature sheet appended to the app's AGENTS.md.
OTHER_TEMPLATES = ("module-AGENTS.md", "api-cheatsheet.md")


@pytest.mark.parametrize("name", OTHER_TEMPLATES)
def test_the_other_shipped_agent_files_name_only_what_exists(name: str) -> None:
    text = (TEMPLATES.parent / name).read_text(encoding="utf-8")
    assert name_problems(name, text) == []


def test_every_shipped_markdown_template_is_checked() -> None:
    """A new Markdown template is either a skill or listed in ``OTHER_TEMPLATES``:
    none ships unchecked."""
    shipped = {
        p.relative_to(TEMPLATES.parent).as_posix()
        for p in TEMPLATES.parent.rglob("*.md")
        if "skills" not in p.relative_to(TEMPLATES.parent).parts
    }
    assert shipped == set(OTHER_TEMPLATES)


def test_the_new_module_router_routes_to_every_other_skill() -> None:
    router = _text("sandloom-new-module")
    for other in APP_SKILLS:
        if other != "sandloom-new-module":
            assert f"`{other}`" in router, other


def test_the_tier_counts_a_skill_states_are_the_api_tiers() -> None:
    tier1 = (
        len(sandloom.sdk.__beginner__) + len(sandloom.sdk.testing.__beginner__) + 1
    )  # the `client` fixture
    tier2 = _tier2_names()
    text = _text("sandloom-new-module")
    assert re.search(rf"\b{tier1} names are free in any file", text)
    assert re.search(rf"\b{len(tier2)} more are free only", text)
    for name in tier2:
        assert f"`{name}`" in text, name


def test_the_background_skill_names_the_tier2_task_names() -> None:
    tasks = {"task", "periodic", "open_uow"}
    assert tasks <= _tier2_names()
    assert "unscoped" not in _tier2_names()  # still advanced: the skill says so
    text = _text("sandloom-background-tasks")
    for name in tasks:
        assert f"`{name}`" in text
    assert "advanced every tenant" in text


# --- each check bites: a deliberately broken skill is reported ---------------


GOOD = (
    "---\nname: sandloom-x\ndescription: Use when a thing.\n---\n\n# X\n\n"
    "Run `uv run sandloom add task <m> <t>`, recipe `periodic-task`, "
    "skill `sandloom-new-module` (SANDLOOM-T009).\n"
)


def test_a_good_text_has_no_problem() -> None:
    assert problems("sandloom-x", GOOD) == []


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        ("Run `sandloom add nothing`.\n", "`sandloom add nothing`"),
        ("Run `sandloom frobnicate`.\n", "`sandloom frobnicate`"),
        ("See SANDLOOM-T999.\n", "SANDLOOM-T999"),
        ("Read recipe `no-such-recipe`.\n", "recipe no-such-recipe"),
        ("Load `sandloom-no-such`.\n", "sandloom-no-such"),
    ],
)
def test_a_template_without_frontmatter_is_checked_for_names(
    broken: str, expected: str
) -> None:
    found = name_problems("module-AGENTS.md", broken)
    assert any(expected in line for line in found), found
    assert name_problems("module-AGENTS.md", "Run `sandloom test`.\n") == []


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        (GOOD.replace("name: sandloom-x", "name: other"), "frontmatter name"),
        (GOOD.replace("Use when a thing.", "Does a thing."), "not a trigger"),
        (GOOD + "line\n" * MAX_LINES, "over 120 lines"),
        (GOOD.replace("periodic-task", "no-such-recipe"), "recipe no-such-recipe"),
        (GOOD + "Run `sandloom add nothing`.\n", "`sandloom add nothing`"),
        (GOOD + "Run `sandloom frobnicate`.\n", "`sandloom frobnicate`"),
        (GOOD.replace("T009", "T999"), "SANDLOOM-T999"),
        (GOOD.replace("sandloom-new-module", "sandloom-no-such"), "sandloom-no-such"),
        ("# no frontmatter\n", "no `name` + `description`"),
    ],
)
def test_a_broken_skill_is_reported(broken: str, expected: str) -> None:
    found = problems("sandloom-x", broken)
    assert any(expected in line for line in found), found
