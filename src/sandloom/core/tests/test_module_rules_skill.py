"""The rules no check enforces: one file per rule, compiled into a skill.

``sandloom-module-rules`` is written by ``tools/docs/module_rules.py`` from the
files under ``templates/skills/sandloom-module-rules/rules/``. These tests keep
the compiled ``SKILL.md`` current, keep every rule file in the format the tool
reads, keep the contract (``module-AGENTS.md``) and the index listing the same
rules, and keep the rule files shipping with the skill. Each check has a test
that feeds it a broken input.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest

import sandloom.core.cli.skills as _skills
from sandloom.core.tests.test_skills import problems

ROOT = Path(__file__).resolve().parents[4]
TEMPLATES = Path(_skills.__file__).resolve().parent / "templates"
SKILL_DIR = TEMPLATES / "skills" / "sandloom-module-rules"
RULE_FILES = sorted((SKILL_DIR / "rules").glob("*.md"))
CONTRACT = TEMPLATES / "module-AGENTS.md"
SECTION = "## Rules no check enforces yet"
REPO_TEMPLATES = "src/sandloom/core/cli/templates/"


def _script(name: str) -> ModuleType:
    path = ROOT / "tools" / "docs" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # a dataclass resolves its module by name
    spec.loader.exec_module(module)
    return module


@cache
def tool() -> ModuleType:
    return _script("module_rules")


@cache
def code_check() -> ModuleType:
    return _script("docs_code_check")


GOOD = """\
---
title: A rule
impact: high
tags: one, two
---

The rule in a sentence.

More text.

## Incorrect

```python
x = 1
```

## Correct

```python
x = 2
```
"""

BROKEN = {
    "no front matter": GOOD.removeprefix("---\n"),
    "front matter not closed": GOOD.replace(
        "tags: one, two\n---\n", "tags: one, two\n"
    ),
    "unknown key": GOOD.replace("impact: high", "impact: high\nowner: me"),
    "repeated key": GOOD.replace("impact: high", "impact: high\nimpact: low"),
    "missing key": GOOD.replace("impact: high\n", ""),
    "unknown impact": GOOD.replace("impact: high", "impact: huge"),
    "repeated tag": GOOD.replace("one, two", "one, one"),
    "uppercase tag": GOOD.replace("one, two", "One, two"),
    "long title": GOOD.replace("title: A rule", "title: " + "x" * 101),
    "headings swapped": GOOD.replace("## Incorrect", "## X")
    .replace("## Correct", "## Incorrect")
    .replace("## X", "## Correct"),
    "extra heading": GOOD + "\n## Notes\n\nA note.\n",
    "no first paragraph": GOOD.replace("The rule in a sentence.\n\nMore text.\n\n", ""),
    "long first paragraph": GOOD.replace("The rule in a sentence.", "word " * 60),
    "no Incorrect block": GOOD.replace("```python\nx = 1\n```\n", ""),
    "two blocks": GOOD.replace("x = 1\n```\n", "x = 1\n```\n\n```python\ny = 1\n```\n"),
    "text beside a block": GOOD.replace(
        "## Incorrect\n\n", "## Incorrect\n\nSee below.\n\n"
    ),
    "block not python": GOOD.replace("```python\nx = 1", "```text\nx = 1"),
    "block does not parse": GOOD.replace("x = 1", "x = ("),
    "empty block": GOOD.replace("x = 1", ""),
    "same block twice": GOOD.replace("x = 2", "x = 1"),
}


def test_there_are_rule_files() -> None:
    assert RULE_FILES, "the parametrized tests below would run on nothing"
    assert (SKILL_DIR / "SKILL.md").is_file()


def test_a_good_rule_parses() -> None:
    rule = tool().parse_rule("a-rule", GOOD)
    assert (rule.title, rule.impact, rule.tags) == ("A rule", "high", ("one", "two"))
    assert rule.summary == "The rule in a sentence."
    assert (rule.incorrect, rule.correct) == ("x = 1", "x = 2")


def test_a_code_ok_waiver_beside_a_block_is_allowed() -> None:
    waived = GOOD.replace(
        "```python\nx = 2", "<!-- code-ok: D4 an example -->\n\n```python\nx = 2"
    )
    assert tool().parse_rule("a-rule", waived).correct == "x = 2"


@pytest.mark.parametrize("what", sorted(BROKEN))
def test_a_rule_file_that_breaks_the_format_is_refused(what: str) -> None:
    with pytest.raises(tool().RuleError, match="a-rule"):
        tool().parse_rule("a-rule", BROKEN[what])


@pytest.mark.parametrize("slug", ["A-rule", "a_rule", "-a-rule", "a rule"])
def test_a_rule_file_name_is_a_slug(slug: str) -> None:
    with pytest.raises(tool().RuleError, match="slug"):
        tool().parse_rule(slug, GOOD)


@pytest.mark.parametrize("path", RULE_FILES, ids=lambda p: p.stem)
def test_every_rule_file_follows_the_format(path: Path) -> None:
    tool().parse_rule(path.stem, path.read_text(encoding="utf-8"))


def test_the_compiled_skill_is_current() -> None:
    """Fails when a rule file changed and ``SKILL.md`` was not rewritten."""
    stale = "run `python3 tools/docs/module_rules.py`"
    current = tool().compile_skill(tool().load_rules()) == (
        SKILL_DIR / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert current, f"sandloom-module-rules/SKILL.md is stale: {stale}"
    assert tool().main(["--check"]) == 0


def test_check_fails_on_a_stale_skill_and_write_repairs_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "a-rule.md").write_text(GOOD, encoding="utf-8")
    skill = tmp_path / "SKILL.md"
    assert tool().main(["--check"], rules, skill) == 1  # no SKILL.md yet
    assert "stale" in capsys.readouterr().err
    assert tool().main([], rules, skill) == 0
    assert tool().main(["--check"], rules, skill) == 0
    (rules / "a-rule.md").write_text(
        GOOD.replace("A rule", "Another"), encoding="utf-8"
    )
    assert tool().main(["--check"], rules, skill) == 1  # a rule changed
    skill.write_text("by hand\n", encoding="utf-8")
    assert tool().main(["--check"], rules, skill) == 1  # the index edited by hand


def test_a_broken_rule_file_stops_both_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "bad-rule.md").write_text(BROKEN["unknown impact"], encoding="utf-8")
    for argv in ([], ["--check"]):
        assert tool().main(argv, rules, tmp_path / "SKILL.md") == 1
        assert "bad-rule" in capsys.readouterr().err
    assert not (tmp_path / "SKILL.md").exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert tool().main([], empty, tmp_path / "SKILL.md") == 1  # no rules at all


def test_an_unknown_argument_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert tool().main(["--frobnicate"]) == 2
    assert "Usage" in capsys.readouterr().out


def _index_slugs(text: str) -> set[str]:
    return set(re.findall(r"\]\(rules/([a-z0-9-]+)\.md\)", text))


def test_the_index_links_every_rule_once_by_impact() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    slugs = {p.stem for p in RULE_FILES}
    assert _index_slugs(text) == slugs
    assert len(re.findall(r"\]\(rules/", text)) == len(slugs)
    rules = tool().load_rules()
    compiled = tool().compile_skill(rules[1:])
    assert _index_slugs(compiled) == slugs - {
        rules[0].slug
    }  # the index follows its input
    for rule in rules:  # each rule sits under its own impact heading
        head = f"## {rule.impact.capitalize()} impact"
        section = text.split(head)[1].split("\n## ")[0]
        assert f"(rules/{rule.slug}.md)" in section, rule.slug


def _contract_slugs(text: str) -> set[str]:
    section = text.split(SECTION)[1].split("\n## ")[0]
    return set(re.findall(r"^- `([a-z0-9-]+)`:", section, re.MULTILINE))


def test_the_contract_lists_exactly_the_rule_files() -> None:
    slugs = {p.stem for p in RULE_FILES}
    text = CONTRACT.read_text(encoding="utf-8")
    assert _contract_slugs(text) == slugs
    assert "`sandloom-module-rules`" in text.split(SECTION)[1].split("\n## ")[0]
    one_less = text.replace("- `cap-list-limit`:", "- `cap-list-limit-gone`:")
    assert _contract_slugs(one_less) != slugs  # the check bites on a drifted list


def test_the_skill_ships_with_its_rule_files() -> None:
    files = _skills.skill_files("shop", ("claude", "codex"))
    for directory in (".claude/skills", ".agents/skills"):
        base = f"{directory}/sandloom-module-rules"
        assert files[f"{base}/SKILL.md"].startswith(
            "---\nname: sandloom-module-rules\n"
        )
        assert "<app>" not in files[f"{base}/SKILL.md"]
        assert "in shop" in files[f"{base}/SKILL.md"]
        for path in RULE_FILES:
            assert files[f"{base}/rules/{path.name}"] == path.read_text(
                encoding="utf-8"
            )
        shipped = {k for k in files if k.startswith(f"{base}/")}
        assert len(shipped) == len(RULE_FILES) + 1  # nothing else rides along


def test_a_rule_file_is_managed_like_a_skill(tmp_path: Path) -> None:
    rel = ".claude/skills/sandloom-module-rules/rules/cap-list-limit.md"
    _skills.write_skills(tmp_path, "shop", ("claude",))
    _, hashes = _skills.load_record(tmp_path)
    assert rel in hashes
    (tmp_path / rel).write_text("my own rule\n", encoding="utf-8")
    _, kept = _skills.write_skills(tmp_path, "shop", ("claude",))
    assert tmp_path / rel in kept  # a user's edit is kept
    assert (tmp_path / rel).read_text(encoding="utf-8") == "my own rule\n"
    _skills.write_skills(tmp_path, "shop", ("claude",), force=True)
    assert "Incorrect" in (tmp_path / rel).read_text(encoding="utf-8")


def _named_problems(slug: str, text: str) -> list[str]:
    """The skills' name check (recipes, ``sandloom`` commands, error codes,
    sibling skills that exist) on a rule file, which has no skill front matter."""
    head = f"---\nname: {slug}\ndescription: Use when a rule applies\n---\n"
    return problems(slug, head + text)


@pytest.mark.parametrize("path", RULE_FILES, ids=lambda p: p.stem)
def test_a_rule_names_only_what_exists(path: Path) -> None:
    assert _named_problems(path.stem, path.read_text(encoding="utf-8")) == []


def test_the_name_check_bites_on_a_rule_file() -> None:
    for broken in (
        "See SANDLOOM-T999.",
        "Run `sandloom add nothing`.",
        "Read skill `sandloom-no-such`.",
    ):
        assert _named_problems("a-rule", GOOD + broken + "\n"), broken


@pytest.mark.parametrize("path", RULE_FILES, ids=lambda p: p.stem)
def test_the_examples_pass_the_docs_code_check(path: Path) -> None:
    """D1 (parses) and D2 (every ``sandloom`` import resolves), D4 waived with a reason."""
    rel = f"{REPO_TEMPLATES}skills/sandloom-module-rules/rules/{path.name}"
    hits = code_check().check_text(rel, path.read_text(encoding="utf-8"))
    assert [(h.rule, h.line) for h in hits] == []


def test_a_waiver_is_what_lets_a_long_example_pass() -> None:
    """Without its ``code-ok`` comment a block over three lines is a D4 hit: the
    waivers are load-bearing, not decoration."""
    waived = [
        p for p in RULE_FILES if "<!-- code-ok: D4" in p.read_text(encoding="utf-8")
    ]
    assert waived
    for path in waived:
        text = re.sub(r"<!-- code-ok:.*?-->\n\n", "", path.read_text(encoding="utf-8"))
        rel = f"{REPO_TEMPLATES}skills/sandloom-module-rules/rules/{path.name}"
        assert {h.rule for h in code_check().check_text(rel, text)} == {"D4"}, path.name
