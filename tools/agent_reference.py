#!/usr/bin/env python3
# ruff: noqa: E501, C901
"""Print one section of the agent reference: ``python3 tools/agent_reference.py <topic>``.

Small sections an agent reads instead of opening the big shared files, the CI
configuration or the kernel entry points to find out how they work. Each topic
is a skill (``sandloom-registries``, ``sandloom-ci-map``, ``sandloom-kernel-map``,
``sandloom-infra-map``, ``sandloom-plan``, in ``.agents/skills`` and the
``.claude/skills`` mirror) whose ``SKILL.md`` runs this script when it loads:

- ``registries``: what each shared file holds, one entry, the command that adds
  or validates an entry, how to find neighbours (hand-written, below; each
  example is checked to still exist in its file);
- ``ci``: the path filters and the commands of ``ci.yml``, ``docs.yml``,
  ``architecture-check.yml``, ``packages.yml``, ``admin.yml`` and
  ``tools/ci/required_ci.py`` (the only topic that needs PyYAML);
- ``kernel``: file and symbol of the entry points (no line numbers; the script
  fails when a symbol vanishes, read from the AST);
- ``infra``: one line per capability, derived from code only: the public classes
  of ``core/infra/ports/<cap>.py``, the ``*Conformance`` classes of
  ``driverkit/kits/<cap>.py`` (both from the AST), the built-in driver rows and
  tiers of ``core/drivers/bootstrap.py``, every ``sandloom.drivers`` entry point
  of ``packages/*/pyproject.toml`` and the capability's test files by glob.
  ``bootstrap.py`` is read from its AST (a small evaluator for the constants the
  rows are built from, f-string prefix ``_DRIVERS`` included), never imported:
  the script runs with a plain ``python3`` and no sandloom import. A row or
  entry point whose module file or class is missing stops it;
- ``plan``: every ``design/**/*.md`` with ``<!-- agent-reference: index -->`` in
  its first five lines, its ``## `` headings listed (the KB migration plan, if
  marked, keeps its KB rows: id, wave, dependencies, M-metrics).

Freshness rule: nothing is generated or committed. The output comes from code
and from marked plans of the checkout the script sits in, rendered each time a
skill loads, so a new capability, driver or a moved port shows up at once. A
wave's orchestrator adds the marker to its plan or spec when the wave starts and
removes it when the wave ends. ``tests/architecture/test_agent_reference.py``
runs every topic and bounds its size. Standard library (PyYAML for ``ci``).
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import itertools
import re
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[1]
#: Byte limits an agent can read whole: a topic, the plan index.
#: 4608: the ci topic lists the admin filter's kernel directories (the files
#: the OpenAPI export loads), about 300 bytes more than the old short list.
MAX_BYTES = 4608
MAX_PLAN_BYTES = 6144
#: Test files listed per capability and owner in the infra map.
_TESTS_SHOWN = 5
PLAN = "design/kernel-boundary/migration-plan.md"
WORKFLOWS = ".github/workflows"
PORTS = "src/sandloom/core/infra/ports"
KITS = "src/sandloom/driverkit/kits"
BOOTSTRAP = "src/sandloom/core/drivers/bootstrap.py"
#: A design doc with this comment in its first five lines is indexed in section 5.
MARKER = "<!-- agent-reference: index -->"

#: (file, literal the hand-written example quotes): a missing literal fails the build.
EXAMPLE_ANCHORS = (
    ("guarantees.yml", "id: G-TENANT-01"),
    ("tests/architecture/kernel_edges.yml", "id: B-N20"),
    ("design/kernel-boundary/edges.yml", "id: B-N20"),
    ("src/sandloom/core/error_codes.py", '"SANDLOOM-T016"'),
    ("public_api_baseline.json", '"ContractConformanceError"'),
)

REGISTRIES = """\
# Registries (shared files: never open whole, never reorder others' lines)

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

**changelog.d/<your-alias>.md** (your entry; alias = the tm task alias; never edit `CHANGELOG.md` or a
package CHANGELOG.md for a new entry: GitHub ignores the union merge, so two PRs conflict). One file per
branch, bullets under `### <kind>`, the kind one of Breaking changes, Security, Deprecated, Added,
Changed, Fixed; a bullet is `**title** (alias). text`:
```
### Fixed

- **The Celery driver registers any task name** (celery-task-name-identifier). What changed, upgrade steps.
```
A change to one package's own `packages/<package>/CHANGELOG.md` goes in `changelog.d/<package>/<alias>.md`.
Check: `python3 tools/packaging/changelog_fragments.py --check`. The release preparation folds the files
(`--fold <version> [--package <package>]`).
"""

#: Names a CI command line must start with to be listed.
_COMMAND = re.compile(
    r"^(uv run|uv export|uvx|python3 |npm run|npm test|node |deploy/|tools/|"
    r"docker compose|\.venv-kernel|\$BIN/)"
)
_SKIP_GLOBS = {
    ".github/workflows/ci.yml",
    "pyproject.toml",
    "uv.lock",
    "tools/packaging/packages.json",
}
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
# `kernel`: every job it selects is in _SKIP_JOBS (the kernel lanes).
_SKIP_AREAS = ("benchmarks", "dependency_review", "kernel")
_SKIP_JOBS = (
    "kernel-without-batteries",
    "kernel-without-redis",
    "redis-drivers",
    "celery-drivers",
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


def _env(values: dict[str, Any] | None) -> str:
    """``K=V`` pairs as a shell prefix; an expression GitHub fills is shown as
    ``<ci>`` (set your own value)."""
    pairs = []
    for key, value in (values or {}).items():
        text = str(value)
        if "${{" in text:
            pairs.append(f"{key}=<ci>")
        elif "not-a-real" in text:  # a throwaway credential: any value works
            pairs.append(f"{key}=<dummy>")
        elif re.fullmatch(r"[\w.:/@+,=-]*", text):
            pairs.append(f"{key}={text}")
        else:
            pairs.append(f"{key}='{text}'")
    return " ".join(pairs)


def _venv_on_path(job: dict[str, Any]) -> bool:
    """A step puts ``.venv/bin`` on ``$GITHUB_PATH``: the job's bare ``python3``
    is the project's environment, not the system one."""
    return any(
        ".venv/bin" in (s.get("run") or "") and "GITHUB_PATH" in (s.get("run") or "")
        for s in job.get("steps") or []
    )


def _commands(job: dict[str, Any]) -> list[str]:
    """The job's commands as they run locally: a bare ``python3`` becomes
    ``uv run python`` where the job puts the venv on its path, and a step's
    own ``env`` is its prefix (the job's ``env`` is a line of its own)."""
    found: list[str] = []
    venv = _venv_on_path(job)
    for step in job.get("steps") or []:
        text = (step.get("run") or "").replace("\\\n", " ")
        prefix = _env(step.get("env"))
        for line in text.splitlines():
            line = " ".join(line.split())
            if not _COMMAND.match(line) or any(n in line for n in _NOISE):
                continue
            if venv and line.startswith("python3 "):
                line = "uv run python " + line[len("python3 ") :]
            if len(line) > _WIDTH:
                line = line[: _WIDTH - 3] + "..."
            line = f"{prefix} {line}" if prefix else line
            if line not in found:
                found.append(line)
    return found


def _braces(globs: list[str]) -> list[str]:
    """Globs that differ only in their last directory (or file) name, as one
    brace glob: ``src/a/**`` and ``src/b/**`` become ``src/{a,b}/**``. Order is
    that of the first glob of each group; a group of one stays as it is."""
    groups: dict[tuple[str, str], list[str]] = {}
    for glob in globs:
        tail = "/**" if glob.endswith("/**") else ""
        parent, _, name = glob.removesuffix(tail).rpartition("/")
        groups.setdefault((parent, tail), []).append(name)
    out = []
    for (parent, tail), names in groups.items():
        base = f"{parent}/" if parent else ""
        if len(names) == 1:
            out.append(f"{base}{names[0]}{tail}")
            continue
        # A shared stem up to its last `_` or `-` comes out of the braces.
        same = itertools.takewhile(
            lambda chars: len(set(chars)) == 1, zip(*names, strict=False)
        )
        stem = "".join(chars[0] for chars in same)
        stem = stem[: max(stem.rfind("_"), stem.rfind("-")) + 1]
        inner = ",".join(name[len(stem) :] for name in names)
        out.append(f"{base}{stem}{{{inner}}}{tail}")
    return out


def _yaml(text: str) -> Any:
    """Parse YAML; only the ``ci`` topic needs it, so a plain ``python3`` without
    PyYAML still renders the other topics and this one says how to run it."""
    try:
        import yaml
    except ImportError:
        raise SystemExit(
            "agent_reference: the ci topic needs PyYAML; "
            "run `uv run python tools/agent_reference.py ci`"
        ) from None
    return yaml.safe_load(text)


def _workflow(root: Path, name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = _yaml(
        (root / WORKFLOWS / name).read_text(encoding="utf-8")
    )
    return loaded


def _ci(root: Path) -> str:
    ci = _workflow(root, "ci.yml")
    required = _load(root / "tools/ci/required_ci.py")
    filters: dict[str, list[str]] = {}
    for step in ci["jobs"]["changes"]["steps"]:
        if step.get("id") == "filter":
            filters = _yaml(step["with"]["filters"])
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
        "# CI by path (a matching path runs the area's jobs; hidden: .github, pyproject, uv.lock, packages.json)",
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
        lines.append(f"**{area}**: " + " ".join(_braces(globs)))
        repeated = [j for j in area_jobs if j in shown]
        for job_id in area_jobs:
            cmds = commands(job_id)
            if job_id in shown or job_id in _SKIP_JOBS or not cmds:
                continue
            shown.add(job_id)
            lines.append(f"- {job_id}: " + " ; ".join(cmds))
            if job_env := _env(jobs[job_id].get("env")):
                lines.append(f"  env for {job_id}: {job_env}")
        if repeated:
            lines.append(f"- also {', '.join(repeated)} (commands above)")
        if not globs and lines[-1].startswith(f"**{area}**"):
            lines.pop()  # no path and no command: nothing to tell
            continue
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
        "# Kernel map (file · symbols; `grep -nE '^(async def |def |class )?NAME\\b' <file>` finds a function, class or constant)",
        "",
    ]
    for rel, names in KERNEL_MAP:
        table = _top_level_lines((root / rel).read_text(encoding="utf-8"))
        missing = [n for n in names if n not in table]
        if missing:
            raise SystemExit(
                f"agent_reference: {rel} no longer defines {', '.join(missing)}"
            )
        lines.append(f"- `{rel}` · " + ", ".join(names))
    lines.append("")
    return "\n".join(lines)


class _Unresolved(Exception):
    """A module-level expression the static reader does not evaluate."""


def _static(node: ast.expr, env: dict[str, Any]) -> Any:
    """Evaluate the constant expressions ``bootstrap.py`` builds its rows from:
    literals, tuples, dicts, names already read, ``d["k"]`` and f-strings."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in env:
        return env[node.id]
    if isinstance(node, ast.Tuple):
        return tuple(_static(e, env) for e in node.elts)
    if isinstance(node, ast.Dict) and all(k is not None for k in node.keys):
        return {
            _static(k, env): _static(v, env)
            for k, v in zip(node.keys, node.values, strict=True)
            if k is not None
        }
    if isinstance(node, ast.Subscript):
        return _static(node.value, env)[_static(node.slice, env)]
    if isinstance(node, ast.JoinedStr):
        return "".join(str(_static(part, env)) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        return _static(node.value, env)
    raise _Unresolved(ast.dump(node)[:60])


def _constants(source: str) -> dict[str, Any]:
    env: dict[str, Any] = {}
    for node in ast.parse(source).body:
        targets: list[ast.expr]
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        try:
            value = _static(node.value, env) if node.value else None
        except _Unresolved:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                env[target.id] = value
    return env


def _classes(path: Path) -> dict[str, ast.ClassDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}


def _declared_tier(cls: ast.ClassDef) -> str:
    """The class-level ``tier`` a driver declares; the port's default otherwise."""
    for node in cls.body:
        target: ast.expr | None = (
            node.target if isinstance(node, ast.AnnAssign) else None
        )
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        value = getattr(node, "value", None)
        if (
            isinstance(target, ast.Name)
            and target.id == "tier"
            and isinstance(value, ast.Constant)
        ):
            return str(value.value)
    return "experimental"


def _driver_class(root: Path, target: str, where: str) -> tuple[str, ast.ClassDef]:
    """``(owner, class)`` of a ``module:Class`` target: ``core`` or the package
    directory that holds the module file; a missing file or class stops the build."""
    module, _, name = target.partition(":")
    parts = module.split(".")
    roots = [root / "src", *sorted((root / "packages").glob("*/src"))]
    for base in roots:
        for tail in (Path(*parts).with_suffix(".py"), Path(*parts, "__init__.py")):
            path = base / tail
            if not path.is_file():
                continue
            cls = _classes(path).get(name)
            if cls is None:
                raise SystemExit(f"agent_reference: {where}: {path} has no {name}")
            owner = "core" if base == roots[0] else base.parent.name
            return owner, cls
    raise SystemExit(f"agent_reference: {where}: no file for module {module}")


def _drivers(root: Path) -> dict[str, dict[str, tuple[str, str]]]:
    """``{capability: {driver: (owner, tier)}}`` from the built-in rows and tiers
    (read from the AST of ``bootstrap.py``, no import: ``generate`` stays light) and
    from every ``sandloom.drivers`` entry point of ``packages/*/pyproject.toml``."""
    env = _constants((root / BOOTSTRAP).read_text(encoding="utf-8"))
    rows, tiers = env.get("BUILTIN_DRIVER_ROWS"), env.get("BUILTIN_DRIVER_TIERS")
    if not isinstance(rows, tuple) or not isinstance(tiers, dict):
        raise SystemExit(f"agent_reference: {BOOTSTRAP} rows or tiers not readable")
    found: dict[str, dict[str, tuple[str, str]]] = {}
    for row in rows:
        cap, name, target = row[0], row[1], row[2]
        owner, cls = _driver_class(root, target, f"row {cap}.{name}")
        found.setdefault(cap, {})[name] = (
            owner,
            tiers.get((cap, name)) or _declared_tier(cls),
        )
    for toml in sorted((root / "packages").glob("*/pyproject.toml")):
        entries = tomllib.loads(toml.read_text(encoding="utf-8"))
        points = (
            entries.get("project", {}).get("entry-points", {}).get("sandloom.drivers")
        )
        for key, target in (points or {}).items():
            cap, _, name = key.partition(".")
            # Resolve first: a missing target stops the build even when a
            # built-in row of the same name wins the listing below.
            owner, cls = _driver_class(root, target, f"{toml.parent.name} {key}")
            if name in found.get(cap, {}):
                continue
            found.setdefault(cap, {})[name] = (owner, _declared_tier(cls))
    return found


def _infra(root: Path) -> str:
    ports = {
        p.stem: p
        for p in sorted((root / PORTS).glob("*.py"))
        if p.stem not in ("_base", "__init__")
    }
    drivers = _drivers(root)
    lines = [
        "# Infrastructure map (capability: port classes; kit classes; drivers (owner, tier); tests)",
        "",
        f"Ports `{PORTS}/<cap>.py`, kits `{KITS}/<cap>.py`, "
        f"driver rows `{BOOTSTRAP}`; owner is `core` or a `packages/<dir>`. "
        "Tests: `src/sandloom/core/tests/test_<cap>*.py`, `packages/<dir>/tests/test_*<cap>*.py` (name without `test_`).",
        "",
    ]
    for cap in sorted(set(ports) | set(drivers)):
        port = (
            [n for n in _classes(ports[cap]) if not n.startswith("_")]
            if cap in ports
            else []
        )
        kit_path = root / KITS / f"{cap}.py"
        kits = (
            [n for n in _classes(kit_path) if n.endswith("Conformance")]
            if kit_path.is_file()
            else []
        )
        shown = ", ".join(
            f"{n} ({o}, {t})" for n, (o, t) in sorted(drivers.get(cap, {}).items())
        )
        lines.append(
            f"- {cap}: {' '.join(port) or 'no port'}; "
            f"{' '.join(kits) or 'no kit'}; {shown or 'no driver'}; "
            f"{_tests(root, cap)}"
        )
    lines.append("")
    return "\n".join(lines)


def _tests(root: Path, cap: str) -> str:
    """The test files of a capability by glob: the kernel's and each package's."""
    where = [("core", root / "src/sandloom/core/tests", f"test_{cap}*.py")]
    where += [
        (d.name, d / "tests", f"test_*{cap}*.py")
        for d in sorted((root / "packages").glob("*"))
    ]
    shown: list[str] = []
    for owner, folder, pattern in where:
        stems = [p.stem.removeprefix("test_") for p in sorted(folder.glob(pattern))]
        if stems:
            more = f" +{len(stems) - _TESTS_SHOWN}" if len(stems) > _TESTS_SHOWN else ""
            shown.append(f"{owner}: {' '.join(stems[:_TESTS_SHOWN])}{more}")
    return "tests " + ("; ".join(shown) or "none")


def _marked(root: Path) -> list[str]:
    """Design docs carrying :data:`MARKER` in their first five lines."""
    found: list[str] = []
    for path in sorted((root / "design").glob("**/*.md")):
        with path.open(encoding="utf-8") as handle:
            if any(MARKER in line for line in itertools.islice(handle, 5)):
                found.append(path.relative_to(root).as_posix())
    return found


def _headings(text: list[str]) -> list[str]:
    """The ``## `` headings outside code fences, compact: backticks and a
    parenthetical dropped, the number's dot too, 48 characters at most."""
    out: list[str] = []
    fence: tuple[str, int] | None = None  # opening character and length
    for line in text:
        opening = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence is None and opening:
            fence = (opening.group(1)[0], len(opening.group(1)))
            continue
        if fence is not None:
            # CommonMark: closed by a run of the same character, at least as
            # long, with nothing after it.
            closing = re.fullmatch(r" {0,3}(`{3,}|~{3,})\s*", line)
            if (
                closing
                and closing.group(1)[0] == fence[0]
                and len(closing.group(1)) >= fence[1]
            ):
                fence = None
            continue
        if line.startswith("## "):
            head = re.sub(r"\s*\(.*", "", line[3:].replace("`", ""))
            out.append(re.sub(r"^(\d+)\.\s*", r"\1 ", head).strip()[:48])
    return out


def _plans(root: Path) -> str:
    """One section per marked design doc: its path and its headings (the KB
    migration plan, if marked, keeps its KB rows)."""
    out = [
        "# Plan index (design docs marked `<!-- agent-reference: index -->`; "
        "`grep -n '^## 7\\.' <file>`, read to the next `## `)",
        "",
    ]
    marked = _marked(root)
    if not marked:
        out += ["No design doc is marked: no wave is active.", ""]
    for rel in marked:
        text = (root / rel).read_text(encoding="utf-8").splitlines()
        body = _plan(text) if rel == PLAN else [" · ".join(_headings(text))]
        out += [f"## `{rel}`", "", *body, ""]
    return "\n".join(out)


def _plan(text: list[str]) -> list[str]:
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
        f"`{PLAN}` (`grep -n '^### KB-30' <plan>`, read to the next `###`; also `## Rules every PR follows`, `## Waves`, `## Edge closure`)",
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
    return out


def _registries(root: Path) -> str:
    for rel, literal in EXAMPLE_ANCHORS:
        if literal not in (root / rel).read_text(encoding="utf-8"):
            raise SystemExit(
                f"agent_reference: {rel} no longer holds {literal!r}; update REGISTRIES"
            )
    return REGISTRIES


#: topic -> renderer; each prints one section for the checkout at ``root``.
TOPICS: dict[str, Callable[[Path], str]] = {
    "registries": _registries,
    "ci": _ci,
    "kernel": _kernel,
    "infra": _infra,
    "plan": _plans,
}


def render(topic: str, root: Path = REPO) -> str:
    """The section ``topic`` (a key of :data:`TOPICS`) of the checkout at ``root``."""
    if topic not in TOPICS:
        raise SystemExit(
            f"agent_reference: unknown topic {topic!r}; one of {', '.join(TOPICS)}"
        )
    return TOPICS[topic](root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("topic", choices=sorted(TOPICS), help="the section to print")
    parser.add_argument(
        "--root", type=Path, default=REPO, help="checkout to read (default: this one)"
    )
    args = parser.parse_args(argv)
    sys.stdout.write(render(args.topic, args.root.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
