#!/usr/bin/env python3
# ruff: noqa: E501, C901
"""Write (default) or check (``--check``) ``.claude/agent-reference.md``.

One sectioned file an agent reads instead of opening the big shared files, the
CI configuration or the kernel entry points to find out how they work:

1. registries: what each shared file holds, one entry, the command that adds or
   validates an entry, how to find neighbours (hand-written, below; each example
   is checked to still exist in its file);
2. CI by path: the path filters and the commands of ``ci.yml``, ``docs.yml``,
   ``architecture-check.yml``, ``packages.yml``, ``admin.yml`` and
   ``tools/ci/required_ci.py``;
3. kernel map: ``file:line`` of the entry points, read from the AST;
4. plan index: KB id -> line range in ``design/kernel-boundary/migration-plan.md``.

``sandloom generate`` calls :func:`render` (the repository checkout only: the
hook looks for this file) and ``tests/architecture/test_agent_reference.py``
compares it with the committed file. Standard library and PyYAML.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
OUT = ".claude/agent-reference.md"
PLAN = "design/kernel-boundary/migration-plan.md"
WORKFLOWS = ".github/workflows"

#: (file, literal the hand-written example quotes): a missing literal fails the build.
EXAMPLE_ANCHORS = (
    ("guarantees.yml", "id: G-TENANT-01"),
    ("tests/architecture/kernel_edges.yml", "id: B-N20"),
    ("design/kernel-boundary/edges.yml", "id: B-N20"),
    ("src/sandloom/core/error_codes.py", '"SANDLOOM-T016"'),
    (
        "tools/packaging/public-source.json",
        '"tests/architecture/test_docs_generated.py"',
    ),
    ("public_api_baseline.json", '"ContractConformanceError"'),
)

REGISTRIES = """\
## 1. Registries (shared files: never open whole, never reorder others' lines)

Add only your own lines; the orchestrator resolves merge conflicts. Find
neighbours with `grep -n`, read 20 lines around the hit.

**guarantees.yml** (G-/C- ledger; page `docs/reference/guarantees.md` is generated)
```yaml
- id: G-TENANT-01
  statement: <one sentence, behaviour that must never weaken>
  sources: [docs/modules/database/multi-tenancy.md]
  tests: [src/sandloom/core/tests/test_tenant_isolation.py::test_no_tenant_bound_fails_closed]
```
Other enforcement keys: `rules:` (`sandloom check` rule), `import_contracts:`; none ->
`gap` + `gap_size` (S/M/L). New id: also bump `issued:` (never reuse an id; dropping one
needs `removed:` with approved_by). New ADR heading: `adr_sections:`. Top keys:
`grep -n '^[a-z_]*:' guarantees.yml`; neighbours: `grep -n 'id: G-TENANT' guarantees.yml`.
Gate: `uv run python tools/checks/guarantees.py check`; then `... render` rewrites the page;
`... show G-TENANT-01` prints one entry.

**tests/architecture/kernel_edges.yml** (live kernel-edge register, SANDLOOM-T017) and
**design/kernel-boundary/edges.yml** (the design copy; same ids, kind, class, target, adr,
anchors, current_status, compared by `test_kernel_edges.py`; its `summary:` counts must match)
```yaml
  - id: B-N20
    kind: documentation          # runtime when absent
    class: b
    type: naming
    scope: runtime
    owner: tenancy
    baseline_verdict: allowed
    current_status: retained     # open | fixed | retained; fixed needs a literal anchor
    target: retained
    adr: ADR-0012
    reason: docstring example    # required when retained
    prs: [KB-03]
    anchors:
      - {path: sandloom/core/contract_cache_shadow.py, literal: tenancy.membership.v1}
```
A PR that carries a row to its target flips `current_status` in both files. Rows:
`grep -n '^  - id: ' <file>`; neighbours: `grep -n -A3 'id: B-S' <file>`. Check:
`uv run python tests/architecture/kernel_edges.py --report --base origin/arch/kernel-boundary`.

**src/sandloom/core/error_codes.py** (`CATALOGUE`; page `docs/reference/error-codes.md` is generated)
```python
    _entry(
        "SANDLOOM-T016", "declared-generated-file", "test",   # code, slug, kind
        "<title>", "<fix, one line>",
        \"\"\"<explain: what happened, why, how to fix>\"\"\",
        docs="docs/admin-console/add-your-module.md",
    ),
```
Newest first at the top of the tuple, but not strictly by number: list codes with
`grep -n '^        "SANDLOOM-T' src/sandloom/core/error_codes.py`, take the next free
number, never reuse or renumber. Then `uv run python tools/docs/docs_reference.py` (page;
`--check` verifies) and `uv run sandloom explain SANDLOOM-T016`.

**tools/packaging/public-source.json** (every tracked file is classified)
`include` = `[{"reason": ..., "paths": [exact paths]}]`, then `exclude_files` / `exclude_trees`
(`{path: reason}`), `symlinks`. Example line in the tests group:
`"tests/architecture/test_docs_generated.py",`. A new file goes into the group whose
reason fits, next to its siblings (`grep -n '"tests/architecture/test_d' tools/packaging/public-source.json`);
owner-local files go to `exclude_files`. Check: `python3 tools/packaging/export_repository.py --check`.

**public_api_baseline.json** (`{module: {name: {kind, members, signature}}}`, SANDLOOM-T008)
```json
"sandloom.core.contract_testing": { "ContractConformanceError": { "kind": "class",
  "members": ["add_note", "args", "with_traceback"], "signature": null } }
```
Never hand-edit. `uv run python tools/packaging/check_public_api.py` is the gate,
`... --update` rewrites it; a new public name is an API decision (upgrade steps in the
CHANGELOG). Find a module: `grep -n '^  "sandloom\\.sdk' public_api_baseline.json`.

**CHANGELOG.md**: a bullet under `## [Unreleased]` in `### Breaking changes`, `### Security`,
`### Added`, `### Changed` or `### Fixed` (`grep -n '^### ' CHANGELOG.md | head -6`).
"""

#: Names a CI command line must start with to be listed.
_COMMAND = re.compile(
    r"^(uv run|uv export|uvx|python3 |npm run|npm test|node |deploy/|tools/|"
    r"docker compose|\.venv-kernel|\$BIN/)"
)
_AREA_NOTE = "Omitted from the globs: `.github/**`, `pyproject.toml`, `uv.lock`, `tools/packaging/**/*.py` repeats."
_SKIP_GLOBS = {".github/workflows/ci.yml", "pyproject.toml", "uv.lock"}
_WIDTH = 100
_NOISE = (
    "--self-test",
    "RUNNER_TEMP",
    "pip-audit",
    "no_batteries.py install",
    "# idempotent",
)
_PLAN_PARTS = ("Rules", "Waves", "Edge ")


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"agent_reference: cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def _commands(job: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for step in job.get("steps") or []:
        text = (step.get("run") or "").replace("\\\n", " ")
        for line in text.splitlines():
            line = " ".join(line.split())
            if not _COMMAND.match(line) or any(n in line for n in _NOISE):
                continue
            if len(line) > _WIDTH:
                line = line[: _WIDTH - 3] + "..."
            if line not in found:
                found.append(line)
    return found


def _workflow(root: Path, name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (root / WORKFLOWS / name).read_text(encoding="utf-8")
    )
    return loaded


def _ci(root: Path) -> str:
    ci = _workflow(root, "ci.yml")
    required = _load(root / "tools/ci/required_ci.py")
    filters: dict[str, list[str]] = {}
    for step in ci["jobs"]["changes"]["steps"]:
        if step.get("id") == "filter":
            filters = yaml.safe_load(step["with"]["filters"])
    jobs = ci["jobs"]

    def commands(job_id: str) -> list[str]:
        job = jobs[job_id]
        if "uses" in job:  # a called workflow: its one job
            called = _workflow(root, job["uses"].rsplit("/", 1)[-1])
            return [c for j in called["jobs"].values() for c in _commands(j)]
        return _commands(job)

    by_area: dict[str, list[str]] = {a: [] for a in sorted(required.AREAS)}
    for job_id, areas in required.AREA_JOBS.items():
        for area in areas:
            by_area[area].append(job_id)
    for job_id, area in required.PR_JOBS.items():
        by_area[area].append(job_id)
    lines = [
        "## 2. CI by path (a path in the filter runs the area's jobs; `ci-required` needs them all)",
        "",
        _AREA_NOTE,
        "Local runner: `tools/ci/ci_local.py --list`; local gates: `uv run sandloom test <module>`.",
        f"Full-validation only (not per PR): {', '.join(sorted(required.FULL_JOBS))}.",
        "Always: `workflow-policy`: " + " ; ".join(commands("workflow-policy")[1:]),
        "",
    ]
    shown: set[str] = set()
    for area, area_jobs in by_area.items():
        globs = [
            g
            for g in filters.get(area, [])
            if g not in _SKIP_GLOBS
            and not g.startswith(".github/")
            and not g.endswith("**/*.py")
        ]
        lines.append(f"**{area}**: " + " ".join(globs))
        repeated = [j for j in area_jobs if j in shown]
        for job_id in area_jobs:
            cmds = commands(job_id)
            if job_id in shown or not cmds:
                continue
            shown.add(job_id)
            lines.append(f"- {job_id}: " + " ; ".join(cmds))
        if repeated:
            lines.append(f"- also {', '.join(repeated)} (commands above)")
        lines.append("")
    return "\n".join(lines)


#: file -> top-level names whose line an agent needs.
KERNEL_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "src/sandloom/core/app_factory/__init__.py",
        (
            "create_app",
            "_new_application",
            "_bind_infrastructure",
            "_validate_and_index_routes",
        ),
    ),
    (
        "src/sandloom/core/loader/plugin_loader.py",
        (
            "load_modules",
            "load_modules_with_summary",
            "load_module_topology",
            "_load_with_summary",
            "_load_list",
            "_load_entry_points",
            "_invoke_register",
            "_preflight_consumes",
            "_check_module_settings",
        ),
    ),
    (
        "src/sandloom/sdk/_decl/module.py",
        (
            "define_module",
            "discover",
            "_Collector",
            "_register_declarations",
            "import_conventional",
        ),
    ),
    (
        "src/sandloom/core/cli/generated.py",
        ("plan", "apply", "main", "module_surface", "render_stub", "_consumer_plan"),
    ),
    ("src/sandloom/core/cli/inspection/generation.py", ("finish",)),
    (
        "src/sandloom/core/error_codes.py",
        ("CATALOGUE", "_entry", "lookup", "render_catalogue_markdown"),
    ),
)


def _top_level_lines(source: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found[node.name] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.lineno
    return found


def _kernel(root: Path) -> str:
    lines = [
        "## 3. Kernel map (file:line of the definition; read 40 lines from there, not the file)",
        "",
    ]
    for rel, names in KERNEL_MAP:
        table = _top_level_lines((root / rel).read_text(encoding="utf-8"))
        missing = [n for n in names if n not in table]
        if missing:
            raise SystemExit(
                f"agent_reference: {rel} no longer defines {', '.join(missing)}"
            )
        lines.append(
            f"- `{rel.removeprefix('src/sandloom/')}`: "
            + ", ".join(f"{n} {table[n]}" for n in names)
        )
    lines.append("")
    return "\n".join(lines)


def _plan(root: Path) -> str:
    text = (root / PLAN).read_text(encoding="utf-8").splitlines()
    heads = [
        (i + 1, m.group(1))
        for i, line in enumerate(text)
        if (m := re.match(r"^### (KB-\d+)", line))
    ]
    sections = {
        line[3:]: i + 1 for i, line in enumerate(text) if line.startswith("## ")
    }
    bounds = sorted([n for n, _ in heads] + list(sections.values()) + [len(text) + 1])

    def end(start: int) -> int:
        return next(n for n in bounds if n > start) - 1

    wave: dict[str, str] = {}
    deps: dict[str, list[str]] = {}
    for line in text:
        row = re.match(r"^\| (W\d) \| (.+?) \| (.+?) \|$", line)
        if not row:
            continue
        ids = re.findall(r"KB-\d+", row.group(2))
        extra = re.search(r"and (KB-\d+) for (KB-\d+)", row.group(3))
        for kb in ids:
            wave[kb] = row.group(1)
        parts = row.group(3).split(";")
        if "→" not in row.group(3):
            base = re.findall(r"KB-\d+", re.sub(r"\(.*?\)", "", row.group(3)))
            for kb in ids:
                deps.setdefault(kb, []).extend(base)
        for part in parts:
            if "→" in part:
                left, right = part.split("→")
                for kb in re.findall(r"KB-\d+", right):
                    deps.setdefault(kb, []).extend(re.findall(r"KB-\d+", left))
        if extra:
            deps.setdefault(extra.group(2), []).append(extra.group(1))
    out = [
        "## 4. Plan index (`grep -n '^### KB-30' <plan>` then read the range; KB-50..56 wait for KB-49)",
        "",
        f"`{PLAN}`: "
        + ", ".join(
            f"{name.split(' (')[0].lower()} L{n}-{end(n)}"
            for name, n in sections.items()
            if name[:5] in _PLAN_PARTS
        ),
        "",
        "id L<first>-<last> wave <- depends on | measurements",
    ]
    for start, kb in heads:
        body = "\n".join(text[start - 1 : end(start)])
        metrics = list(dict.fromkeys(re.findall(r"\bM-[a-z]+\b", body)))
        needs = ",".join(dict.fromkeys(deps.get(kb, []))) or "-"
        out.append(
            f"{kb} L{start}-{end(start)} {wave.get(kb, '?')} <- {needs}"
            + (f" | {' '.join(metrics)}" if metrics else "")
        )
    out.append("")
    return "\n".join(out)


def render(root: Path = REPO) -> str:
    """The reference file's content for the checkout at ``root``."""
    for rel, literal in EXAMPLE_ANCHORS:
        if literal not in (root / rel).read_text(encoding="utf-8"):
            raise SystemExit(
                f"agent_reference: {rel} no longer holds {literal!r}; update REGISTRIES"
            )
    head = (
        "# Agent reference (generated by `sandloom generate`; never edit)\n\n"
        "Read the matching section before opening a registry, a CI config or a kernel "
        "entry point. Source: `tools/agent_reference.py`.\n\n"
    )
    return head + "\n".join([REGISTRIES, _ci(root), _kernel(root), _plan(root)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when the file is stale"
    )
    args = parser.parse_args(argv)
    path = REPO / OUT
    content = render(REPO)
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    if args.check:
        if current != content:
            print(f"{OUT} is stale: run `uv run sandloom generate`")
            return 1
        return 0
    if current != content:
        path.write_text(content, encoding="utf-8")
        print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
