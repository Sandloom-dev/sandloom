"""The sixteen module subsets of I-19 / A-B7 and their expected outcomes (KB-04).

Every subset of identity, tenancy, billing and admin boots in a fresh
interpreter: ``create_app`` (through the profile's composition root), the
lifespan, ``/health``, ``/ready`` and ``sandloom --help``. The infrastructure
probes are replaced by passing ones (no database, cache or broker runs): what
is checked is that the subset composes and that the probes answer, not the
services behind them.

An outcome is one of:

- **ready**: boots, ``/health`` 200, ``/ready`` 200;
- **not ready, taskqueue**: boots, ``/health`` 200, ``/ready`` 503 with
  ``taskqueue_status: fail``: a loaded module uses the taskqueue on the
  request path and its broker cannot be reached (KB-IC-3). No bundled module
  does: billing needs only ``periodic`` (the worker and beat run it) and
  identity's queue is optional, so the web process reports ``unused`` and a
  missing driver is a startup refusal instead (B037, KB-IC-1);
- **refused**: startup stops before serving, only for a reason in
  :data:`PREDEFINED_REFUSALS`, with its code and a message naming what is
  missing (owner answer #4). On today's declarations no bundled module
  requires another's contract, so no subset expects a refusal.

Anything else fails the matrix: a ``ModuleLoadError`` (an undeclared
dependency), any other exception, a refusal for a reason outside the
predefined ones, or a refusal the table does not expect (a decision that makes
a consumed contract required updates :func:`expected` and I-19's table).
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]

#: Module entry-point name -> (import path, distribution).
MODULES: dict[str, tuple[str, str]] = {
    "identity": ("sandloom_identity", "sandloom-identity"),
    "tenancy": ("sandloom_tenancy", "sandloom-tenancy"),
    "billing": ("sandloom_billing", "sandloom-billing"),
    "admin": ("sandloom_admin", "sandloom-admin"),
}

#: All 16 subsets, smallest first (the empty one is P1's lean install).
SUBSETS: tuple[tuple[str, ...], ...] = tuple(
    combo
    for size in range(len(MODULES) + 1)
    for combo in itertools.combinations(MODULES, size)
)

#: Modules that register tasks (a ``tasks.py``, the worker's submodule).
#: Their web processes do not gate on the taskqueue (billing: ``periodic``
#: only; identity: optional), so they report ``unused`` (KB-IC-3). Pinned
#: against the source by ``test_task_modules_are_the_modules_with_tasks``.
TASK_MODULES = frozenset({"identity", "billing"})

#: The reasons a subset may refuse to start (I-19): code -> what it means.
#: ``configuration`` is a :class:`ConfigurationError` a selected module raised
#: for its own settings (I-18, answer #29).
PREDEFINED_REFUSALS: dict[str, str] = {
    "SANDLOOM-B005": "a required consumed contract has no provider and no binding",
    "SANDLOOM-B037": "a required capability has no installed driver with its features",
    "configuration": "a selected module refuses its configuration",
}

#: Modules whose required capabilities need a driver extra plain ``sandloom``
#: lacks (KB-IC-1): identity requires the cache (redis-py), billing the
#: taskqueue with ``periodic`` (Celery). Their subsets refuse with B037 there.
NEEDS_DRIVER_EXTRAS = frozenset({"identity", "billing"})

#: The environment every probe runs with: placeholders only, nothing listens.
PROBE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/db",
    "CACHE_URL": "redis://127.0.0.1:1/0",
    "BROKER_URL": "redis://127.0.0.1:1/1",
    "JWT_SECRET": "module-subset-matrix-not-a-real-secret",
    "EVENT_BUS_BACKEND": "memory",
    "HEALTH_CACHE_TTL_SECONDS": "0",
    "INTERNAL_API_TOKEN": "module-subset-matrix-internal-token",
}

#: Variables of the parent kept in a probe's environment (the rest, ``.env.test``
#: and every ``SANDLOOM_*`` included, never reach it).
_KEPT = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "LANG", "LC_ALL")


@dataclass(frozen=True)
class Outcome:
    """What one subset is expected to do, or did."""

    kind: str  # "ready" | "not_ready_taskqueue" | "refused"
    taskqueue: str | None = None  # /ready's taskqueue_status
    refusal: str | None = None  # a PREDEFINED_REFUSALS key


def expected(subset: Sequence[str], *, celery: bool) -> Outcome:
    """The expected outcome of ``subset`` (I-19's table) with or without Celery."""
    if not celery and NEEDS_DRIVER_EXTRAS.intersection(subset):
        return Outcome("refused", refusal="SANDLOOM-B037")
    # No bundled module uses the queue on the request path (KB-IC-3): the
    # web process never probes it, with or without Celery.
    return Outcome("ready", taskqueue="unused")


#: Run in a fresh interpreter of the profile's environment. argv[1] is the
#: composition root (``kernel``: the kernel factory with no default modules,
#: the empty subset where modules are installed). The last stdout line is the
#: JSON result.
BOOT_PROBE = r"""
import asyncio, importlib, importlib.util, json, re, sys, time, traceback

root = sys.argv[1]
result = {"celery_installed": importlib.util.find_spec("celery") is not None}
started = time.perf_counter()
try:
    if root == "kernel":
        from sandloom.sdk.app import create_app
        app = create_app()
    else:
        app = importlib.import_module(root).app
except SystemExit as exc:
    from sandloom.core.config.common import INVALID_CONFIGURATION
    message = str(exc.code if exc.code is not None else exc)
    code = re.search(r"SANDLOOM-[CBRT]\d{3}", message)
    result.update(
        outcome="refused",
        code=code.group(0) if code else (
            "configuration" if message.startswith(INVALID_CONFIGURATION) else None
        ),
        message=message,
    )
except BaseException as exc:
    from sandloom.core.error_codes import code_for_exception
    code = code_for_exception(exc)
    result.update(
        outcome="refused" if code in ("SANDLOOM-B005", "SANDLOOM-B037") else "error",
        code=code,
        message=f"{type(exc).__name__}: {exc}",
        traceback=traceback.format_exc()[-3000:],
    )
else:
    result["create_app_seconds"] = round(time.perf_counter() - started, 3)
    import httpx
    from sandloom.core.descriptors import HealthResult

    core = app.state.core

    async def ok():
        return HealthResult(status="ok")

    async def probe():
        async with app.router.lifespan_context(app):
            for name in list(core.health_checks):
                core.register_health_check(name, ok)
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Internal-Token": "module-subset-matrix-internal-token"}
            async with httpx.AsyncClient(
                transport=transport, base_url="http://probe", headers=headers
            ) as client:
                return {p: await client.get(p) for p in ("/health", "/ready")}

    responses = asyncio.run(probe())
    ready = responses["/ready"].json()
    result.update(
        outcome="booted",
        loaded=sorted(core.loaded_modules),
        health=responses["/health"].status_code,
        ready=responses["/ready"].status_code,
        taskqueue=ready.get("taskqueue_status"),
        ready_body=ready,
        celery_imported="celery" in sys.modules,
    )
print(json.dumps(result))
"""


@dataclass
class SubsetRun:
    """One subset booted in one profile."""

    subset: tuple[str, ...]
    probe: dict[str, Any]
    help_returncode: int
    help_output: str
    seconds: float
    stderr: str = field(repr=False, default="")

    def outcome(self) -> Outcome:
        probe = self.probe
        if probe.get("outcome") == "refused":
            return Outcome("refused", refusal=probe.get("code"))
        if probe.get("outcome") != "booted":
            return Outcome("error")
        if probe["health"] == 200 and probe["ready"] == 200:
            return Outcome("ready", taskqueue=probe["taskqueue"])
        if (
            probe["health"] == 200
            and probe["ready"] == 503
            and probe["taskqueue"] == "fail"
        ):
            return Outcome("not_ready_taskqueue", taskqueue="fail")
        return Outcome(f"health {probe['health']}, ready {probe['ready']}")


def probe_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _KEPT if k in os.environ}
    return {**env, **PROBE_ENV, **(extra or {})}


def run_subset(
    subset: tuple[str, ...],
    *,
    python: Path,
    sandloom: Path,
    root: str,
    cwd: Path,
    env: Mapping[str, str],
) -> SubsetRun:
    """Boot ``subset`` with ``python`` and run ``sandloom --help``, both fresh."""
    started = time.perf_counter()
    done = subprocess.run(
        [str(python), "-c", BOOT_PROBE, root],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        cwd=cwd,
        env=dict(env),
    )
    lines = done.stdout.strip().splitlines()
    try:
        probe = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        probe = {}
    if not probe:
        probe = {"outcome": "error", "message": f"exit {done.returncode}, no result"}
    cli = subprocess.run(
        [str(sandloom), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        cwd=cwd,
        env=dict(env),
    )
    return SubsetRun(
        subset=subset,
        probe=probe,
        help_returncode=cli.returncode,
        help_output=(cli.stdout + cli.stderr)[-3000:],
        seconds=round(time.perf_counter() - started, 2),
        stderr=done.stderr[-3000:],
    )


def run_matrix(
    run: Any, subsets: Iterable[tuple[str, ...]] = SUBSETS, *, workers: int = 4
) -> dict[tuple[str, ...], SubsetRun]:
    """``run(subset) -> SubsetRun`` for every subset, ``workers`` at a time."""
    subsets = list(subsets)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(subsets, pool.map(run, subsets), strict=True))


def check_run(run: SubsetRun, want: Outcome, *, celery: bool) -> None:
    """Assert the run's outcome is ``want``, with everything I-19 asks of it."""
    probe = run.probe
    context = json.dumps(
        {k: v for k, v in probe.items() if k != "traceback"}, indent=1, default=str
    )
    assert probe.get("outcome") != "error", (
        f"{run.subset} did not boot (never allowed: not a predefined refusal)\n"
        f"{probe.get('traceback') or probe.get('message')}\n{run.stderr}"
    )
    got = run.outcome()
    if got.kind == "refused":
        assert got.refusal in PREDEFINED_REFUSALS, (
            f"{run.subset} refused for a reason that is not predefined: {context}"
        )
        assert probe.get("message"), f"{run.subset}: a refusal names what is missing"
    assert got == want, f"{run.subset}: expected {want}, got {got}\n{context}"
    if got.kind != "refused":
        assert probe["loaded"] == sorted(MODULES[m][0] for m in run.subset), (
            f"{run.subset}: loaded {probe['loaded']}\n{context}"
        )
        assert probe["ready_body"].get("loaded_modules") is not None, context
    assert probe["celery_installed"] is celery, context
    if not celery and got.kind != "refused":
        assert not probe["celery_imported"], f"{run.subset} imported celery: {context}"
    assert run.help_returncode == 0, f"{run.subset}: sandloom --help\n{run.help_output}"
    assert "Usage" in run.help_output or "usage" in run.help_output, run.help_output


def subset_id(subset: Sequence[str]) -> str:
    return "+".join(subset) or "none"
