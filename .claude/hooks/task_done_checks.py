#!/usr/bin/env python3
"""PreToolUse hook on tm's ``task_done``: run the checks at the handover.

The tm-fleet procedure (``.agents/skills/tm-fleet/SKILL.md``, section 8):
where the repository runs its checks at ``task_done``, the first handover is
refused once with a ``{"kind": "checks", ...}`` deliverable in the reason; the
agent calls ``task_done`` again with that deliverable appended, unchanged, and
the handover goes through carrying a record that was run, not retold. A red
result does not block it: it is recorded for whoever reviews.

Only a ``sandloom-implementer`` subagent's handover is checked (the hook input
names the subagent type). The agent's checkout is the worktree whose HEAD is the
commit its ``git_commit`` deliverable names (the hook's cwd can be the main
checkout); none at that commit gives a ``not_run`` record, never a run of another
tree. The run is the static gate of that checkout:
``pre-commit run --from-ref origin/main --to-ref HEAD`` with the dummy settings
CI gives the same job, skipping the architecture tests (they need the agent's
own test servers, which a hook does not know) and the two hooks that rewrite
files (a dirty tree would make tm refuse the handover). Any failure of the hook
itself lets the call through.

A second stage runs beside it, in a thread: the CI steps of the areas the diff
(``git diff --name-only origin/main...HEAD``) touches, the ones pre-commit does
not run (the docs job's code check under Python 3.11, the generated-files
check, the no-Redis pending list, a driver package's tests). Which path selects
which step is :data:`STEPS`; the docs paths are the ``docs`` filter of
``.github/workflows/ci.yml`` (a test keeps them equal). Each step is recorded in
``ci_steps`` as ``Passed``, ``Failed`` or ``not_run`` with its reason. ``not_run``
is a record, never a refusal: a missing tool or Python, a step that needs the
agent's own ``SANDLOOM_TEST_*`` which the hook's environment lacks, a timeout
or the stage budget. A ``Failed`` step lands in ``failed`` as ``ci:<name>`` and is
recorded like a failed pre-commit hook: it does not block the handover either.
The stage never writes into the checkout (the 3.11 environment is a temporary
directory outside it, bytecode is off) and every step is killed with its process
group at its timeout, so it cannot hang the hook.

Only the record this hook emitted passes the second call: its digest is kept in
``~/.cache/sandloom-checks`` (outside every checkout), so an agent cannot attach
a self-written all-green record. If that directory cannot be written, the hook
lets the handover through without a record (the catch-all below) rather than
refusing a second call it could never accept. Credentials (URL userinfo and
secret-looking query values) are masked in everything stored in tm.

Trust: the run uses the agent's own checkout, its ``.pre-commit-config.yaml`` and
``uv.lock`` included, with this machine's environment, exactly as the agent's own
pre-commit run would. The record is evidence of what that tree's gates said, not a
boundary against a hostile agent: CI on GitHub stays the gate.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

AGENT = "sandloom-implementer"
SKIP = "architecture-tests,end-of-file-fixer,trailing-whitespace"
CI_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://ci:ci@127.0.0.1:5433/ci",
    "CACHE_URL": "redis://127.0.0.1:6380/0",
    "BROKER_URL": "redis://127.0.0.1:6380/1",
    "JWT_SECRET": "ci-architecture-tests-not-a-real-secret",
}
_RESULT = re.compile(r"^(.+?)\.{3,}(?:\(.*\))?(Passed|Failed|Skipped)\s*$")
# user:pass@ and the password-only :pass@ form (redis://:secret@host).
# Any userinfo in a URL: user:pass@, :pass@ and a bare token@ (https://<token>@host).
_CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://[^/\s@]+@")
# Secret-looking query values: ?token=..., &password=..., api_key=...
_SECRET_QUERY = re.compile(
    r"([?&;](?:[A-Za-z0-9_.-]*?(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|auth))=)[^&#\s]+",
    re.IGNORECASE,
)
_SHA = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
#: Digests of the records this hook emitted, outside every checkout.
ISSUED = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "sandloom-checks"
)


def _git(args: list[str], cwd: str, *, strict: bool = False) -> str:
    """Git's stdout; ``strict`` raises on a nonzero exit instead of returning
    whatever it printed (an empty status from a failed run is not "clean")."""
    argv = ["git", *args]
    done = subprocess.run(  # noqa: S603 - fixed git argv
        argv, cwd=cwd, capture_output=True, text=True, timeout=10, check=False
    )
    if strict and done.returncode:
        raise subprocess.CalledProcessError(done.returncode, argv, done.stdout)
    return done.stdout.strip()


def _deliverables(tool_input: dict) -> list[dict]:
    raw = tool_input.get("deliverables") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [d for d in raw if isinstance(d, dict)]


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def remember(content: str) -> None:
    """Note the record this hook emitted, outside any checkout an agent edits."""
    ISSUED.mkdir(parents=True, exist_ok=True)
    (ISSUED / _digest(content)).write_text(content, encoding="utf-8")


def already_checked(tool_input: dict, head: str) -> bool:
    """The ``checks`` deliverable this hook emitted for this very commit is
    attached. A record the agent wrote itself (same shape, all green) is not
    one: only a content whose digest the hook stored when it refused passes."""
    for item in _deliverables(tool_input):
        if item.get("kind") != "checks":
            continue
        content = item.get("content")
        if not isinstance(content, str) or not (ISSUED / _digest(content)).is_file():
            continue
        try:
            record = json.loads(content)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict) and record.get("commit") == head:
            return True
    return False


def redact(text: str) -> str:
    """Credentials out of text stored in tm: URL userinfo (``user:pass@``,
    ``:pass@``, ``token@``) and secret-looking query values."""
    return _SECRET_QUERY.sub(r"\1***", _CREDENTIALS.sub(r"\1://***@", text))


def results(output: str) -> dict[str, str]:
    """``{hook name: Passed|Failed|Skipped}`` from pre-commit's output."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        m = _RESULT.match(line.strip())
        if m:
            found[m.group(1).strip()] = m.group(2)
    return found


def run_checks(top: str) -> tuple[dict[str, str], str, int]:
    env = {**os.environ, **CI_ENV, "SKIP": SKIP}
    argv = ["uv", "run", "--locked", "pre-commit", "run"]
    argv += ["--from-ref", "origin/main", "--to-ref", "HEAD"]
    done = subprocess.run(  # noqa: S603 - fixed argv in the agent's checkout
        argv,
        cwd=top,
        env=env,
        capture_output=True,
        text=True,
        timeout=540,
        check=False,
    )
    output = done.stdout + done.stderr
    failed = [
        ln for ln in output.splitlines() if ln.strip() and not _RESULT.match(ln.strip())
    ]
    return results(output), "\n".join(failed[-25:]), done.returncode


#: The stage's own clock: it runs beside pre-commit (540 s at most) inside the
#: hook's 600 s limit in ``.claude/settings.json``, so it ends before either.
STAGE_BUDGET = 480
STEP_TIMEOUT = 300
#: A step is not started with less than this left of the budget.
MIN_STEP_SECONDS = 10
_PLACEHOLDER = "{python}"
#: The ``docs`` filter of ``.github/workflows/ci.yml`` (``changes`` job): a
#: path matching one runs the docs job in CI. A test keeps this list equal to it.
DOCS_PATHS = (
    "docs/**",
    "examples/**",
    "website/**",
    "LICENSE",
    "NOTICE",
    "tools/docs/**",
    "zensical.toml",
    "pyproject.toml",
    "uv.lock",
    "src/**",
    ".github/workflows/docs.yml",
    ".github/workflows/pages.yml",
    ".github/workflows/ci.yml",
    "**/AGENTS.md",
    "**/CLAUDE.md",
    "**/README.md",
    ".claude/**",
    "deploy/**",
    "benchmarks/dx/**",
)
_LANE_ENV = (
    "SANDLOOM_TEST_DATABASE_URL",
    "SANDLOOM_TEST_CACHE_URL",
    "SANDLOOM_TEST_BROKER_URL",
    "SANDLOOM_TEST_OWNER",
)
# The driver lanes of ci.yml (redis-drivers, celery-drivers) over the source tree
# the agent's .venv has installed, serial (xdist workers would take Redis
# indexes beyond the two the agent owns), without the skip ledger (it writes
# into the checkout) and without the wheel check (that one reinstalls the venv).
_LANE = ("uv", "run", "--locked", "pytest", "-q", "-p", "no:xdist", "--no-cov")
_LANE += ("-p", "no:cacheprovider")


class Step(NamedTuple):
    """One CI step: the paths that select it and how it runs."""

    name: str
    paths: tuple[str, ...]
    argv: tuple[str, ...] = ()
    #: In-process check of the checkout: returns the problems found (empty: pass).
    call: Callable[[str], str] | None = None
    #: Runs with the temporary Python 3.11 environment (``{python}`` in argv).
    docs_python: bool = False
    env: tuple[tuple[str, str], ...] = ()
    #: Variables the hook's own environment must carry, else ``not_run``.
    needs_env: tuple[str, ...] = ()
    timeout: int = STEP_TIMEOUT


def _defined_ids(root: Path, tests: dict[str, str]) -> list[str]:
    """``path::[Class::]name`` of each function in the files ``tests`` lists."""
    found: list[str] = []

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}{node.name}::")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(f"{prefix}{node.name}")

    for rel in sorted({entry.split("::", 1)[0] for entry in tests}):
        source = root / rel
        if source.is_file():
            walk(ast.parse(source.read_text(encoding="utf-8")).body, f"{rel}::")
    return found


def no_redis_problems(top: str) -> str:
    """The ``kernel-without-redis`` list checks that need no venv: the pending
    list is within its ceilings and names no gone file or test. CI's run also
    collects the suites with Redis uninstalled; that part needs the CI venv and
    stays CI's."""
    root = Path(top)
    path = root / "tools/ci/no_redis.py"
    spec = importlib.util.spec_from_file_location("sandloom_ci_no_redis", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    files, tests = module.load_pending()
    problems = list(module.above_ceiling(files, tests))
    problems += [f"gone file: {p}" for p in module.stale_files(files, root)]
    try:
        ids = _defined_ids(root, tests)
    except SyntaxError as exc:
        return "; ".join([*problems, f"unparsable test file: {exc}"])
    # The source has no parametrization: an entry is judged by its function.
    bare = {t.split("[", 1)[0]: why for t, why in tests.items()}
    problems += [f"stale test: {t}" for t in module.stale_tests(bare, ids, root)]
    return "; ".join(problems)


STEPS = (
    Step(
        "docs-code-check-self-test",
        DOCS_PATHS,
        (_PLACEHOLDER, "tools/docs/docs_code_check.py", "--self-test"),
        docs_python=True,
        timeout=120,
    ),
    Step(
        "docs-code-check",
        DOCS_PATHS,
        (_PLACEHOLDER, "tools/docs/docs_code_check.py"),
        docs_python=True,
        timeout=180,
    ),
    Step(
        "docs-reference-check",
        DOCS_PATHS,
        (_PLACEHOLDER, "tools/docs/docs_reference.py", "--check"),
        docs_python=True,
        timeout=180,
    ),
    Step(
        "docs-stale-symbols-self-test",
        ("tools/docs/**",),
        (_PLACEHOLDER, "tools/docs/docs_stale_symbols.py", "--self-test"),
        docs_python=True,
        timeout=60,
    ),
    Step(
        "docs-prose-lint-self-test",
        ("tools/docs/**",),
        (_PLACEHOLDER, "tools/docs/docs_prose_lint.py", "--self-test"),
        docs_python=True,
        timeout=60,
    ),
    Step(
        "generate-check",
        ("src/**", "pyproject.toml", "uv.lock"),
        ("uv", "run", "--locked", "sandloom", "generate", "--check"),
        env=(("SANDLOOM_COMPOSITION_ROOT", "sandloom_standard.main"),),
        timeout=180,
    ),
    Step(
        "no-redis-pending",
        (
            "src/sandloom/core/**",
            "tests/sdk/**",
            "tests/architecture/**",
            "tests/integration/replaceability/**",
            "tools/ci/no_redis*",
            "conftest.py",
        ),
        call=no_redis_problems,
        timeout=60,
    ),
    Step(
        "redis-drivers",
        ("packages/sandloom-redis/**",),
        (
            *_LANE,
            "packages/sandloom-redis/tests",
            "src/sandloom/core/tests/test_atomic_store.py",
            "src/sandloom/core/tests/test_lease.py",
            "src/sandloom/core/tests/test_broadcast_redis.py",
        ),
        env=(("SANDLOOM_REDIS_REQUIRED", "1"),),
        needs_env=_LANE_ENV,
    ),
    Step(
        "celery-drivers",
        ("packages/sandloom-celery/**",),
        (
            *_LANE,
            "packages/sandloom-celery/tests",
            "src/sandloom/core/tests/test_celery_error_mapping.py",
            "src/sandloom/core/tests/test_beat_lease.py",
        ),
        env=(("SANDLOOM_CELERY_REQUIRED", "1"),),
        needs_env=_LANE_ENV,
    ),
)


def _glob(pattern: str) -> re.Pattern[str]:
    """A ``dorny/paths-filter`` glob: ``**/`` any directories (or none), ``**``
    anything, ``*`` anything inside one path segment."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def selected(changed: list[str]) -> list[Step]:
    """The steps whose paths the diff touches, in :data:`STEPS` order."""
    return [
        step
        for step in STEPS
        if any(_glob(p).match(path) for p in step.paths for path in changed)
    ]


def _run_bounded(
    argv: list[str], cwd: str, env: dict[str, str], timeout: float
) -> tuple[int, str]:
    """``(exit status, output)``; past ``timeout`` the whole process group is
    killed (uv's child included) and ``TimeoutExpired`` raised."""
    proc = subprocess.Popen(  # noqa: S603 - fixed argv in the agent's checkout
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return proc.returncode, out


def _docs_python(top: str, env: dict[str, str], tmp: str, timeout: float) -> str:
    """The interpreter of a temporary Python 3.11 environment with the project
    and every extra, as the docs job installs it. It lives outside the checkout
    (``UV_PROJECT_ENVIRONMENT``), so the agent's ``.venv`` is never replaced.
    Raises ``RuntimeError`` with the reason when it cannot be built."""
    venv = str(Path(tmp) / "venv")
    uv_env = {k: v for k, v in env.items() if k != "VIRTUAL_ENV"}
    uv_env["UV_PROJECT_ENVIRONMENT"] = venv
    for argv in (
        ["uv", "venv", "--python", "3.11", "--no-python-downloads", venv],
        ["uv", "sync", "--locked", "--all-extras", "--no-python-downloads"],
    ):
        try:
            code, out = _run_bounded(argv, top, uv_env, timeout)
        except FileNotFoundError:
            raise RuntimeError("uv is not installed") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"`{' '.join(argv[:2])}` timed out") from None
        if code:
            raise RuntimeError(
                f"Python 3.11 environment: `{' '.join(argv[:2])}` exited {code}: "
                + out.strip()[-200:]
            )
    return str(Path(venv) / "bin" / "python")


def _run_step(
    step: Step, top: str, env: dict[str, str], python: str, timeout: float
) -> tuple[str, str]:
    """``(status, text)``: ``Passed`` / ``Failed`` with the output tail, or
    ``not_run`` with the reason."""
    missing = [name for name in step.needs_env if not env.get(name)]
    if missing:
        return "not_run", "the hook's environment has no " + ", ".join(missing)
    step_env = {**env, **dict(step.env)}
    try:
        if step.call is not None:
            problems = step.call(top)
            return ("Failed", problems) if problems else ("Passed", "")
        argv = [python if a == _PLACEHOLDER else a for a in step.argv]
        code, out = _run_bounded(argv, top, step_env, timeout)
    except subprocess.TimeoutExpired:
        return "not_run", f"timed out after {int(timeout)} s"
    except FileNotFoundError as exc:
        return "not_run", f"not found: {exc.filename or exc}"
    except Exception as exc:  # noqa: BLE001 - an in-process check that raised failed
        return "Failed", f"{type(exc).__name__}: {exc}"
    return ("Failed", out.strip()[-400:]) if code else ("Passed", "")


def run_ci_steps(top: str, budget: float = STAGE_BUDGET) -> dict[str, dict]:
    """``{step: {"status", "seconds", "reason" | "tail"}}`` for the CI steps the
    diff against ``origin/main`` selects; never raises, never exceeds ``budget``
    seconds by more than a process-group kill."""
    started = time.monotonic()
    try:
        diff = _git(["diff", "--name-only", "origin/main...HEAD"], top, strict=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ci-steps": {"status": "not_run", "reason": f"no diff: {exc}"[:200]}}
    steps = selected([p for p in diff.splitlines() if p])
    env = {**os.environ, **CI_ENV, "PYTHONDONTWRITEBYTECODE": "1"}
    record: dict[str, dict] = {}
    tmp = tempfile.mkdtemp(prefix="sandloom-ci-steps-") if steps else ""
    python = ""
    python_error = ""

    def left() -> float:
        return budget - (time.monotonic() - started)

    try:
        for step in steps:
            begun = time.monotonic()
            if left() < MIN_STEP_SECONDS:
                status, text = "not_run", "the stage budget was spent"
            else:
                if step.docs_python and not python and not python_error:
                    try:
                        python = _docs_python(top, env, tmp, min(120, left()))
                    except RuntimeError as exc:
                        python_error = str(exc)
                if step.docs_python and python_error:
                    status, text = "not_run", python_error
                else:
                    wait = min(step.timeout, max(left(), 1))
                    status, text = _run_step(step, top, env, python, wait)
            entry = {"status": status, "seconds": round(time.monotonic() - begun, 1)}
            if text:
                entry["reason" if status == "not_run" else "tail"] = redact(text)[-300:]
            record[step.name] = entry
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return record


def handed_over(tool_input: dict, cwd: str) -> tuple[bool, str | None]:
    """``(named, sha)``: whether a ``git_commit`` deliverable is attached, and
    the full SHA of the first commit of this clone it names. An abbreviated
    SHA is resolved (``rev-parse <sha>^{commit}``); when nothing resolves, the
    first full SHA named is kept, so the record names what was handed over."""
    named = False
    unresolved: str | None = None
    for item in _deliverables(tool_input):
        if item.get("kind") != "git_commit":
            continue
        named = True
        text = item.get("content") or item.get("reference")
        for token in _SHA.findall(text) if isinstance(text, str) else []:
            full = _git(
                ["rev-parse", "--verify", "--quiet", f"{token}^{{commit}}"], cwd
            )
            if full:
                return True, full.lower()
            if len(token) == 40 and unresolved is None:
                unresolved = token.lower()
    return named, unresolved


_is_dir = os.path.isdir


def _clean(tree: str) -> bool | None:
    """Whether ``tree`` has no changes; ``None`` when it cannot be probed (a
    worktree deleted but not yet pruned is still listed)."""
    if not _is_dir(tree):
        return None
    try:
        return not _git(["status", "--porcelain"], tree, strict=True)
    except (OSError, subprocess.SubprocessError):
        return None


def checkout_at(sha: str, cwd: str) -> str | None:
    """The worktree of this clone whose HEAD is ``sha``: the agent's own, not
    the session's cwd (an agent that made its own worktree still reports the
    main checkout as its cwd). Of several at that commit (a stale named
    worktree beside the agent's), a clean one wins; one that cannot be probed
    is skipped."""
    listing = _git(["worktree", "list", "--porcelain"], cwd)
    path = None
    at: list[str] = []
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("HEAD ") and line[len("HEAD ") :].lower() == sha and path:
            at.append(path)
    usable = [(tree, clean) for tree in at if (clean := _clean(tree)) is not None]
    for tree, clean in usable:
        if clean:
            return tree
    return usable[0][0] if usable else None


def _unresolved_id(tool_input: dict) -> str:
    """A stable identity for a handover whose ``git_commit`` names no commit:
    the same retry matches its own record, the cwd's HEAD never does."""
    texts = [
        str(item.get("content") or item.get("reference") or "")
        for item in _deliverables(tool_input)
        if item.get("kind") == "git_commit"
    ]
    return "unresolved:" + _digest("\n".join(texts))[:16]


def locate(tool_input: dict, cwd: str) -> tuple[str, str, str]:
    """``(checkout, head, why_not)``: the tree to check, its commit, and why
    it cannot be checked (then the record says so instead of checking another
    tree). Only a handover with no ``git_commit`` deliverable falls back to
    the cwd's checkout."""
    named, sha = handed_over(tool_input, cwd)
    if sha:
        top = checkout_at(sha, cwd)
        if top:
            return top, sha, ""
        return "", sha, f"no usable worktree of this clone is at {sha[:12]}"
    if named:
        return (
            "",
            _unresolved_id(tool_input),
            "the git_commit deliverable names no commit of this clone",
        )
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    return top, _git(["rev-parse", "HEAD"], top or cwd), ""


def main() -> int:
    event = json.load(sys.stdin)
    if event.get("agent_type") != AGENT:
        return 0
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    top, head, unlocated = locate(tool_input, cwd)
    if not head or already_checked(tool_input, head):
        return 0
    if not top and not unlocated:
        return 0
    exit_status: int | None = None
    outcome: dict[str, str] = {}
    ci_steps: dict[str, dict] = {}
    tail = ""
    if unlocated:
        # Checking another tree (the main checkout) would record its results
        # under this commit: say why there are none instead.
        not_run = unlocated
    else:
        # The CI steps run beside pre-commit; they are bounded by their own
        # budget and never raise, so the join below cannot hang.
        with ThreadPoolExecutor(max_workers=1) as pool:
            stage = pool.submit(run_ci_steps, top)
            try:
                outcome, tail, exit_status = run_checks(top)
                # Mask first, then cut: a cut could drop a URL's prefix, keep its
                # password.
                shown = redact(tail)[-300:]
                not_run = (
                    "" if outcome else "pre-commit printed no hook result: " + shown
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                outcome, tail = {}, ""
                not_run = redact(f"{type(exc).__name__}: {exc}")[:300]
            ci_steps = stage.result()
        try:
            now = _git(["rev-parse", "HEAD"], top)
        except (subprocess.TimeoutExpired, OSError):
            now = ""
        if now != head:
            # The tree moved under the run: its results belong to no commit.
            outcome, ci_steps, not_run = {}, {}, "HEAD moved while the checks ran"
    record = {
        "commit": head,
        "ran": "pre-commit run --from-ref origin/main --to-ref HEAD",
        "skipped": SKIP.split(","),
        "results": outcome,
        "ci_steps": ci_steps,
        "failed": sorted(
            [k for k, v in outcome.items() if v == "Failed"]
            + [f"ci:{k}" for k, v in ci_steps.items() if v["status"] == "Failed"]
        ),
        "exit_status": exit_status,
    }
    if not_run:
        # Recorded once, so the reviewer sees why there is no result; the
        # second call carries it through like any other record.
        record["not_run"] = redact(not_run)
    if record["failed"] or exit_status not in (0, None):
        # A nonzero exit with no failed hook is an errored or partial run:
        # keep its output, so it never reads as a clean pass.
        record["tail"] = redact(tail)
    deliverable = {"kind": "checks", "content": json.dumps(record, sort_keys=True)}
    remember(deliverable["content"])
    reason = (
        "Checks ran at the handover (tm-fleet: checks at task_done). Call task_done "
        "again with the same arguments and this deliverable appended, unchanged, "
        "editing nothing in between; a red result does not block the handover:\n"
        + json.dumps(deliverable)
    )
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a broken hook must never block a handover
        sys.exit(0)
