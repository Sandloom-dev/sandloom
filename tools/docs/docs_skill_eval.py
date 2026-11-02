#!/usr/bin/env python3
"""Prepare and grade re-runnable evaluations of the docs-writing skills.

A scenario in ``design/docs-writing/evals/*.json`` names a page, the commit to
start it from, the commit holding the page the owner approved, what to hide
from the agents, and the checks a result must pass. The agents themselves run
from the ``sandloom-docs-eval`` skill; this script does the two parts that must
be the same every time.

``prepare`` builds a blind copy of the repository: the working tree's tracked
and untracked files (so uncommitted skill edits are tested), minus the
scenario's ``exclude`` prefixes, with the page reset to ``start_from`` and the
``scrub`` patterns removed. ``--skills-ref`` takes the docs skills from another
commit instead, to run the same scenario as a baseline. It fails when a
``leaks`` phrase of the approved page survives in the copy.

``grade`` runs the mechanical checks on a written page and prints the
scenario's judge questions for a reviewer subagent.

Usage::

    python3 tools/docs/docs_skill_eval.py prepare SCENARIO DEST [--skills-ref REF]
    python3 tools/docs/docs_skill_eval.py grade SCENARIO PAGE
    python3 tools/docs/docs_skill_eval.py --self-test

SCENARIO is a path or a name in ``design/docs-writing/evals``. Standard
library only; ``grade`` exits 1 when a check fails.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "design" / "docs-writing" / "evals"
SKILL_DIRS = (".claude/skills", ".agents/skills")
SKILL_PREFIX = "sandloom-docs-"

_FENCE = re.compile(r"^\s*(```|~~~).*?^\s*\1\s*$", re.M | re.S)
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_HEADING = re.compile(r"^\s*#+ .*$", re.M)
_ADMONITION = re.compile(r"^\s*(!!!|\?\?\?|===)[^\n]*$", re.M)
_CARD_TITLE = re.compile(r"^\s*-\s+:[\w-]+:\s+\*\*.*?\*\*\s*$", re.M)
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_TAG = re.compile(r"<[^>]+>")
_CODE = re.compile(r"`[^`]*`")


@dataclass
class Result:
    check: str
    ok: bool
    detail: str


def load(scenario: str) -> dict[str, Any]:
    path = Path(scenario)
    if not path.exists():
        path = EVALS / f"{scenario}.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 -- fixed git subcommands, no shell
        ["git", *args],  # noqa: S607 -- git from PATH, as the other docs tools
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _copy_tree(dest: Path, exclude: list[str]) -> None:
    listed = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for rel in filter(None, listed.split("\0")):
        src = ROOT / rel
        if not src.is_file() or any(rel.startswith(p) for p in exclude):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def _take_skills(dest: Path, ref: str) -> None:
    for skills in SKILL_DIRS:
        for old in (dest / skills).glob(f"{SKILL_PREFIX}*"):
            shutil.rmtree(old)
        names = git("ls-tree", "-r", "--name-only", ref, "--", skills).split()
        for rel in names:
            if Path(rel).parts[2].startswith(SKILL_PREFIX):
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(git("show", f"{ref}:{rel}"), encoding="utf-8")


def _scrub(dest: Path, scrub: dict[str, list[str]]) -> None:
    for rel, patterns in scrub.items():
        path = dest / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            text = re.sub(pattern, "", text, flags=re.S)
        path.write_text(text, encoding="utf-8")


def _leaks(dest: Path, phrases: list[str]) -> list[str]:
    found = []
    for path in dest.rglob("*"):
        if path.suffix not in {".md", ".json", ".html", ".txt", ".yml", ".toml"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        found += [f"{path.relative_to(dest)}: {p!r}" for p in phrases if p in text]
    return found


def prepare(data: dict[str, Any], dest: Path, skills_ref: str | None) -> int:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _copy_tree(dest, data.get("exclude", []))
    if skills_ref:
        _take_skills(dest, skills_ref)
    page = dest / data["page"]
    page.write_text(
        git("show", f"{data['start_from']}:{data['page']}"), encoding="utf-8"
    )
    _scrub(dest, data.get("scrub", {}))
    leaks = _leaks(dest, data.get("leaks", []))
    if leaks:
        print("leaks of the approved page survive in the copy:", *leaks, sep="\n  ")
        return 1
    print(
        f"prepared {dest} from {data['start_from']},",
        f"skills from {skills_ref or 'the working tree'}",
    )
    return 0


def prose(markdown: str) -> str:
    """The page's prose as the entry anatomy counts it: no front matter,
    headings, admonition and tab labels, card titles, code or link targets."""
    text = _FRONT.sub("", markdown)
    for pattern in (_FENCE, _HEADING, _ADMONITION, _CARD_TITLE):
        text = pattern.sub("", text)
    text = _LINK_TARGET.sub("]", text)
    return _TAG.sub(" ", text)


def front_matter(markdown: str) -> set[str]:
    match = _FRONT.match(markdown)
    block = match.group(1) if match else ""
    return {m.group(1) for m in re.finditer(r"^([\w-]+):", block, re.M)}


def grade(data: dict[str, Any], page: Path) -> list[Result]:
    checks = data.get("checks", {})
    markdown = page.read_text(encoding="utf-8")
    body = prose(markdown)
    words = len(body.split())
    results = []
    if "max_prose_words" in checks:
        limit = checks["max_prose_words"]
        results.append(Result("prose words", words <= limit, f"{words} of {limit}"))
    missing = [
        k for k in checks.get("front_matter", []) if k not in front_matter(markdown)
    ]
    results.append(
        Result(
            "front matter", not missing, f"missing {missing}" if missing else "all keys"
        )
    )
    plain = _CODE.sub(" ", body)
    hits = [
        t
        for t in checks.get("absent_terms", [])
        if re.search(rf"\b{re.escape(t)}\b", plain, re.I)
    ]
    results.append(
        Result("absent terms", not hits, f"found {hits}" if hits else "none")
    )
    lacking = [p for p in checks.get("present_phrases", []) if p not in markdown]
    results.append(
        Result(
            "present phrases", not lacking, f"lacking {lacking}" if lacking else "all"
        )
    )
    if checks.get("lint_strict"):
        results.append(_lint(page))
    return results


def _lint(page: Path) -> Result:
    linter = ROOT / "tools" / "docs" / "docs_prose_lint.py"
    if not linter.exists():
        return Result("prose lint", False, "tools/docs/docs_prose_lint.py is missing")
    run = subprocess.run(  # noqa: S603 -- this interpreter, the repository's linter
        [sys.executable, str(linter), str(page)], capture_output=True, text=True
    )
    hits = [line for line in run.stdout.splitlines() if line.strip()]
    return Result(
        "prose lint", run.returncode == 0, f"{len(hits)} line(s)" if hits else "clean"
    )


def report(data: dict[str, Any], results: list[Result]) -> int:
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'}  {r.check}: {r.detail}")
    print(f"\nJudge questions (reference page: {data['page']} at {data['reference']}):")
    for i, question in enumerate(data.get("judge", []), 1):
        print(f"  {i}. {question}")
    return 0 if all(r.ok for r in results) else 1


def _expect(ok: bool, what: str) -> None:
    if not ok:
        raise SystemExit(f"self-test failed: {what}")


def self_test() -> int:
    sample = (
        "---\ntitle: X\njob: j\n---\n\n# Title\n\n"
        "One two three [link](modules/x.md).\n\n"
        '```text\nmodule hidden in code\n```\n\n!!! tip "Start"\n\n    === "Tab"\n\n'
        "        Four `module` five.\n\n-   :material-x: **[Card title](a.md)**\n\n"
        "    Six seven.\n"
    )
    body = prose(sample)
    _expect(len(body.split()) == 9, f"prose words {body.split()}")
    _expect("modules/x.md" not in body, "link target counted")
    _expect("hidden" not in body, "code block counted")
    _expect("Card title" not in body, "card title counted")
    _expect(front_matter(sample) == {"title", "job"}, "front matter keys")
    _expect(not re.search(r"\bmodule\b", _CODE.sub(" ", body)), "inline code as a term")
    print("self-test: ok")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return self_test()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("scenario")
    p.add_argument("dest", type=Path)
    p.add_argument("--skills-ref")
    g = sub.add_parser("grade")
    g.add_argument("scenario")
    g.add_argument("page", type=Path)
    args = parser.parse_args(argv)
    data = load(args.scenario)
    if args.command == "prepare":
        return prepare(data, args.dest, args.skills_ref)
    return report(data, grade(data, args.page))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
