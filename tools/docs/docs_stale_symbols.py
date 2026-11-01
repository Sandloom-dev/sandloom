#!/usr/bin/env python3
"""Fail on names Public API 1.0 removed or renamed, in the prose docs.

Spec: ``tests/fixtures/specs/public-api-1.0-spec.md`` §9.1 ("Stale-symbol lint") and
Appendix C (statuses ``adr0009``, ``renamed``, ``replaced``, ``changed``).

Scanned (tracked Markdown / text files only): ``docs/**`` except
``docs/adr/**``, ``docs/design/**`` and ``docs/archive/**`` (history keeps
the old names); every ``AGENTS.md`` and ``CLAUDE.md``; ``.claude/**``; every
``README.md``; ``deploy/**``; ``benchmarks/dx/**`` except its recorded
``results/`` and the frozen pre-1.0 competitor snapshot.

Waiver: a line that must keep a stale name (a migration note, a changelog
line) carries ``<!-- stale-ok: <reason> -->``. The reason is required.

Usage::

    python3 tools/docs/docs_stale_symbols.py            # scan the repository
    python3 tools/docs/docs_stale_symbols.py PATH ...   # scan these files/dirs
    python3 tools/docs/docs_stale_symbols.py --summary  # counts per top directory
    python3 tools/docs/docs_stale_symbols.py --self-test

Standard library only; exit 1 when a stale name is found.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (name, regex, replacement). Word boundaries treat "_" as a word character,
# so contract_session never matches contract_session_bound and
# operation_class never matches operation_classes (listed on its own).
_IDENT = r"(?<![\w-]){}(?![\w-])"
_DASHED = r"(?<![\w-]){}(?![\w-])"


def _ident(name: str) -> str:
    return _IDENT.format(re.escape(name))


STALE: tuple[tuple[str, str, str], ...] = (
    # Appendix C, status adr0009: removed by ADR-0009 (model E).
    (
        "operation_class",
        _ident("operation_class"),
        "nothing: deny-by-default, risk at its source and the route snapshot (ADR-0009)",
    ),
    (
        "operation_classes",
        _ident("operation_classes"),
        "sandloom.sdk.testing.assert_route_snapshot",
    ),
    (
        "assert_operation_classes",
        _ident("assert_operation_classes"),
        "sandloom.sdk.testing.assert_route_snapshot",
    ),
    (
        "strict_revocation",
        _ident("strict_revocation"),
        "nothing: every authenticated route checks the session live (G-REVOKE-21)",
    ),
    (
        "revocation_freshness",
        _ident("revocation_freshness"),
        "nothing: every authenticated route checks the session live (G-REVOKE-21)",
    ),
    (
        "MAX_REVOCATION_STALENESS_SECONDS",
        _ident("MAX_REVOCATION_STALENESS_SECONDS"),
        "nothing: every authenticated route checks the session live (G-REVOKE-21)",
    ),
    # Appendix C, status renamed.
    ("revocation_at_commit", _ident("revocation_at_commit"), "sandloom.sdk.web.at_commit()"),
    ("db_session", _ident("db_session"), "sandloom.sdk.data.open_uow()"),
    ("make_user_headers", _ident("make_user_headers"), "sandloom.sdk.testing.as_user"),
    ("deliver_event", _ident("deliver_event"), "sandloom.sdk.testing.deliver"),
    # Appendix C, status replaced.
    (
        "contract_session",
        _ident("contract_session"),
        "sandloom.sdk.db() (inside a contract method)",
    ),
    (
        "get_current_user_id",
        _ident("get_current_user_id"),
        "sandloom.sdk.CurrentUser (user.user_id), or sandloom.sdk.web.current_user_id",
    ),
    # Appendix C, status changed: tenant_type + org_param -> of= on the billing atoms.
    (
        "tenant_type=",
        r"(?<!\w)tenant_type\s*=",
        "of=Account.user() / Account.org(param=...) / Account.bound_tenant()",
    ),
    # Spec §9.1 extras.
    (
        "EVENT_SCHEMAS",
        _ident("EVENT_SCHEMAS"),
        'a typed Event class: class E(Event, channel="m.e.v1")',
    ),
    (
        "AUTH_REVOCATION_STRICT_PATH_PREFIXES",
        _ident("AUTH_REVOCATION_STRICT_PATH_PREFIXES"),
        "nothing: every authenticated route is strict (G-REVOKE-21)",
    ),
    (
        "sandloom-scaffold",
        _DASHED.format(re.escape("sandloom-scaffold")),
        "sandloom add module <name>",
    ),
    ("sandloom-migrate", _DASHED.format(re.escape("sandloom-migrate")), "sandloom migrate <command>"),
    (
        "sandloom scaffold",
        r"(?<![\w-])sandloom scaffold\b",
        "sandloom add module <name> / sandloom add saga / sandloom add deploy",
    ),
)

_COMPILED = tuple((name, re.compile(rx), repl) for name, rx, repl in STALE)
_WAIVER = re.compile(r"<!--\s*stale-ok:\s*\S.*?-->")

TEXT_SUFFIXES = {".md", ".markdown", ".mdx", ".txt", ".rst"}
EXEMPT_PREFIXES = (
    "docs/adr/",
    "docs/design/",
    "docs/archive/",
    "benchmarks/dx/results/",
    "benchmarks/dx/competitors/",  # frozen pre-1.0 comparison (d89189b6)
    # Generated pages: their text comes from the ledger (integrator) and from
    # code docstrings and error-code texts (sandloom.core, sandloom.sdk); stale names
    # there are fixed in the source, which `sandloom docs` / the hooks regenerate.
    "docs/reference/guarantees.md",
    "docs/reference/sdk/",
    "docs/reference/error-codes.md",
    # An old -> new mapping by design, and history.
    "docs/operate/upgrade-1.0.md",
    "docs/changelog.md",
)
EXEMPT_NAMES = {"CHANGELOG.md"}


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    name: str
    replacement: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.name} -> {self.replacement}"


def in_scope(rel: str) -> bool:
    """Whether a repository-relative POSIX path is a doc this lint covers."""
    p = Path(rel)
    if p.suffix.lower() not in TEXT_SUFFIXES or p.name in EXEMPT_NAMES:
        return False
    if rel.startswith(EXEMPT_PREFIXES) or "/.claude/worktrees/" in f"/{rel}":
        return False
    if p.name in {"AGENTS.md", "CLAUDE.md", "README.md"}:
        return True
    return rel.startswith(("docs/", ".claude/", "deploy/", "benchmarks/dx/"))


def tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return [f for f in out.split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if d not in {".git", "node_modules", ".venv"}
            ]
            for fn in filenames:
                files.append(Path(dirpath, fn).relative_to(root).as_posix())
        return files


def scan_text(rel: str, text: str) -> Iterator[Hit]:
    for lineno, line in enumerate(text.splitlines(), 1):
        if _WAIVER.search(line):
            continue
        for name, rx, repl in _COMPILED:
            if rx.search(line):
                yield Hit(rel, lineno, name, repl)


def scan(root: Path, rels: Iterable[str]) -> list[Hit]:
    hits: list[Hit] = []
    for rel in rels:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits.extend(scan_text(rel, text))
    return hits


def select(root: Path, args: list[str]) -> list[str]:
    files = [f for f in tracked_files(root) if in_scope(f)]
    if not args:
        return files
    wanted = [Path(a).resolve() for a in args]
    picked = []
    for f in files:
        full = (root / f).resolve()
        if any(full == w or w in full.parents for w in wanted):
            picked.append(f)
    return picked


def self_test() -> int:
    cases = {
        'use `operation_class("W")` here': ["operation_class"],
        "assert_operation_classes(app) and operation_classes(app)": [
            "operation_classes",
            "assert_operation_classes",
        ],
        "contract_session_bound() is kept": [],
        "db_session() and open_uow()": ["db_session"],
        'Subscribed(tenant_type="user")': ["tenant_type="],
        'OrgRole("admin", org_param="org_id") is kept': [],
        "run uv run sandloom-migrate revision x": ["sandloom-migrate"],
        "run uv run sandloom-scaffold notes": ["sandloom-scaffold"],
        "run uv run sandloom scaffold deploy notes": ["sandloom scaffold"],
        "the my-sandloom-migrate-tool and sandloom-migrated are other words": [],
        'sandloom migrate new notes "init"': [],
        "make_user_headers(user_id) <!-- stale-ok: migration note -->": [],
        "make_user_headers(user_id) <!-- stale-ok: -->": ["make_user_headers"],
        "EVENT_SCHEMAS = {...}": ["EVENT_SCHEMAS"],
        'deliver_event("a.b.v1", {})': ["deliver_event"],
        "core.di.revocation_at_commit": ["revocation_at_commit"],
        "Depends(get_current_user_id)": ["get_current_user_id"],
    }
    failed = 0
    for text, expected in cases.items():
        got = sorted(h.name for h in scan_text("x.md", text))
        if got != sorted(expected):
            print(f"self-test FAIL: {text!r}: got {got}, expected {sorted(expected)}")
            failed += 1
    scope = {
        "docs/modules/between/events.md": True,
        "docs/adr/0009-x.md": False,
        "docs/design/spec.md": False,
        "docs/archive/old.md": False,
        "AGENTS.md": True,
        "src/sandloom/AGENTS.md": True,
        ".claude/skills/sandloom-new-module/SKILL.md": True,
        "examples/linkly/README.md": True,
        "deploy/README.md": True,
        "src/sandloom/core/cli.py": False,
        "CHANGELOG.md": False,
        "benchmarks/dx/results/run1/report.md": False,
        "benchmarks/dx/tasks/t1_notes.md": True,
    }
    for rel, expected_in in scope.items():
        if in_scope(rel) != expected_in:
            print(f"self-test FAIL: in_scope({rel!r}) != {expected_in}")
            failed += 1
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "a.md").write_text("ok\nuse strict_revocation()\n", encoding="utf-8")
        hits = scan(Path(tmp), ["a.md"])
        expected_line = (
            "a.md:2: strict_revocation -> nothing: every authenticated route checks "
            "the session live (G-REVOKE-21)"
        )
        if [h.render() for h in hits] != [expected_line]:
            print(f"self-test FAIL: file scan {hits}")
            failed += 1
    print("self-test:", "FAIL" if failed else "ok")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    summary = "--summary" in argv
    args = [a for a in argv if not a.startswith("--")]
    hits = scan(ROOT, select(ROOT, args))
    for h in hits:
        print(h.render())
    if summary:
        per_dir = Counter(
            h.path.split("/", 1)[0] if "/" in h.path else "." for h in hits
        )
        for top, n in sorted(per_dir.items()):
            print(f"{n:6d}  {top}")
    if hits:
        print(
            f"\n{len(hits)} stale Public API name(s) (spec Appendix C). Replace each with "
            "the name after '->', or waive the line with <!-- stale-ok: <reason> -->.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
