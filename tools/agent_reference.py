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
3. kernel map: file and symbol of the entry points (no line numbers, so a PR that
   moves code does not drift; the build fails only when a symbol vanishes, read
   from the AST);
4. plan index: KB id, wave, dependencies, M-metrics and heading text of
   ``design/kernel-boundary/migration-plan.md`` (no line numbers).

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
    ("public_api_baseline.json", '"ContractConformanceError"'),
)

REGISTRIES = """\
## 1. Registries (shared files: never open whole, never reorder others' lines)

Add only your own lines (the orchestrator resolves conflicts). Routine edits are commands; run
`uv run python tools/agent_registry.py <command>` instead of opening the file:
`classify <new files>` (public-source.json), `next-id SANDLOOM-C` (error code),
`next-id G-AUTHZ --bump` (guarantee id, `issued:` raised), `edges-summary` (edges.yml `summary:`).

**guarantees.yml** (page `docs/reference/guarantees.md` is generated)
```yaml
- id: G-TENANT-01
  statement: <one sentence, behaviour that must never weaken>
  sources: [docs/modules/database/multi-tenancy.md]
  tests: [src/sandloom/core/tests/test_tenant_isolation.py::test_no_tenant_bound_fails_closed]
```
Or `rules:` / `import_contracts:`; none -> `gap` + `gap_size` (S/M/L). Id: `next-id G-<AREA> --bump`;
`removed:` needs approved_by; new ADR heading: `adr_sections:`. Insert after the family's last id
(`grep -n 'id: G-TENANT' guarantees.yml`). Gate: `uv run python tools/checks/guarantees.py check`, then `render`.

**tests/architecture/kernel_edges.yml** (SANDLOOM-T017) and **design/kernel-boundary/edges.yml**
(same ids and fields; `test_kernel_edges.py` compares them, and its `summary:` counts)
```yaml
  - id: B-N20
    kind: documentation
    class: b
    type: naming
    scope: runtime
    owner: tenancy
    baseline_verdict: allowed
    current_status: retained     # open | fixed | retained
    target: retained
    adr: ADR-0012
    reason: docstring example    # required when retained
    prs: [KB-03]
    anchors:
      - {path: sandloom/core/contract_cache_shadow.py, literal: tenancy.membership.v1}
```
A PR that closes a row flips `current_status` in both, then `edges-summary`. Find: `grep -n -A3 'id: B-S' <file>`.
Check: `uv run python tests/architecture/kernel_edges.py --report --base origin/arch/kernel-boundary`.

**src/sandloom/core/error_codes.py** (`CATALOGUE`; page `docs/reference/error-codes.md` is generated)
```python
    _entry("SANDLOOM-T016", "declared-generated-file", "test",   # code, slug, kind
        "<title>", "<fix, one line>", \"\"\"<explain>\"\"\",
        docs="docs/admin-console/add-your-module.md"),
```
Code: `next-id SANDLOOM-<K>`; place it in its kind's section (`grep -n '# ---' <file>`). Then `uv run python tools/docs/docs_reference.py` (page), `uv run sandloom explain <code>`.

**tools/packaging/public-source.json** (every tracked file is classified): `classify <paths>` puts each
beside its nearest sibling; owner-local: `classify <paths> --exclude-file "<reason>"`. Check:
`python3 tools/packaging/export_repository.py --check`.

**public_api_baseline.json** (`{module: {name: {kind, members, signature}}}`, SANDLOOM-T008)
```json
"sandloom.core.contract_testing": { "ContractConformanceError": { "kind": "class",
  "members": ["add_note", "args", "with_traceback"], "signature": null } }
```
Never hand-edit: `uv run python tools/packaging/check_public_api.py [--update]`. A new public name
is an API decision (upgrade steps). Find: `grep -n '^  "sandloom\\.sdk' public_api_baseline.json`.

**CHANGELOG.md**: a bullet under `## [Unreleased]`, in its `###` kind.
"""

#: Names a CI command line must start with to be listed.
_COMMAND = re.compile(
    r"^(uv run|uv export|uvx|python3 |npm run|npm test|node |deploy/|tools/|"
    r"docker compose|\.venv-kernel|\$BIN/)"
)
_SKIP_GLOBS = {".github/workflows/ci.yml", "pyproject.toml", "uv.lock"}
_WIDTH = 72
_NOISE = (
    "--self-test",
    "RUNNER_TEMP",
    "pip-audit",
    "no_batteries.py install",
    "# idempotent",
    "sync_release",
    "check_wheel_rpc",
    "tutorial_check",
    "twine",
)
_SKIP_AREAS = ("benchmarks", "dependency_review")
_SKIP_JOBS = (
    "kernel-without-batteries",
    "supply-chain",
    "backend-tests",
    "second-db-test-lane",
    "skip-ledger",
    "admin-ui",
)


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
        "## 2. CI by path (a path in the filter runs the area's jobs; globs omit .github, pyproject, uv.lock)",
        "",
        "Not listed (full suites, SBOM, UI): `uv run sandloom test <module>`, `admin/AGENTS.md`, `tools/ci/ci_local.py --list`.",
        "",
    ]
    shown: set[str] = set()
    for area, area_jobs in by_area.items():
        if area in _SKIP_AREAS:
            continue
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
            if job_id in shown or job_id in _SKIP_JOBS or not cmds:
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
            "_bind_infrastructure",
            "_validate_and_index_routes",
        ),
    ),
    (
        "src/sandloom/core/loader/plugin_loader.py",
        (
            "load_modules",
            "load_module_topology",
            "_load_with_summary",
            "_load_list",
            "_load_entry_points",
            "_preflight_consumes",
        ),
    ),
    (
        "src/sandloom/sdk/_decl/module.py",
        (
            "define_module",
            "discover",
        ),
    ),
    (
        "src/sandloom/core/cli/generated.py",
        ("plan", "apply", "main", "render_stub"),
    ),
    ("src/sandloom/core/cli/inspection/generation.py", ("finish",)),
    (
        "src/sandloom/core/error_codes.py",
        ("CATALOGUE", "_entry"),
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
        "## 3. Kernel map (file · symbols; `grep -n '^def create_app' <file>`)",
        "",
    ]
    for rel, names in KERNEL_MAP:
        table = _top_level_lines((root / rel).read_text(encoding="utf-8"))
        missing = [n for n in names if n not in table]
        if missing:
            raise SystemExit(
                f"agent_reference: {rel} no longer defines {', '.join(missing)}"
            )
        lines.append(f"- `{rel.removeprefix('src/sandloom/')}` · " + ", ".join(names))
    lines.append("")
    return "\n".join(lines)


def _plan(root: Path) -> str:
    text = (root / PLAN).read_text(encoding="utf-8").splitlines()
    heads = [
        (i + 1, m.group(1))
        for i, line in enumerate(text)
        if (m := re.match(r"^### (KB-\d+)", line))
    ]
    titles = {
        m.group(1): m.group(2)[:24]
        for line in text
        if (m := re.match(r"^### (KB-\d+) (.*)", line))
    }
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
        "## 4. Plan index (`grep -n '^### KB-30' <plan>`, read to the next `###`; KB-50..56 wait for KB-49)",
        "",
        f"`{PLAN}` (also `## Rules every PR follows`, `## Waves`, `## Edge closure`)",
        "id wave <needs> [metrics] heading",
    ]
    for start, kb in heads:
        body = "\n".join(text[start - 1 : end(start)])
        metrics = list(dict.fromkeys(re.findall(r"\bM-[a-z]+\b", body)))
        out.append(
            f"{kb} {wave.get(kb, '?')}"
            + (f" <{','.join(dict.fromkeys(deps[kb]))}>" if deps.get(kb) else "")
            + (f" [{' '.join(metrics)}]" if metrics else "")
            + f" {titles[kb]}"
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
        "Index of registries, CI jobs and kernel entry points. "
        "Source: `tools/agent_reference.py`.\n\n"
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
