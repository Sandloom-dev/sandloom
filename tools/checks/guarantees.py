"""The guarantee & capability ledger (``guarantees.yml``).

The ledger lists every guarantee the framework promises and every capability a
user can rely on, each with the tests / ``sandloom check`` rules / import contracts
that prove it. ``tests/architecture/test_guarantee_ledger.py`` keeps it
honest (see ``check``); a simplification wave runs the enforcing tests first::

    uv run python tools/checks/guarantees.py check      # the gate, standalone
    uv run python tools/checks/guarantees.py render     # rewrite docs/reference/guarantees.md
    uv run python tools/checks/guarantees.py nodeids    # the enforcing backend tests
    uv run pytest -m guarantees src                # the same selection, by marker
    uv run python tools/checks/guarantees.py show G-TENANT-01

Pure standard library + PyYAML; imported by the architecture test and by the
``src/conftest.py`` marker hook, so it must stay import-cheap.
"""

from __future__ import annotations

import ast
import configparser
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND
LEDGER = BACKEND / "guarantees.yml"
REFERENCE = REPO / "docs" / "reference" / "guarantees.md"
ADR_DIR = REPO / "docs" / "adr"

ID_RE = re.compile(r"^(?P<prefix>[GC]-[A-Z]+)-(?P<num>\d{2,3})$")
GAP_SIZES = ("S", "M", "L")
INFORMATIONAL = "informational"
#: An Accepted ADR whose decision is not built yet maps its sections to
#: ``planned``; the ADR must be listed under ``planned_adrs`` with the wave
#: that will turn each planned section into ledger ids.
PLANNED = "planned"
#: ADRs from this number on must map every ``##`` / ``###`` heading.
ADR_MIN_NUMBER = 1
ENTRY_KEYS = {
    "id",
    "statement",
    "sources",
    "tests",
    "rules",
    "import_contracts",
    "startup_checks",
    "gap",
    "gap_size",
    "see",
}
REMOVED_KEYS = {"id", "statement", "reason", "approved_by", "date", "replaced_by"}


@dataclass(frozen=True)
class Entry:
    kind: str  # "guarantee" | "capability"
    id: str
    statement: str
    sources: tuple[str, ...]
    tests: tuple[str, ...]
    rules: tuple[str, ...]
    import_contracts: tuple[str, ...]
    startup_checks: str
    gap: str
    gap_size: str
    see: tuple[str, ...]

    @property
    def enforced(self) -> bool:
        return bool(self.tests or self.rules or self.import_contracts)


@dataclass
class Ledger:
    raw: dict[str, Any]
    entries: list[Entry] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    issued: dict[str, int] = field(default_factory=dict)
    adr_sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    planned_adrs: dict[str, str] = field(default_factory=dict)

    def by_id(self) -> dict[str, Entry]:
        return {e.id: e for e in self.entries}


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def load(path: Path = LEDGER) -> Ledger:
    return parse(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def parse(raw: dict[str, Any]) -> Ledger:
    ledger = Ledger(raw=raw)
    for kind, key in (("guarantee", "guarantees"), ("capability", "capabilities")):
        for item in raw.get(key) or []:
            ledger.entries.append(
                Entry(
                    kind=kind,
                    id=str(item.get("id", "")),
                    statement=str(item.get("statement", "") or "").strip(),
                    sources=_tuple(item.get("sources")),
                    tests=_tuple(item.get("tests")),
                    rules=_tuple(item.get("rules")),
                    import_contracts=_tuple(item.get("import_contracts")),
                    startup_checks=str(item.get("startup_checks", "") or "").strip(),
                    gap=str(item.get("gap", "") or "").strip(),
                    gap_size=str(item.get("gap_size", "") or "").strip(),
                    see=_tuple(item.get("see")),
                )
            )
    ledger.removed = list(raw.get("removed") or [])
    ledger.issued = {str(k): int(v) for k, v in (raw.get("issued") or {}).items()}
    ledger.adr_sections = dict(raw.get("adr_sections") or {})
    planned = raw.get("planned_adrs") or {}
    ledger.planned_adrs = {str(k): str(v) for k, v in planned.items()}
    return ledger


# --------------------------------------------------------------------------- #
# Node ids


def split_nodeid(nodeid: str) -> tuple[str, tuple[str, ...]]:
    """``src/x/test_a.py::TestB::test_c[p]`` → (path, ("TestB", "test_c"))."""
    path, *parts = nodeid.split("::")
    return path, tuple(re.sub(r"\[.*\]$", "", p) for p in parts)


@cache
def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_names(tree: ast.Module) -> dict[str, ast.AST]:
    names: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names[(alias.asname or alias.name).split(".")[0]] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = node
    return names


def _class_has(
    cls: ast.ClassDef, name: str, scope: dict[str, ast.AST], depth: int = 0
) -> bool:
    for node in cls.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return True
    if depth > 5:
        return False
    for base in cls.bases:
        resolved = scope.get(base.id) if isinstance(base, ast.Name) else None
        if isinstance(resolved, ast.ClassDef) and _class_has(
            resolved, name, scope, depth + 1
        ):
            return True
        if isinstance(resolved, (ast.Import, ast.ImportFrom)):
            return True  # inherited from an imported base: trust it
    return False


def nodeid_problem(nodeid: str) -> str | None:
    """None if ``nodeid`` names an existing test (file, class or function)."""
    rel, parts = split_nodeid(nodeid)
    path = REPO / rel
    if not rel.endswith(".py") or not path.is_file():
        return f"{nodeid}: file {rel} does not exist"
    if not parts:
        return None
    scope = _top_level_names(_module_tree(path))
    head = scope.get(parts[0])
    if head is None:
        return f"{nodeid}: {parts[0]} is not defined in {rel}"
    if len(parts) == 1:
        return None
    if not isinstance(head, ast.ClassDef):
        return f"{nodeid}: {parts[0]} is not a class"
    if len(parts) > 2:
        return f"{nodeid}: nested test classes are not supported in the ledger"
    if not _class_has(head, parts[1], scope):
        return f"{nodeid}: {parts[0]} has no {parts[1]}"
    return None


def backend_nodeids(ledger: Ledger) -> list[str]:
    """Enforcing tests runnable from the repository root (paths relative to it)."""
    out: list[str] = []
    for entry in ledger.entries:
        for nodeid in entry.tests:
            if nodeid.startswith(("src/", "tests/")) and nodeid not in out:
                out.append(nodeid)
    return out


def ledger_nodeids_for_marker() -> tuple[str, ...]:
    """Backend-relative node ids, for the ``guarantees`` marker hook (never raises)."""
    try:
        return tuple(backend_nodeids(load()))
    except Exception:  # noqa: BLE001 - a broken ledger must not break collection; the gate test reports it
        return ()


def item_matches(item_nodeid: str, ledger_ids: Iterable[str]) -> bool:
    base = re.sub(r"\[.*\]$", "", item_nodeid)
    return any(base == n or base.startswith(n + "::") for n in ledger_ids)


# --------------------------------------------------------------------------- #
# ADR headings


def adr_files(adr_dir: Path = ADR_DIR) -> list[Path]:
    out = []
    for path in sorted(adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md")):
        if int(path.name[:4]) >= ADR_MIN_NUMBER:
            out.append(path)
    return out


def headings(path: Path, levels: tuple[int, ...] = (2, 3)) -> list[str]:
    """``##`` / ``###`` heading texts outside fenced code blocks."""
    out: list[str] = []
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m and len(m.group(1)) in levels:
            out.append(m.group(2).strip())
    return out


def all_headings(path: Path) -> set[str]:
    return set(headings(path, levels=(1, 2, 3, 4, 5, 6)))


# --------------------------------------------------------------------------- #
# The gate


def known_rules() -> set[str]:
    from sandloom.core.archcheck import RULES

    return set(RULES)


def known_import_contracts() -> set[str]:
    parser = configparser.ConfigParser()
    parser.read(BACKEND / ".importlinter", encoding="utf-8")
    return {
        s.removeprefix("importlinter:contract:")
        for s in parser.sections()
        if s.startswith("importlinter:contract:")
    }


def check(ledger: Ledger | None = None, adr_dir: Path = ADR_DIR) -> list[str]:
    """Every problem with the ledger (empty list = green)."""
    ledger = ledger or load()
    problems: list[str] = []
    rules = known_rules()
    contracts = known_import_contracts()

    # -- entries -------------------------------------------------------------
    seen: dict[str, str] = {}
    numbers: dict[str, set[int]] = {}
    for entry in ledger.entries:
        where = entry.id or "<entry without id>"
        m = ID_RE.match(entry.id)
        if not m:
            problems.append(f"{where}: id must look like G-AREA-01 / C-AREA-01")
            continue
        expected = "G" if entry.kind == "guarantee" else "C"
        if entry.id[0] != expected:
            problems.append(f"{where}: a {entry.kind} id starts with {expected}-")
        if entry.id in seen:
            problems.append(f"{where}: duplicate id")
        seen[entry.id] = entry.kind
        numbers.setdefault(m["prefix"], set()).add(int(m["num"]))
        if not entry.statement:
            problems.append(f"{where}: missing statement")
        if not entry.sources:
            problems.append(f"{where}: missing sources")
        for source in entry.sources:
            problems.extend(_source_problems(where, source))
        for nodeid in entry.tests:
            problem = nodeid_problem(nodeid)
            if problem:
                problems.append(f"{where}: enforcing test vanished — {problem}")
        for rule in entry.rules:
            if rule not in rules:
                problems.append(f"{where}: unknown sandloom check rule {rule!r}")
        for contract in entry.import_contracts:
            if contract not in contracts:
                problems.append(f"{where}: unknown import contract {contract!r}")
        if not entry.enforced and not entry.gap:
            problems.append(
                f"{where}: no enforcing test/rule/contract — record it as a gap"
            )
        if entry.gap and entry.gap_size not in GAP_SIZES:
            problems.append(f"{where}: gap_size must be one of {GAP_SIZES}")
        if entry.gap_size and not entry.gap:
            problems.append(f"{where}: gap_size without a gap")
    raw_keys = [
        (item, key)
        for key in ("guarantees", "capabilities")
        for item in ledger.raw.get(key) or []
    ]
    for item, _key in raw_keys:
        extra = set(item) - ENTRY_KEYS
        if extra:
            problems.append(f"{item.get('id')}: unknown keys {sorted(extra)}")
    for entry in ledger.entries:
        for ref in entry.see:
            if ref not in seen:
                problems.append(f"{entry.id}: see: {ref} is not a ledger id")

    # -- removals: an id is never reused and never silently dropped ---------
    removed_ids: set[str] = set()
    for record in ledger.removed:
        rid = str(record.get("id", ""))
        m = ID_RE.match(rid)
        if not m:
            problems.append(f"removed: bad id {rid!r}")
            continue
        extra = set(record) - REMOVED_KEYS
        if extra:
            problems.append(f"removed {rid}: unknown keys {sorted(extra)}")
        for key in ("statement", "reason", "approved_by", "date"):
            if not str(record.get(key, "") or "").strip():
                problems.append(
                    f"removed {rid}: needs a non-empty {key!r} (the owner's approval note "
                    "goes in approved_by)"
                )
        if rid in seen:
            problems.append(f"removed {rid}: id is both live and removed")
        if rid in removed_ids:
            problems.append(f"removed {rid}: duplicate removal record")
        removed_ids.add(rid)
        numbers.setdefault(m["prefix"], set()).add(int(m["num"]))
    for prefix, nums in sorted(numbers.items()):
        top = ledger.issued.get(prefix)
        if top is None:
            problems.append(
                f"issued: no counter for prefix {prefix} (set it to {max(nums)})"
            )
            continue
        for n in range(1, top + 1):
            if n not in nums:
                problems.append(
                    f"{prefix}-{n:02d} was issued but is neither in the ledger nor under "
                    "removed: — deleting an entry needs a removed: record with reason and "
                    "approved_by"
                )
        over = sorted(n for n in nums if n > top)
        if over:
            problems.append(
                f"issued: {prefix} counter {top} is below used number(s) {over}"
            )
    for prefix in sorted(set(ledger.issued) - set(numbers)):
        problems.append(f"issued: counter for unused prefix {prefix}")

    # -- ADR heading coverage -------------------------------------------------
    files = {p.name: p for p in adr_files(adr_dir)}
    for name, path in files.items():
        mapping = ledger.adr_sections.get(name)
        if mapping is None:
            problems.append(
                f"adr_sections: {name} is not mapped — list each ##/### heading with the "
                f"ledger ids that cover it (or {INFORMATIONAL!r})"
            )
            continue
        actual = headings(path)
        for heading in actual:
            if heading not in mapping:
                problems.append(
                    f"adr_sections[{name}]: heading {heading!r} has no ledger entry "
                    f"(list the ids that cover it, or {INFORMATIONAL!r})"
                )
        for heading, value in mapping.items():
            if heading not in actual:
                problems.append(f"adr_sections[{name}]: stale heading {heading!r}")
                continue
            if value == INFORMATIONAL:
                continue
            if value == PLANNED:
                if not ledger.planned_adrs.get(name, "").strip():
                    problems.append(
                        f"adr_sections[{name}][{heading!r}]: {PLANNED!r} needs a "
                        f"planned_adrs[{name!r}] entry naming the wave that builds it"
                    )
                continue
            ids = _tuple(value)
            if not ids:
                problems.append(
                    f"adr_sections[{name}][{heading!r}]: empty — ids or {INFORMATIONAL!r}"
                )
            for ref in ids:
                if ref not in seen:
                    problems.append(
                        f"adr_sections[{name}][{heading!r}]: {ref} is not a ledger id"
                    )
    for name in sorted(ledger.planned_adrs):
        if name not in files:
            problems.append(f"planned_adrs: {name} is not an ADR file")
        elif PLANNED not in (ledger.adr_sections.get(name) or {}).values():
            problems.append(
                f"planned_adrs: {name} has no section marked {PLANNED!r} (stale)"
            )
    for name in sorted(set(ledger.adr_sections) - set(files)):
        problems.append(
            f"adr_sections: {name} is not an ADR file (>= {ADR_MIN_NUMBER:04d})"
        )
    return problems


def _source_problems(where: str, source: str) -> list[str]:
    rel, _, heading = source.partition("#")
    path = REPO / rel
    if not path.is_file():
        return [f"{where}: source file {rel} does not exist"]
    # ADRs are stable decision records: their headings are checked exactly.
    # Living docs (AGENTS.md, guides) are rewritten often — file existence only.
    if heading and path.parent == ADR_DIR and heading not in all_headings(path):
        return [f"{where}: source heading {heading!r} not found in {rel}"]
    return []


# --------------------------------------------------------------------------- #
# Reference page


def _md(text: str) -> str:
    out = text.replace("|", "\\|").replace("\n", " ")
    return out.replace("[", "\\[").replace("]", "\\]")


def _id(entry_id: str) -> str:
    """An entry id that never breaks at its hyphens (`.sl-id`, docs/assets/sandloom.css)."""
    return f'<span class="sl-id">{entry_id}</span>'


def render(ledger: Ledger | None = None) -> str:
    ledger = ledger or load()
    entries = ledger.entries
    guarantees = [e for e in entries if e.kind == "guarantee"]
    capabilities = [e for e in entries if e.kind == "capability"]
    gaps = [e for e in entries if e.gap]
    lines = [
        "---",
        "title: Guarantees & Capabilities",
        "---",
        "",
        "<!-- GENERATED from guarantees.yml by "
        "`uv run python tools/checks/guarantees.py render` — do not edit by hand. -->",
        "",
        "# Guarantees & capabilities ledger",
        "",
        "Every guarantee Sandloom promises and every capability a user can rely on, with the",
        "tests, `sandloom check` rules and import contracts that prove it. The ledger",
        "(`guarantees.yml`) is a CI gate "
        "(`tests/architecture/test_guarantee_ledger.py`): an enforcing test that",
        "disappears, an entry deleted without a `removed:` record, or a new ADR section",
        "without an entry fails the build.",
        "",
        f"**{len(guarantees)} guarantees, {len(capabilities)} capabilities, "
        f"{len(gaps)} recorded gaps.**",
        "",
        "Run exactly the enforcing tests (from the repository root):",
        "",
        "```bash",
        "uv run pytest -m guarantees src",
        "```",
        "",
    ]
    for title, group in (("Guarantees", guarantees), ("Capabilities", capabilities)):
        lines += [f"## {title}", ""]
        prefixes: dict[str, list[Entry]] = {}
        for entry in group:
            prefixes.setdefault(entry.id.rsplit("-", 1)[0], []).append(entry)
        for prefix, items in prefixes.items():
            lines += [
                f"### {prefix}",
                "",
                "| Id | Statement | Enforced by |",
                "| --- | --- | --- |",
            ]
            for entry in items:
                enforcement: list[str] = []
                enforcement += [f"`{_short(t)}`" for t in entry.tests]
                enforcement += [f"rule `{r}`" for r in entry.rules]
                enforcement += [
                    f"import contract `{c}`" for c in entry.import_contracts
                ]
                if entry.gap:
                    enforcement.append(f"**gap ({entry.gap_size})**: {_md(entry.gap)}")
                lines.append(
                    f"| {_id(entry.id)} | {_md(entry.statement)} | {'<br>'.join(enforcement)} |"
                )
            lines.append("")
    lines += ["## Gaps", ""]
    if gaps:
        lines += ["| Id | Size | Missing |", "| --- | --- | --- |"]
        for entry in sorted(
            gaps,
            key=lambda e: (
                GAP_SIZES.index(e.gap_size) if e.gap_size in GAP_SIZES else 9,
                e.id,
            ),
        ):
            lines.append(f"| {_id(entry.id)} | {entry.gap_size} | {_md(entry.gap)} |")
    else:
        lines.append("None.")
    lines += ["", "## Removed", ""]
    if ledger.removed:
        lines += [
            "| Id | Statement | Reason | Approved | Date |",
            "| --- | --- | --- | --- | --- |",
        ]
        for record in ledger.removed:
            lines.append(
                "| {id} | {statement} | {reason} | {approved_by} | {date} |".format(
                    **{
                        k: _md(str(record.get(k, "")))
                        for k in ("id", "statement", "reason", "approved_by", "date")
                    }
                )
            )
    else:
        lines.append("Nothing has been removed.")
    lines += ["", "## Sources", ""]
    lines += ["| Id | Source |", "| --- | --- |"]
    for entry in entries:
        lines.append(f"| {_id(entry.id)} | {'<br>'.join(_md(s) for s in entry.sources)} |")
    return "\n".join(lines) + "\n"


def _short(nodeid: str) -> str:
    path, parts = split_nodeid(nodeid)
    path = path.removeprefix("src/").removeprefix("backend/")
    return "::".join((path, *parts))


# --------------------------------------------------------------------------- #
# CLI


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "check"
    if cmd == "check":
        problems = check()
        for p in problems:
            print(p)
        if not problems:
            ledger = load()
            print(f"ledger ok: {len(ledger.entries)} entries")
        return 1 if problems else 0
    if cmd == "render":
        REFERENCE.write_text(render(), encoding="utf-8")
        print(f"wrote {REFERENCE.relative_to(REPO)}")
        return 0
    if cmd == "nodeids":
        print("\n".join(backend_nodeids(load())))
        return 0
    if cmd == "show":
        entries = load().by_id()
        for ident in argv[1:]:
            entry = entries.get(ident)
            if entry is None:
                print(f"{ident}: not in the ledger")
                continue
            print(yaml.safe_dump(entry.__dict__, sort_keys=False, allow_unicode=True))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
