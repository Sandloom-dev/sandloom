#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML>=6"]
# ///
"""Local Docker CI runner: reproduce ``.github/workflows/ci.yml`` for a git ref.

GitHub Actions may be unavailable (billing, outages); this runs the same jobs
locally, in Docker, against an exported git ref — never the working tree.

    tools/ci/ci_local.py <git-ref> [--jobs a,b,...] [--python 3.11,3.14]
                        [--parallel N] [--rerun-failed] [--fetch]
                        [--report-pr N]
    tools/ci/ci_local.py --list            # local job names
    tools/ci/ci_local.py --check           # workflow drift check only

Two engines
-----------
* ``act`` (default where possible): the REAL workflow job is run by
  nektos/act (``catthehacker/ubuntu:act-latest``, native arm64) with a
  ``pull_request`` event whose base is the merge base with origin/main (an
  empty base for main itself, so every path filter is true) -- the ``changes``
  job and dorny/paths-filter run as on GitHub, with git diff (no token: act's
  automatic ``gh auth token`` is overridden with an empty GITHUB_TOKEN; no
  credentials are ever passed). Job containers join the run's own network.
* ``fallback``: for the jobs act cannot run safely on this host (see
  ``CI_JOBS[].why``) this script executes the job itself. The COMMANDS are
  still not copied here: it reads the exported ref's workflow and runs each
  ``run:`` step verbatim with the job/step ``env``, ``working-directory``,
  ``if:``, ``continue-on-error``, ``timeout-minutes``, ``$GITHUB_ENV`` /
  ``$GITHUB_PATH`` and the matrix (``${{ }}`` evaluated by a small GitHub
  expression evaluator). Each workspace gets its own Git index of the export
  before tools run, so tracked-file checks work without the original history
  or remotes. It owns only the runner environment:

  - a container job runs in ``python:<setup-python version>-slim-bookworm`` +
    the ``uv`` binary pinned by ``astral-sh/setup-uv`` (``UV_PYTHON`` set like
    the action does), with its OWN fresh service containers (postgres /
    redis) sharing the job container's network namespace -- so ci.yml's
    ``localhost:5433`` / ``localhost:6380`` URLs work unchanged (each service
    listens on the host-side port of its ``ports:`` mapping);
  - a host job (microservices-smoke) runs with the host docker CLI in a
    fresh export, with its own compose project, gateway port, subnet and
    image tag.

Every run gets an isolated docker network and run-id-named containers; the
shared dev containers (sandloom_postgres_test, sandloom_redis_test, sandloom_redis_bench)
are never touched. ``--engine fallback`` forces the fallback for every job
(e.g. without act).

``--check`` (and the start of every run) compares the workflows with this
mapping: an unmapped job, an unknown ``uses:`` action, an unknown expression,
an unknown service image, or a changed job body (``FINGERPRINTS``) is
reported. ``tests/architecture/test_ci_local_runner.py`` runs the
same check in CI, so a ci.yml change forces a review of this file.

While running, ``/tmp/sandloom-ci-local.lock`` is held (``fcntl.flock``, protocol
in ``tools/ci/ci_lock.py``; the file names run id, pid, ref and start time);
CPU benchmarks take or probe the same flock, so neither runs beside the other.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_lock

LOCK_FILE = ci_lock.LOCK_FILE
# "skipped" = the workflow itself skipped the job (paths filter / if:), as on
# GitHub; an interrupted run reports "cancelled" (a failure).
OK_STATUSES = ("pass", "skipped")
UV_CACHE_VOLUME = "sandloom-ci-local-uv-cache"
RUNNER_IMAGE_REPO = "sandloom-ci-local/runner"
LABEL = "sandloom-ci-local.run"
WORKSPACE = "/github/workspace"
DEFAULT_UV_VERSION = "0.13.4"  # used when a job has no setup-uv step (docs)
DEFAULT_JOB_TIMEOUT_MIN = 360  # GitHub's default job timeout
# Opt-in multiplier of the fallback runner's job/step timeouts (and act's
# outer kill timer) for an overloaded shared host. The workflow's own
# `timeout-minutes` are never edited; a scale != 1 is printed in the summary
# and in every job's note, so a stretched run is never mistaken for CI.
TIMEOUT_SCALE_ENV = "SANDLOOM_CI_LOCAL_TIMEOUT_SCALE"

ACT_IMAGE = "catthehacker/ubuntu:act-latest"  # native arm64 on Apple Silicon
ACT_CACHE_DIR = Path.home() / ".cache" / "sandloom-ci-local" / "act-cache"
# act has no usable artifact backend locally: its built-in server binds a
# fixed port (two at once collide) and actions/upload-artifact talks to
# GitHub's results API, which act does not serve (CreateArtifact times out and
# fails the step). An act job that uses an artifact action therefore runs a
# private workflow copy (act_workflow_copy) in which every such step is a local
# `run:` step: an upload lists what it would upload and honours
# `if-no-files-found`, a download fails with an explanation. Nothing is
# uploaded; the fallback copies upload paths into the run directory instead.
# act's cache server takes port 0 (it binds a free one itself) and each job
# gets its own cache directory (one cache database per server).
ARTIFACT_ACTIONS = ("actions/upload-artifact", "actions/download-artifact")
# The emulated steps read the action's `with:` inputs as INPUT_<NAME>
# environment variables (GitHub's convention for action inputs).
UPLOAD_ARTIFACT_EMULATION = r"""echo "ci_local: actions/upload-artifact emulated under act (no artifact server): nothing is uploaded"
name="${INPUT_NAME:-artifact}"
found=0
while IFS= read -r pattern; do
  case "$pattern" in ""|"!"*) continue ;; esac
  for f in $pattern; do
    if [ -e "$f" ]; then
      found=1
      echo "would upload to '$name': $f ($(du -sh "$f" | cut -f1))"
    fi
  done
done <<< "${INPUT_PATH:-}"
if [ "$found" = 0 ]; then
  case "${INPUT_IF_NO_FILES_FOUND:-warn}" in
    error) echo "::error::no files found for artifact '$name'"; exit 1 ;;
    ignore) echo "no files found for artifact '$name'" ;;
    *) echo "::warning::no files found for artifact '$name'" ;;
  esac
fi
"""
DOWNLOAD_ARTIFACT_EMULATION = (
    'echo "::error::ci_local: actions/download-artifact cannot run under act '
    '(no artifact server); run this job with --engine fallback or on GitHub"\n'
    "exit 1\n"
)
# Docker Desktop under parallel load occasionally fails act's container setup
# ("Error response from daemon: RWLayer of container ... is unexpectedly
# nil"). Such an attempt is retried ONCE, and only when it ran no workflow
# step at all (see act_setup_infra_failure): a step failure is never retried.
ACT_SETUP_RETRIES = 1
# act names a job's container from the workflow's top-level `name:` and the
# job name only (sha256 of "<workflow name>/<job>"), so every act job that
# `needs: changes` runs its OWN `changes` job in a container of the SAME name:
# two of them at once create / `--rm` one container under each other -- the
# "RWLayer of container ... is unexpectedly nil" setup failures of parallel
# runs. Such a job runs a private copy of its workflow whose `name:` carries
# the local job name (act_workflow_copy); nothing in the workflows reads
# github.workflow except architecture-check's concurrency group, which act
# ignores and which has no `needs`.
_WORKFLOW_NAME_RE = re.compile(r"^name:[ \t]*(.*?)[ \t]*$", re.MULTILINE)


_ARTIFACT_USES_RE = re.compile(
    r"^(?P<lead>[ \t]*(?:- )?)uses:[ \t]*"
    r"(?P<action>actions/(?:upload|download)-artifact)@[^\s#]+[ \t]*(?:#.*)?$"
)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _step_keys(
    lines: list[str], start: int, key_col: int
) -> tuple[list[tuple[int, str]], int]:
    """``(line index, key)`` of every key of the step starting at ``start``,
    and the index of the step's first line after it."""
    keys: list[tuple[int, str]] = []
    end = len(lines)
    for k in range(start, len(lines)):
        line = lines[k]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if k > start and _indent(line) < key_col:
            end = k
            break
        if line[:key_col].strip() in ("", "-") and line[key_col : key_col + 1] not in (
            " ",
            "",
        ):
            keys.append((k, line[key_col:].split(":", 1)[0].strip()))
    return keys, end


def _emulate_artifact_steps(text: str) -> str:
    """Every artifact-action step of the workflow as a local ``run:`` step."""
    lines = text.splitlines(keepends=True)
    out: list[str] = list(lines)
    for j, line in enumerate(lines):
        m = _ARTIFACT_USES_RE.match(line.rstrip("\n"))
        if m is None:
            continue
        lead = m.group("lead")
        key_col = len(lead)
        start = j
        if not lead.rstrip().endswith("-"):
            dash = re.compile(rf"^ {{{key_col - 2}}}- ")
            start = next(k for k in range(j, -1, -1) if dash.match(lines[k]))
        keys, step_end = _step_keys(lines, start, key_col)
        if any(key == "env" for _, key in keys):
            raise ValueError(
                f"line {j + 1}: an artifact step with its own env: cannot be emulated"
            )
        for k, key in keys:
            if key != "with":
                continue
            out[k] = lines[k][:key_col] + "env:" + lines[k][key_col + 5 :]
            end = next((n for n, _ in keys if n > k), step_end)
            child = None
            for n in range(k + 1, end):
                if not lines[n].strip():
                    continue
                child = _indent(lines[n]) if child is None else child
                if _indent(lines[n]) == child and ":" in lines[n]:
                    name, rest = lines[n][child:].split(":", 1)
                    env = "INPUT_" + name.strip().upper().replace("-", "_")
                    out[n] = " " * child + env + ":" + rest
        script = (
            UPLOAD_ARTIFACT_EMULATION
            if m.group("action") == "actions/upload-artifact"
            else DOWNLOAD_ARTIFACT_EMULATION
        )
        body = "".join(
            " " * (key_col + 2) + s + "\n" for s in script.rstrip("\n").split("\n")
        )
        out[j] = f"{lead}run: |\n{body}"
    return "".join(out)


def act_workflow_copy(text: str, job_name: str, *, rename: bool = True) -> str:
    """The workflow act runs for ``job_name``: artifact steps emulated
    locally and, with ``rename``, its top-level `name:` made unique."""
    # The local runner is the full validation path, including under act.
    text = text.replace("inputs.full_validation", "true")
    text = _emulate_artifact_steps(text)
    if not rename:
        return text
    m = _WORKFLOW_NAME_RE.search(text)
    if m is None:
        raise ValueError("workflow has no top-level name:")
    name = m.group(1).strip("\"'")
    return f'{text[: m.start()]}name: "{name} [{job_name}]"{text[m.end() :]}'


def act_setup_infra_failure(text: str) -> bool:
    """True when an act attempt died in "Set up job" on a Docker daemon error
    before any workflow step (Pre / Main / Post) of any job started."""
    if "Error response from daemon" not in text:
        return False
    if "Failure - Set up job" not in text:
        return False
    return not re.search(r"\bRun (Pre|Main|Post) ", text)


def uses_artifacts(body: Mapping[str, Any]) -> bool:
    return any(
        str(step.get("uses", "")).split("@")[0] in ARTIFACT_ACTIONS
        for step in body.get("steps") or []
    )


# --------------------------------------------------------------------------
# Mapping table: local job  ->  workflow job.
#
# Line pointers are as of this file's last review; FINGERPRINTS (below) is the
# machine check that a job body has not changed since.
#
#   .github/workflows/ci.yml                          engine (default)
#     L16   changes            -> run by act as the `needs` of act jobs
#     L47   backend-static     -> backend-static            act
#     --    supply-chain       -> supply-chain              act
#     L117  backend-tests      -> backend-tests-3.11/3.13/3.14/3.12 fallback (container)
#     --    backend-tests-isolation -> backend-tests-isolation fallback (container)
#     L222  benchmarks-security-> benchmarks-security       fallback (container)
#     L314  examples           -> examples-linkly/-helpdesk/-projecthub/-academy/-tasklist fallback
#     --    quickstart         -> quickstart                fallback (container)
#     --    app-split          -> app-split                 act (host docker)
#     --    app-recipes        -> app-recipes               fallback (container)
#     L389  deploy-profiles    -> deploy-profiles           act
#     L408  microservices-smoke-> microservices-smoke       fallback (host docker)
#     L442  docker-build       -> docker-build              act
#     --    admin-contracts    -> admin-contracts           act (host fallback: Node)
#   .github/workflows/docs.yml
#     L28   build              -> docs                      act
#   .github/workflows/architecture-check.yml
#     L18   check              -> architecture-check        act
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CiJob:
    workflow: str  # file under .github/workflows/
    job: str  # job id in that workflow
    local: str  # local job name (matrix values are appended)
    runner: str  # fallback runner: "container" | "host"
    engine: str  # default engine: "act" (the real workflow) | "fallback"
    why: str = ""  # why a job cannot run under act on this host


# Why the service-backed jobs cannot use act here: ci.yml reaches its services
# at localhost:5433 / localhost:6380 (the GitHub VM's published ports). act
# publishes service ports on the Docker host and only reaches them from the
# job container with `--network host` -- where localhost:5433/6380 ARE the
# shared dev containers sandloom_postgres_test / sandloom_redis_test (the tests would
# TRUNCATE them), the ports collide, and backend-tests' own
# `docker run --name redis -p 6380:6379` collides by name and port too. With an
# isolated network instead, localhost does not reach the services at all.
_SERVICES_WHY = (
    "ci.yml reaches services at localhost:5433/6380: under act that is the "
    "host network = the shared sandloom_postgres_test/sandloom_redis_test (port + name "
    "collisions, tests would truncate them)"
)
_SMOKE_WHY = (
    "hard-coded gateway localhost:8080 + default subnet 172.28.0.0/24 and host "
    "networking: two stacks/agents collide; fallback gives its own project, port, "
    "subnet and image tag"
)

# Local runs deliberately select full validation; PR cost reduction is remote-only.
CI_JOBS: tuple[CiJob, ...] = (
    CiJob("ci.yml", "workflow-policy", "workflow-policy", "container", "act"),
    CiJob("ci.yml", "backend-static", "backend-static", "container", "act"),
    CiJob("ci.yml", "supply-chain", "supply-chain", "container", "act"),
    CiJob(
        "ci.yml",
        "backend-tests",
        "backend-tests",
        "container",
        "fallback",
        _SERVICES_WHY,
    ),
    CiJob(
        "ci.yml",
        "backend-tests-isolation",
        "backend-tests-isolation",
        "container",
        "fallback",
        _SERVICES_WHY,
    ),
    CiJob(
        "ci.yml",
        "benchmarks-security",
        "benchmarks-security",
        "container",
        "fallback",
        _SERVICES_WHY,
    ),
    CiJob("ci.yml", "examples", "examples", "container", "fallback", _SERVICES_WHY),
    CiJob("ci.yml", "quickstart", "quickstart", "container", "fallback", _SERVICES_WHY),
    CiJob(
        "ci.yml", "app-recipes", "app-recipes", "container", "fallback", _SERVICES_WHY
    ),
    # No services; `docker compose config` needs the docker socket act mounts.
    CiJob("ci.yml", "app-split", "app-split", "host", "act"),
    CiJob("ci.yml", "deploy-profiles", "deploy-profiles", "host", "act"),
    CiJob(
        "ci.yml",
        "microservices-smoke",
        "microservices-smoke",
        "host",
        "fallback",
        _SMOKE_WHY,
    ),
    CiJob("ci.yml", "docker-build", "docker-build", "host", "act"),
    # npm + the backend venv: the fallback runs it on the host (Node there).
    CiJob("ci.yml", "admin-contracts", "admin-contracts", "host", "act"),
    CiJob("docs.yml", "build", "docs", "container", "act"),
    CiJob("architecture-check.yml", "check", "architecture-check", "container", "act"),
)

# Workflow jobs deliberately not reproduced (anything else unmapped = drift).
IGNORED_JOBS: dict[tuple[str, str], str] = {
    (
        "ci.yml",
        "changes",
    ): "runs under act as the `needs` of every act job; fallback jobs always run",
}
# Reusable suites keep their direct mappings above where supported. The GitHub
# aggregator consumes hosted job results, not a subset selected by this runner.
IGNORED_JOBS.update(
    {
        ("ci.yml", "docs"): "reusable suite: mapped directly as docs.yml:build",
        (
            "ci.yml",
            "architecture",
        ): "reusable suite: mapped directly as architecture-check.yml:check",
        (
            "ci.yml",
            "packages",
        ): "reusable package suite; validate with the documented package checker",
        (
            "ci.yml",
            "admin-ui",
        ): "reusable admin suite; validate separately with the admin workflow",
        ("ci.yml", "dependency-review"): "requires the hosted GitHub dependency graph",
        (
            "ci.yml",
            "ci-required",
        ): "hosted result aggregator; its fail-closed behavior is unit-tested",
    }
)
# Other workflows (packages/admin/dependency-review/benchmarks/extraction-e2e/
# full-validation/release) are outside this runner; run their documented
# checks separately.
CHECKED_WORKFLOWS = ("ci.yml", "docs.yml", "architecture-check.yml")

# sha256(json(job body))[:16] per mapped/ignored job when the mapping was last
# reviewed. A mismatch is a WARNING at run time (the runner still executes the
# new commands) and a failure of the architecture test, so the change gets
# reviewed here. Print the current values with `tools/ci/ci_local.py --check`.
FINGERPRINTS: dict[str, str] = {
    "ci.yml:ci-required": "7b3550dbf6899f8f",
    "ci.yml:dependency-review": "2b19360e2aa6b603",
    "ci.yml:admin-ui": "2381440aadc9830d",
    "ci.yml:packages": "d7ee97312d5b4b87",
    "ci.yml:architecture": "7fa82238721b3d55",
    "ci.yml:docs": "2bfd2034c9234a2a",
    "ci.yml:workflow-policy": "63763ee16e8fd08d",
    "ci.yml:changes": "5278fd22bdce278e",
    "ci.yml:backend-static": "0a113b038dd0245d",
    "ci.yml:supply-chain": "e461aa1b6fdeadca",
    "ci.yml:backend-tests": "a24ea0500fc4bcb1",
    "ci.yml:backend-tests-isolation": "396af7768a771758",
    "ci.yml:benchmarks-security": "d8af95a6a0e38048",
    "ci.yml:examples": "5ceec9e17b801f89",
    "ci.yml:quickstart": "0f6563faeb3c2d10",
    "ci.yml:app-split": "40ce521d833336e8",
    "ci.yml:app-recipes": "3708a6d3b9426a26",
    "ci.yml:deploy-profiles": "f0c3c51f23604a21",
    "ci.yml:microservices-smoke": "3f8664b3c7f430a6",
    "ci.yml:docker-build": "6d9cbd4057d87e24",
    "ci.yml:admin-contracts": "52c8b6998210436f",
    "docs.yml:build": "27a1b2fda90627c1",
    "architecture-check.yml:check": "c1dd1ca1c5524d5c",
}

# `uses:` actions and how they map locally (action name without @version).
KNOWN_USES: dict[str, str] = {
    "actions/checkout": "git archive; fallback workspaces get an isolated Git index",
    "actions/setup-python": "runner image python:<version>-slim-bookworm",
    "actions/setup-node": "act runs it; the host fallback uses the host's Node",
    "astral-sh/setup-uv": "uv binary of that version copied into the image; UV_PYTHON set",
    "actions/cache": "not restored (named uv-cache volume instead)",
    "actions/upload-artifact": "act: emulated, files listed; fallback: path copied into the run directory",
    "docker/setup-buildx-action": "host docker buildx",
    "docker/build-push-action": "translated to `docker buildx build --load`",
}

# Service images ci.yml may use, and how a service listens on its ci.yml host
# port inside the shared network namespace.
SERVICE_IMAGES = ("postgres", "redis")

# Literal rewrites of `run:` text for HOST jobs whose commands hard-code a
# host port that a parallel-safe local run must not use. `{GATEWAY_ADDR}` is
# the gateway's real `host:port`, read back from docker after `compose up`.
HOST_RUN_REWRITES: dict[str, list[tuple[str, str]]] = {
    "microservices-smoke": [("http://localhost:8080", "http://{GATEWAY_ADDR}")],
}

# The microservices gateway publishes `${GATEWAY_PORT:-8080}:8080`. The runner
# never picks a host port itself (pick-then-bind races other workloads on a
# shared host: "Address already in use"): `127.0.0.1:0` makes the mapping
# `127.0.0.1:0:8080`, so Docker allocates a free ephemeral port atomically at
# bind time; `docker compose port gateway 8080` reads it back afterwards.
GATEWAY_EPHEMERAL_PORT = "127.0.0.1:0"
GATEWAY_SERVICE, GATEWAY_CONTAINER_PORT = "gateway", 8080

# Container resource limits (Docker Desktop VM is ~8 GB; GitHub runners have
# 4 vCPU / 16 GB). `-n auto` (xdist) is pinned to the job's CPU budget via
# PYTEST_XDIST_AUTO_NUM_WORKERS, as on a 4-vCPU runner.
DEFAULT_JOB_MEMORY = "4g"
DEFAULT_JOB_CPUS = 4
SERVICE_MEMORY = {"postgres": "1g", "redis": "512m"}
# Server settings of every fallback job's own Postgres: it lives for one job
# and no test crashes the server, so the flushes only cost time (they were
# ~45 CPU-s of server work and most of the iowait per backend-tests job).
# synchronous_commit stays at its default: a suite test pins `SHOW
# synchronous_commit` = on (the outbox's delivered-mark sets it LOCAL).
PG_SERVICE_SETTINGS = ("fsync=off", "full_page_writes=off")

# Parallel scheduling (`--parallel`, default from the Docker VM's size: see
# default_parallel). A job container is capped at 4 GB + its services, but
# measured peaks stay near 2.5 GB, so an 8 GB VM affords three at once.
MAX_DEFAULT_PARALLEL = 3
CPUS_PER_PARALLEL_JOB = 3
MEMORY_PER_PARALLEL_JOB = int(2.5 * 1024**3)
# Jobs that assert on timing-sensitive behaviour (wall-clock cancellations,
# load-harness latencies) start FIRST and take the reserved slot 0, whose
# cpuset no other container job shares (see cpuset_for).
TIMING_SENSITIVE_JOBS = ("benchmarks-security",)
# Serial wall time (s) per job group from recent full runs: the scheduler
# starts the longest first (LPT) so the run does not end on one long job. The
# previous run's measured durations win when there is one.
JOB_SECONDS = {
    "benchmarks-security": 240,
    "backend-tests": 100,
    # The whole suite serially in one process (the test isolation gate).
    "backend-tests-isolation": 300,
    "microservices-smoke": 45,
    "backend-static": 35,
    "docker-build": 35,
    "architecture-check": 15,
    # Per app: its own `sandloom test --ci --full` (suite, mypy, drift) + migrate.
    "examples": 60,
    "quickstart": 120,
    "app-split": 90,
    "app-recipes": 100,
    "docs": 10,
    "supply-chain": 10,
    "deploy-profiles": 2,
    "admin-contracts": 60,
}


class DriftError(Exception):
    """The workflow uses something this runner does not know how to map."""


# --------------------------------------------------------------------------
# GitHub expression evaluator (the subset ci.yml uses).
# --------------------------------------------------------------------------

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)|(?P<op>&&|\|\||==|!=|!|\(|\)|,)"
    r"|(?P<id>[A-Za-z_][A-Za-z0-9_.\-]*))"
)


def _tokenize(src: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    src = src.strip()
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise DriftError(f"cannot parse expression: {src!r}")
        pos = m.end()
        kind = m.lastgroup
        assert kind is not None
        out.append((kind, m.group(kind)))
    return out


class Expr:
    """Evaluate one ``${{ }}`` body against a context dict."""

    FUNCS = ("always", "success", "failure", "cancelled")

    def __init__(self, src: str, ctx: dict[str, Any], status: str = "success") -> None:
        self.src = src
        self.toks = _tokenize(src)
        self.i = 0
        self.ctx = ctx
        self.status = status

    def eval(self) -> Any:
        v = self._or()
        if self.i != len(self.toks):
            raise DriftError(f"trailing tokens in expression: {self.src!r}")
        return v

    def _peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _take(self, val: str | None = None) -> tuple[str, str]:
        tok = self._peek()
        if tok is None or (val is not None and tok[1] != val):
            raise DriftError(f"unexpected token in expression: {self.src!r}")
        self.i += 1
        return tok

    def _or(self) -> Any:
        v = self._and()
        while self._peek() == ("op", "||"):
            self._take()
            rhs = self._and()
            v = v if v else rhs
        return v

    def _and(self) -> Any:
        v = self._cmp()
        while self._peek() == ("op", "&&"):
            self._take()
            rhs = self._cmp()
            v = rhs if v else v
        return v

    def _cmp(self) -> Any:
        v = self._unary()
        tok = self._peek()
        if tok in (("op", "=="), ("op", "!=")):
            self._take()
            rhs = self._unary()
            eq = str(v).lower() == str(rhs).lower() if isinstance(v, str) else v == rhs
            return eq if tok[1] == "==" else not eq
        return v

    def _unary(self) -> Any:
        if self._peek() == ("op", "!"):
            self._take()
            return not self._unary()
        return self._atom()

    def _atom(self) -> Any:
        kind, val = self._take()
        if kind == "str":
            return val[1:-1].replace("''", "'")
        if kind == "num":
            return float(val) if "." in val else int(val)
        if kind == "op" and val == "(":
            v = self._or()
            self._take(")")
            return v
        if kind == "id":
            if val in ("true", "false"):
                return val == "true"
            if val == "null":
                return None
            if self._peek() == ("op", "("):
                return self._call(val)
            return self._lookup(val)
        raise DriftError(f"unexpected token {val!r} in expression: {self.src!r}")

    def _call(self, name: str) -> Any:
        self._take("(")
        args = []
        while self._peek() != ("op", ")"):
            args.append(self._or())
            if self._peek() == ("op", ","):
                self._take()
        self._take(")")
        if name == "always":
            return True
        if name == "success":
            return self.status == "success"
        if name == "failure":
            return self.status == "failure"
        if name == "cancelled":
            return False
        if name == "fromJSON":
            return json.loads(args[0])
        if name == "hashFiles":
            return "local"
        raise DriftError(f"unknown expression function {name}() in {self.src!r}")

    def _lookup(self, dotted: str) -> Any:
        node: Any = self.ctx
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                raise DriftError(
                    f"unknown expression context {dotted!r} in {self.src!r}"
                )
            node = node[part]
        return node


_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}", re.S)


def _to_str(v: Any) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return ""
    return str(v)


def interpolate(text: Any, ctx: dict[str, Any], status: str = "success") -> str:
    return _EXPR.sub(
        lambda m: _to_str(Expr(m.group(1), ctx, status).eval()), _to_str(text)
    )


def eval_if(cond: Any, ctx: dict[str, Any], status: str) -> bool:
    if cond is None:
        return status == "success"
    if isinstance(cond, bool):
        return cond
    body = str(cond).strip()
    m = _EXPR.fullmatch(body)
    if m:
        body = m.group(1)
    # GitHub implies success() unless a status function is used.
    if not re.search(r"\b(always|success|failure|cancelled)\(", body):
        body = f"success() && ({body})"
    return bool(Expr(body, ctx, status).eval())


# --------------------------------------------------------------------------
# Workflow model
# --------------------------------------------------------------------------


@dataclass
class LocalJob:
    name: str
    spec: CiJob
    body: dict[str, Any]
    matrix: dict[str, Any]

    @property
    def python(self) -> str | None:
        for step in self.body.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/setup-python"):
                return interpolate(
                    step.get("with", {}).get("python-version"), self.ctx()
                )
        return None

    @property
    def uv_version(self) -> str:
        for step in self.body.get("steps", []):
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv"):
                v = step.get("with", {}).get("version")
                if v:
                    return interpolate(v, self.ctx())
        return DEFAULT_UV_VERSION

    @property
    def matrix_python(self) -> str | None:
        v = self.matrix.get("python")
        return None if v is None else str(v)

    def ctx(self, workspace: str = WORKSPACE, sha: str = "0" * 40) -> dict[str, Any]:
        return {
            "inputs": {"full_validation": True},
            "matrix": self.matrix,
            "github": {
                "workspace": workspace,
                "sha": sha,
                "ref": "refs/heads/ci-local",
                "event_name": "pull_request",
                "event": {"action": "synchronize"},
                "repository": "local/ci-local",
            },
            "runner": {"os": "Linux", "temp": "/tmp"},
            "needs": {
                "changes": {
                    "outputs": dict.fromkeys(
                        ("backend", "admin", "examples", "deploy", "benchmarks"),
                        "true",
                    )
                }
            },
            "env": {},
            "steps": {},
        }


def load_workflows(root: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for wf in CHECKED_WORKFLOWS:
        path = root / ".github" / "workflows" / wf
        out[wf] = yaml.safe_load(path.read_text()) if path.exists() else {}
    return out


def _expand_matrix(
    body: dict[str, Any], *, full_validation: bool = True
) -> list[dict[str, Any]]:
    matrix = (body.get("strategy") or {}).get("matrix")
    if not matrix:
        return [{}]
    if isinstance(matrix, str):
        expression = _EXPR.fullmatch(matrix.strip())
        if expression is None:
            raise DriftError(f"invalid matrix expression: {matrix!r}")
        matrix = Expr(
            expression.group(1), {"inputs": {"full_validation": full_validation}}
        ).eval()
    include = matrix.get("include") or []
    axes = {k: v for k, v in matrix.items() if k not in ("include", "exclude")}
    combos: list[dict[str, Any]] = [{}]
    for key, values in axes.items():
        combos = [{**c, key: v} for c in combos for v in values]
    if axes:
        combos = [c for c in combos if c]
    else:
        combos = []
    combos.extend(dict(i) for i in include)
    return combos or [{}]


def local_jobs(root: Path) -> list[LocalJob]:
    wfs = load_workflows(root)
    jobs: list[LocalJob] = []
    for spec in CI_JOBS:
        body = (wfs.get(spec.workflow) or {}).get("jobs", {}).get(spec.job)
        if body is None:
            continue
        combos = _expand_matrix(body)
        for combo in combos:
            # A path value (examples' `path: examples/linkly`) names nothing
            # new beside its `app` and is no file-name part: left out.
            suffix = "-".join(
                str(v)
                for k, v in combo.items()
                if not isinstance(v, bool) and "/" not in str(v)
            )
            name = f"{spec.local}-{suffix}" if suffix else spec.local
            jobs.append(LocalJob(name, spec, body, combo))
    return jobs


def fingerprint(body: dict[str, Any]) -> str:
    blob = json.dumps(body, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def check_workflows(root: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) of the workflows vs this runner's mapping."""
    errors: list[str] = []
    warnings: list[str] = []
    wfs = load_workflows(root)
    mapped = {(j.workflow, j.job) for j in CI_JOBS}
    for wf, doc in wfs.items():
        if not doc:
            errors.append(f"{wf}: workflow file missing")
            continue
        for job_id, body in (doc.get("jobs") or {}).items():
            key = (wf, job_id)
            fp_key = f"{wf}:{job_id}"
            if key not in mapped and key not in IGNORED_JOBS:
                errors.append(
                    f"{wf}: job {job_id!r} is not mapped (add it to CI_JOBS or IGNORED_JOBS)"
                )
                continue
            want = FINGERPRINTS.get(fp_key)
            have = fingerprint(body)
            if want != have:
                warnings.append(
                    f"{wf}: job {job_id!r} changed since the mapping was reviewed "
                    f"(FINGERPRINTS[{fp_key!r}] {want or '<unset>'} -> {have}); the runner "
                    f"follows the new commands, but review tools/ci/ci_local.py"
                )
            if key in IGNORED_JOBS:
                continue
    for spec in CI_JOBS:
        if not (wfs.get(spec.workflow) or {}).get("jobs", {}).get(spec.job):
            errors.append(f"{spec.workflow}: mapped job {spec.job!r} no longer exists")
    for job in local_jobs(root):
        errors.extend(f"{job.name}: {e}" for e in _check_job(job))
    return errors, warnings


def _check_job(job: LocalJob) -> list[str]:
    errs: list[str] = []
    ctx = job.ctx()
    body = job.body

    def probe(text: Any, where: str) -> None:
        try:
            interpolate(text, ctx)
        except DriftError as exc:
            errs.append(f"{where}: {exc}")

    for k, v in (body.get("env") or {}).items():
        probe(v, f"env {k}")
    wd = ((body.get("defaults") or {}).get("run") or {}).get("working-directory")
    probe(wd, "defaults.run.working-directory")
    for name, svc in (body.get("services") or {}).items():
        image = str(svc.get("image", ""))
        if image.split(":")[0] not in SERVICE_IMAGES:
            errs.append(f"service {name!r}: unknown image {image!r}")
        if svc.get("command"):
            errs.append(f"service {name!r}: `command` is not supported")
    if job.spec.runner == "container" and not job.python:
        errs.append("no actions/setup-python step (cannot pick the runner image)")
    for i, step in enumerate(body.get("steps") or []):
        where = f"step {i} ({step.get('name') or step.get('uses') or 'run'})"
        if "uses" in step:
            action = str(step["uses"]).split("@")[0]
            if action not in KNOWN_USES:
                errs.append(f"{where}: unknown action {step['uses']!r}")
            for k, v in (step.get("with") or {}).items():
                if k in ("key", "restore-keys", "cache-from", "cache-to"):
                    continue
                probe(v, f"{where} with.{k}")
        for k, v in (step.get("env") or {}).items():
            probe(v, f"{where} env {k}")
        if "run" in step:
            probe(step["run"], where)
            if job.spec.runner == "container" and re.search(r"\bdocker\b", step["run"]):
                if _parse_docker_service(step["run"]) is None:
                    errs.append(
                        f"{where}: uses docker inside a container job (unmapped)"
                    )
        try:
            eval_if(step.get("if"), ctx, "success")
        except DriftError as exc:
            errs.append(f"{where} if: {exc}")
    return errs


def _parse_docker_service(run: str) -> dict[str, Any] | None:
    """A `docker run -d --name X -p H:C image args...` step = an extra service.

    (backend-tests starts Redis this way to pass `--databases 256`.)
    """
    text = run.replace("\\\n", " ")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("docker run"):
            continue
        argv = shlex.split(line)[2:]
        port = None
        name = None
        i = 0
        while i < len(argv) and argv[i].startswith("-"):
            flag = argv[i]
            if flag in ("-p", "--publish"):
                port = argv[i + 1]
                i += 2
            elif flag == "--name":
                name = argv[i + 1]
                i += 2
            elif flag in ("-d", "--detach", "--rm"):
                i += 1
            else:
                return None
        if i >= len(argv):
            return None
        image, args = argv[i], argv[i + 1 :]
        if image.split(":")[0] not in SERVICE_IMAGES:
            return None
        return {
            "name": name or image.split(":")[0],
            "image": image,
            "ports": [port] if port else [],
            "args": args,
        }
    return None


# --------------------------------------------------------------------------
# Shell / docker helpers
# --------------------------------------------------------------------------


def sh(
    cmd: list[str], *, check: bool = True, capture: bool = True, **kw: Any
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        **kw,
    )


def index_export(workspace: str | Path, *, prefix: tuple[str, ...] = ()) -> None:
    """Track exactly the exported files before sync/build creates artifacts.

    A fallback checkout has no source history or remote. Force-add includes
    committed files matched by .gitignore, as a real checkout would. ``prefix``
    runs Git in the job container rather than on the host.
    """
    # A pre-commit/pre-push invocation exports GIT_DIR/GIT_INDEX_FILE. `-C`
    # does not override them: even `git init` can reconfigure the caller's
    # repository. Unset Git's own local-variable list at the execution boundary,
    # including inside a prefixed job container.
    local_vars = sh(["git", "rev-parse", "--local-env-vars"]).stdout.splitlines()
    unset = [part for name in local_vars for part in ("-u", name)]
    git = [*prefix, "env", *unset, "git", "-C", str(workspace)]
    sh([*git, "init", "--quiet"])
    sh([*git, "add", "--force", "--all"])


def parse_compose_port(out: str) -> str:
    """`docker compose port` output -> the `host:port` to dial.

    Docker prints one line per binding (`127.0.0.1:55001`, `0.0.0.0:55001`,
    `[::]:55001`); a wildcard host is dialled on loopback.
    """
    for line in out.split():
        host, sep, port = line.strip().rpartition(":")
        if not sep or not port.isdigit() or int(port) == 0:
            continue
        host = host.strip("[]")
        if host in ("", "0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    raise RuntimeError(f"no published port in `docker compose port` output: {out!r}")


#: Where the microservices stack's /24 may come from, in order: 172.30-31
#: first (the documented range), then 10.212-215 when stale networks of other
#: runs hold every 172.30-31 /24 (a whole /16 each on a busy machine).
SUBNET_POOLS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("172.30.0.0/15"),
    ipaddress.IPv4Network("10.212.0.0/14"),
)


def _docker_subnets() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    used: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    ids = sh(["docker", "network", "ls", "-q"]).stdout.split()
    if ids:
        out = sh(
            [
                "docker",
                "network",
                "inspect",
                "--format",
                "{{range .IPAM.Config}}{{.Subnet}} {{end}}",
                *ids,
            ]
        ).stdout
        for tok in out.split():
            with contextlib.suppress(ValueError):
                used.append(ipaddress.ip_network(tok, strict=False))
    return used


def pick_subnet(
    used: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    pools: tuple[ipaddress.IPv4Network, ...] = SUBNET_POOLS,
) -> ipaddress.IPv4Network:
    """A random free /24 of the first pool that has one."""
    taken = [u for u in used if u.version == 4]
    for pool in pools:
        free = [
            net
            for net in pool.subnets(new_prefix=24)
            if not any(net.overlaps(u) for u in taken)  # type: ignore[arg-type]
        ]
        if free:
            return secrets.choice(free)
    raise RuntimeError(
        "no free /24 subnet for the microservices stack in "
        + ", ".join(str(p) for p in pools)
        + " (stale docker networks of other runs hold them)"
    )


def free_subnet() -> tuple[str, str, str]:
    """A /24 not used by any docker network (for compose): 172.30-31.x.0,
    else 10.212-215.x.0.

    Returns (subnet, gateway ip, ip_range): the gateway's static address is
    .10, the dynamic pool the upper /25 — outside it, as the profile requires.
    """
    return stack_addresses(pick_subnet(_docker_subnets()))


def stack_addresses(net: ipaddress.IPv4Network) -> tuple[str, str, str]:
    """(SANDLOOM_SUBNET, SANDLOOM_GATEWAY_IP, SANDLOOM_IP_RANGE) for a /24, laid out like
    the profile's defaults: gateway .10, dynamic pool = the upper /25."""
    return (
        str(net),
        str(net.network_address + 10),
        str(list(net.subnets(new_prefix=25))[1]),
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


@dataclass
class JobResult:
    name: str
    status: str = "pending"  # pass | fail | error | skipped
    seconds: float = 0.0
    log: Path | None = None
    note: str = ""
    engine: str = ""
    steps: list[tuple[str, str, float]] = field(default_factory=list)


class Run:
    def __init__(self, args: argparse.Namespace, repo: Path) -> None:
        self.args = args
        self.repo = repo
        self.timeout_scale = timeout_scale()
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{stamp}-{secrets.token_hex(2)}"
        self.prefix = f"sandloom-ci-{self.run_id}"
        self.network = f"{self.prefix}-net"
        self.run_dir = runs_dir(repo, args.out) / self.run_id
        self.tmp = Path(tempfile.mkdtemp(prefix=f"{self.prefix}-"))
        self.tar = self.tmp / "src.tar"
        self.sha = ""
        self.src_root: Path | None = None
        self.lock: ci_lock.Lock | None = None
        self.act = ""
        self.act_repo = self.tmp
        self.event = self.tmp / "event.json"
        self.print_lock = threading.Lock()
        self.compose_projects: list[tuple[Path, dict[str, str]]] = []
        self.images: list[str] = []
        self.cancelled = threading.Event()
        self.slots: list[int] = list(range(max(1, args.parallel)))
        self.ncpu = getattr(args, "docker_cpus", None) or os.cpu_count() or 1
        self.slot_lock = threading.Lock()

    # -- output ------------------------------------------------------------
    def say(self, msg: str) -> None:
        with self.print_lock:
            print(f"[ci-local {dt.datetime.now():%H:%M:%S}] {msg}", flush=True)

    # -- setup -------------------------------------------------------------
    def export(self) -> None:
        if self.args.fetch:
            self.say("git fetch origin")
            sh(["git", "-C", str(self.repo), "fetch", "--quiet", "origin"])
        ref = self.args.ref
        m = re.fullmatch(r"(?:pr:|#)(\d+)", ref)
        if m:
            local = f"refs/ci-local/pr-{m.group(1)}"
            sh(
                [
                    "git",
                    "-C",
                    str(self.repo),
                    "fetch",
                    "--quiet",
                    "--force",
                    "origin",
                    f"pull/{m.group(1)}/head:{local}",
                ]
            )
            ref = local
        self.sha = sh(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", f"{ref}^{{commit}}"]
        ).stdout.strip()
        sh(
            [
                "git",
                "-C",
                str(self.repo),
                "archive",
                "--format=tar",
                "-o",
                str(self.tar),
                self.sha,
            ]
        )
        self.src_root = self.tmp / "src"
        self.src_root.mkdir()
        with tarfile.open(self.tar) as tf:
            tf.extractall(self.src_root, filter="tar")
        self.say(f"exported {self.args.ref} @ {self.sha[:12]} -> {self.tmp}")

    def setup_network(self) -> None:
        sh(
            [
                "docker",
                "network",
                "create",
                "--label",
                f"{LABEL}={self.run_id}",
                self.network,
            ]
        )
        sh(["docker", "volume", "create", UV_CACHE_VOLUME])

    def runner_image(self, python: str, uv_version: str) -> str:
        tag = f"{RUNNER_IMAGE_REPO}:py{python}-uv{uv_version}"
        if sh(["docker", "image", "inspect", tag], check=False).returncode == 0:
            return tag
        dockerfile = f"""\
FROM ghcr.io/astral-sh/uv:{uv_version} AS uv
FROM python:{python}-slim-bookworm
COPY --from=uv /uv /uvx /usr/local/bin/
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git curl ca-certificates openssl \\
      postgresql-client build-essential \\
 && rm -rf /var/lib/apt/lists/*
"""
        self.say(f"building runner image {tag}")
        r = subprocess.run(
            ["docker", "build", "-q", "-t", tag, "-"],
            input=dockerfile,
            text=True,
            capture_output=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"runner image build failed:\n{r.stdout}\n{r.stderr}")
        return tag

    def engine_of(self, job: LocalJob) -> str:
        return "fallback" if self.args.engine == "fallback" else job.spec.engine

    def prepare_act(self) -> None:
        """A git repo for act: commit 1 = merge base with origin/main (empty for
        main itself, so every path filter is true), commit 2 = the ref."""
        self.act = shutil.which("act") or ""
        if not self.act:
            raise RuntimeError(
                "act not found (brew install act) -- or use --engine fallback"
            )
        if (
            sh(
                ["docker", "image", "inspect", self.args.act_image], check=False
            ).returncode
            != 0
        ):
            self.say(f"pulling {self.args.act_image}")
            sh(["docker", "pull", "-q", self.args.act_image])
        repo = self.act_repo = self.tmp / "act-repo"
        repo.mkdir()
        genv = {
            **os.environ,
            "GIT_AUTHOR_NAME": "ci-local",
            "GIT_AUTHOR_EMAIL": "ci-local@localhost",
            "GIT_COMMITTER_NAME": "ci-local",
            "GIT_COMMITTER_EMAIL": "ci-local@localhost",
        }
        g = ["git", "-C", str(repo)]
        sh([*g, "init", "-q", "-b", "ci-local"], env=genv)
        mb = sh(
            ["git", "-C", str(self.repo), "merge-base", self.sha, self.args.base],
            check=False,
        ).stdout.strip()

        def tree(commit: str) -> str:
            return sh(
                ["git", "-C", str(self.repo), "rev-parse", f"{commit}^{{tree}}"]
            ).stdout

        if mb and tree(mb) != tree(self.sha):
            self._extract(mb, repo)
            sh([*g, "add", "-A"], env=genv)
            base_label = f"merge base {mb[:12]} with {self.args.base}"
        else:
            base_label = "empty tree (ref == base: every path filter true)"
        sh([*g, "commit", "-q", "--allow-empty", "-m", "base"], env=genv)
        base_sha = sh([*g, "rev-parse", "HEAD"]).stdout.strip()
        sh([*g, "rm", "-rq", "--ignore-unmatch", "."], env=genv)
        self._extract(self.sha, repo)
        sh([*g, "add", "-A"], env=genv)
        sh(
            [*g, "commit", "-q", "--allow-empty", "-m", f"ci-local {self.args.ref}"],
            env=genv,
        )
        head_sha = sh([*g, "rev-parse", "HEAD"]).stdout.strip()
        self.event = self.tmp / "event.json"
        self.event.write_text(
            json.dumps(
                {
                    "number": 1,
                    "pull_request": {
                        "number": 1,
                        "base": {"ref": "main", "sha": base_sha},
                        "head": {"ref": "ci-local", "sha": head_sha},
                    },
                    "repository": {
                        "default_branch": "main",
                        "full_name": "local/ci-local",
                    },
                }
            )
        )
        self.say(f"act repo ready; paths-filter base = {base_label}")

    def _extract(self, commit: str, dest: Path) -> None:
        tar = self.tmp / f"{commit[:12]}.tar"
        sh(
            [
                "git",
                "-C",
                str(self.repo),
                "archive",
                "--format=tar",
                "-o",
                str(tar),
                commit,
            ]
        )
        with tarfile.open(tar) as tf:
            tf.extractall(dest, filter="tar")

    # -- cleanup -----------------------------------------------------------
    def cleanup(self) -> None:
        for wd, env in self.compose_projects:
            subprocess.run(
                ["docker", "compose", "down", "-v", "--remove-orphans"],
                cwd=wd,
                env=env,
                capture_output=True,
            )
        ids = sh(
            ["docker", "ps", "-aq", "--filter", f"label={LABEL}={self.run_id}"],
            check=False,
        ).stdout.split()
        if ids:
            sh(["docker", "rm", "-f", "-v", *ids], check=False)
        sh(["docker", "network", "rm", self.network], check=False)
        for image in self.images:
            sh(["docker", "image", "rm", image], check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- job execution -----------------------------------------------------
    def run_job(self, job: LocalJob) -> JobResult:
        res = JobResult(
            job.name, log=self.run_dir / f"{job.name}.log", engine=self.engine_of(job)
        )
        if self.cancelled.is_set():
            res.status = "cancelled"
            res.note = "cancelled"
            return res
        with self.slot_lock:
            slot = take_slot(self.slots, job.spec.local in TIMING_SENSITIVE_JOBS)
        start = time.monotonic()
        self.say(f"START {job.name}")
        try:
            assert res.log is not None
            with open(res.log, "w") as log:
                log.write(
                    f"# {job.name}  ({job.spec.workflow}:{job.spec.job} matrix={job.matrix})\n"
                )
                log.write(f"# ref {self.args.ref} @ {self.sha}\n")
                if self.engine_of(job) == "act":
                    ActJob(self, job, log, res).execute()
                elif job.spec.runner == "container":
                    log.write("# engine: fallback\n")
                    ContainerJob(self, job, log, slot, res).execute()
                else:
                    HostJob(self, job, log, res).execute()
        except Exception as exc:  # runner problem, not a test failure
            res.status = "error"
            res.note = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            with self.slot_lock:
                self.slots.append(slot)
        if scale_note := timeout_scale_note(self.timeout_scale):
            res.note = f"{res.note}; {scale_note}" if res.note else scale_note
        res.seconds = time.monotonic() - start
        self.say(
            f"{res.status.upper():5} {job.name}  ({fmt_secs(res.seconds)})"
            + (f"  {res.note}" if res.note else "")
        )
        if res.status in ("fail", "error") and res.log and res.log.exists():
            tail = res.log.read_text(errors="replace").splitlines()[-self.args.tail :]
            with self.print_lock:
                print("\n".join(f"    | {line}" for line in tail), flush=True)
        return res


def timeout_scale(environ: Mapping[str, str] | None = None) -> float:
    raw = (os.environ if environ is None else environ).get(TIMEOUT_SCALE_ENV, "")
    try:
        value = float(raw.strip() or "1")
    except ValueError:
        raise SystemExit(f"{TIMEOUT_SCALE_ENV}={raw!r} is not a number") from None
    if not 1 <= value <= 10:
        raise SystemExit(f"{TIMEOUT_SCALE_ENV}={raw!r} must be within [1, 10]")
    return value


def timeout_scale_note(scale: float) -> str:
    return f"timeouts x{scale:g} ({TIMEOUT_SCALE_ENV})" if scale != 1 else ""


def fmt_secs(s: float) -> str:
    return f"{int(s // 60)}m{int(s % 60):02d}s"


class _JobBase:
    def __init__(self, run: Run, job: LocalJob, log: Any, res: JobResult) -> None:
        self.run = run
        self.job = job
        self.log = log
        self.res = res
        self.body = job.body
        self.timeout = (
            float(self.body.get("timeout-minutes") or DEFAULT_JOB_TIMEOUT_MIN)
            * 60
            * run.timeout_scale
        )
        self.deadline = time.monotonic() + self.timeout
        self.extra_env: dict[str, str] = {}
        self.extra_path: list[str] = []

    @property
    def workspace(self) -> str:
        raise NotImplementedError

    def ctx(self) -> dict[str, Any]:
        return self.job.ctx(self.workspace, self.run.sha)

    def job_env(self) -> dict[str, str]:
        ctx = self.ctx()
        return {k: interpolate(v, ctx) for k, v in (self.body.get("env") or {}).items()}

    def default_wd(self) -> str | None:
        wd = ((self.body.get("defaults") or {}).get("run") or {}).get(
            "working-directory"
        )
        return interpolate(wd, self.ctx()) if wd else None

    def write(self, text: str) -> None:
        self.log.write(text)
        self.log.flush()

    def execute(self) -> None:
        status = "success"
        self.setup()
        try:
            for i, step in enumerate(self.body.get("steps") or []):
                if self.run.cancelled.is_set():
                    raise KeyboardInterrupt
                name = (
                    step.get("name")
                    or step.get("uses")
                    or (step.get("run") or "").splitlines()[0]
                )
                ctx = self.ctx()
                ctx["env"] = {**self.job_env(), **self.extra_env}
                if not eval_if(step.get("if"), ctx, status):
                    self.write(
                        f"\n## step {i}: {name} -- skipped (if: {step.get('if')})\n"
                    )
                    self.res.steps.append((name, "skipped", 0.0))
                    continue
                t0 = time.monotonic()
                self.write(f"\n## step {i}: {name}\n")
                if "uses" in step:
                    ok = self.uses(step)
                else:
                    ok = self.run_step(step, ctx)
                dt_s = time.monotonic() - t0
                outcome = (
                    "ok"
                    if ok
                    else (
                        "failed (continue-on-error)"
                        if step.get("continue-on-error")
                        else "FAILED"
                    )
                )
                self.write(f"## step {i}: {name} -> {outcome} ({dt_s:.1f}s)\n")
                self.res.steps.append((name, outcome, dt_s))
                if not ok and not step.get("continue-on-error"):
                    status = "failure"
                    if not self.res.note:
                        self.res.note = f"step: {name}"
                if time.monotonic() > self.deadline:
                    status = "failure"
                    self.res.note = (
                        f"timeout-minutes ({self.timeout / 60:.0f}) exceeded"
                    )
                    break
        finally:
            self.teardown()
        self.res.status = "pass" if status == "success" else "fail"

    # hooks
    def setup(self) -> None: ...
    def teardown(self) -> None: ...
    def uses(self, step: dict[str, Any]) -> bool:
        return True

    def run_step(self, step: dict[str, Any], ctx: dict[str, Any]) -> bool:
        raise NotImplementedError

    def remaining(self) -> float:
        return max(1.0, self.deadline - time.monotonic())


class ContainerJob(_JobBase):
    def __init__(
        self, run: Run, job: LocalJob, log: Any, slot: int, res: JobResult
    ) -> None:
        super().__init__(run, job, log, res)
        self.slot = slot
        self.name = f"{run.prefix}-{job.name}".replace(".", "")
        self.services: list[dict[str, Any]] = []
        self.artifacts: list[str] = []

    @property
    def workspace(self) -> str:
        return WORKSPACE

    def setup(self) -> None:
        python = self.job.python
        assert python
        image = self.run.runner_image(python, self.job.uv_version)
        cpus = self.run.args.cpus
        cpuset = ",".join(str(c) for c in cpuset_for(self.slot, cpus, self.run.ncpu))
        env = {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "GITHUB_WORKSPACE": WORKSPACE,
            "GITHUB_SHA": self.run.sha,
            "RUNNER_OS": "Linux",
            "UV_PYTHON": python,  # what astral-sh/setup-uv's python-version sets
            "UV_LINK_MODE": "copy",  # cache volume vs container fs
            "UV_CACHE_DIR": "/root/.cache/uv",
            "PYTEST_XDIST_AUTO_NUM_WORKERS": str(cpus),
        }
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self.name,
            "--hostname",
            "runner",
            "--label",
            f"{LABEL}={self.run.run_id}",
            "--network",
            self.run.network,
            "--memory",
            self.run.args.memory,
            "--memory-swap",
            self.run.args.memory,
            "--cpuset-cpus",
            cpuset,
            "--shm-size",
            "1g",
            "-v",
            f"{UV_CACHE_VOLUME}:/root/.cache/uv",
            "-w",
            WORKSPACE,
        ]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        sh([*cmd, image, "sleep", "infinity"])
        sh(["docker", "exec", self.name, "mkdir", "-p", WORKSPACE])
        with open(self.run.tar, "rb") as fh:
            subprocess.run(
                ["docker", "exec", "-i", self.name, "tar", "-x", "-C", WORKSPACE],
                stdin=fh,
                check=True,
                capture_output=True,
            )
        index_export(WORKSPACE, prefix=("docker", "exec", self.name))
        # services: workflow `services:` + `docker run` service steps
        for sname, svc in (self.body.get("services") or {}).items():
            self.start_service(
                sname,
                str(svc["image"]),
                svc.get("env") or {},
                [str(p) for p in svc.get("ports") or []],
                [],
            )
        for step in self.body.get("steps") or []:
            spec = _parse_docker_service(step.get("run") or "")
            if spec:
                self.start_service(
                    spec["name"], spec["image"], {}, spec["ports"], spec["args"]
                )
        for svc in self.services:
            self.wait_service(svc)

    def start_service(
        self,
        sname: str,
        image: str,
        env: dict[str, Any],
        ports: list[str],
        args: list[str],
    ) -> None:
        kind = image.split(":")[0]
        host_port = int(ports[0].split(":")[0]) if ports else None
        cname = f"{self.name}-{sname}"
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            cname,
            "--label",
            f"{LABEL}={self.run.run_id}",
            "--network",
            f"container:{self.name}",
            "--memory",
            SERVICE_MEMORY[kind],
        ]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        if kind == "postgres":
            port = host_port or 5432
            cmd += ["-e", f"PGPORT={port}", image]  # postgres reads PGPORT as its port
            if args:
                cmd += args
            else:  # a `services:` postgres: run the server with its defaults
                cmd += ["postgres"]
            # A throwaway per-job server: no crash durability to protect, so
            # no data-file / WAL flushes (PG_SERVICE_SETTINGS).
            for setting in PG_SERVICE_SETTINGS:
                cmd += ["-c", setting]
        else:  # redis
            port = host_port or 6379
            rargs = list(args)
            if rargs and rargs[0] == "redis-server":
                rargs = rargs[1:]
            cmd += [image, "redis-server", "--port", str(port), *rargs]
        sh(cmd)
        self.write(
            f"# service {sname}: {image} on localhost:{port} ({' '.join(args)})\n"
        )
        self.services.append(
            {"name": sname, "kind": kind, "cname": cname, "port": port, "env": env}
        )

    def wait_service(self, svc: dict[str, Any]) -> None:
        if svc["kind"] == "postgres":
            user = svc["env"].get("POSTGRES_USER", "postgres")
            probe = [
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-p",
                str(svc["port"]),
                "-U",
                str(user),
            ]
        else:
            probe = ["redis-cli", "-p", str(svc["port"]), "ping"]
        for _ in range(120):
            r = sh(["docker", "exec", svc["cname"], *probe], check=False)
            if r.returncode == 0 and ("PONG" in r.stdout or svc["kind"] == "postgres"):
                return
            time.sleep(1)
        raise RuntimeError(f"service {svc['name']} not healthy after 120s")

    def uses(self, step: dict[str, Any]) -> bool:
        action = str(step["uses"]).split("@")[0]
        if action == "actions/upload-artifact":
            path = interpolate(step.get("with", {}).get("path", ""), self.ctx())
            src = path if path.startswith("/") else f"{WORKSPACE}/{path}"
            dest = self.run.run_dir / f"{self.job.name}-artifact-{Path(path).name}"
            r = sh(["docker", "cp", f"{self.name}:{src}", str(dest)], check=False)
            self.write(
                f"artifact {path} -> {dest} ({'ok' if r.returncode == 0 else r.stdout})\n"
            )
            return r.returncode == 0 or bool(step.get("continue-on-error"))
        self.write(f"(local mapping: {KNOWN_USES.get(action, '?')})\n")
        return True

    def run_step(self, step: dict[str, Any], ctx: dict[str, Any]) -> bool:
        script = interpolate(step["run"], ctx)
        if _parse_docker_service(step["run"]) is not None:
            self.write(f"(mapped to a service container; not executed)\n{script}\n")
            return True
        env = {**self.job_env(), **self.extra_env}
        env.update({k: interpolate(v, ctx) for k, v in (step.get("env") or {}).items()})
        wd = step.get("working-directory")
        wd = interpolate(wd, ctx) if wd else self.default_wd()
        cwd = (
            WORKSPACE if not wd else (wd if wd.startswith("/") else f"{WORKSPACE}/{wd}")
        )
        env["GITHUB_ENV"] = "/tmp/github_env"
        env["GITHUB_PATH"] = "/tmp/github_path"
        prelude = 'export PATH="$(cat /tmp/github_path_acc 2>/dev/null)$PATH"\n'
        cmd = ["docker", "exec", "-w", cwd]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        shell = step.get("shell", "bash")
        if shell != "bash":
            raise DriftError(f"unsupported shell {shell!r}")
        cmd += [
            self.name,
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            prelude + script,
        ]
        self.write(f"$ (cd {cwd}) {script}\n")
        try:
            r = subprocess.run(
                cmd, stdout=self.log, stderr=subprocess.STDOUT, timeout=self.remaining()
            )
        except subprocess.TimeoutExpired:
            self.write("\n!! step timed out (timeout-minutes)\n")
            return False
        self._collect_github_files()
        if r.returncode != 0:
            state = sh(
                ["docker", "inspect", "-f", "{{.State.OOMKilled}}", self.name],
                check=False,
            ).stdout.strip()
            self.write(
                f"\n!! exit code {r.returncode}"
                + (" (container OOM-killed: raise --memory)" if state == "true" else "")
                + (
                    "  (137 = SIGKILL, likely out of memory)"
                    if r.returncode == 137
                    else ""
                )
                + "\n"
            )
        return r.returncode == 0

    def _collect_github_files(self) -> None:
        out = sh(
            [
                "docker",
                "exec",
                self.name,
                "sh",
                "-c",
                "cat /tmp/github_env 2>/dev/null; rm -f /tmp/github_env",
            ],
            check=False,
        ).stdout
        self.extra_env.update(_parse_github_env(out))
        paths = sh(
            [
                "docker",
                "exec",
                self.name,
                "sh",
                "-c",
                "cat /tmp/github_path 2>/dev/null; rm -f /tmp/github_path",
            ],
            check=False,
        ).stdout
        new = [p.strip() for p in paths.splitlines() if p.strip()]
        if new:
            self.extra_path = new + self.extra_path
            acc = "".join(f"{p}:" for p in self.extra_path)
            sh(
                [
                    "docker",
                    "exec",
                    self.name,
                    "sh",
                    "-c",
                    f"printf %s {shlex.quote(acc)} > /tmp/github_path_acc",
                ]
            )

    def teardown(self) -> None:
        if self.run.args.keep_containers:
            return
        names = [s["cname"] for s in self.services] + [self.name]
        sh(["docker", "rm", "-f", "-v", *names], check=False)


def _parse_github_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)<<(.+)$", line)
        if m:
            key, delim = m.groups()
            buf = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            out[key] = "\n".join(buf)
        elif "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
        i += 1
    return out


class HostJob(_JobBase):
    def __init__(self, run: Run, job: LocalJob, log: Any, res: JobResult) -> None:
        super().__init__(run, job, log, res)
        self.dir = run.tmp / f"ws-{job.name}"
        self.local_env: dict[str, str] = {}

    @property
    def workspace(self) -> str:
        return str(self.dir)

    def setup(self) -> None:
        self.dir.mkdir()
        with tarfile.open(self.run.tar) as tf:
            tf.extractall(self.dir, filter="tar")
        index_export(self.dir)
        tag = f"{self.run.prefix}".lower()
        if self.job.spec.local == "microservices-smoke":
            subnet, gw_ip, ip_range = free_subnet()
            self.local_env = {
                "COMPOSE_PROJECT_NAME": f"{tag}-ms",
                "GATEWAY_PORT": GATEWAY_EPHEMERAL_PORT,
                "SANDLOOM_SUBNET": subnet,
                "SANDLOOM_GATEWAY_IP": gw_ip,
                "SANDLOOM_IP_RANGE": ip_range,
                "SANDLOOM_IMAGE": f"sandloom:{self.run.run_id}",
            }
            self.run.images.append(self.local_env["SANDLOOM_IMAGE"])
            env = {**os.environ, **self.job_env(), **self.local_env}
            self.run.compose_projects.append((self.dir, env))
            self.write(f"# compose project {self.local_env}\n")

    def env(self, step: dict[str, Any], ctx: dict[str, Any]) -> dict[str, str]:
        env = {**os.environ, "CI": "true", "GITHUB_WORKSPACE": str(self.dir)}
        # GitHub provides a per-job scratch dir; the smoke's key-generation
        # step (JWT + per-service RPC identity keys) writes its PEMs there.
        runner_temp = self.run.tmp / f"{self.job.name}.runner_temp"
        runner_temp.mkdir(parents=True, exist_ok=True)
        env["RUNNER_TEMP"] = str(runner_temp)
        env.update(self.job_env())
        env.update(self.extra_env)
        env.update({k: interpolate(v, ctx) for k, v in (step.get("env") or {}).items()})
        env.update(self.local_env)
        if self.extra_path:
            env["PATH"] = (
                os.pathsep.join(self.extra_path) + os.pathsep + env.get("PATH", "")
            )
        return env

    def rewrite_vars(self, env: dict[str, str]) -> dict[str, str]:
        """Values for HOST_RUN_REWRITES; the gateway port only exists once
        Docker has bound it, so it is read back lazily (and re-read each time:
        nothing in the smoke recreates the gateway, but a stale port would be
        silently wrong)."""
        out = dict(self.local_env)
        if self.job.spec.local == "microservices-smoke":
            r = subprocess.run(
                [
                    "docker",
                    "compose",
                    "port",
                    GATEWAY_SERVICE,
                    str(GATEWAY_CONTAINER_PORT),
                ],
                cwd=self.dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    "cannot read the gateway's published port "
                    f"(is the stack up?): {(r.stderr or r.stdout).strip()}"
                )
            out["GATEWAY_ADDR"] = parse_compose_port(r.stdout)
            self.write(f"# gateway published at {out['GATEWAY_ADDR']}\n")
        return out

    def uses(self, step: dict[str, Any]) -> bool:
        action = str(step["uses"]).split("@")[0]
        if action != "docker/build-push-action":
            self.write(f"(local mapping: {KNOWN_USES.get(action, '?')})\n")
            return True
        w = {k: interpolate(v, self.ctx()) for k, v in (step.get("with") or {}).items()}
        tag = f"{w.get('tags', 'sandloom-ci')}-{self.run.run_id}".split(",")[0]
        self.run.images.append(tag)
        cmd = ["docker", "buildx", "build", "--load", "-t", tag]
        if w.get("target"):
            cmd += ["--target", w["target"]]
        if w.get("file"):
            cmd += ["-f", str(self.dir / w["file"])]
        cmd.append(str(self.dir / w.get("context", ".")))
        self.write("$ " + " ".join(cmd) + "\n")
        r = subprocess.run(
            cmd,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            cwd=self.dir,
            timeout=self.remaining(),
        )
        return r.returncode == 0

    def run_step(self, step: dict[str, Any], ctx: dict[str, Any]) -> bool:
        script = interpolate(step["run"], ctx)
        wd = step.get("working-directory")
        wd = interpolate(wd, ctx) if wd else self.default_wd()
        cwd = self.dir / wd if wd else self.dir
        envf = self.run.tmp / f"{self.job.name}.github_env"
        pathf = self.run.tmp / f"{self.job.name}.github_path"
        envf.write_text("")
        pathf.write_text("")
        env = self.env(step, ctx)
        for old, new in HOST_RUN_REWRITES.get(self.job.spec.local, []):
            if old in script:
                script = script.replace(old, new.format(**self.rewrite_vars(env)))
        env["GITHUB_ENV"] = str(envf)
        env["GITHUB_PATH"] = str(pathf)
        self.write(f"$ (cd {cwd}) {script}\n")
        try:
            r = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                cwd=cwd,
                env=env,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                timeout=self.remaining(),
            )
        except subprocess.TimeoutExpired:
            self.write("\n!! step timed out (timeout-minutes)\n")
            return False
        self.extra_env.update(_parse_github_env(envf.read_text()))
        self.extra_path = [
            p for p in pathf.read_text().splitlines() if p
        ] + self.extra_path
        if r.returncode != 0:
            self.write(f"\n!! exit code {r.returncode}\n")
        return r.returncode == 0


class ActJob:
    """Run the real workflow job with nektos/act."""

    def __init__(self, run: Run, job: LocalJob, log: Any, res: JobResult) -> None:
        self.run = run
        self.job = job
        self.log = log
        self.res = res

    def execute(self) -> None:
        run, job = self.run, self.job
        timeout = (
            float(job.body.get("timeout-minutes") or DEFAULT_JOB_TIMEOUT_MIN)
            * 60
            * run.timeout_scale
            + 600
        )
        mem, cpus = run.args.memory, run.args.cpus
        empty = run.tmp / "empty.env"
        empty.touch()
        cache_dir = ACT_CACHE_DIR / job.name
        cache_dir.mkdir(parents=True, exist_ok=True)
        workflow = f".github/workflows/{job.spec.workflow}"
        needs = bool(job.body.get("needs"))
        if (
            needs
            or uses_artifacts(job.body)
            or "inputs.full_validation" in str(job.body.get("if", ""))
        ):
            # untracked: the paths filter diffs commits, never sees this file
            private = f".github/workflows/.ci-local-{job.name}.yml"
            src = (run.act_repo / workflow).read_text()
            (run.act_repo / private).write_text(
                act_workflow_copy(src, job.name, rename=needs)
            )
            workflow = private
        cmd = [
            run.act,
            "pull_request",
            "-e",
            str(run.event),
            "-W",
            workflow,
            "-j",
            job.spec.job,
            "-P",
            f"ubuntu-latest={run.args.act_image}",
            "--pull=false",
            "--network",
            run.network,
            "--rm",
            # never credentials: overrides act's automatic `gh auth token`
            "-s",
            "GITHUB_TOKEN=",
            "--env-file",
            str(empty),
            "--secret-file",
            str(empty),
            "--var-file",
            str(empty),
            "--container-options",
            f"--memory {mem} --memory-swap {mem} --cpus {cpus} --label {LABEL}={run.run_id}",
            "--cache-server-path",
            str(cache_dir),
            "--cache-server-port",
            "0",
        ]
        for k, v in job.matrix.items():
            cmd += ["--matrix", f"{k}:{_to_str(v)}"]
        for attempt in range(1 + ACT_SETUP_RETRIES):
            self.log.write("# engine: act\n$ " + shlex.join(cmd) + "\n")
            self.log.flush()
            mark = self.log.tell()
            try:
                r = subprocess.run(
                    cmd,
                    cwd=run.act_repo,
                    stdout=self.log,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                )
                rc = r.returncode
            except subprocess.TimeoutExpired:
                rc = -1
                self.res.note = "act timed out"
            self.log.flush()
            text = ""
            if self.res.log:
                with open(self.res.log, errors="replace") as fh:
                    fh.seek(mark)
                    text = fh.read()  # this attempt's output only
            if rc == 0 or attempt == ACT_SETUP_RETRIES:
                break
            if not act_setup_infra_failure(text):
                break
            self.log.write(
                "\n# Docker daemon error in act's job setup, before any "
                "workflow step: retrying once\n"
            )
            self.res.note = "retried: Docker daemon error in act job setup"
        status, steps = parse_act_log(text, rc)
        self.res.status = status
        self.res.steps = steps
        if status == "skipped":
            self.res.note = "skipped by the workflow (paths filter / if:)"
        elif status == "fail" and not self.res.note:
            bad = [n for n, o, _ in steps if o == "FAILED"]
            self.res.note = f"step: {bad[0]}" if bad else f"act exit {rc}"


def parse_act_log(text: str, rc: int) -> tuple[str, list[tuple[str, str, float]]]:
    """(pass|skipped|fail, steps) of the job under test from an act log."""
    # act prefixes every line with "[<workflow name>/<job name>]"; the `needs`
    # job (ci.yml's `changes`) is not the job under test.
    mine = [
        ln
        for ln in text.splitlines()
        if ln.startswith("[") and not ln.split("]", 1)[0].endswith("/changes")
    ]
    steps: list[tuple[str, str, float]] = []
    for ln in mine:
        m = re.search(
            r"(Success|Failure) - (Main|Pre|Post) (.*?) \[([\d.]+)(ms|s|m)", ln
        )
        if m:
            secs = float(m.group(4)) / (1000 if m.group(5) == "ms" else 1)
            outcome = "ok" if m.group(1) == "Success" else "FAILED"
            steps.append((f"{m.group(2)} {m.group(3)}", outcome, secs))
    succeeded = any("Job succeeded" in ln for ln in mine)
    failed = any("Job failed" in ln for ln in mine)
    if rc == 0 and succeeded and not failed:
        return "pass", steps
    if rc == 0 and not failed and not any("Run Main" in ln for ln in mine):
        return "skipped", steps
    return "fail", steps


# --------------------------------------------------------------------------
# Parallel scheduling
# --------------------------------------------------------------------------


def docker_resources() -> tuple[int, int]:
    """(CPUs, memory bytes) of the Docker engine (the VM on macOS)."""
    try:
        out = sh(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"]).stdout
        ncpu, mem = (int(v) for v in out.split())
        if ncpu > 0 and mem > 0:
            return ncpu, mem
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass
    return os.cpu_count() or 1, 8 * 1024**3


def default_parallel(ncpu: int, mem_bytes: int) -> int:
    """Jobs at once on a Docker engine of this size (1..MAX_DEFAULT_PARALLEL)."""
    return max(
        1,
        min(
            MAX_DEFAULT_PARALLEL,
            ncpu // CPUS_PER_PARALLEL_JOB,
            mem_bytes // MEMORY_PER_PARALLEL_JOB,
        ),
    )


def take_slot(free: list[int], timing_sensitive: bool) -> int:
    """Pop a slot: slot 0 (the reserved cpuset) for a timing-sensitive job,
    else the highest free slot, so slot 0 stays free while others are."""
    slot = min(free) if timing_sensitive else max(free)
    free.remove(slot)
    return slot


def cpuset_for(slot: int, cpus: int, ncpu: int) -> list[int]:
    """CPUs of the container job in ``slot``.

    Slot 0 gets the first ``cpus`` CPUs. When the engine has room for that
    and one more job, the other slots share only the remaining CPUs (they may
    overlap each other, never slot 0), so a timing-sensitive job on slot 0 is
    not starved by its neighbours.
    """
    ncpu = max(1, ncpu)
    cpus = max(1, min(cpus, ncpu))
    if slot == 0 or ncpu < 2 * cpus:
        first = (slot * cpus) % ncpu
        return [(first + k) % ncpu for k in range(cpus)]
    pool = ncpu - cpus
    first = ((slot - 1) * cpus) % pool
    return [cpus + (first + k) % pool for k in range(cpus)]


def schedule(jobs: list[LocalJob], history: Mapping[str, float]) -> list[LocalJob]:
    """Start order: timing-sensitive jobs first, then longest first (LPT)."""

    def seconds(j: LocalJob) -> float:
        return history.get(j.name, JOB_SECONDS.get(j.spec.local, 30))

    return sorted(
        jobs,
        key=lambda j: (j.spec.local not in TIMING_SENSITIVE_JOBS, -seconds(j)),
    )


def runs_dir(repo: Path, out: str | None) -> Path:
    return Path(out) if out else repo / ".ci-local" / "runs"


def run_summaries(parent: Path) -> list[dict[str, Any]]:
    """Every readable summary.json under ``parent``, newest run first."""
    out = []
    for path in sorted(parent.glob("*/summary.json"), reverse=True):
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return out


def job_history(parent: Path) -> dict[str, float]:
    """Each job's duration in its most recent completed (pass/fail) run."""
    seen: dict[str, float] = {}
    for summary in run_summaries(parent):
        for job in summary.get("jobs") or []:
            if job.get("status") in ("pass", "fail") and job.get("seconds"):
                seen.setdefault(str(job["name"]), float(job["seconds"]))
    return seen


#: Statuses `--rerun-failed` reruns (a cancelled job never finished either).
RERUN_STATUSES = ("fail", "error", "cancelled")


def last_failed_jobs(parent: Path, ref: str) -> tuple[str, list[str]] | None:
    """(run id, failed job names) of the newest run of ``ref``; None if none."""
    for summary in run_summaries(parent):
        if summary.get("ref") == ref:
            failed = [
                str(j["name"])
                for j in summary.get("jobs") or []
                if j.get("status") in RERUN_STATUSES
            ]
            return str(summary.get("run_id")), failed
    return None


# --------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------


def acquire_lock(run: Run, wait: bool, path: Path = LOCK_FILE) -> None:
    """Take the CPU-quiet lock (``tools/ci/ci_lock.py``) for the whole run."""
    info = {
        "run_id": run.run_id,
        "ref": run.args.ref,
        "started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if not wait:
        run.lock = ci_lock.try_acquire(info, path)
        if run.lock is None:
            raise SystemExit(f"{path} is held:\n{ci_lock.holder_text(path)}")
        return
    run.lock = ci_lock.acquire(
        info,
        path,
        on_wait=lambda held: run.say(f"waiting for {path} ({' '.join(held.split())})"),
    )


def release_lock(run: Run) -> None:
    if run.lock is not None:
        run.lock.release()
        run.lock = None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def select_jobs(all_jobs: list[LocalJob], args: argparse.Namespace) -> list[LocalJob]:
    jobs = all_jobs
    if args.jobs:
        wanted = [j.strip() for j in args.jobs.split(",") if j.strip()]
        names = {j.name for j in all_jobs} | {j.spec.local for j in all_jobs}
        unknown = [w for w in wanted if w not in names]
        if unknown:
            raise SystemExit(f"unknown job(s) {unknown}; choose from {sorted(names)}")
        jobs = [j for j in all_jobs if j.name in wanted or j.spec.local in wanted]
    if args.python:
        pys = {p.strip() for p in args.python.split(",")}
        jobs = [j for j in jobs if j.matrix_python is None or j.matrix_python in pys]
    return jobs


#: Markers of FULL passing runs, one file per git tree (content) id; the
#: pre-push hooks that mirror CI jobs skip a push of that exact tree
#: (tools/ci/unless_ci_passed.sh).
CI_PASS_DIR = Path(
    os.environ.get("SANDLOOM_CI_PASS_DIR")
    or Path.home() / ".cache" / "sandloom-ci-local" / "passed"
)


def record_ci_pass(repo: Path, run: Run, ref: str) -> None:
    """Mark the run's tree as fully CI-green (best effort: never fails the run)."""
    try:
        tree = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", f"{run.sha}^{{tree}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        CI_PASS_DIR.mkdir(parents=True, exist_ok=True)
        (CI_PASS_DIR / tree).write_text(
            f"run_id={run.run_id}\nsha={run.sha}\nref={ref}\n"
            f"at={dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"WARNING: could not record the CI pass marker: {exc}", file=sys.stderr)


def summary_markdown(run: Run, results: list[JobResult], total: float) -> str:
    ok = bool(results) and all(r.status in OK_STATUSES for r in results)
    lines = [
        f"### Local CI (`tools/ci/ci_local.py`) — {'PASS' if ok else 'FAIL'}",
        "",
        f"ref `{run.args.ref}` @ `{run.sha[:12]}` · run `{run.run_id}` · {fmt_secs(total)}"
        " · local Docker (act + fallback runner)",
        "",
    ]
    if note := timeout_scale_note(run.timeout_scale):
        lines += [
            f"> **WARNING: {note}** — job/step timeouts are stretched for an "
            "overloaded host; a pass here does not prove the job fits its CI "
            "`timeout-minutes`.",
            "",
        ]
    lines += [
        "| job | engine | result | duration | note |",
        "|---|---|---|---|---|",
    ]
    icon = {
        "pass": "pass",
        "fail": "**FAIL**",
        "error": "**ERROR**",
        "skipped": "skipped",
    }
    for r in results:
        lines.append(
            f"| {r.name} | {r.engine} | {icon.get(r.status, r.status)} | {fmt_secs(r.seconds)} | "
            f"{r.note.replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "ref", nargs="?", help="git ref / sha to test (pr:<N> fetches a PR head)"
    )
    p.add_argument("--jobs", help="comma list of jobs or job groups (default: all)")
    p.add_argument("--python", help="comma list of python versions for matrix jobs")
    p.add_argument(
        "--parallel",
        type=int,
        help=(
            "jobs at once (default: from the Docker engine's size, "
            f"min({MAX_DEFAULT_PARALLEL}, CPUs//{CPUS_PER_PARALLEL_JOB}, "
            "memory//2.5GB); 1 = sequential)"
        ),
    )
    p.add_argument(
        "--rerun-failed",
        action="store_true",
        help=(
            "rerun only the jobs that failed (fail/error/cancelled) in the "
            "newest run of this same ref; never records a CI pass"
        ),
    )
    p.add_argument(
        "--memory", default=DEFAULT_JOB_MEMORY, help="job container memory limit"
    )
    p.add_argument(
        "--cpus", type=int, default=DEFAULT_JOB_CPUS, help="CPUs per job container"
    )
    p.add_argument(
        "--engine",
        choices=("auto", "fallback"),
        default="auto",
        help="auto: act where safe, fallback elsewhere (default); fallback: never act",
    )
    p.add_argument("--act-image", default=ACT_IMAGE, help="act image for ubuntu-latest")
    p.add_argument(
        "--base",
        default="origin/main",
        help="base for the paths filter (merge base with the ref; default origin/main)",
    )
    p.add_argument(
        "--fetch", action="store_true", help="git fetch origin before resolving ref"
    )
    p.add_argument("--out", help="run-directory parent (default <repo>/.ci-local/runs)")
    p.add_argument(
        "--tail", type=int, default=40, help="log lines shown for a failed job"
    )
    p.add_argument(
        "--no-wait", action="store_true", help="fail instead of waiting for the lock"
    )
    p.add_argument(
        "--keep-containers", action="store_true", help="debug: keep job containers"
    )
    p.add_argument(
        "--report-pr", type=int, help="post the summary as a comment on this PR (gh)"
    )
    p.add_argument("--list", action="store_true", help="list local jobs and exit")
    p.add_argument("--check", action="store_true", help="workflow drift check and exit")
    args = p.parse_args(argv)

    repo = Path(sh(["git", "rev-parse", "--show-toplevel"]).stdout.strip())
    if args.list or args.check:
        root = repo
        if args.list:
            for j in local_jobs(root):
                where = f"{j.spec.workflow}:{j.spec.job}"
                print(f"{j.name:22} {where:30} {j.spec.engine:8} {j.spec.why[:60]}")
        if args.check:
            errors, warnings = check_workflows(root)
            for w in warnings:
                print(f"WARNING: {w}")
            for e in errors:
                print(f"ERROR: {e}")
            print(
                "fingerprints:",
                json.dumps(
                    {
                        f"{wf}:{jid}": fingerprint(b)
                        for wf, d in load_workflows(root).items()
                        for jid, b in (d.get("jobs") or {}).items()
                    },
                    indent=2,
                ),
            )
            return 1 if errors else 0
        return 0
    if not args.ref:
        p.error("a git ref is required")
    if args.rerun_failed:
        if args.jobs or args.python:
            p.error("--rerun-failed picks the jobs itself: drop --jobs / --python")
        last = last_failed_jobs(runs_dir(repo, args.out), args.ref)
        if last is None:
            p.error(f"--rerun-failed: no earlier run of {args.ref!r} to rerun")
        last_id, failed = last
        if not failed:
            print(f"--rerun-failed: run {last_id} of {args.ref} had no failed job")
            return 0
        print(f"--rerun-failed: rerunning {', '.join(failed)} (failed in {last_id})")
        args.jobs = ",".join(failed)
    args.docker_cpus, docker_mem = docker_resources()
    if args.parallel is None:
        args.parallel = default_parallel(args.docker_cpus, docker_mem)
    args.parallel = max(1, args.parallel)

    run = Run(args, repo)

    def on_signal(signum: int, _frame: Any) -> None:
        run.cancelled.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    # explicit: a shell starting us in the background would leave SIGINT ignored
    signal.signal(signal.SIGINT, on_signal)
    results: list[JobResult] = []
    t0 = time.monotonic()
    try:
        acquire_lock(run, wait=not args.no_wait)
        run.run_dir.mkdir(parents=True, exist_ok=True)
        run.export()
        assert run.src_root
        errors, warnings = check_workflows(run.src_root)
        for w in warnings:
            run.say(f"WARNING drift: {w}")
        for e in errors:
            run.say(f"ERROR drift: {e}")
        jobs = select_jobs(local_jobs(run.src_root), args)
        if errors:
            bad = {e.split(":")[0] for e in errors}
            for j in [j for j in jobs if j.name in bad]:
                results.append(
                    JobResult(j.name, "error", note="workflow drift: see --check")
                )
            jobs = [j for j in jobs if j.name not in bad]
        run.say(
            f"run {run.run_id}: {len(jobs)} job(s), {args.parallel} at once "
            f"-> {run.run_dir}"
        )
        run.setup_network()
        if any(run.engine_of(j) == "act" for j in jobs):
            run.prepare_act()
        if args.parallel > 1:
            order = schedule(jobs, job_history(runs_dir(repo, args.out)))
            with concurrent.futures.ThreadPoolExecutor(args.parallel) as pool:
                by_name = {r.name: r for r in pool.map(run.run_job, order)}
            results += [by_name[j.name] for j in jobs]  # report in workflow order
        else:
            for j in jobs:
                results.append(run.run_job(j))
    except KeyboardInterrupt:
        run.cancelled.set()
        run.say("interrupted — cleaning up")
        done = {r.name for r in results}
        results += (
            [
                JobResult(n, "cancelled", note="interrupted")
                for n in [j.name for j in select_jobs(local_jobs(run.src_root), args)]
                if n not in done
            ]
            if run.src_root
            else []
        )
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            run.cleanup()
        finally:
            release_lock(run)
    total = time.monotonic() - t0
    md = summary_markdown(run, results, total)
    run.run_dir.mkdir(parents=True, exist_ok=True)
    (run.run_dir / "summary.md").write_text(md)
    (run.run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run.run_id,
                "timeout_scale": run.timeout_scale,
                "ref": args.ref,
                "sha": run.sha,
                "seconds": round(total, 1),
                "jobs": [
                    {
                        "name": r.name,
                        "status": r.status,
                        "seconds": round(r.seconds, 1),
                        "note": r.note,
                        "log": str(r.log) if r.log else None,
                        "steps": [
                            {"name": n, "outcome": o, "seconds": round(s, 1)}
                            for n, o, s in r.steps
                        ],
                    }
                    for r in results
                ],
            },
            indent=2,
        )
    )
    print()
    width = max([len(r.name) for r in results] + [4])
    print(f"{'job':{width}}  engine    result   duration  log")
    for r in results:
        print(
            f"{r.name:{width}}  {r.engine:8}  {r.status:7}  {fmt_secs(r.seconds):>8}  {r.log or ''}"
            + (f"\n{'':{width}}  └ {r.note}" if r.note and r.status != "pass" else "")
        )
    ok = bool(results) and all(r.status in OK_STATUSES for r in results)
    if ok and not args.jobs and not args.python and not args.rerun_failed:
        record_ci_pass(repo, run, args.ref)
    if note := timeout_scale_note(run.timeout_scale):
        print(f"\n!!! WARNING: {note} — timeouts stretched, not CI's !!!")
    print(
        f"\n{'PASS' if ok else 'FAIL'}  ref {args.ref} @ {run.sha[:12]}  total {fmt_secs(total)}"
        f"  logs {run.run_dir}"
    )
    if args.report_pr:
        posted = subprocess.run(
            [
                "gh",
                "pr",
                "comment",
                str(args.report_pr),
                "--body-file",
                str(run.run_dir / "summary.md"),
            ],
            cwd=repo,
        )
        if posted.returncode != 0:
            print("WARNING: posting the PR comment failed", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
