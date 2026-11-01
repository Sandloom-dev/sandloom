"""DX eval runner: fresh headless agents build features on Sandloom.

Two modes (``--mode``):

* ``app`` (the default): the framework distributions (wheels) are built once
  from the ref under test; each task's ``claude -p`` session starts in an EMPTY
  directory outside the repository with the installed ``sandloom`` CLI on PATH,
  runs ``sandloom new <app>`` itself and builds the feature in that application —
  the way a framework user works. The agent context is what ``sandloom new``
  generates (AGENTS.md / CLAUDE.md) plus the task text; the framework source
  is not there, only the installed package.
* ``framework``: the session's cwd is a throwaway ``git worktree`` of the ref
  under test (the framework repository itself), so it loads that ref's
  CLAUDE.md / AGENTS.md / skills. The worktree hides ``benchmarks/dx/`` (the
  hidden checkers) and the DX research note before the agent starts.

After the agent stops the runner scores the worktree:

* gates — ruff, ruff format, mypy, ``sandloom check``, the architecture manifest,
  contract snapshots, the fast suite and the tests of every touched module;
* hidden checks — ``checks/check_<id>.py``: ``test_fn_*`` (the feature exists)
  and ``test_dev_*`` (rules no gate enforces; a failure with green gates is a
  "deviation no check caught");
* transcript — tokens, turns, tool calls, wall time, framework files read;
* diff — files edited outside the task's ``allowed`` paths.

Run it with the backend's interpreter (it needs ``redis`` to find free test
Redis indexes)::

        uv run python benchmarks/dx/run_eval.py run --label baseline --ref HEAD
    uv run python benchmarks/dx/run_eval.py report --label post-r1 --compare baseline

Raw artefacts (transcripts, patches, gate logs, worktrees) go to
``$SANDLOOM_DX_EVAL_HOME`` (default ``~/.cache/sandloom-dx-eval``); the committed,
reviewable results go to ``benchmarks/dx/results/<label>/``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import appmode  # noqa: E402  (app-mode mechanics, next to this file)

REPO = HERE.parents[1]
TASKS_DIR = HERE / "tasks"
#: North-star suite: a whole product (benchmarks/dx/product/README.md).
PRODUCT_DIR = HERE / "product"
SUITES = {"tasks": TASKS_DIR, "product": PRODUCT_DIR / "tasks"}
CHECKS_DIR = HERE / "checks"
RESULTS_DIR = HERE / "results"
FOOTER = HERE / "footer.md"
#: The app-mode footer: ``{start}`` is the first bullet (start the app / the
#: app exists), ``{app}`` the application's name.
FOOTER_APP = HERE / "footer.app.md"
#: ``--mode``: ``app`` = an ``sandloom new`` application on the built wheels (the
#: default); ``framework`` = a worktree of the framework repository.
MODES = ("app", "framework")
DEFAULT_MODE = "app"
#: The application each suite's agent creates in app mode (``app = ...`` in a
#: task's metadata overrides it).
DEFAULT_APP = {"tasks": "teamhub", "product": "academy"}
#: Paths removed from the agent's worktree before it starts (hidden from it).
HIDDEN = ("benchmarks/dx", "docs/design/dx-cognitive-load-2026-10-25.md")
PG = "postgresql+asyncpg://test_user:test_password@localhost:5433"
#: The test Redis, and (``SANDLOOM_DX_REDIS_INDEXES=200-239``) the indexes the
#: per-task pairs may come from — a shared machine gives each runner its own.
REDIS = os.environ.get("SANDLOOM_DX_REDIS", "redis://localhost:6380")
#: Prefix of the per-task test databases (``SANDLOOM_DX_DB_PREFIX``).
DB_PREFIX = os.environ.get("SANDLOOM_DX_DB_PREFIX", "sandloom_test_dx_")
DEFAULT_MODEL = "claude-sonnet-5-5"


def redis_candidates() -> list[int]:
    """Indexes a per-task pair may use, highest first."""
    raw = os.environ.get("SANDLOOM_DX_REDIS_INDEXES", "")
    if re.fullmatch(r"\d+-\d+", raw.strip()):
        lo, hi = (int(x) for x in raw.strip().split("-"))
        return list(range(hi, lo - 1, -1))
    return list(range(255, 31, -1))


#: The agent runs in ``--permission-mode auto``: these tools are pre-approved
#: (the framework's own workflow: ``sandloom add``, ``sandloom migrate``, gates, read-only
#: inspection) and anything else goes through Claude Code's safety classifier —
#: never an unrestricted shell. ``dontAsk`` (deny everything unlisted) proved
#: too brittle: compound commands built only from listed parts were denied and
#: an agent gave up on its first denial, measuring the allowlist, not the
#: contract. Denials are still recorded (``permission_denials``).
_RO = (
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "find",
    "wc",
    "diff",
    "tree",
    "pwd",
    "sort",
    "uniq",
    "cut",
    "tr",
    "echo",
    "sed",
    "awk",
    "basename",
    "dirname",
    "which",
    "stat",
    "file",
    "true",
)
_UV = (
    "pytest",
    "mypy",
    "ruff",
    "sandloom",  # sandloom add, sandloom migrate, sandloom test, sandloom docs, sandloom explain, sandloom check
    "lint-imports",
    "python scripts/",
    "alembic",
)
_GIT = ("status", "diff", "log", "show", "ls-files", "grep")
ALLOWED = (
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "TodoWrite",
    "Skill",
    "Task",
    "Bash(cd:*)",
    "Bash(uv sync:*)",
    *(f"Bash({c}:*)" for c in _RO),
    *(f"Bash(uv run {c}:*)" for c in _UV),
    *(f"Bash(git {c}:*)" for c in _GIT),
)
#: App mode adds the installed CLI itself (``sandloom new``, ``sandloom test``, …, run
#: without ``uv run`` before the app's venv exists) and refreshing the lock.
ALLOWED_APP = (*ALLOWED, "Bash(sandloom:*)", "Bash(uv lock:*)")
#: Gate name -> command, run in ``<worktree>``. ``{module_tests}`` is
#: replaced by the touched modules' test directories.
GATES: dict[str, list[str]] = {
    "ruff": ["uv", "run", "ruff", "check", "src/"],
    "ruff-format": ["uv", "run", "ruff", "format", "--check", "src/"],
    "mypy": ["uv", "run", "mypy", "src/"],
    "sandloom-check": ["uv", "run", "sandloom", "check"],
    "arch-manifest": ["uv", "run", "python", "tools/checks/check_architecture.py"],
    "contracts": ["uv", "run", "sandloom", "contracts", "snapshot", "--check"],
    "fast-suite": [
        "uv",
        "run",
        "pytest",
        "-q",
        "-m",
        "not slow",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=--tb=short --strict-markers --asyncio-mode=auto",
    ],
    "module-tests": [
        "uv",
        "run",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=--tb=short --strict-markers --asyncio-mode=auto",
        "{module_tests}",
    ],
}
READ_VERBS = re.compile(r"(^|[|;&]\s*|\s)(cat|sed|head|tail|less|nl|bat|awk)\s")
#: A kernel read is a DX defect: the docs should have answered it. Each path
#: maps to the topic the agent was looking for (first match wins) and the doc
#: that is meant to answer it.
DOC_GAPS: tuple[tuple[str, str, str], ...] = (
    (
        r"sandloom/(core|sdk/_[a-z]+)/testing",
        "testing kit",
        "docs/reference/testing.md",
    ),
    (
        r"sandloom/core/(module_api|di|facade|descriptors)\b|sandloom/sdk/_",
        "public signatures",
        "AGENTS.md cheat-sheet, docs/reference/sdk/index.md (sandloom docs)",
    ),
    (r"sandloom/core/projections/", "projections", "docs/modules/between/projections.md"),
    (
        r"sandloom/core/unit_of_work/versioning",
        "event versioning",
        "skill sandloom-event-version-roll, docs/modules/between/events.md",
    ),
    (
        r"sandloom/core/(eventing|consumers)/",
        "events and consumers",
        "docs/modules/between/events.md",
    ),
    (
        r"sandloom/core/unit_of_work/",
        "units, db() and effects",
        "docs/modules/database/units.md",
    ),
    (
        r"sandloom/core/(idempotency/|route_policy|route_markers)",
        "route policy and idempotency",
        "docs/modules/http/route-policy.md",
    ),
    (
        r"sandloom/core/(tenant|orm|http/tenant)",
        "tenancy",
        "docs/modules/database/units.md",
    ),
    (
        r"sandloom/core/(archcheck|cli)/",
        "checks and CLI",
        "docs/reference/error-codes.md",
    ),
    (
        r"sandloom/core/(loader|rpc|contract)",
        "contracts and bindings",
        "docs/modules/between/contracts.md",
    ),
    (
        r"sandloom/core/(config|auth|http)/",
        "settings, auth and http",
        "docs/modules/http/route-policy.md",
    ),
    (
        r"sandloom/shared/",
        "authz and shared DTOs",
        "docs/modules/http/authorization.md",
    ),
    (r"sandloom/core/", "other kernel", "—"),
)
#: How a path outside the task's module is classified:
#:
#: - ``framework``: the "framework-file tax", a DX defect;
#: - ``other-module``: another business module, an isolation breach;
#: - ``wiring``: deploy wiring (``sandloom add deploy`` writes it);
#: - ``rule``: a file ``sandloom generate`` writes in ``sandloom.shared``
#:   (contract stubs, DTO copies, event schemas);
#: - ``docs``: documentation the agent chose to update.
OUTSIDE_KINDS: tuple[tuple[str, str], ...] = (
    (r"^src/sandloom/shared/(contracts|events)/", "rule"),
    (
        r"^src/sandloom/main\.py$|^pyproject\.toml$|^\.env\.example$|^deploy/",
        "wiring",
    ),
    (
        r"^src/sandloom/(core|shared)/|^tests/|^docs/adr/|^\.github/"
        r"|^tools/"
        r"|^(architecture_rules\.yml|\.importlinter|public_api_baseline\.json)$",
        "framework",
    ),
    (r"^src/sandloom_[a-z0-9_]+/", "other-module"),
    (r"\.md$|^docs/", "docs"),
    (r".", "other"),
)


#: Files ``sandloom generate`` writes (``sandloom.core.cli.generated``): event
#: schemas, contract stubs and DTO copies (and their package ``__init__``), and each
#: module's module / route / contract snapshots. They follow the agent's edits,
#: so they are recorded (``generated_files``) but never count as edited outside.
GENERATED_RE = re.compile(
    r"^src/sandloom/shared/events/[^/]+\.json$"
    r"|^src/sandloom/shared/contracts/[a-z0-9_]+\.py$"
    r"|^src/sandloom_[a-z0-9_]+/(module|routes|contracts)\.snapshot\.json$"
)
#: First line of a generated stub / DTO copy; the package ``__init__`` docstrings.
GENERATED_HEADERS = (
    "# Generated by `sandloom generate`",
    '"""Contract DTO package (generated stubs and DTO copies)."""',
    '"""Contract DTO package (generated stubs)."""',
)


def is_generated(path: str, wt: Path | None = None) -> bool:
    """``path`` is a ``sandloom generate`` output. With the worktree at hand, a
    ``.py`` must also carry the generator's header (a hand-written module in
    ``shared/contracts`` stays an edit)."""
    if not GENERATED_RE.search(path):
        return False
    if wt is None or not path.endswith(".py") or not (wt / path).is_file():
        return True
    head = (wt / path).read_text(errors="replace").lstrip()
    return head.startswith(GENERATED_HEADERS)


def doc_gap(path: str) -> tuple[str, str]:
    for pattern, topic, doc in DOC_GAPS:
        if re.search(pattern, path):
            return topic, doc
    return "other kernel", "—"


def outside_kind(path: str) -> str:
    return next(kind for pattern, kind in OUTSIDE_KINDS if re.search(pattern, path))


def classify(result_row: dict[str, Any], tree_ref: str | None = None) -> dict[str, Any]:
    """Derived fields: kernel reads as doc gaps, outside edits by kind."""
    edited = set(result_row.get("edited_outside", []))
    reads = [  # a file the agent wrote itself is not a kernel read
        p
        for p in (
            *result_row.get("kernel_files_read", []),
            *result_row.get("shared_files_read", []),
        )
        if f"src/{p}" not in edited
    ]
    gaps = []
    for path in reads:
        topic, doc = doc_gap(path)
        exists = True
        if tree_ref and not path.startswith("repo:"):
            exists = (
                sh(["git", "cat-file", "-e", f"{tree_ref}:src/{path}"], REPO).returncode
                == 0
            )
        gaps.append({"path": path, "topic": topic, "doc": doc, "exists": exists})
    kinds: dict[str, list[str]] = {}
    app = result_row.get("app_dir", "").rsplit("/", 1)[-1]
    for path in result_row.get("edited_outside", []):
        kind = (
            appmode.classify_app_path(path, app)
            if result_row.get("mode") == "app"
            else outside_kind(path)
        )
        kinds.setdefault(kind, []).append(path)
    if result_row.get("mode") == "app":  # recorded, not counted as outside
        for path in result_row.get("expected_wiring", []):
            kinds.setdefault(
                "expected " + appmode.classify_app_path(path, app), []
            ).append(path)
    return {
        "kernel_reads": gaps,
        "outside_by_kind": kinds,
        "framework_edits": len(kinds.get("framework", [])),
        "other_module_edits": len(kinds.get("other-module", [])),
    }


#: A kernel read (spec 2.5): sandloom/core/**, sandloom/sdk/_*/**, sandloom/shared/{authz,guards}/**.
KERNEL_RE = re.compile(
    r"sandloom/(?:core/|sdk/_[a-z]+/|shared/(?:authz|guards)/)[\w/.-]+\.py"
)
SHARED_RE = re.compile(r"sandloom/shared/[\w/.-]+\.py")


@dataclass
class Task:
    id: str
    title: str
    allowed: list[str]
    prompt: str
    experiment: str = ""
    # product suite
    kind: str = "task"  # task | build | add | switch | change
    start: str = ""  # task id whose checkpoint this task starts from
    seed: str = ""  # app mode: the seed app (``seeds/<seed>/``) the task starts from
    modules: list[str] = field(default_factory=list)  # spec sections
    target: str = ""  # the one module a "change" task may touch
    hidden_deselect: list[str] = field(default_factory=list)
    # modes
    modes: list[str] = field(default_factory=lambda: ["framework"])
    #: Modes in which the task is a kernel-contributor task (it edits a bundled
    #: module or the framework repository's deploy profile): run only when
    #: named with --tasks, never by a whole-suite run.
    kernel_modes: list[str] = field(default_factory=list)
    mode: str = "framework"  # the mode this Task was loaded for
    app: str = ""  # app mode: the application's name (`sandloom new <app>`)
    app_allowed: list[str] = field(default_factory=list)  # relative to the app dir


@dataclass
class Result:
    task: str
    label: str
    ref: str
    model: str
    mode: str = "framework"  # app | framework (results before modes: framework)
    outcome: str = "not-run"  # pass | fail | error
    gates: dict[str, bool] = field(default_factory=dict)
    fn_checks: dict[str, bool] = field(default_factory=dict)
    dev_checks: dict[str, bool] = field(default_factory=dict)
    uncaught_deviations: list[str] = field(default_factory=list)
    tokens_total: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    wall_min: float = 0.0
    kernel_files_read: list[str] = field(default_factory=list)
    shared_files_read: list[str] = field(default_factory=list)
    docs_read: list[str] = field(default_factory=list)
    edited_outside: list[str] = field(default_factory=list)
    edited_inside: int = 0
    generated_files: list[str] = field(default_factory=list)  # sandloom generate outputs
    permission_denials: int = 0
    created_env_file: bool = False  # the agent hand-made the source checkout .env
    kernel_reads: list[dict[str, Any]] = field(default_factory=list)  # doc gaps
    outside_by_kind: dict[str, list[str]] = field(default_factory=dict)
    framework_edits: int = 0
    other_module_edits: int = 0
    # product suite
    kind: str = "task"
    acceptance: dict[str, bool] = field(default_factory=dict)
    footguns: dict[str, bool] = field(default_factory=dict)
    code_changes: list[str] = field(default_factory=list)
    config_changes: list[str] = field(default_factory=list)
    modules_touched: list[str] = field(default_factory=list)
    # app mode
    app_dir: str = ""  # the application's directory, relative to the task dir
    expected_wiring: list[str] = field(default_factory=list)  # not "outside"
    # how the diff base was made: transcript | inferred | checkpoint | seed:<name>
    pristine: str = ""
    error: str = ""


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


MODE_BLOCK = re.compile(r"^<!-- mode: (\w+) -->\n(.*?)^<!-- /mode -->\n?", re.M | re.S)


def for_mode(text: str, mode: str) -> str:
    """Keep the ``<!-- mode: <mode> -->`` … ``<!-- /mode -->`` blocks of
    ``mode`` (markers dropped), remove the other modes' blocks."""
    return MODE_BLOCK.sub(lambda m: m.group(2) if m.group(1) == mode else "", text)


def product_spec(modules: list[str], mode: str = "framework") -> str:
    """The product spec for ``modules`` (base text + one section each)."""
    parts = [for_mode((PRODUCT_DIR / "spec-base.md").read_text(), mode).strip()]
    parts += [
        for_mode((PRODUCT_DIR / "modules" / f"{m}.md").read_text(), mode).strip()
        for m in modules
    ]
    return "\n\n".join(parts) + "\n"


#: A framework module path and its app-mode equivalent.
_FRAMEWORK_MODULE = re.compile(r"^src/sandloom_([a-z0-9_]+)/")


def derive_app_allowed(allowed: list[str]) -> list[str]:
    """``src/sandloom_<m>/`` -> ``{app}/modules/<m>/``; ``deploy/`` stays."""
    return [
        _FRAMEWORK_MODULE.sub(r"{app}/modules/\1/", a)
        if _FRAMEWORK_MODULE.match(a)
        else a
        for a in allowed
    ]


def load_tasks(
    selected: list[str] | None = None,
    suite: str = "tasks",
    mode: str = "framework",
) -> list[Task]:
    """The suite's tasks, their prompts rendered for ``mode`` (every task is
    returned; :func:`runnable` filters by the task's ``modes``)."""
    tasks = []
    for path in sorted(SUITES[suite].glob("*.md")):
        raw = path.read_text()
        _, front, body = raw.split("+++", 2)
        meta = tomllib.loads(front)
        app = meta.get("app", DEFAULT_APP[suite])
        task = Task(
            id=meta["id"],
            title=meta["title"],
            allowed=list(meta["allowed"]),
            prompt=for_mode(body, mode).strip(),
            experiment=meta.get("experiment", ""),
            kind=meta.get("kind", "task"),
            start=meta.get("start", ""),
            seed=meta.get("seed", ""),
            modules=list(meta.get("modules", [])),
            target=meta.get("target", ""),
            hidden_deselect=list(meta.get("hidden_deselect", [])),
            modes=list(meta.get("modes", ["framework"])),
            kernel_modes=list(meta.get("kernel_modes", [])),
            mode=mode,
            app=app,
            app_allowed=[
                a.replace("{app}", app)
                for a in meta.get("app_allowed", derive_app_allowed(meta["allowed"]))
            ],
        )
        if "{spec}" in task.prompt:
            task.prompt = task.prompt.replace(
                "{spec}", product_spec(task.modules, mode)
            )
        task.prompt = task.prompt.replace("{app}", app)
        if not selected or task.id in selected or task.id.split("_")[0] in selected:
            tasks.append(task)
    return tasks


def runnable(
    tasks: list[Task], mode: str, *, explicit: bool
) -> tuple[list[Task], list[Task]]:
    """(tasks supporting ``mode``, the others). Asking for an unsupported task
    by name is an error; a whole-suite run skips it with a note. A task that
    is a kernel-contributor task in ``mode`` (``kernel_modes``) runs only when
    named: a whole-suite run skips it."""
    unsupported = [t for t in tasks if mode not in t.modes]
    if unsupported and explicit:
        raise SystemExit(
            f"--mode {mode} is not supported by: "
            + ", ".join(f"{t.id} (modes: {', '.join(t.modes)})" for t in unsupported)
            + " (README: 'Modes')"
        )
    skipped = [
        t
        for t in tasks
        if mode not in t.modes or (mode in t.kernel_modes and not explicit)
    ]
    ok = [t for t in tasks if t not in skipped]
    return ok, skipped


def task_allowed(task: Task) -> list[str]:
    """The task's own paths: repository-relative (framework) or app-relative."""
    return task.app_allowed if task.mode == "app" else task.allowed


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #

_alloc_lock = threading.Lock()
#: Owner marker of the sandloom test harness (sandloom.core.testing.ownership).
_REDIS_OWNER_KEY = "sandloom:test:owner"
#: One Redis index pair per task id, for good (all labels, all runner
#: processes): ``{task id: [cache, broker]}``. Two concurrent runs (another
#: suite, another label) never share a pair, and a pair is never lent to
#: another task.
_SLOTS_FILE = "redis-slots.json"


def reserved_redis_indexes() -> set[int]:
    """Indexes any eval run (any label, any runner process) was given."""
    found: set[int] = set()
    for env_file in eval_home().glob("*/*/env.json"):
        for key in ("SANDLOOM_TEST_CACHE_URL", "SANDLOOM_TEST_BROKER_URL"):
            url = json.loads(env_file.read_text()).get(key, "")
            if url.rsplit("/", 1)[-1].isdigit():
                found.add(int(url.rsplit("/", 1)[-1]))
    return found


@contextmanager
def _slots_locked() -> Iterator[dict[str, list[int]]]:
    """The task -> indexes map under an exclusive cross-process lock."""
    home = eval_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / _SLOTS_FILE
    with _alloc_lock, (home / (_SLOTS_FILE + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            slots = json.loads(path.read_text()) if path.is_file() else {}
            yield slots
            path.write_text(json.dumps(slots, indent=1, sort_keys=True))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _earlier_indexes(task_id: str) -> list[int] | None:
    for env_file in sorted(eval_home().glob(f"*/{task_id}/env.json")):
        old = json.loads(env_file.read_text())
        try:
            return [
                int(old[k].rsplit("/", 1)[-1])
                for k in ("SANDLOOM_TEST_CACHE_URL", "SANDLOOM_TEST_BROKER_URL")
            ]
        except (KeyError, ValueError):
            continue
    return None


def task_redis_indexes(task_id: str, owner: str) -> list[int]:
    """This task's own ``[cache, broker]`` indexes (see :data:`_SLOTS_FILE`),
    marked with ``owner`` at once so no other run's xdist worker allocation
    (``SANDLOOM_TEST_REDIS_DB_STRIDE=auto`` takes unmarked empty indexes) can claim
    them before this run starts."""
    import redis  # backend venv

    with _slots_locked() as slots:
        taken = {i for t, pair in slots.items() if t != task_id for i in pair}
        pair = slots.get(task_id)
        if pair is None:
            earlier = _earlier_indexes(task_id)
            if earlier is not None and not set(earlier) & taken:
                pair = earlier
        if pair is None:
            pair = []
            avoid = taken | reserved_redis_indexes()
            for idx in redis_candidates():
                if idx in avoid:
                    continue
                client = redis.Redis.from_url(f"{REDIS}/{idx}")
                try:
                    if client.dbsize() == 0:
                        pair.append(idx)
                finally:
                    client.close()
                if len(pair) == 2:
                    break
            if len(pair) < 2:
                raise RuntimeError(
                    f"no free Redis indexes for a new task ({task_id}): "
                    + redis_holders(slots)
                )
        slots[task_id] = pair
        for idx in pair:
            client = redis.Redis.from_url(f"{REDIS}/{idx}")
            try:
                client.set(_REDIS_OWNER_KEY, owner)
            finally:
                client.close()
    return pair


#: Per-task markers in ``<eval home>/<label>/<task>/``: ``RUNNING`` (the
#: runner's pid) while an agent or a rescore works on it, ``FINISHED`` after.
RUN_MARKER = "RUNNING"
DONE_MARKER = "FINISHED"
#: A run directory from before the markers counts as finished only once
#: nothing in it changed for this long (longer than any agent + gates).
LEGACY_IDLE_HOURS = 6.0
#: Redis indexes one ``sandloom test`` (xdist) run claims: workers x 2 bases + spares.
INDEXES_PER_SANDLOOM_TEST = 12


@contextmanager
def run_marker(out_dir: Path) -> Iterator[None]:
    """Mark ``out_dir`` as in use by this process; ``FINISHED`` afterwards."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / DONE_MARKER).unlink(missing_ok=True)
    (out_dir / RUN_MARKER).write_text(
        json.dumps({"pid": os.getpid(), "started": time.time()})
    )
    try:
        yield
    finally:
        (out_dir / DONE_MARKER).write_text(f"{time.time()}\n")
        (out_dir / RUN_MARKER).unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0: an existence check, nothing is sent
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_state(out_dir: Path) -> str:
    """``running`` | ``finished`` | ``unknown`` (any doubt keeps the slots)."""
    marker = out_dir / RUN_MARKER
    if marker.is_file():
        try:
            pid = int(json.loads(marker.read_text())["pid"])
        except (ValueError, KeyError, TypeError):
            return "unknown"
        return "running" if _pid_alive(pid) else "finished"
    if (out_dir / DONE_MARKER).is_file():
        return "finished"
    # A run from before the markers: its result is written last, so a result
    # newer than everything in the run directory means it finished.
    files = [p for d in (out_dir, out_dir / "gates") if d.is_dir() for p in d.iterdir()]
    newest = max((p.stat().st_mtime for p in files if p.is_file()), default=None)
    if newest is None:
        return "unknown"
    stored = RESULTS_DIR / out_dir.parent.name / f"{out_dir.name}.json"
    if stored.is_file() and stored.stat().st_mtime >= newest:
        return "finished"
    if time.time() - newest > LEGACY_IDLE_HOURS * 3600:
        return "finished"
    return "unknown"


def eval_owners() -> dict[str, Path]:
    """Test-resource owner of every eval run directory -> that directory."""
    found: dict[str, Path] = {}
    for env_file in eval_home().glob("*/*/env.json"):
        try:
            owner = json.loads(env_file.read_text()).get("SANDLOOM_TEST_OWNER")
        except (OSError, ValueError):
            continue
        if owner:
            found[owner] = env_file.parent
    return found


def _scan_redis(*, include_empty: bool = False) -> list[Any]:
    from sandloom.core.testing import resources  # backend venv

    return resources.scan_redis(f"{REDIS}/0", include_empty=include_empty)


def _index(resource: Any) -> int:
    return int(str(resource.name).rsplit("/", 1)[-1])


def redis_holders(slots: dict[str, list[int]] | None = None) -> str:
    """Who holds the test Redis's indexes, and how to free them."""
    try:
        scanned = _scan_redis()
    except Exception as exc:  # the explanation must never mask the error
        return f"(could not scan Redis: {exc})"
    owners: dict[str, int] = {}
    for r in scanned:
        key = r.owner or "<unmarked, not empty>"
        owners[key] = owners.get(key, 0) + 1
    top = ", ".join(
        f"{o} {n}" for o, n in sorted(owners.items(), key=lambda kv: -kv[1])[:8]
    )
    if slots is None:
        path = eval_home() / _SLOTS_FILE
        try:
            slots = json.loads(path.read_text()) if path.is_file() else {}
        except (OSError, ValueError):
            slots = {}
    reserved = sum(len(p) for p in slots.values())
    return (
        f"{len(scanned)} indexes are held (top owners: {top}); {reserved} of them "
        "are the eval's own per-task pairs. Free the indexes of finished eval "
        "runs with `uv run python benchmarks/dx/run_eval.py prune-slots` (it "
        "also runs before every `run`), and other stale ones with `uv run python "
        "tools/checks/test_resources.py prune --apply` (only those unused for --days)."
    )


def prune_stale_slots(*, apply: bool) -> dict[str, Any]:
    """Empty the Redis indexes held by FINISHED eval runs.

    Only an index marked with a ``dx-eval-*`` owner whose run directory is
    finished (:func:`run_state`), that nobody is connected to and that is not
    one of the eval's per-task pairs (:data:`_SLOTS_FILE`) is emptied, with
    the atomic check-and-FLUSHDB of ``sandloom.core.testing.resources``. Any other
    owner, a running run, or any doubt keeps the index.
    """
    from sandloom.core.testing import resources  # backend venv

    owners = eval_owners()
    pruned: list[str] = []
    kept: dict[str, int] = {}
    with _slots_locked() as slots:
        pairs = {i for pair in slots.values() for i in pair}
        pairs |= reserved_redis_indexes()
        for r in _scan_redis():
            owner = r.owner or ""
            if not owner.startswith("dx-eval-"):
                continue
            out_dir = owners.get(owner)
            state = run_state(out_dir) if out_dir else "unknown (no run directory)"
            if _index(r) in pairs:
                why = "per-task pair"
            elif state != "finished":
                why = state
            elif r.live:
                why = "connected"
            elif not apply or resources.prune_redis(f"{REDIS}/0", r):
                pruned.append(f"{_index(r)} ({owner})")
                continue
            else:
                why = "changed since the scan"
            kept[why] = kept.get(why, 0) + 1
        free = sum(
            1
            for r in _scan_redis(include_empty=True)
            if not r.owner and not r.size and not r.live and _index(r) not in pairs
        )
    return {"pruned": pruned, "kept": kept, "free": free, "applied": apply}


def report_prune(outcome: dict[str, Any], parallel: int) -> None:
    verb = "freed" if outcome["applied"] else "would free"
    free = outcome["free"] + (0 if outcome["applied"] else len(outcome["pruned"]))
    print(
        f"redis slots: {verb} {len(outcome['pruned'])} index(es) of finished eval "
        f"runs; eval-owned kept: {outcome['kept'] or 0}; free after: {free}",
        flush=True,
    )
    needed = INDEXES_PER_SANDLOOM_TEST * max(parallel, 1)
    if free < needed:
        print(
            f"WARNING: {free} free Redis indexes, but {parallel} parallel "
            f"agent(s) running `sandloom test` need about {needed}: its parallel steps "
            "will exit 4. " + redis_holders(),
            flush=True,
        )


def prune_before_run(parallel: int) -> None:
    """Free finished eval runs' Redis indexes; never fails a run by itself."""
    try:
        report_prune(prune_stale_slots(apply=True), parallel)
    except Exception as exc:
        print(f"redis slots: not pruned ({type(exc).__name__}: {exc})", flush=True)


def eval_home() -> Path:
    return Path(
        os.environ.get(
            "SANDLOOM_DX_EVAL_HOME", Path.home() / ".cache" / "sandloom-dx-eval"
        )
    )


def sh(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )


# --------------------------------------------------------------------------- #
# Worktree
# --------------------------------------------------------------------------- #


def make_worktree(task: Task, label: str, ref: str) -> tuple[Path, str]:
    wt = eval_home() / label / task.id / "worktree"
    if wt.exists():
        sh(["git", "worktree", "remove", "--force", str(wt)], REPO)
        shutil.rmtree(wt, ignore_errors=True)
    wt.parent.mkdir(parents=True, exist_ok=True)
    out = sh(["git", "worktree", "add", "--force", "--detach", str(wt), ref], REPO)
    if out.returncode:
        raise RuntimeError(out.stderr)
    hidden = [h for h in HIDDEN if (wt / h).exists()]
    if hidden:
        sh(["git", "rm", "-r", "-q", *hidden], wt)
        sh(
            [
                "git",
                "-c",
                "user.name=dx-eval",
                "-c",
                "user.email=dx-eval@localhost",
                "commit",
                "-q",
                "--no-verify",
                "-m",
                "dx-eval: hide checkers",
            ],
            wt,
        )
    base = sh(["git", "rev-parse", "HEAD"], wt).stdout.strip()
    sync = sh(["uv", "sync", "--frozen", "--quiet"], wt, timeout=900)
    if sync.returncode:
        raise RuntimeError(f"uv sync failed: {sync.stderr[-2000:]}")
    return wt, base


def agent_env(task: Task, label: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SANDLOOM_", "VIRTUAL_ENV", "CLAUDE_CODE_SSE"))
        and k not in {"DATABASE_URL", "CACHE_URL", "BROKER_URL", "JWT_SECRET"}
    }
    # One index pair per task id, for good, marked for this run at once:
    # concurrent runs never share one (see task_redis_indexes). An earlier
    # label's run of the same task is finished, so taking it over is safe.
    owner = f"dx-eval-{label}-{task.id}"
    cache, broker = task_redis_indexes(task.id, owner)
    take_over = True
    # Short: the harness's own tests append suffixes to the database name, and
    # PostgreSQL truncates identifiers at 63 bytes.
    slug = hashlib.sha1(
        f"{label}/{task.id}".encode(), usedforsecurity=False
    ).hexdigest()[:10]
    env.update(
        SANDLOOM_TEST_OWNER=owner,
        SANDLOOM_TEST_DATABASE_URL=f"{PG}/{DB_PREFIX}{slug}",
        SANDLOOM_TEST_CACHE_URL=f"{REDIS}/{cache}",
        SANDLOOM_TEST_BROKER_URL=f"{REDIS}/{broker}",
    )
    if take_over:
        env["SANDLOOM_TEST_TAKE_OWNERSHIP"] = "1"
    return env


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


def agent_prompt(task: Task) -> str:
    """The task text plus the footer of its mode."""
    if task.mode != "app":
        return task.prompt + "\n" + FOOTER.read_text()
    if task.start or task.seed:
        start = (
            f"- The product is the Sandloom application in `{task.app}/`; work inside it.\n"
            "  Its `AGENTS.md` is the project's agent guide."
        )
    else:
        start = (
            "- This directory is empty: the product does not exist yet. Start it\n"
            f"  here as a new Sandloom application named `{task.app}` with `sandloom new` (`sandloom\n"
            "  new --help` lists the options). The product has user accounts,\n"
            "  organizations and billing: use Sandloom's bundled modules for them. Then\n"
            f"  work inside `{task.app}/`; its `AGENTS.md` is the project's agent guide."
        )
    footer = FOOTER_APP.read_text().replace("{start}", start)
    return task.prompt + "\n" + footer.replace("{app}", task.app)


# --------------------------------------------------------------------------- #
# App mode (appmode.py holds the mechanics)
# --------------------------------------------------------------------------- #


def app_home() -> Path:
    """Where app-mode agents work: outside the repository and away from the
    eval's transcripts and framework worktrees (``SANDLOOM_DX_APP_HOME``)."""
    return Path(
        os.environ.get("SANDLOOM_DX_APP_HOME", eval_home().parent / "sandloom-dx-apps")
    )


def work_dir(label: str, task_id: str) -> Path:
    return app_home() / label / task_id


def app_checkpoint(label: str, task_id: str) -> Path:
    """An app-mode task's product, the start of its follow-ups."""
    return eval_home() / label / task_id / "checkpoint"


SEEDS_DIR = HERE / "seeds"


def seed_app(task: Task, label: str, dist: appmode.Dist) -> Path:
    """The built seed app of a seeded task (``seeds/<seed>/``), once per label
    and dist: ``<eval home>/<label>/_seed/<seed>/<app>``."""
    probe = Task(
        id=f"_seed_{task.seed}", title="seed", allowed=[], prompt="", mode="app",
        app=task.app,
    )  # fmt: skip
    return appmode.build_seed(
        SEEDS_DIR / task.seed,
        dist,
        eval_home() / label / "_seed" / task.seed,
        app_env_for(probe, label, dist),
    )


def make_work_dir(task: Task, label: str, seed: Path | None = None) -> Path:
    """An empty task directory (or, for a follow-up, its start's product; for
    a seeded task, a copy of the built seed app ``seed``)."""
    work = work_dir(label, task.id)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    if seed is not None:
        appmode.copy_app(seed, work / seed.name)
    elif task.start:
        start = app_checkpoint(label, task.start)
        apps = [p for p in start.iterdir() if p.is_dir()] if start.is_dir() else []
        if not apps:
            raise RuntimeError(f"no app checkpoint of {task.start}: run it first")
        for app in apps:
            appmode.copy_app(app, work / app.name)
    return work


def bash_commands(transcript: Path) -> list[str]:
    """Every Bash command of the session, in order."""
    commands: list[str] = []
    if not transcript.is_file():
        return commands
    for line in transcript.read_text().splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") != "assistant":
            continue
        for block in msg.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                commands.append(str(block.get("input", {}).get("command", "")))
    return commands


def score_app(
    task: Task,
    work: Path,
    env: dict[str, str],
    out_dir: Path,
    result: Result,
    dist: appmode.Dist,
    *,
    base_app: Path | None = None,
) -> None:
    """Score an app-mode task: the diff against what ``sandloom new`` (or the start
    checkpoint, or the seed app) produced, the app's own gates, the hidden
    checks with the app layout, and (product suite) the hidden acceptance /
    footgun tests. ``base_app`` overrides the diff base (the dry run's
    stand-in for a follow-up's start checkpoint)."""
    final = parse_transcript(
        out_dir / "transcript.jsonl", work, result, repo_reads=True
    )
    (out_dir / "report.md").write_text(final)
    app = appmode.find_app(work, task.app)
    if app is None:
        result.outcome = "fail"
        result.error = f"no Sandloom application under {work} (sandloom new never ran?)"
        return
    result.app_dir = app.relative_to(work).as_posix()
    # A run on another build of the framework measures nothing (the agent's
    # own edits of installed files are scored below, as tampering).
    stale = (
        appmode.framework_version_mismatches(app, dist)
        if (app / ".venv").is_dir()
        else []
    )
    if stale:
        result.outcome = "error"
        result.error = f"the app's framework is not {dist.sha[:12]}: {'; '.join(stale)}"
        return
    pristine_root = out_dir / "pristine"
    if base_app is not None:
        appmode.copy_app(base_app, pristine_root / app.name)
        result.pristine = "stand-in"
    elif task.seed:
        appmode.copy_app(seed_app(task, result.label, dist), pristine_root / app.name)
        result.pristine = f"seed:{task.seed}"
    elif task.start:
        appmode.copy_app(app_checkpoint(result.label, task.start) / app.name,
                         pristine_root / app.name)  # fmt: skip
        result.pristine = "checkpoint"
    else:
        result.pristine = appmode.make_pristine(
            app, bash_commands(out_dir / "transcript.jsonl"), dist, pristine_root, env
        )
    pristine = pristine_root / app.name
    changed = appmode.app_changes(app, pristine)
    changed += appmode.tampered_framework_files(app)
    (out_dir / "changes.patch").write_text(appmode.app_patch(app, pristine, changed))
    (out_dir / "changed.txt").write_text("\n".join(changed) + "\n")
    gen = appmode.generated_re(app.name)
    result.generated_files = [
        c
        for c in changed
        if gen.search(c)
        and (
            not c.endswith(".py")
            or not (app / c).is_file()
            or (app / c)
            .read_text(errors="replace")
            .lstrip()
            .startswith(GENERATED_HEADERS)
        )
    ]
    edits = [c for c in changed if c not in result.generated_files]
    allowed = [a.removeprefix(f"{task.app}/") for a in task.app_allowed]
    allowed = [f"{app.name}/{a}" if a.startswith("modules/") else a for a in allowed]
    outside_ = [c for c in edits if not any(c.startswith(p) for p in allowed)]
    result.expected_wiring = [
        c
        for c in outside_
        if appmode.classify_app_path(c, app.name) in appmode.EXPECTED_KINDS
    ]
    result.edited_outside = [c for c in outside_ if c not in result.expected_wiring]
    result.edited_inside = len(edits) - len(outside_)
    result.created_env_file = False  # `sandloom new` writes the app's .env itself
    tests = sorted(
        f"{a.rstrip('/')}/tests"
        for a in allowed
        if (app / a / "tests").is_dir() and any(c.startswith(a) for c in changed)
    )
    result.gates = appmode.run_app_gates(
        app,
        env,
        tests,
        out_dir / "gates",
        require_module_tests=task.kind not in {"switch"},
    )
    result.fn_checks, result.dev_checks = run_checks(
        task, work, "", out_dir, app=app, changed_file=out_dir / "changed.txt"
    )
    gates_green = all(result.gates.values())
    result.outcome = (
        "pass" if gates_green and all(result.fn_checks.values()) else "fail"
    )
    if gates_green:
        result.uncaught_deviations = dev_failures(asdict(result))
    if task.kind != "task":
        result.code_changes = [
            c for c in changed if c.endswith(".py") and "/tests/" not in c
            and not c.startswith("tests/")
        ]  # fmt: skip
        result.config_changes = [c for c in changed if c not in result.code_changes]
        prefix = f"{app.name}/modules/"
        touched = sorted(
            {c[len(prefix) :].split("/")[0] for c in changed if c.startswith(prefix)}
        )
        own = {a[len(prefix) :].split("/")[0] for a in allowed if a.startswith(prefix)}
        result.modules_touched = [
            m for m in touched if m != task.target and m not in own
        ]
        result.acceptance, result.footguns = run_hidden_product(
            task, app, env, out_dir, app_mode=True
        )
        if not result.acceptance or not all(result.acceptance.values()):
            result.outcome = "fail"


def run_agent(
    task: Task,
    wt: Path,
    env: dict[str, str],
    out_dir: Path,
    *,
    model: str,
    budget_usd: float,
    timeout_min: float,
) -> None:
    prompt = agent_prompt(task)
    allowed = ALLOWED_APP if task.mode == "app" else ALLOWED
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--permission-mode",
        "auto",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--max-budget-usd",
        str(budget_usd),
        "--allowedTools",
        *allowed,
    ]
    with (out_dir / "transcript.jsonl").open("w") as stream:
        proc = subprocess.Popen(
            cmd,
            cwd=wt,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)  # our own child's group only
            proc.wait(timeout=30)
            (out_dir / "TIMEOUT").write_text(f"killed after {timeout_min} min\n")


#: App mode also counts ``grep -A/-B/-C <n>`` with n >= 20 as reading source.
GREP_CONTEXT = re.compile(r"\bgrep\b[^|;&\n]*\s-[ABC]\s*([2-9]\d|\d{3,})\b")


def cd_resolved_paths(cmd: str) -> str:
    """Relative ``.py`` paths of ``cmd`` joined to each directory it ``cd``s
    into (one per line): a ``cd …/site-packages/sandloom; sed -n 1,80p
    shared/authz/x.py`` reads ``…/sandloom/shared/authz/x.py``."""
    dirs = re.findall(r"(?:^|[;&|\n]\s*)cd\s+([^\s;&|]+)", cmd)
    rels = re.findall(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.py)\b", cmd)
    return "\n".join(f"{d.rstrip('/')}/{r}" for d in dirs for r in rels)


def framework_roots() -> tuple[str, ...]:
    """The framework repository's checkouts (this worktree and the main one):
    in app mode any read under them is a read of framework source."""
    common = sh(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], REPO
    )
    roots = {str(REPO)}
    if common.returncode == 0 and common.stdout.strip():
        roots.add(str(Path(common.stdout.strip()).parent))
    return tuple(sorted(roots, key=len, reverse=True))


def parse_transcript(
    path: Path, wt: Path, result: Result, *, repo_reads: bool = False
) -> str:
    """Fill transcript metrics into ``result``; return the agent's final text.

    A kernel read is a ``Read`` (or a ``cat``/``sed``/… in Bash) of an
    ``sandloom/core``, ``sandloom/sdk/_*`` or ``sandloom/shared/{authz,guards}`` file —
    in app mode those are the installed package's files (site-packages).
    With ``repo_reads`` (app mode) any read under the framework repository
    counts too, recorded as ``repo:<path>``."""
    roots = framework_roots() if repo_reads else ()
    repo_re = (
        re.compile("(?:" + "|".join(re.escape(r) for r in roots) + r")/[\w/.-]+")
        if roots
        else None
    )
    kernel: set[str] = set()
    shared: set[str] = set()
    docs: set[str] = set()
    final = ""
    wall_ms = 0.0
    last_usage: dict[str, Any] = {}
    for line in path.read_text().splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") != "tool_use":
                    continue
                result.tool_calls += 1
                inp = block.get("input", {})
                paths: list[str] = []
                if block.get("name") == "Read":
                    paths = [str(inp.get("file_path", ""))]
                elif block.get("name") == "Bash" and (
                    READ_VERBS.search(str(inp.get("command", "")))
                    or (repo_reads and GREP_CONTEXT.search(str(inp.get("command", ""))))
                ):
                    cmd = str(inp.get("command", ""))
                    if (
                        repo_reads
                    ):  # app mode: `cd …/site-packages/sandloom; sed … x.py`
                        cmd += "\n" + cd_resolved_paths(cmd)
                    paths = (
                        KERNEL_RE.findall(cmd)
                        + SHARED_RE.findall(cmd)
                        + re.findall(r"[\w/.-]*docs/[\w/. -]+\.md", cmd)
                    )
                    if repo_re is not None:
                        paths += repo_re.findall(cmd)
                for p in paths:
                    root = next((r for r in roots if p.startswith(r + "/")), None)
                    if root is not None:  # app mode: framework source, not the app
                        kernel.add("repo:" + p[len(root) + 1 :])
                        continue
                    rel = p.replace(str(wt) + "/", "")
                    if "/tests/" in rel:
                        continue
                    if KERNEL_RE.search(rel):
                        kernel.add(KERNEL_RE.search(rel).group(0))  # type: ignore[union-attr]
                    elif SHARED_RE.search(rel):
                        shared.add(SHARED_RE.search(rel).group(0))  # type: ignore[union-attr]
                    elif "docs/" in rel and rel.endswith(".md"):
                        docs.add(rel[rel.index("docs/") :])
        elif msg.get("type") == "result":
            # A session that started background work gets one result per
            # wake-up: turns and wall time add up, cost and modelUsage are
            # cumulative (take the last), the report is the longest text.
            text = str(msg.get("result") or "")
            if len(text) > len(final):
                final = text
            result.turns += int(msg.get("num_turns") or 0)
            wall_ms += float(msg.get("duration_ms") or 0)
            result.cost_usd = float(msg.get("total_cost_usd") or 0.0)
            last_usage = msg.get("modelUsage") or {}
            result.permission_denials += len(msg.get("permission_denials") or [])
            if msg.get("is_error"):
                result.error = str(msg.get("subtype"))
    result.wall_min = round(wall_ms / 60000, 1)
    for usage in last_usage.values():
        result.tokens_output += int(usage.get("outputTokens", 0))
        result.tokens_total += sum(
            int(usage.get(k, 0))
            for k in (
                "inputTokens",
                "outputTokens",
                "cacheReadInputTokens",
                "cacheCreationInputTokens",
            )
        )
    result.kernel_files_read = sorted(kernel)
    result.shared_files_read = sorted(shared)
    result.docs_read = sorted(docs)
    return final


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def changed_paths(wt: Path, base: str) -> list[str]:
    tracked = sh(["git", "diff", "--name-only", base], wt).stdout.split()
    untracked = sh(
        ["git", "ls-files", "--others", "--exclude-standard"], wt
    ).stdout.split()
    return sorted(set(tracked) | set(untracked))


def full_diff(wt: Path, base: str, out_dir: Path) -> str:
    """``git diff <base>`` including untracked (not ignored) files — a new
    module is usually all untracked. They are marked intent-to-add in a
    throwaway copy of the index, so the worktree's own index is untouched."""
    index = Path(sh(["git", "rev-parse", "--git-path", "index"], wt).stdout.strip())
    if not index.is_absolute():
        index = wt / index
    scratch = out_dir / "patch.index"
    try:
        if index.is_file():
            shutil.copyfile(index, scratch)
        env = {**os.environ, "GIT_INDEX_FILE": str(scratch)}
        sh(["git", "add", "--intent-to-add", "."], wt, env=env)
        return sh(["git", "diff", "--binary", base], wt, env=env).stdout
    finally:
        scratch.unlink(missing_ok=True)


def touched_test_dirs(task: Task, changed: list[str], wt: Path) -> list[str]:
    dirs = set()
    for prefix in task.allowed:
        tests = wt / prefix / "tests"
        if tests.is_dir() and any(c.startswith(prefix) for c in changed):
            dirs.add(str(tests.relative_to(wt)))
    return sorted(dirs)


def gate_env(wt: Path, env: dict[str, str]) -> dict[str, str]:
    """The gates' settings: ``.env.test`` (minus ENVIRONMENT) under the task's
    own ``SANDLOOM_TEST_*`` resources — the same on every ref, whether or not the
    tree under test can bootstrap a fresh checkout by itself."""
    from dotenv import dotenv_values  # backend venv

    values = {
        k: v
        for k, v in dotenv_values(wt / ".env.test").items()
        if v is not None and k != "ENVIRONMENT"
    }
    for var in ("DATABASE_URL", "CACHE_URL", "BROKER_URL"):
        if env.get(f"SANDLOOM_TEST_{var}"):
            values[var] = env[f"SANDLOOM_TEST_{var}"]
    merged = {**values, **env}
    # Taking over a borrowed index is for the agent's first run only; the
    # harness's own ownership tests fail with it set.
    merged.pop("SANDLOOM_TEST_TAKE_OWNERSHIP", None)
    return merged


def run_gates(
    task: Task, wt: Path, env: dict[str, str], changed: list[str], out_dir: Path
) -> dict[str, bool]:
    results: dict[str, bool] = {}
    env = gate_env(wt, env)
    # CI never sees an untracked .env: an agent-made one (typically copied from
    # .env.example) is set aside while the gates run, and recorded.
    agent_env_file = wt / ".env"
    parked = wt / ".env.dx-eval-parked"
    if agent_env_file.exists():
        agent_env_file.rename(parked)
    module_tests = touched_test_dirs(task, changed, wt)
    logs = out_dir / "gates"
    logs.mkdir(exist_ok=True)
    for name, template in GATES.items():
        cmd: list[str] = []
        for part in template:
            cmd.extend(module_tests if part == "{module_tests}" else [part])
        if name == "module-tests" and task.kind == "switch":
            continue  # a deploy-only task owns no module tests
        if name == "module-tests" and not module_tests:
            results[name] = False
            (logs / f"{name}.log").write_text("no tests in the task's module(s)\n")
            continue
        try:
            done = sh(cmd, wt, env=env, timeout=1800)
            results[name] = done.returncode == 0
            (logs / f"{name}.log").write_text(
                done.stdout[-20000:] + done.stderr[-20000:]
            )
        except subprocess.TimeoutExpired:
            results[name] = False
            (logs / f"{name}.log").write_text("timeout\n")
    if parked.exists():
        parked.rename(agent_env_file)
    return results


def run_checks(
    task: Task,
    wt: Path,
    base: str,
    out_dir: Path,
    *,
    app: Path | None = None,
    changed_file: Path | None = None,
) -> tuple[dict[str, bool], dict[str, bool]]:
    """The hidden ``checks/check_<task>.py``: against the framework worktree
    (``DX_WORKTREE`` + ``DX_BASE``) or, in app mode, the app (``DX_APP`` + the
    changed paths in ``DX_CHANGED``). A skipped check is left out."""
    junit = out_dir / "checks.xml"
    junit.unlink(missing_ok=True)
    env = {**os.environ, "DX_WORKTREE": str(wt), "DX_BASE": base}
    for var in ("DX_APP", "DX_CHANGED"):
        env.pop(var, None)
    if app is not None:
        env["DX_APP"] = str(app)
        if changed_file is not None:
            env["DX_CHANGED"] = str(changed_file)
    done = sh(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            str(CHECKS_DIR / "pytest.ini"),
            str(CHECKS_DIR / f"check_{task.id}.py"),
            f"--junitxml={junit}",
        ],
        CHECKS_DIR,
        env=env,
    )
    (out_dir / "checks.log").write_text(done.stdout + done.stderr)
    fn: dict[str, bool] = {}
    dev: dict[str, bool] = {}
    if junit.exists():
        for case in ET.parse(junit).iter("testcase"):
            name = case.get("name", "")
            if any(child.tag == "skipped" for child in case):
                continue  # the rule does not apply in this layout
            ok = not any(child.tag in {"failure", "error"} for child in case)
            (dev if name.startswith("test_dev_") else fn)[name] = ok
    return fn, dev


def outside(task: Task, changed: list[str]) -> list[str]:
    return [c for c in changed if not any(c.startswith(p) for p in task.allowed)]


def score(
    task: Task, wt: Path, base: str, env: dict[str, str], out_dir: Path, result: Result
) -> None:
    final = parse_transcript(out_dir / "transcript.jsonl", wt, result)
    (out_dir / "report.md").write_text(final)
    changed = changed_paths(wt, base)
    (out_dir / "changes.patch").write_text(full_diff(wt, base, out_dir))
    result.generated_files = [c for c in changed if is_generated(c, wt)]
    edits = [c for c in changed if c not in result.generated_files]
    result.edited_outside = outside(task, edits)
    result.edited_inside = len(edits) - len(result.edited_outside)
    result.created_env_file = (wt / ".env").exists()
    result.gates = run_gates(task, wt, env, changed, out_dir)
    result.fn_checks, result.dev_checks = run_checks(task, wt, base, out_dir)
    gates_green = all(result.gates.values())
    result.outcome = (
        "pass" if gates_green and all(result.fn_checks.values()) else "fail"
    )
    if gates_green:
        result.uncaught_deviations = [
            k for k, ok in result.dev_checks.items() if not ok
        ]
    if task.kind != "task":
        score_product(task, wt, env, changed, out_dir, result)


PRODUCT_MODULE_RE = re.compile(r"^src/sandloom_([a-z0-9_]+)/")


def score_product(
    task: Task,
    wt: Path,
    env: dict[str, str],
    changed: list[str],
    out_dir: Path,
    result: Result,
) -> None:
    """Hidden acceptance tests + footgun probes, and the change-shape metrics."""
    result.code_changes = [
        c
        for c in changed
        if c.endswith(".py") and c.startswith("src/") and "/tests/" not in c
    ]
    result.config_changes = [c for c in changed if c not in result.code_changes]
    touched = sorted({m.group(1) for c in changed if (m := PRODUCT_MODULE_RE.match(c))})
    allowed = {m.group(1) for a in task.allowed if (m := PRODUCT_MODULE_RE.match(a))}
    result.modules_touched = [
        m for m in touched if m != task.target and m not in allowed
    ]
    result.acceptance, result.footguns = run_hidden_product(task, wt, env, out_dir)
    if not result.acceptance or not all(result.acceptance.values()):
        result.outcome = "fail"


def run_hidden_product(
    task: Task,
    wt: Path,
    env: dict[str, str],
    out_dir: Path,
    *,
    app_mode: bool = False,
) -> tuple[dict[str, bool], dict[str, bool]]:
    """The hidden acceptance tests and footgun probes, copied into the product
    only while they run: ``tests/_dx_product`` of the framework
    worktree, or ``tests/_dx_product`` of the app (``wt`` is then the app root;
    the app's own pytest config loads the framework's test plugin)."""
    if app_mode:
        target = wt / "tests" / "_dx_product"
        cwd, run_env = (
            wt,
            {
                **{k: v for k, v in env.items() if k != "SANDLOOM_TEST_TAKE_OWNERSHIP"},
                "DX_APP": str(wt),
            },
        )
        opts = ["--tb=short"]
    else:
        target = wt / "tests" / "_dx_product"
        cwd, run_env = wt, {**gate_env(wt, env), "DX_WORKTREE": str(wt)}
        opts = ["-o", "addopts=--tb=short --strict-markers --asyncio-mode=auto"]
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(PRODUCT_DIR / "hidden", target)
    shutil.copy(CHECKS_DIR / "_dx.py", target / "_dx.py")
    junit = out_dir / "product.xml"
    junit.unlink(missing_ok=True)
    cmd = [
        "uv",
        "run",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *opts,
        str(target.relative_to(cwd)),
        f"--junitxml={junit}",
    ]
    if task.hidden_deselect:
        cmd += ["-k", " and ".join(f"not {t}" for t in task.hidden_deselect)]
    try:
        done = sh(cmd, cwd, env=run_env, timeout=1800)
        (out_dir / "product.log").write_text(
            done.stdout[-30000:] + done.stderr[-10000:]
        )
    finally:
        shutil.rmtree(target, ignore_errors=True)
    acc: dict[str, bool] = {}
    fg: dict[str, bool] = {}
    not_run: list[str] = []
    if junit.exists():
        for case in ET.parse(junit).iter("testcase"):
            name = case.get("name", "").split("[")[0]
            if any(child.tag == "skipped" for child in case):
                continue
            bad = [child for child in case if child.tag in {"failure", "error"}]
            ok = not bad
            if (
                not ok
                and name.startswith("test_fg_")
                and any(
                    "SetupFailed" in ((child.get("message") or "") + (child.text or ""))
                    for child in bad
                )
            ):
                # The probe could not set itself up (the product's own feature
                # is broken; acceptance reports that): it proves nothing about
                # the footgun, so it is not evaluated, never counted an escape.
                not_run.append(name)
                continue
            (fg if name.startswith("test_fg_") else acc)[name] = ok
    if not_run:
        (out_dir / "probes-not-evaluated.txt").write_text("\n".join(not_run) + "\n")
    return acc, fg


def checkpoint(task: Task, wt: Path, label: str) -> None:
    """Commit the agent's product as ``refs/dx-eval/<label>/<task>`` (follow-ups
    start from it). ``.gitignore``d files (``.env``, venvs) stay out."""
    sh(["git", "add", "-A"], wt)
    sh(
        [
            "git",
            "-c",
            "user.name=dx-eval",
            "-c",
            "user.email=dx-eval@localhost",
            "commit",
            "-q",
            "--no-verify",
            "--allow-empty",
            "-m",
            f"dx-eval: {task.id}",
        ],
        wt,
    )
    head = sh(["git", "rev-parse", "HEAD"], wt).stdout.strip()
    sh(["git", "update-ref", checkpoint_ref(label, task.id), head], REPO)


def checkpoint_ref(label: str, task_id: str) -> str:
    return f"refs/dx-eval/{label}/{task_id}"


def run_task(task: Task, args: argparse.Namespace) -> Result:
    out_dir = eval_home() / args.label / task.id
    if task.mode == "app":
        # The framework under test is the wheels of --ref; a follow-up starts
        # from its start task's app checkpoint, not from a git ref.
        ref = sh(["git", "rev-parse", "--short", args.ref], REPO).stdout.strip()
        result = Result(
            task=task.id,
            label=args.label,
            ref=ref,
            model=args.model,
            kind=task.kind,
            mode="app",
        )
        with run_marker(out_dir):
            _run_app_task(task, args, result, out_dir)
            write_result(result, out_dir)
        print(f"[{task.id}] {result.outcome}", flush=True)
        return result
    start = checkpoint_ref(args.label, task.start) if task.start else args.ref
    ref = sh(["git", "rev-parse", "--short", start], REPO).stdout.strip()
    result = Result(
        task=task.id, label=args.label, ref=ref, model=args.model, kind=task.kind
    )
    with run_marker(out_dir):
        _run_task(task, args, result, start, out_dir)
        write_result(result, out_dir)
    print(f"[{task.id}] {result.outcome}", flush=True)
    return result


def app_env_for(task: Task, label: str, dist: appmode.Dist) -> dict[str, str]:
    return appmode.app_agent_env(agent_env(task, label), dist, REPO)


def _run_app_task(
    task: Task, args: argparse.Namespace, result: Result, out_dir: Path
) -> None:
    started = time.monotonic()
    try:
        dist: appmode.Dist = args.dist
        seed = seed_app(task, args.label, dist) if task.seed else None
        work = make_work_dir(task, args.label, seed)
        env = app_env_for(task, args.label, dist)
        (out_dir / "env.json").write_text(
            json.dumps(
                {k: v for k, v in env.items() if k.startswith("SANDLOOM_TEST_")},
                indent=2,
            )
        )
        (out_dir / "app-mode.json").write_text(
            json.dumps(
                {"work": str(work), "dist": str(dist.root), "sha": dist.sha}, indent=2
            )
            + "\n"
        )
        print(f"[{task.id}] agent starting in {work} (app mode)", flush=True)
        run_agent(
            task,
            work,
            env,
            out_dir,
            model=args.model,
            budget_usd=args.budget_usd,
            timeout_min=args.timeout_min,
        )
        print(
            f"[{task.id}] agent done after {(time.monotonic() - started) / 60:.0f} min; scoring",
            flush=True,
        )
        score_app(task, work, env, out_dir, result, dist)
        app = work / result.app_dir if result.app_dir else None
        if task.kind != "task" and app is not None and app.is_dir():
            appmode.copy_app(app, app_checkpoint(args.label, task.id) / app.name)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    except Exception as exc:  # recorded, never aborts the other tasks
        result.outcome = "error"
        result.error = f"{type(exc).__name__}: {exc}"[:2000]


def _run_task(
    task: Task, args: argparse.Namespace, result: Result, start: str, out_dir: Path
) -> None:
    ref = result.ref
    started = time.monotonic()
    try:
        if not ref:
            raise RuntimeError(f"no checkpoint {start}: run {task.start} first")
        wt, base = make_worktree(task, args.label, start)
        (out_dir / "base.txt").write_text(base + "\n")
        env = agent_env(task, args.label)
        (out_dir / "env.json").write_text(
            json.dumps(
                {k: v for k, v in env.items() if k.startswith("SANDLOOM_TEST_")},
                indent=2,
            )
        )
        print(f"[{task.id}] agent starting in {wt}", flush=True)
        run_agent(
            task,
            wt,
            env,
            out_dir,
            model=args.model,
            budget_usd=args.budget_usd,
            timeout_min=args.timeout_min,
        )
        print(
            f"[{task.id}] agent done after {(time.monotonic() - started) / 60:.0f} min; scoring",
            flush=True,
        )
        score(task, wt, base, env, out_dir, result)
        if task.kind != "task":
            checkpoint(task, wt, args.label)
        if not args.keep:
            sh(["git", "worktree", "remove", "--force", str(wt)], REPO)
    except Exception as exc:  # recorded, never aborts the other tasks
        result.outcome = "error"
        result.error = f"{type(exc).__name__}: {exc}"[:2000]


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


def write_result(result: Result, out_dir: Path) -> None:
    derived = classify(asdict(result), result.ref or None)
    result.kernel_reads = derived["kernel_reads"]
    result.outside_by_kind = derived["outside_by_kind"]
    result.framework_edits = derived["framework_edits"]
    result.other_module_edits = derived["other_module_edits"]
    dest = RESULTS_DIR / result.label
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{result.task}.json").write_text(
        json.dumps(asdict(result), indent=2) + "\n"
    )
    report = out_dir / "report.md"
    if report.exists():
        (dest / f"{result.task}.report.md").write_text(
            report.read_text().strip() + "\n"
        )


def load_results(label: str) -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text())
        for p in sorted((RESULTS_DIR / label).glob("[tp][0-9]*.json"))
    ]


def errored(row: dict[str, Any]) -> bool:
    """The task did not run to a score: a harness error (``outcome=error``,
    e.g. no free Redis index) or an agent that never produced a token."""
    return row.get("outcome") in {"error", "not-run"} or not row.get("tokens_total")


def dev_failures(row: dict[str, Any]) -> list[str]:
    """Failed ``test_dev_*`` checks, whatever the gates said."""
    return [k for k, ok in row.get("dev_checks", {}).items() if not ok]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline: ``dev_checks_failed`` (every failed ``test_dev_*``, gates
    green or not). ``uncaught_deviations`` (failed dev checks of tasks whose
    gates were all green) is secondary: it moves with gate noise. Means are
    over the tasks that ran; errored tasks are listed, not averaged in."""
    ran = [r for r in rows if not errored(r)]
    n = len(ran) or 1

    def mean(key: str) -> float:
        return round(
            sum(len(r[key]) if isinstance(r[key], list) else r[key] for r in ran) / n,
            1,
        )

    return {
        "mode": ",".join(sorted({r.get("mode", "framework") for r in rows})) or "-",
        "tasks": len(rows),
        "tasks_ran": len(ran),
        "errored": [r["task"] for r in rows if errored(r)],
        "passed": sum(r["outcome"] == "pass" for r in rows),
        "dev_checks_failed": sum(len(dev_failures(r)) for r in ran),
        "tasks_free_of_dev_failures": sum(
            r["outcome"] == "pass" and not dev_failures(r) for r in ran
        ),
        "uncaught_deviations": sum(len(r["uncaught_deviations"]) for r in rows),
        "tasks_free_of_uncaught_deviation": sum(
            r["outcome"] == "pass" and not r["uncaught_deviations"] for r in rows
        ),
        "mean_tokens_total": int(mean("tokens_total")),
        "mean_turns": mean("turns"),
        "mean_tool_calls": mean("tool_calls"),
        "mean_wall_min": mean("wall_min"),
        "mean_kernel_files_read": mean("kernel_files_read"),
        "kernel_reads_total": sum(
            len(r.get("kernel_reads", r["kernel_files_read"])) for r in ran
        ),
        "framework_edits_total": sum(r.get("framework_edits", 0) for r in ran),
        "other_module_edits_total": sum(r.get("other_module_edits", 0) for r in ran),
        "mean_edited_outside": mean("edited_outside"),
        "tasks_that_created_a_dotenv": sum(
            bool(r.get("created_env_file")) for r in ran
        ),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 2),
    }


def table(label: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"### {label} ({summarize(rows)['mode']} mode)",
        "",
        "| task | outcome | gates failed | fn checks | dev checks failed "
        "| uncaught (gates green) | tokens | turns | tools | min | kernel files read "
        "| edited outside |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        failed = ", ".join(k for k, ok in r["gates"].items() if not ok) or "—"
        fn = f"{sum(r['fn_checks'].values())}/{len(r['fn_checks'])}"
        dev = (
            ", ".join(d.removeprefix("test_dev_") for d in r["uncaught_deviations"])
            or "—"
        )
        any_dev = ", ".join(d.removeprefix("test_dev_") for d in dev_failures(r)) or "—"
        lines.append(
            f"| {r['task']} | {r['outcome']} | {failed} | {fn} | {any_dev} | {dev} "
            f"| {r['tokens_total']:,} "
            f"| {r['turns']} | {r['tool_calls']} | {r['wall_min']} | {len(r['kernel_files_read'])} "
            f"| {len(r['edited_outside'])} |"
        )
    s = summarize(rows)
    lines += [
        "",
        f"**dev checks failed: {s['dev_checks_failed']}** over {s['tasks_ran']} "
        f"tasks that ran (uncaught with green gates: {s['uncaught_deviations']})",
        "",
    ]
    bad = [r for r in rows if errored(r)]
    if bad:
        lines += [
            f"**Errored, excluded from the means ({len(bad)}):**",
            "",
            *(
                f"- {r['task']}: "
                + " ".join((r.get("error") or r["outcome"]).split())[:300]
                for r in bad
            ),
            "",
        ]
    lines += ["Summary: " + ", ".join(f"{k}={v}" for k, v in s.items()), ""]
    return "\n".join(lines)


def product_table(label: str, rows: list[dict[str, Any]]) -> str:
    """The north-star numbers, reported apart from the small-task table."""
    rows = [r for r in rows if r.get("kind", "task") != "task"]
    if not rows:
        return ""
    lines = [
        f"### {label}: product suite",
        "",
        "| task | kind | gates failed | acceptance | footgun escapes | tokens | turns "
        "| min | code changes | config changes | outside allowed | other modules touched |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        acc = r.get("acceptance", {})
        fg = r.get("footguns", {})
        escapes = [k.removeprefix("test_fg_") for k, ok in fg.items() if not ok]
        failed = ", ".join(k for k, ok in r["gates"].items() if not ok) or "—"
        lines.append(
            f"| {r['task']} | {r['kind']} | {failed} | {sum(acc.values())}/{len(acc)} "
            f"| {len(escapes)}/{len(fg)}: {', '.join(escapes) or '—'} | {r['tokens_total']:,} "
            f"| {r['turns']} | {r['wall_min']} | {len(r.get('code_changes', []))} "
            f"| {len(r.get('config_changes', []))} | {len(r['edited_outside'])} "
            f"| {', '.join(r.get('modules_touched', [])) or '—'} |"
        )
    return "\n".join([*lines, ""])


def gaps_section(label: str, rows: list[dict[str, Any]]) -> str:
    """Kernel reads as doc gaps (target 0) and outside edits by kind."""
    reads = [g for r in rows for g in r.get("kernel_reads", [])]
    topics: dict[str, int] = {}
    for g in reads:
        topics[g["topic"]] = topics.get(g["topic"], 0) + 1
    by_topic = ", ".join(
        f"{t} {n}" for t, n in sorted(topics.items(), key=lambda x: -x[1])
    )
    lines = [
        f"### {label}: doc gaps",
        "",
        f"**kernel reads: {len(reads)} (target 0)**"
        + (f": {by_topic}" if by_topic else ""),
        "",
        "| task | kernel reads (path → topic; ✗ = the path does not exist) "
        "| framework edits | other-module edits | wiring | rule | docs | other |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        cells = (
            "<br>".join(
                f"`{g['path']}` → {g['topic']}{'' if g['exists'] else ' ✗'}"
                for g in r.get("kernel_reads", [])
            )
            or "—"
        )
        k = r.get("outside_by_kind", {})
        lines.append(
            f"| {r['task']} | {cells} | {len(k.get('framework', []))} "
            f"| {len(k.get('other-module', []))} "
            f"| {len(k.get('wiring', [])) + len(k.get('expected wiring', []))} "
            f"| {len(k.get('rule', [])) + len(k.get('expected rule', []))} "
            f"| {len(k.get('docs', []))} | {len(k.get('other', []))} |"
        )
    return "\n".join([*lines, ""])


def cmd_recompute(args: argparse.Namespace) -> None:
    """Re-derive doc gaps / outside kinds for stored results (no agent, no
    gates); generated files move out of ``edited_outside``."""
    for path in sorted((RESULTS_DIR / args.label).glob("[tp][0-9]*.json")):
        row = json.loads(path.read_text())
        outside_ = row.get("edited_outside", [])
        generated = [p for p in outside_ if is_generated(p)]
        row["edited_outside"] = [p for p in outside_ if p not in generated]
        row["generated_files"] = sorted(
            set(row.get("generated_files", [])) | set(generated)
        )
        row.update(classify(row, row.get("ref") or None))
        path.write_text(json.dumps(row, indent=2) + "\n")
    cmd_report(argparse.Namespace(label=args.label, compare=None))


def cmd_report(args: argparse.Namespace) -> None:
    rows = load_results(args.label)
    small = [r for r in rows if r.get("kind", "task") == "task"]
    out = [
        x
        for x in (
            table(args.label, small) if small else "",
            product_table(args.label, rows),
            gaps_section(args.label, rows),
        )
        if x
    ]
    if args.compare:
        modes = {r.get("mode", "framework") for r in rows}
        check_modes(args.compare, modes, force=getattr(args, "force_compare", False))
        base_rows = load_results(args.compare)
        tasks = {r["task"] for r in rows}
        base_rows = [r for r in base_rows if r["task"] in tasks]
        out.append(table(f"{args.compare} (same tasks)", base_rows))
        a, b = summarize(base_rows), summarize(rows)
        out.append(
            "| metric | " + args.compare + " | " + args.label + " |\n|---|---:|---:|"
        )
        out += [f"| {k} | {a[k]} | {b[k]} |" for k in a]
    text = "\n".join(out) + "\n"
    (RESULTS_DIR / args.label / "summary.md").write_text(text)
    (RESULTS_DIR / args.label / "summary.json").write_text(
        json.dumps(summarize(rows), indent=2) + "\n"
    )
    print(text)


def cmd_promote(args: argparse.Namespace) -> None:
    rows = load_results(args.label)
    baseline = {
        "label": args.label,
        "ref": rows[0]["ref"] if rows else "",
        "model": rows[0]["model"] if rows else "",
        "summary": summarize(rows),
        "tasks": {
            r["task"]: {
                k: r[k]
                for k in (
                    "outcome",
                    "dev_checks",
                    "uncaught_deviations",
                    "tokens_total",
                    "turns",
                    "tool_calls",
                    "wall_min",
                    "kernel_files_read",
                    "edited_outside",
                )
            }
            for r in rows
        },
    }
    (HERE / "baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")
    print(f"baseline.json <- {args.label}")


def waves(tasks: list[Task]) -> list[list[Task]]:
    """Group tasks so each runs after the task it starts from."""
    ids = {t.id for t in tasks}
    done: set[str] = set()
    out: list[list[Task]] = []
    pending = list(tasks)
    while pending:
        ready = [
            t for t in pending if not t.start or t.start in done or t.start not in ids
        ]
        if not ready:
            raise SystemExit(f"start cycle among {[t.id for t in pending]}")
        out.append(ready)
        done |= {t.id for t in ready}
        pending = [t for t in pending if t not in ready]
    return out


def cmd_run(args: argparse.Namespace) -> None:
    selected = args.tasks.split(",") if args.tasks else None
    tasks, skipped = runnable(
        load_tasks(selected, args.suite, args.mode),
        args.mode,
        explicit=bool(selected),
    )
    print(f"mode: {args.mode}", flush=True)
    for task in skipped:
        why = (
            f"a kernel-contributor task in mode {args.mode} (name it with --tasks)"
            if args.mode in task.kernel_modes
            else f"modes {task.modes}"
        )
        print(f"skip {task.id}: {why} (README: 'Modes')", flush=True)
    for i, wave in enumerate(waves(tasks)):
        for task in wave:
            print(
                f"wave {i}: {task.id} [{task.kind}] {task.title} "
                f"(start: {task.start or args.ref}; prompt {len(agent_prompt(task))} chars)",
                flush=True,
            )
    if args.compare:
        check_modes(args.compare, {args.mode}, force=args.force_compare)
    if args.dry_run:
        if args.mode == "app" and tasks:
            dry_run_app(tasks, args)
        return
    if args.mode == "app":
        args.dist = appmode.build_dist(REPO, args.ref, eval_home())
        print(f"dist: {args.dist.root} ({len(args.dist.packages)} wheels)", flush=True)
    prune_before_run(args.parallel)
    for wave in waves(tasks):
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            list(pool.map(lambda t: run_task(t, args), wave))
    cmd_report(
        argparse.Namespace(
            label=args.label, compare=args.compare, force_compare=args.force_compare
        )
    )


#: The ``sandloom new`` the dry run runs itself (what the footer asks the agent for).
DRY_RUN_NEW = ("--with", "identity,tenancy,billing")


def dry_run_app(tasks: list[Task], args: argparse.Namespace) -> None:
    """Prove the app-mode setup without an agent: build the wheels, make a task
    directory, run ``sandloom new`` + ``uv sync`` through the agent's PATH and
    find-links, score that untouched app through the real scoring path (a
    synthetic transcript), and collect every selected checker against it."""
    started = time.monotonic()
    report: dict[str, Any] = {"label": args.label, "ref": args.ref, "tasks": {}}
    dist = appmode.build_dist(REPO, args.ref, eval_home())
    report["dist"] = {"root": str(dist.root), "packages": dist.packages}
    print(f"dist: {len(dist.packages)} wheels in {dist.wheels}", flush=True)
    probe = Task(
        id="_dryrun",
        title="dry run",
        allowed=[],
        prompt="",
        modes=["app"],
        mode="app",
        app=tasks[0].app,
        app_allowed=tasks[0].app_allowed,
        kind="task",
    )
    out_dir = eval_home() / args.label / probe.id
    with run_marker(out_dir):
        work = make_work_dir(probe, args.label)
        env = app_env_for(probe, args.label, dist)
        (out_dir / "env.json").write_text(
            json.dumps({k: v for k, v in env.items() if k.startswith("SANDLOOM_TEST_")})
        )
        which = shutil.which("sandloom", path=env["PATH"])
        report["sandloom_on_path"] = which
        assert which and Path(which).parent == dist.bin, which
        command = f"sandloom new {probe.app} {' '.join(DRY_RUN_NEW)}"
        # `sandloom` resolved through the agent's PATH, as the agent would run it.
        new = sh(
            ["sandloom", "new", probe.app, *DRY_RUN_NEW], work, env=env, timeout=300
        )
        print(
            f"$ {command}\n{new.stdout.strip().splitlines()[0] if new.stdout else new.stderr}"
        )
        if new.returncode:
            raise SystemExit(f"sandloom new failed: {new.stderr}")
        app = work / probe.app
        sync = sh(["uv", "sync"], app, env=env, timeout=900)
        if sync.returncode:
            raise SystemExit(f"uv sync failed: {sync.stderr[-3000:]}")
        lock = (app / "uv.lock").read_text()
        # uv.lock records the find-links directory, relative to the app.
        src = re.search(
            r'name = "sandloom"\nversion = "[^"]*"\nsource = \{ registry = "([^"]+)"',
            lock,
        )
        from_wheels = (
            src is not None and (app / src.group(1)).resolve() == dist.wheels.resolve()
        )
        report["framework_from_wheels"] = from_wheels
        print(f"uv sync: OK; framework resolved from the built wheels: {from_wheels}")
        # The installed framework IS the ref: every Sandloom distribution carries
        # its wheel's `+g<sha>` version, and every installed sandloom/ file is
        # byte-identical to that commit (a stale cached build fails here).
        versions = appmode.framework_version_mismatches(app, dist)
        content = appmode.framework_content_mismatches(app, dist, REPO)
        report["framework_matches_ref"] = {
            "sha": dist.sha,
            "versions": dist.versions(),
            "version_mismatches": versions,
            "content_mismatches": content,
            "ok": not versions and not content,
        }
        print(
            f"framework matches {dist.sha[:12]}: versions "
            f"{'OK' if not versions else versions}; files "
            f"{'OK' if not content else f'{len(content)} differ: {content[:5]}'}",
            flush=True,
        )
        if versions or content:
            raise SystemExit("the app's installed framework is not the ref")
        # A synthetic session: the one Bash call the agent would have made.
        transcript = out_dir / "transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": f"cd {work} && {command}"},
                            }
                        ]
                    },
                }
            )
            + "\n"
        )
        # A follow-up's start checkpoint does not exist in a dry run: its
        # stand-in is this untouched app (the scoring path and the checkers
        # run; the checks themselves fail, nothing was built).
        standin = out_dir / "standin" / probe.app
        appmode.copy_app(app, standin)
        for task in tasks:
            res = Result(
                task=task.id, label=args.label, ref=args.ref, model="-", mode="app",
                kind=task.kind,
            )  # fmt: skip
            if task.seed:
                # The seed app, built once, copied as an agent gets it, synced.
                seed = seed_app(task, args.label, dist)
                seed_work = work_dir(args.label, f"_dryrun_{task.id}")
                shutil.rmtree(seed_work, ignore_errors=True)
                appmode.copy_app(seed, seed_work / seed.name)
                done = sh(["uv", "sync", "-q"], seed_work / seed.name, env=env,
                          timeout=900)  # fmt: skip
                if done.returncode:
                    raise SystemExit(f"uv sync of the seed failed: {done.stderr}")
                score_app(task, seed_work, env, out_dir, res, dist)
                shutil.rmtree(seed_work, ignore_errors=True)
            elif task.start:
                if task.app != probe.app:
                    report["tasks"][task.id] = f"app {task.app}: not scored here"
                    continue
                score_app(task, work, env, out_dir, res, dist, base_app=standin)
            else:
                score_app(task, work, env, out_dir, res, dist)
            failing_gates = [k for k, ok in res.gates.items() if not ok]
            row = {
                "pristine": res.pristine,
                "changed": len((out_dir / "changed.txt").read_text().split()),
                "generated": res.generated_files,
                "edited_outside": res.edited_outside,
                "expected_wiring": res.expected_wiring,
                "gates": f"{len(res.gates) - len(failing_gates)}/{len(res.gates)} green",
                "failing_gates": failing_gates,
                "fn_checks": res.fn_checks,
                "dev_checks": res.dev_checks,
                "checks_collected": (out_dir / "checks.xml").is_file(),
                "acceptance": res.acceptance,
                "footguns": res.footguns,
                "error": res.error,
            }
            report["tasks"][task.id] = row
            print(
                f"[{task.id}] pristine={row['pristine']} changed={row['changed']} "
                f"outside={len(res.edited_outside)} gates {row['gates']} "
                f"(failing: {', '.join(failing_gates) or '-'}); checks collected: "
                f"{row['checks_collected']} fn {sum(res.fn_checks.values())}/"
                f"{len(res.fn_checks)} dev {sum(res.dev_checks.values())}/"
                f"{len(res.dev_checks)}"
                + (
                    f"; hidden acc {len(res.acceptance)} fg {len(res.footguns)}"
                    if task.kind != "task"
                    else ""
                ),
                flush=True,
            )
            # The task's `sandloom generate` wrote generated files: start the next
            # task's scoring from the untouched app again.
            shutil.rmtree(app / probe.app / "shared", ignore_errors=True)
            appmode.copy_app(standin / probe.app / "shared", app / probe.app / "shared")
            for snap in app.glob(f"{probe.app}/modules/*/*.snapshot.json"):
                snap.unlink()
        report["seconds"] = round(time.monotonic() - started)
        (out_dir / "dry-run.json").write_text(json.dumps(report, indent=2) + "\n")
        shutil.rmtree(work, ignore_errors=True)
    print(f"dry run: {out_dir / 'dry-run.json'} ({report['seconds']} s)")


def check_modes(other: str, modes: set[str], *, force: bool) -> None:
    """Refuse to compare runs of different modes (``--force-compare`` warns)."""
    theirs = {r.get("mode", "framework") for r in load_results(other)}
    if theirs and theirs != modes:
        msg = (
            f"{other} ran in mode {sorted(theirs)}, this run in {sorted(modes)}: "
            "the numbers are not comparable (different context, gates and layout)"
        )
        if not force:
            raise SystemExit(msg + " — pass --force-compare to compare anyway")
        print("WARNING: " + msg, flush=True)


def cmd_rescore(args: argparse.Namespace) -> None:
    """Re-score kept worktrees / app directories (after editing a checker or
    gate list); the mode is the stored result's."""
    selected = args.tasks.split(",") if args.tasks else None
    for task in load_tasks(selected, args.suite):
        out_dir = eval_home() / args.label / task.id
        stored = RESULTS_DIR / args.label / f"{task.id}.json"
        if stored.is_file() and json.loads(stored.read_text()).get("mode") == "app":
            task = load_tasks([task.id], args.suite, "app")[0]
            with run_marker(out_dir):
                result = _rescore_app(task, args, out_dir)
            print(f"[{task.id}] {result.outcome}")
            continue
        wt = out_dir / "worktree"
        if not wt.exists():
            print(f"[{task.id}] no kept worktree")
            continue
        with run_marker(out_dir):  # its gates use the task's Redis indexes
            result = _rescore_one(task, args, out_dir, wt)
        print(f"[{task.id}] {result.outcome}")
    cmd_report(argparse.Namespace(label=args.label, compare=None))


def _rescore_app(task: Task, args: argparse.Namespace, out_dir: Path) -> Result:
    """App mode: the kept app directory (``run --keep``), re-scored in full or
    (``--checks-only``) the hidden checks only."""
    old = json.loads((RESULTS_DIR / args.label / f"{task.id}.json").read_text())
    meta = json.loads((out_dir / "app-mode.json").read_text())
    work = Path(meta["work"])
    if not work.is_dir():
        print(f"[{task.id}] no kept app directory ({work})")
        return Result(
            **{k: v for k, v in old.items() if k in Result.__dataclass_fields__}
        )
    if args.checks_only:
        result = Result(
            **{k: v for k, v in old.items() if k in Result.__dataclass_fields__}
        )
        app = work / result.app_dir
        result.fn_checks, result.dev_checks = run_checks(
            task, work, "", out_dir, app=app, changed_file=out_dir / "changed.txt"
        )
        gates_green = all(result.gates.values())
        result.outcome = (
            "pass" if gates_green and all(result.fn_checks.values()) else "fail"
        )
        result.uncaught_deviations = dev_failures(asdict(result)) if gates_green else []
    else:
        result = Result(
            task=task.id,
            label=args.label,
            ref=old["ref"],
            model=old["model"],
            kind=task.kind,
            mode="app",
        )
        dist = appmode.build_dist(REPO, meta["sha"], eval_home())
        env = app_env_for(task, args.label, dist)
        score_app(task, work, env, out_dir, result, dist)
    write_result(result, out_dir)
    return result


def _rescore_one(
    task: Task, args: argparse.Namespace, out_dir: Path, wt: Path
) -> Result:
    old = json.loads((RESULTS_DIR / args.label / f"{task.id}.json").read_text())
    result = Result(task=task.id, label=args.label, ref=old["ref"], model=old["model"])
    base_file = out_dir / "base.txt"
    base = (
        base_file.read_text().strip()
        if base_file.exists()
        else sh(["git", "rev-parse", "HEAD"], wt).stdout.strip()
    )
    env = {**os.environ, **json.loads((out_dir / "env.json").read_text())}
    result.kind = task.kind
    if args.hidden_only:  # product suite: rerun only the hidden tests
        result = Result(
            **{k: v for k, v in old.items() if k in Result.__dataclass_fields__}
        )
        result.kind = task.kind
        changed = changed_paths(wt, base)
        score_product(task, wt, env, changed, out_dir, result)
        if (
            all(result.gates.values())
            and result.acceptance
            and all(result.acceptance.values())
        ):
            result.outcome = "pass"
    elif args.checks_only:
        score_checks_only(task, wt, base, out_dir, result, old)
    else:
        score(task, wt, base, env, out_dir, result)
    write_result(result, out_dir)
    return result


def score_checks_only(
    task: Task, wt: Path, base: str, out_dir: Path, result: Result, old: dict[str, Any]
) -> None:
    for key in (
        "gates",
        "tokens_total",
        "tokens_output",
        "cost_usd",
        "turns",
        "tool_calls",
        "wall_min",
        "kernel_files_read",
        "shared_files_read",
        "docs_read",
        "edited_outside",
        "edited_inside",
        "generated_files",
        "permission_denials",
        "error",
    ):
        setattr(result, key, old.get(key, getattr(result, key)))
    result.created_env_file = (wt / ".env").exists()
    result.fn_checks, result.dev_checks = run_checks(task, wt, base, out_dir)
    gates_green = all(result.gates.values())
    result.outcome = (
        "pass" if gates_green and all(result.fn_checks.values()) else "fail"
    )
    result.uncaught_deviations = (
        [k for k, ok in result.dev_checks.items() if not ok] if gates_green else []
    )


#: Changes that must be re-measured (owner decision, DX plan section 8 Q9):
#: the agent context, the generators / ``sandloom new`` output, the public API.
TRIGGERS = (
    re.compile(r"(^|/)(AGENTS|CLAUDE)\.md$"),
    re.compile(r"^\.claude/(rules|skills/sandloom-[^/]+)/"),
    re.compile(r"^\.agents/skills/sandloom-[^/]+/"),
    re.compile(r"^src/sandloom/core/cli/(scaffold|project|add)\.py$"),
    re.compile(r"^src/sandloom/core/cli/templates/"),
    re.compile(r"^public_api_baseline\.json$"),
    re.compile(r"^architecture_rules\.yml$"),
)


def cmd_triggers(args: argparse.Namespace) -> None:
    """Exit 1 (and list the paths) when the diff needs a DX eval run."""
    diff = sh(["git", "diff", "--name-only", f"{args.base}...HEAD"], REPO)
    hits = [p for p in diff.stdout.split() if any(t.search(p) for t in TRIGGERS)]
    if hits:
        print("DX eval needed (benchmarks/dx/README.md) for:\n  " + "\n  ".join(hits))
        raise SystemExit(1)
    print("no DX-eval trigger in the diff")


def cmd_prune_slots(args: argparse.Namespace) -> None:
    """Free (or, with --dry-run, list) the Redis indexes of finished eval runs."""
    outcome = prune_stale_slots(apply=not args.dry_run)
    for line in outcome["pruned"]:
        print(f"  {'freed' if outcome['applied'] else 'would free'} {line}")
    report_prune(outcome, args.parallel)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run tasks with fresh agents")
    run.add_argument("--label", required=True, help="results/<label>/")
    run.add_argument("--ref", default="HEAD", help="git ref the agents start from")
    run.add_argument(
        "--tasks", help="comma list of task ids or prefixes (t1,t3); default all"
    )
    run.add_argument("--suite", choices=sorted(SUITES), default="tasks")
    run.add_argument(
        "--mode",
        choices=MODES,
        default=DEFAULT_MODE,
        help="app: an `sandloom new` app on wheels built from --ref (default); "
        "framework: a worktree of the framework repository",
    )
    run.add_argument(
        "--force-compare",
        action="store_true",
        help="compare with a run of another mode (warns instead of refusing)",
    )
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--parallel", type=int, default=3)
    run.add_argument("--budget-usd", type=float, default=25.0, help="per task")
    run.add_argument("--timeout-min", type=float, default=60.0, help="per task")
    run.add_argument("--keep", action="store_true", help="keep worktrees for rescore")
    run.add_argument("--compare", help="label to compare against in the report")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)
    rep = sub.add_parser("report", help="summarize results/<label>/")
    rep.add_argument("--label", required=True)
    rep.add_argument("--compare")
    rep.add_argument("--force-compare", action="store_true")
    rep.set_defaults(func=cmd_report)
    res = sub.add_parser("rescore", help="re-score kept worktrees")
    res.add_argument("--label", required=True)
    res.add_argument("--tasks")
    res.add_argument("--checks-only", action="store_true", help="hidden checks only")
    res.add_argument("--suite", choices=sorted(SUITES), default="tasks")
    res.add_argument(
        "--hidden-only",
        action="store_true",
        help="product suite: rerun the hidden acceptance/footgun tests only",
    )
    res.set_defaults(func=cmd_rescore)
    pro = sub.add_parser("promote", help="write baseline.json from results/<label>/")
    pro.add_argument("--label", required=True)
    pro.set_defaults(func=cmd_promote)
    rec = sub.add_parser("recompute", help="re-derive doc gaps / outside kinds")
    rec.add_argument("--label", required=True)
    rec.set_defaults(func=cmd_recompute)
    trg = sub.add_parser("triggers", help="does the diff vs --base need an eval run?")
    trg.add_argument("--base", default="origin/main")
    trg.set_defaults(func=cmd_triggers)
    prn = sub.add_parser(
        "prune-slots", help="free the Redis indexes of finished eval runs"
    )
    prn.add_argument("--dry-run", action="store_true", help="only list them")
    prn.add_argument("--parallel", type=int, default=3, help="for the free-index check")
    prn.set_defaults(func=cmd_prune_slots)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
