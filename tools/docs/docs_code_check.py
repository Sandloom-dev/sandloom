#!/usr/bin/env python3
"""Fail on Python in the docs that does not parse, import or come from a tested file.

Rules:

- **D1**  an inline ``python`` / ``py`` fence does not parse with ``ast.parse``.
  A bare ``...`` is valid Python; a block that is deliberately a fragment (a
  signature, a partial class) carries ``<!-- code-ok: D1 fragment ... -->``.
- **D2**  an import in an inline Python fence does not resolve:
  ``import sandloom...`` / ``from sandloom... import X`` (and the module
  packages ``sandloom_<name>``) is imported, and ``X`` must be an attribute or
  a submodule. A ``sandloom_<name>`` package that is not installed is an app's
  or a third party's and is skipped. Runs against the code, so the check runs
  in the project environment: ``uv run python tools/docs/docs_code_check.py``.
- **D3**  a ``--8<--`` include (in any fence, or on its own line) names a file
  that does not exist, or a line range or section (``# --8<-- [start:name]``)
  that is missing or empty.
- **D4**  an inline Python block of more than 3 non-blank lines: page code is
  tested only when it is an ``--8<--`` include of a file under
  ``TESTED_PREFIXES`` (``docs/snippets/**``, ``examples/**``). A Python include
  from anywhere else is a hit too.

Scanned: the pages of ``docs_prose_lint.py`` (tracked ``docs/**/*.md`` except
``docs/adr/**``, ``docs/design/**``, ``docs/archive/**`` and generated pages:
their code is fixed in the docstrings and generators they come from).

Waiver: ``<!-- code-ok: <rule>[,<rule>] <reason> -->`` on the line directly
before the fence (or before a bare include line); further HTML comment lines may
sit between them. The reason is required and covers the whole block.

Ratchet: the docs predate D4, so ``docs_code_check_baseline.json`` holds the
number of D4 hits per file. The check fails when a count rises above its
baseline or a file without an entry has hits, and reports a count that dropped.
D1, D2 and D3 have no baseline: every hit fails. ``--update-baseline`` only
lowers counts and drops zeros; ``--reset-baseline`` rewrites it from the
current hits, for a reviewed change of the rules only.

Usage::

    uv run python tools/docs/docs_code_check.py                    # ratchet check
    uv run python tools/docs/docs_code_check.py PATH ...           # strict
    uv run python tools/docs/docs_code_check.py --summary          # counts
    uv run python tools/docs/docs_code_check.py --update-baseline  # lower counts
    python3 tools/docs/docs_code_check.py --self-test              # no venv needed

Standard library only (D2 imports the project's packages); exit 1 on a hit
(strict) or a count above its baseline, 2 when ``sandloom`` is not importable.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import textwrap
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().with_name("docs_code_check_baseline.json")

MAX_UNTESTED_LINES = 3
PYTHON_LANGS = frozenset(["python", "py", "python3"])
TESTED_PREFIXES = ("docs/snippets/", "examples/")
BASELINE_RULES = frozenset(["D4"])

# The scope of tools/docs/docs_prose_lint.py (test_docs_site.py keeps them equal).
EXEMPT_PREFIXES = ("docs/adr/", "docs/design/", "docs/archive/")
GENERATED_PREFIXES = (
    "docs/snippets/generated/",  # tools/docs/docs_generated.py (SNIPPETS)
    "docs/reference/sdk/",  # tools/docs/docs_reference.py
    "docs/reference/error-codes.md",  # tools/docs/docs_reference.py
    "docs/reference/configuration.md",  # tools/docs/gen_config_reference.py
    "docs/reference/configuration-tiers.md",  # tools/docs/gen_config_reference.py
    "docs/reference/routes.md",  # tools/docs/gen_reference_pages.py
    "docs/reference/guarantees.md",  # tools/checks/guarantees.py render
)
_GENERATED_HEADER = re.compile(r"^\s*<!--\s*generated\b", re.IGNORECASE)

_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_WAIVER = re.compile(
    r"<!--\s*code-ok:\s*([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\s+(\S[^>]*?)\s*-->"
)
_INCLUDE = re.compile(r"^--8<--\s+(['\"])(.+?)\1\s*$")
_INCLUDE_BLOCK = re.compile(r"^--8<--\s*$")
_RANGES = re.compile(r"^\d*(?::\d*)?(?:,\d*(?::\d*)?)*$")
_IMPORT_ROOT = re.compile(r"^sandloom(?:_\w+)?$")
_IMPORT_LINE = re.compile(r"^\s*(?:from\s+\S+\s+import\b|import\s+\S)")

# (module, name or None) -> an error message, or None when it resolves.
Resolver = Callable[[str, str | None], str | None]


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


@dataclass
class _Block:
    """A fenced block, or a bare include line outside one."""

    line: int  # the fence (or include) line, 1-based
    lang: str
    body: list[tuple[int, str]] = field(default_factory=list)  # (line, dedented)
    waived: frozenset[str] = frozenset()

    @property
    def code(self) -> str:
        return textwrap.dedent("\n".join(t for _, t in self.body))

    @property
    def includes(self) -> list[tuple[int, str]]:
        return _includes(self.body)[0]

    @property
    def is_include(self) -> bool:
        """Every non-blank line is part of an include."""
        covered = _includes(self.body)[1]
        lines = [n for n, t in self.body if t.strip()]
        return bool(lines) and all(n in covered for n in lines)


def _includes(body: list[tuple[int, str]]) -> tuple[list[tuple[int, str]], set[int]]:
    """(line, target) of each include (``--8<-- "x"`` or a ``--8<--`` block),
    and the lines the includes take up."""
    out: list[tuple[int, str]] = []
    covered: set[int] = set()
    in_block = False
    for n, raw in body:
        text = raw.strip()
        if _INCLUDE_BLOCK.match(text):
            in_block = not in_block
            covered.add(n)
        elif in_block:
            covered.add(n)
            if text and not text.startswith(";"):
                out.append((n, text))
        elif m := _INCLUDE.match(text):
            covered.add(n)
            if not m.group(2).startswith(";"):
                out.append((n, m.group(2)))
    return out, covered


def _waivers_before(lines: list[str], idx: int) -> frozenset[str]:
    """Rules waived by the HTML comment lines directly above ``lines[idx]``."""
    rules: set[str] = set()
    i = idx - 1
    while i >= 0 and lines[i].strip().startswith("<!--"):
        for w in _WAIVER.finditer(lines[i]):
            rules.update(r.strip() for r in w.group(1).split(","))
        i -= 1
    return frozenset(rules)


def _front_matter_end(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    return next((i + 1 for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)


def blocks(text: str) -> list[_Block]:
    """The fenced blocks of a page, and its include lines outside a fence."""
    lines = text.splitlines()
    out: list[_Block] = []
    open_: tuple[_Block, str, int, int] | None = None  # block, char, len, indent
    for idx in range(_front_matter_end(lines), len(lines)):
        line, lineno = lines[idx], idx + 1
        m = _FENCE.match(line)
        if open_ is not None:
            block, char, size, indent = open_
            if (
                m
                and m.group(2)[0] == char
                and len(m.group(2)) >= size
                and not m.group(3).strip()
            ):
                open_ = None
                continue
            strip = min(indent, len(line) - len(line.lstrip(" ")))
            block.body.append((lineno, line[strip:]))
        elif m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
            info = m.group(3).strip().lstrip("{").lstrip(".")
            lang = (info.split() or [""])[0].rstrip("}").lower()
            block = _Block(lineno, lang, waived=_waivers_before(lines, idx))
            out.append(block)
            open_ = (block, m.group(2)[0], len(m.group(2)), len(m.group(1)))
        elif line.strip().startswith("--8<--"):
            out.append(
                _Block(lineno, "", [(lineno, line)], _waivers_before(lines, idx))
            )
    return out


def _syntax_error(code: str) -> SyntaxError | None:
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return exc
    return None


def _import_nodes(code: str) -> list[tuple[int, ast.Import | ast.ImportFrom]]:
    """(offset line, node) of each import; a block that does not parse gives
    the import statements that do, one by one."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        out: list[tuple[int, ast.Import | ast.ImportFrom]] = []
        lines = code.splitlines()
        for i, line in enumerate(lines):
            if not _IMPORT_LINE.match(line):
                continue
            stmt = line.strip()
            j = i
            while "(" in stmt and ")" not in stmt and j + 1 < len(lines):
                j += 1
                stmt += " " + lines[j].strip()
            if _syntax_error(stmt) is None:
                node = ast.parse(stmt).body[0]
                if isinstance(node, ast.Import | ast.ImportFrom):
                    out.append((i, node))
        return out
    return [
        (node.lineno - 1, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]


def _imports(code: str) -> list[tuple[int, str, str | None]]:
    """(offset line, module, name or None) of each ``sandloom*`` import."""
    out: list[tuple[int, str, str | None]] = []
    for offset, node in _import_nodes(code):
        if isinstance(node, ast.Import):
            out.extend(
                (offset, a.name, None)
                for a in node.names
                if _IMPORT_ROOT.match(a.name.split(".")[0])
            )
        elif (
            node.module
            and not node.level
            and _IMPORT_ROOT.match(node.module.split(".")[0])
        ):
            out.extend((offset, node.module, a.name) for a in node.names)
    return out


def resolve_import(module: str, name: str | None) -> str | None:
    """Import ``module`` (and ``name`` from it) in this interpreter."""
    top = module.split(".")[0]
    if top != "sandloom" and importlib.util.find_spec(top) is None:
        return None  # an app's or a third party's module package
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 - any import failure is the finding
        return f"cannot import {module} ({type(exc).__name__}: {exc})"
    if name is None or name == "*" or hasattr(mod, name):
        return None
    try:
        importlib.import_module(f"{module}.{name}")
    except ImportError:
        return f"{module} has no {name}"
    return None


def _cached(resolve: Resolver) -> Resolver:
    cache: dict[tuple[str, str | None], str | None] = {}

    def run(module: str, name: str | None) -> str | None:
        key = (module, name)
        if key not in cache:
            cache[key] = resolve(module, name)
        return cache[key]

    return run


def _target(root: Path, spec: str) -> tuple[Path, str]:
    """An include's file and its selector (a line range or section names)."""
    path, _, selector = spec.partition(":")
    return root / path, selector


def _include_error(root: Path, spec: str) -> str | None:
    if re.match(r"^[a-z]+://", spec):
        return None
    path, selector = _target(root, spec)
    if not path.is_file():
        return f'"{spec}": no such file'
    if not selector:
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if _RANGES.match(selector):
        picked: list[str] = []
        for part in selector.split(","):
            start_s, _, end_s = part.partition(":")
            start = int(start_s) if start_s else 1
            end = int(end_s) if end_s else len(lines)
            if start > len(lines) or end < start:
                return f'"{spec}": lines {part} outside the {len(lines)}-line file'
            picked.extend(lines[start - 1 : end])
        return None if any(t.strip() for t in picked) else f'"{spec}": empty range'
    for name in selector.split(","):
        opened = next((i for i, t in enumerate(lines) if f"[start:{name}]" in t), None)
        if opened is None:
            return f'"{spec}": no section marker --8<-- [start:{name}]'
        end = next(
            (i for i, t in enumerate(lines) if i > opened and f"[end:{name}]" in t),
            len(lines),
        )
        if not any(t.strip() for t in lines[opened + 1 : end]):
            return f'"{spec}": section {name} is empty'
    return None


def _check_block(
    rel: str, block: _Block, root: Path, resolve: Resolver, hits: list[Hit]
) -> None:
    def add(line: int, rule: str, message: str) -> None:
        if rule not in block.waived:
            hits.append(Hit(rel, line, rule, message))

    includes = block.includes
    for line, spec in includes:
        if (error := _include_error(root, spec)) is not None:
            add(line, "D3", error)
    if block.lang not in PYTHON_LANGS:
        return
    if block.is_include:
        untested = [s for _, s in includes if not s.startswith(TESTED_PREFIXES)]
        if untested:
            add(block.line, "D4", f'include of an untested file "{untested[0]}"')
    else:
        _check_python(block, resolve, add)


def _check_python(
    block: _Block, resolve: Resolver, add: Callable[[int, str, str], None]
) -> None:
    """D1, D2 and D4 on an inline Python block."""
    first = block.body[0][0] if block.body else block.line + 1
    code = block.code
    if (err := _syntax_error(code)) is not None:
        line = first + (err.lineno or 1) - 1
        add(line, "D1", f"does not parse: {err.msg}")
    for offset, module, name in _imports(code):
        if (error := resolve(module, name)) is not None:
            add(first + offset, "D2", error)
    size = sum(1 for _, t in block.body if t.strip())
    if size > MAX_UNTESTED_LINES:
        add(
            block.line,
            "D4",
            f"inline Python block of {size} lines "
            f"(max {MAX_UNTESTED_LINES}; include a tested file)",
        )


def check_text(
    rel: str, text: str, root: Path = ROOT, resolve: Resolver = resolve_import
) -> list[Hit]:
    """Every hit in one page, waived ones removed."""
    hits: list[Hit] = []
    for block in blocks(text):
        _check_block(rel, block, root, resolve, hits)
    return hits


def in_scope(rel: str, text: str | None = None) -> bool:
    """Whether a repository-relative POSIX path is a page this check covers."""
    if not rel.startswith("docs/") or not rel.endswith(".md"):
        return False
    if rel.startswith(EXEMPT_PREFIXES + GENERATED_PREFIXES):
        return False
    if text is None:
        return True
    lines = text.splitlines()
    first = next((ln for ln in lines[_front_matter_end(lines) :] if ln.strip()), "")
    return not _GENERATED_HEADER.match(first)


def tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "docs"],  # noqa: S607 - git from PATH, as CI has it
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return [f for f in out.split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        return [
            p.relative_to(root).as_posix()
            for p in sorted((root / "docs").rglob("*.md"))
        ]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def scan(root: Path, rels: Iterable[str], resolve: Resolver) -> list[Hit]:
    """Hits in the in-scope pages among ``rels``."""
    hits: list[Hit] = []
    for rel in rels:
        text = _read(root / rel)
        if text is not None and in_scope(rel, text):
            hits.extend(check_text(rel, text, root, resolve))
    return hits


def scan_paths(root: Path, args: list[str], resolve: Resolver) -> list[Hit]:
    """Strict mode: a named file is checked whatever its scope (a draft may be
    untracked); a named directory contributes its in-scope pages."""
    hits: list[Hit] = []
    for arg in args:
        path = Path(arg).resolve()
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        if path.is_file():
            text = _read(path)
            if text is not None:
                hits.extend(check_text(rel, text, root, resolve))
        elif path.is_dir():
            rels = [
                p.relative_to(root).as_posix()
                for p in sorted(path.rglob("*.md"))
                if root in p.parents
            ]
            hits.extend(scan(root, rels, resolve))
        else:
            print(f"{arg}: no such file or directory", file=sys.stderr)
    return hits


def counts(hits: Iterable[Hit]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for h in hits:
        rules = out.setdefault(h.path, {})
        rules[h.rule] = rules.get(h.rule, 0) + 1
    return {p: dict(sorted(r.items())) for p, r in sorted(out.items())}


def load_baseline(path: Path) -> dict[str, dict[str, int]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {p: dict(r) for p, r in data.get("counts", {}).items()}


def write_baseline(path: Path, table: dict[str, dict[str, int]]) -> None:
    """Write the D4 counts; D1-D3 never enter the baseline."""
    table = {
        p: {r: n for r, n in rules.items() if n and r in BASELINE_RULES}
        for p, rules in table.items()
    }
    data = {
        "schema": 1,
        "note": (
            "D4 hits (inline Python blocks of more than 3 lines) per file of "
            "tools/docs/docs_code_check.py. Entries only shrink: lower them with "
            "--update-baseline, never raise them by hand."
        ),
        "counts": {p: dict(sorted(r.items())) for p, r in sorted(table.items()) if r},
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def compare(
    current: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> tuple[list[tuple[str, str, int, int]], list[tuple[str, str, int, int]]]:
    """(over, dropped): (file, rule, now, baseline) above and below the baseline."""
    over, dropped = [], []
    for path in sorted(set(current) | set(baseline)):
        cur, base = current.get(path, {}), baseline.get(path, {})
        for rule in sorted(set(cur) | set(base)):
            n, b = cur.get(rule, 0), base.get(rule, 0)
            if n > b:
                over.append((path, rule, n, b))
            elif n < b:
                dropped.append((path, rule, n, b))
    return over, dropped


def lowered(
    current: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """The baseline with every count lowered to the current one; never raised."""
    return {
        path: {
            rule: min(b, current.get(path, {}).get(rule, 0))
            for rule, b in rules.items()
        }
        for path, rules in baseline.items()
    }


def _fake_resolve(module: str, name: str | None) -> str | None:
    known = {"sandloom.sdk": {"db", "on", "Event"}, "sandloom.sdk.web": set()}
    if module not in known:
        return f"cannot import {module} (ModuleNotFoundError)"
    if name is None or name in known[module]:
        return None
    return f"{module} has no {name}"


def _self_test_rules(root: Path) -> int:
    py = "```python\n{}\n```\n"
    long = "a = 1\nb = 2\nc = 3\nd = 4"
    cases: dict[str, list[str]] = {
        # D1
        py.format("x = 1"): [],
        py.format("class A:\n    ..."): [],
        py.format("def f(a, b) -> None"): ["D1"],
        "<!-- code-ok: D1 fragment, a signature -->\n"
        + py.format("def f(a, b) -> None"): [],
        "<!-- code-ok: D1 -->\n" + py.format("def f("): ["D1"],
        "<!-- code-ok: D4 wrong rule -->\n" + py.format("def f("): ["D1"],
        "```bash\nif then fi (\n```\n": [],
        "```text\n```python\n```\n": [],
        "````markdown\n```python\ndef f(\n```\n````\n": [],
        "- Item:\n\n    ```python\n    x = 1\n    ```\n": [],
        '=== "A"\n\n    ```py\n        def f(\n    ```\n': ["D1"],
        "```{.python}\ndef f(\n```\n": ["D1"],
        "---\ntitle: x\n---\n" + py.format("x = 1"): [],
        # D2
        py.format("from sandloom.sdk import db, on"): [],
        py.format("from sandloom.sdk import nope"): ["D2"],
        py.format("import sandloom.sdk.web"): [],
        py.format("import sandloom.gone"): ["D2"],
        py.format("from sandloom_x.y import z"): ["D2"],
        py.format("from os import path\nfrom .models import Note"): [],
        py.format("from sandloom.sdk import (\n    db,\n    nope,\n)\ndef f("): [
            "D1",
            "D2",
            "D4",
        ],
        "<!-- code-ok: D1,D2 fragment, an import that moved -->\n"
        + py.format("from sandloom.sdk import nope\ndef f("): [],
        # D3
        py.format('--8<-- "docs/snippets/a.py"'): [],
        py.format('--8<-- "docs/snippets/missing.py"'): ["D3"],
        py.format('--8<-- "docs/snippets/a.py:2:3"'): [],
        py.format('--8<-- "docs/snippets/a.py:9:12"'): ["D3"],
        py.format('--8<-- "docs/snippets/a.py:4:4"'): ["D3"],
        py.format('--8<-- "docs/snippets/a.py:part"'): [],
        py.format('--8<-- "docs/snippets/a.py:none"'): ["D3"],
        py.format('--8<-- "docs/snippets/a.py:empty"'): ["D3"],
        py.format('--8<-- ";docs/snippets/missing.py"'): [],
        py.format("--8<--\ndocs/snippets/a.py\ndocs/snippets/missing.py\n--8<--"): [
            "D3"
        ],
        '--8<-- "CHANGELOG.md"\n': [],
        '--8<-- "NOPE.md"\n': ["D3"],
        '<!-- code-ok: D3 rendered elsewhere -->\n--8<-- "NOPE.md"\n': [],
        '```yaml\n--8<-- "nope.yml"\n```\n': ["D3"],
        'Use `--8<-- "x"` in a page.\n': [],
        # D4
        py.format("a = 1\nb = 2\n\nc = 3"): [],
        py.format(long): ["D4"],
        "<!-- code-ok: D4 a shape, not a program -->\n" + py.format(long): [],
        "<!-- prose-ok: K3 x -->\n<!-- code-ok: D4 a shape -->\n" + py.format(long): [],
        "<!-- code-ok: D4 a shape -->\n\n" + py.format(long): ["D4"],
        py.format('--8<-- "CHANGELOG.md"'): ["D4"],
        py.format('--8<-- "examples/x.py"\n--8<-- "docs/snippets/a.py"'): [],
        "```bash\n" + long + "\n```\n": [],
    }
    failed = 0
    for text, expected in cases.items():
        got = sorted(h.rule for h in check_text("docs/x.md", text, root, _fake_resolve))
        if got != sorted(expected):
            print(f"self-test FAIL: {text!r}: got {got}, expected {sorted(expected)}")
            failed += 1
    return failed


def _self_test_files(root: Path) -> None:
    snippets = root / "docs" / "snippets"
    snippets.mkdir(parents=True)
    (snippets / "a.py").write_text(
        "# --8<-- [start:part]\nx = 1\n# --8<-- [end:part]\n\n"
        "# --8<-- [start:empty]\n# --8<-- [end:empty]\n",
        encoding="utf-8",
    )
    (root / "examples").mkdir()
    (root / "examples" / "x.py").write_text("x = 1\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")


def _self_test_scope() -> int:
    failed = 0
    scope = {
        ("docs/index.md", "# Sandloom\n"): True,
        ("docs/adr/0009-x.md", ""): False,
        ("docs/design/spec.md", ""): False,
        ("docs/snippets/generated/README.md", ""): False,
        ("docs/reference/sdk/web.md", ""): False,
        ("docs/reference/testing.md", "# Testing\n"): True,
        ("docs/reference/x.md", "---\ntitle: x\n---\n\n<!-- Generated by y -->\n"): (
            False
        ),
        ("README.md", ""): False,
    }
    for (rel, text), expected_in in scope.items():
        if in_scope(rel, text) != expected_in:
            print(f"self-test FAIL: in_scope({rel!r}) != {expected_in}")
            failed += 1
    hits = check_text(
        "docs/x.md", "# T\n\nRun:\n\n```python\nx = (\n```\n", ROOT, _fake_resolve
    )
    if [h.render() for h in hits] != [
        "docs/x.md:6: D1 does not parse: '(' was never closed"
    ]:
        print(f"self-test FAIL: render {hits}")
        failed += 1
    return failed


def _self_test_baseline() -> int:
    failed = 0
    base = {"a.md": {"D4": 2}, "b.md": {"D4": 1}}
    cur = {"a.md": {"D4": 3}, "c.md": {"D1": 1}}
    over, dropped = compare(cur, base)
    if over != [("a.md", "D4", 3, 2), ("c.md", "D1", 1, 0)] or dropped != [
        ("b.md", "D4", 0, 1)
    ]:
        print(f"self-test FAIL: compare {over} {dropped}")
        failed += 1
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "b.json")
        write_baseline(path, lowered(cur, base))
        if load_baseline(path) != {"a.md": {"D4": 2}}:
            print(f"self-test FAIL: baseline round trip {load_baseline(path)}")
            failed += 1
        write_baseline(path, cur)
        if load_baseline(path) != {"a.md": {"D4": 3}}:
            print(f"self-test FAIL: D1 entered the baseline {load_baseline(path)}")
            failed += 1
    return failed


def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _self_test_files(root)
        failed = _self_test_rules(root)
    failed += _self_test_scope() + _self_test_baseline()
    print("self-test:", "FAIL" if failed else "ok")
    return 1 if failed else 0


def _summary(hits: list[Hit]) -> None:
    for rule, n in sorted(Counter(h.rule for h in hits).items()):
        print(f"{n:6d}  {rule}")
    print()
    for path, n in Counter(h.path for h in hits).most_common(15):
        print(f"{n:6d}  {path}")


def _strict(args: list[str], summary: bool) -> int:
    hits = scan_paths(ROOT, args, _cached(resolve_import))
    for h in hits:
        print(h.render())
    if summary:
        _summary(hits)
    if hits:
        print(
            f"\n{len(hits)} docs-code hit(s). Fix each (D4: move the code into a "
            "tested file under docs/snippets/ or examples/ and include it with "
            "--8<--), or waive the block with <!-- code-ok: <rule> <reason> --> "
            "on the line before its fence.",
            file=sys.stderr,
        )
        return 1
    return 0


def _ratchet(hits: list[Hit], baseline: dict[str, dict[str, int]]) -> int:
    over, dropped = compare(counts(hits), baseline)
    for path, rule, n, b in over:
        for h in hits:
            if h.path == path and h.rule == rule:
                print(h.render())
        print(f"{path}: {rule} {n} hit(s), baseline {b}\n")
    for path, rule, n, b in dropped:
        print(f"{path}: {rule} dropped to {n} from {b}", file=sys.stderr)
    if dropped:
        print(
            f"{len(dropped)} count(s) dropped: tighten the baseline with "
            "uv run python tools/docs/docs_code_check.py --update-baseline",
            file=sys.stderr,
        )
    if over:
        print(
            f"{len(over)} (file, rule) count(s) above the baseline. Fix the new "
            "hits (`uv run python tools/docs/docs_code_check.py PATH` lists every "
            "hit of a page), or waive a block with <!-- code-ok: <rule> <reason> -->.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    if importlib.util.find_spec("sandloom") is None and (ROOT / "src").is_dir():
        # A copy of the repository whose environment has not installed it.
        sys.path.insert(0, str(ROOT / "src"))
    if importlib.util.find_spec("sandloom") is None:
        print(
            "sandloom is not importable, so D2 cannot run: "
            "uv run python tools/docs/docs_code_check.py",
            file=sys.stderr,
        )
        return 2
    args = [a for a in argv if not a.startswith("--")]
    if args:
        return _strict(args, "--summary" in argv)
    hits = scan(ROOT, tracked_files(ROOT), _cached(resolve_import))
    if "--summary" in argv:
        _summary(hits)
        return 0
    if "--reset-baseline" in argv:
        write_baseline(BASELINE, counts(hits))
        print(f"wrote {BASELINE.relative_to(ROOT)} from {len(hits)} hit(s)")
        return 0
    baseline = load_baseline(BASELINE)
    if "--update-baseline" in argv:
        write_baseline(BASELINE, lowered(counts(hits), baseline))
        _, dropped = compare(counts(hits), baseline)
        print(f"lowered {len(dropped)} baseline count(s) in {BASELINE.name}")
        return 0
    return _ratchet(hits, baseline)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
