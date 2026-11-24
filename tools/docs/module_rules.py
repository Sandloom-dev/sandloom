#!/usr/bin/env python3
"""Compile the module rules no check enforces into the ``sandloom-module-rules`` skill.

The rules live one per file in
``src/sandloom/core/cli/templates/skills/sandloom-module-rules/rules/<slug>.md``::

    ---
    title: A short rule, as a sentence
    impact: high | medium | low
    tags: comma, separated, lowercase
    ---

    The rule and why no check holds it (the first paragraph is its line in the index).

    ## Incorrect

    ```python
    ...
    ```

    ## Correct

    ```python
    ...
    ```

This tool reads them and writes the skill's ``SKILL.md``: an index, one entry per
rule (title, tags, first paragraph, link), grouped by impact. ``sandloom new`` and
``sandloom add skills`` ship the index and the rule files together, so an agent
reads the index first and opens only the rule for the work at hand.

A rule file that breaks the format (an unknown key, a missing or second block, a
block that does not parse, an Incorrect block equal to the Correct one) is
refused with its file name. The Python blocks are also read by
``tools/docs/docs_code_check.py`` (D1 parse, D2 imports resolve).

Usage::

    python3 tools/docs/module_rules.py           # write SKILL.md
    python3 tools/docs/module_rules.py --check   # exit 1 when SKILL.md is stale

Standard library only.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "src/sandloom/core/cli/templates/skills/sandloom-module-rules"
RULES_DIR = SKILL_DIR / "rules"
SKILL = SKILL_DIR / "SKILL.md"

#: The impacts a rule may declare, in the order the index lists them.
IMPACTS = ("high", "medium", "low")
#: The longest first paragraph (the index line): the skill stays a short router.
MAX_SUMMARY = 260
MAX_TITLE = 100
_SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_TAG = re.compile(r"[a-z][a-z0-9-]*")
_KEYS = ("title", "impact", "tags")
_HEADINGS = ("Incorrect", "Correct")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK = re.compile(r"^```([^\n]*)\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)

DESCRIPTION = (
    "Use when you write or review the parts of a Sandloom module that no check "
    "enforces: a table reached through another tenant row, an effect that can run "
    "twice, event order and versions, a contract command, a list route, a password "
    "or role change, a side effect outside the database, or a task. Each rule has an "
    "Incorrect and a Correct example."
)
_INTRO = """\
# Module rules no check enforces in <app>

Nothing fails when you get one of these wrong, so read the rule for the work at
hand before you write it. Each entry links to a file with an Incorrect and a
Correct example. A rule a check does enforce names its code, and
`uv run sandloom explain <code>` gives the fix.
"""
_OUTRO = "Finish with skill `sandloom-quality-gates`.\n"


class RuleError(ValueError):
    """A rule file that breaks the format."""


@dataclass(frozen=True)
class Rule:
    """One rule file, parsed."""

    slug: str
    title: str
    impact: str
    tags: tuple[str, ...]
    summary: str
    incorrect: str
    correct: str


def _front_matter(slug: str, text: str) -> tuple[dict[str, str], str]:
    """``(keys, body)`` of a rule file; refuses a missing, unknown or repeated key."""
    if not text.startswith("---\n"):
        raise RuleError(f"{slug}: no front matter (`---` on the first line)")
    head, closed, body = text[4:].partition("\n---\n")
    if not closed:
        raise RuleError(f"{slug}: the front matter is not closed with `---`")
    keys: dict[str, str] = {}
    for line in head.splitlines():
        key, colon, value = line.partition(":")
        if not colon or key not in _KEYS:
            raise RuleError(f"{slug}: front matter line {line!r}; keys are {_KEYS}")
        if key in keys:
            raise RuleError(f"{slug}: front matter key `{key}` is repeated")
        keys[key] = value.strip()
    missing = [key for key in _KEYS if not keys.get(key)]
    if missing:
        raise RuleError(f"{slug}: front matter lacks {', '.join(missing)}")
    return keys, body


def _example(slug: str, heading: str, section: str) -> str:
    """The one Python block of a ``## <heading>`` section. Only HTML comments (a
    ``code-ok`` waiver) may sit beside it."""
    prose = _COMMENT.sub("", section)
    blocks = list(_BLOCK.finditer(prose))
    if len(blocks) != 1:
        raise RuleError(
            f"{slug}: `## {heading}` needs one code block, has {len(blocks)}"
        )
    block = blocks[0]
    if prose[: block.start()].strip() or prose[block.end() :].strip():
        raise RuleError(f"{slug}: `## {heading}` holds text beside its code block")
    if block[1].strip() != "python":
        raise RuleError(f"{slug}: the `## {heading}` block is not ```python")
    code = block[2].strip("\n")
    if not code.strip():
        raise RuleError(f"{slug}: the `## {heading}` block is empty")
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise RuleError(
            f"{slug}: the `## {heading}` block does not parse: {exc.msg}"
        ) from exc
    return code


def parse_rule(slug: str, text: str) -> Rule:
    """The rule in ``text``, the file ``<slug>.md``; raises :class:`RuleError`."""
    if not _SLUG.fullmatch(slug):
        raise RuleError(f"{slug}: the file name is a lowercase slug (a-z, 0-9, `-`)")
    keys, body = _front_matter(slug, text)
    title = keys["title"]
    if len(title) > MAX_TITLE or "\n" in title:
        raise RuleError(f"{slug}: the title is over {MAX_TITLE} characters")
    if keys["impact"] not in IMPACTS:
        raise RuleError(f"{slug}: impact {keys['impact']!r} is not one of {IMPACTS}")
    tags = tuple(tag.strip() for tag in keys["tags"].split(","))
    if not all(_TAG.fullmatch(tag) for tag in tags) or len(set(tags)) != len(tags):
        raise RuleError(
            f"{slug}: tags are unique lowercase words, got {keys['tags']!r}"
        )
    parts = re.split(r"^## (.*?)[ \t]*$", body, flags=re.MULTILINE)
    intro, sections = parts[0], parts[1:]
    if tuple(sections[0::2]) != _HEADINGS:
        raise RuleError(
            f"{slug}: the headings are {_HEADINGS}, got {tuple(sections[0::2])}"
        )
    paragraphs = [p for p in intro.strip().split("\n\n") if p.strip()]
    summary = " ".join(paragraphs[0].split()) if paragraphs else ""
    if not summary or "```" in summary:
        raise RuleError(f"{slug}: the rule needs a first paragraph of text")
    if len(summary) > MAX_SUMMARY:
        raise RuleError(f"{slug}: the first paragraph is over {MAX_SUMMARY} characters")
    incorrect = _example(slug, "Incorrect", sections[1])
    correct = _example(slug, "Correct", sections[3])
    if incorrect.strip() == correct.strip():
        raise RuleError(f"{slug}: the Incorrect and the Correct block are the same")
    return Rule(slug, title, keys["impact"], tags, summary, incorrect, correct)


def load_rules(directory: Path = RULES_DIR) -> list[Rule]:
    """Every rule of ``directory``, by slug; refuses a directory with none."""
    rules = [
        parse_rule(p.stem, p.read_text(encoding="utf-8"))
        for p in sorted(directory.glob("*.md"))
    ]
    if not rules:
        raise RuleError(f"{directory}: no rule files")
    return rules


def compile_skill(rules: list[Rule]) -> str:
    """The text of ``SKILL.md`` for ``rules``: the index, grouped by impact."""
    out = [
        f"---\nname: sandloom-module-rules\ndescription: {DESCRIPTION}\n---\n\n{_INTRO}"
    ]
    for impact in IMPACTS:
        group = [rule for rule in rules if rule.impact == impact]
        if not group:
            continue
        entries = []
        for rule in group:
            tags = ", ".join(rule.tags)
            link = f"[{rule.title}](rules/{rule.slug}.md)"
            # One line per sentence: a hard wrap would split an inline code span.
            entries.append(f"- {link} ({tags})\n  {rule.summary}")
        out.append(f"## {impact.capitalize()} impact\n\n" + "\n".join(entries) + "\n")
    out.append(_OUTRO)
    return "\n".join(out)


def main(argv: list[str], rules_dir: Path = RULES_DIR, skill: Path = SKILL) -> int:
    """Write ``skill`` from ``rules_dir``, or with ``--check`` fail when it is stale."""
    if set(argv) - {"--check"}:
        print(__doc__)
        return 2
    try:
        text = compile_skill(load_rules(rules_dir))
    except RuleError as exc:
        print(f"module rules: {exc}", file=sys.stderr)
        return 1
    if "--check" in argv:
        current = skill.read_text(encoding="utf-8") if skill.is_file() else ""
        if current != text:
            print(
                f"module rules: {skill.name} is stale; run "
                "`python3 tools/docs/module_rules.py` and commit it",
                file=sys.stderr,
            )
            return 1
        return 0
    skill.write_text(text, encoding="utf-8")
    print(f"wrote {skill}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
