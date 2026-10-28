"""Benchmark runner: implementations x servers x scenarios x repetitions.

For every repetition the implementations are run in a ROTATED order (rep 0:
a,b,c; rep 1: b,c,a; ...) and each server is started fresh, so slow drift of a
shared VM (thermal, noisy neighbours) spreads evenly instead of biasing one
implementation. Per (implementation, server, scenario, rep) it records:

* rps (successful responses / elapsed), p50/p90/p99/p99.9 latency, errors —
  from the selected load generators (``--loadgen oha,wrk,wrk2|all``, see
  ``benchsuite.loadgen``). Closed-loop generators (oha, wrk) measure the max
  throughput; wrk2 then runs open-loop at a fixed rate (``--rate``, or
  ``--rate-fraction`` x the closed-loop max measured for that (impl, server,
  scenario) in THIS run) for coordinated-omission-corrected latency;
* server CPU time per request — ``(user+sys)`` delta of the server's whole
  process tree (psutil; includes worker processes and runtime threads)
  divided by the successful requests. This is the per-core efficiency metric:
  it is far less sensitive to the load generator / machine noise than rps.

Before measuring, every implementation passes a semantic contract check (same
status codes and bodies for S1-S4, 401 without a token, 404 for a missing
row), and after S4 the database is checked for exactly one outbox row per
written row. Output: JSON (``--json-out``), the same JSON printed to stdout in
``BENCHJSON`` chunks (``--print-json``, for CI logs), and a Markdown table
(``--markdown-out``, e.g. ``$GITHUB_STEP_SUMMARY``).

    python -m benchsuite.run --impls sandloom,fastapi --servers uvicorn-uvloop \\
        --scenarios s1 --duration 5 --reps 1 --loadgen oha,wrk --oha ./oha
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.client
import json
import os
import platform
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import jwt
import psutil

from benchsuite import common, loadgen, render
from benchsuite.setup_db import count_writes, truncate_writes

# --------------------------------------------------------------------------- #
# Servers / implementations
# --------------------------------------------------------------------------- #

#: server name -> (launcher, extra args). One event loop per worker process.
SERVERS: dict[str, tuple[str, list[str]]] = {
    "uvicorn-h11": ("uvicorn", ["--loop", "asyncio", "--http", "h11"]),
    "uvicorn-httptools": ("uvicorn", ["--loop", "asyncio", "--http", "httptools"]),
    "uvicorn-uvloop": ("uvicorn", ["--loop", "uvloop", "--http", "httptools"]),
    "granian-asyncio": ("granian", ["--loop", "asyncio"]),
    "granian-uvloop": ("granian", ["--loop", "uvloop"]),
    "granian-rloop": ("granian", ["--loop", "rloop"]),
}


@dataclass(frozen=True)
class Impl:
    name: str
    target: str
    scenarios: tuple[str, ...]
    outbox_table: str
    env: dict[str, str] = field(default_factory=dict)


def _sandloom_env() -> dict[str, str]:
    """Sandloom as deployed: production defaults, only the bench module loaded."""
    return {
        "ENVIRONMENT": "prod",
        "SANDLOOM_MODULES": '["benchsuite.bench"]',
        "MODULE_REQUIRED": '["benchsuite.bench"]',
        # Fairness: no per-request log lines in ANY implementation (uvicorn /
        # granian access logs are off too). Sandloom's prod JSON log format stays.
        "LOG_LEVEL": "WARNING",
        # The load generator is one client IP; a limiter would just answer 429.
        "RATE_LIMIT_ENABLED": "false",
        "STORAGE_PATH": os.environ.get("STORAGE_PATH", tempfile.gettempdir()),
    }


IMPLS: dict[str, Impl] = {
    "sandloom": Impl(
        "sandloom",
        "benchsuite.apps.sandloom_app:app",
        common.SCENARIOS,
        "outbox_messages",
        env=_sandloom_env(),
    ),
    "fastapi": Impl(
        "fastapi",
        "benchsuite.apps.fastapi_app:app",
        common.SCENARIOS,
        "bare_outbox_messages",
    ),
    "litestar": Impl(
        "litestar",
        "benchsuite.apps.litestar_app:app",
        common.SCENARIOS,
        "bare_outbox_messages",
    ),
    # Raw asyncpg variant: only the DB scenarios differ from "litestar".
    "litestar-asyncpg": Impl(
        "litestar-asyncpg",
        "benchsuite.apps.litestar_asyncpg_app:app",
        ("s3", "s4"),
        "bare_outbox_messages",
    ),
}

HOST = loadgen.HOST
S4_BODY = loadgen.S4_BODY


def _bin(name: str) -> str:
    exe = Path(sys.executable).parent / name
    return str(exe) if exe.exists() else (shutil.which(name) or name)


def _pin(cpus: str | None) -> list[str]:
    """``taskset -c <cpus>`` prefix (Linux only; ignored elsewhere)."""
    if not cpus or not sys.platform.startswith("linux") or not shutil.which("taskset"):
        return []
    return ["taskset", "-c", cpus]


def server_argv(server: str, target: str, port: int, workers: int) -> list[str]:
    launcher, extra = SERVERS[server]
    if launcher == "uvicorn":
        return [
            sys.executable,
            "-m",
            "uvicorn",
            target,
            "--host",
            HOST,
            "--port",
            str(port),
            "--workers",
            str(workers),
            "--no-access-log",
            "--log-level",
            "warning",
            *extra,
        ]
    return [
        _bin("granian"),
        "--interface",
        "asgi",
        "--host",
        HOST,
        "--port",
        str(port),
        "--workers",
        str(workers),
        "--http",
        "1",
        "--no-ws",
        "--log-level",
        "warning",
        *extra,
        target,
    ]


# --------------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------------- #


def tree_cpu_seconds(pid: int) -> float:
    """user+sys CPU of a process and all its live descendants."""
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return 0.0
    total = 0.0
    for proc in procs:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            t = proc.cpu_times()
            total += t.user + t.system
    return total


def tree_rss_mb(pid: int) -> float:
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return 0.0
    total = 0
    for proc in procs:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            total += proc.memory_info().rss
    return round(total / 1e6, 1)


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex((HOST, port)) == 0


def request(
    port: int,
    method: str,
    path: str,
    *,
    body: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    conn = http.client.HTTPConnection(HOST, port, timeout=10)
    try:
        hdrs = dict(headers or {})
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = raw.decode(errors="replace")
        return resp.status, data
    finally:
        conn.close()


class Server:
    def __init__(self, argv: list[str], env: dict[str, str], port: int) -> None:
        self.argv, self.env, self.port = argv, env, port
        self.log = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed in stop()
            prefix="bench-server-", suffix=".log", delete=False
        )
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self, timeout: float = 90.0) -> None:
        if _port_open(self.port):
            raise RuntimeError(f"port {self.port} is already in use")
        self.proc = subprocess.Popen(
            self.argv,
            env=self.env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early:\n{self.tail()}")
            if _port_open(self.port):
                with contextlib.suppress(OSError, http.client.HTTPException):
                    if request(self.port, "GET", "/s1")[0] == 200:
                        return
            time.sleep(0.2)
        raise RuntimeError(f"server not ready after {timeout}s:\n{self.tail()}")

    def tail(self, lines: int = 40) -> str:
        self.log.flush()
        text = Path(self.log.name).read_text(errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=10)
        deadline = time.monotonic() + 15
        while _port_open(self.port) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.log.close()
        with contextlib.suppress(OSError):
            os.unlink(self.log.name)


# --------------------------------------------------------------------------- #
# Load generation
# --------------------------------------------------------------------------- #


def mint_token() -> tuple[str, str]:
    sub = str(uuid.uuid4())
    now = int(time.time())
    claims = {
        "sub": sub,
        "token_type": "access",
        "iat": now,
        "nbf": now,
        "exp": now + 6 * 3600,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, common.jwt_secret(), algorithm="HS256"), sub


# --------------------------------------------------------------------------- #
# Semantic contract check
# --------------------------------------------------------------------------- #


def contract_check(port: int, impl: Impl, token: str, sub: str) -> None:
    """Same observable behaviour for every implementation, or fail loudly."""

    def expect(cond: bool, what: str) -> None:
        if not cond:
            raise RuntimeError(f"[{impl.name}] contract check failed: {what}")

    if "s1" in impl.scenarios:
        st, body = request(port, "GET", "/s1")
        expect(st == 200 and body == {"ok": True}, f"S1 -> {st} {body!r}")
    if "s2" in impl.scenarios:
        st, _ = request(port, "GET", "/s2")
        expect(st == 401, f"S2 without token -> {st}")
        st, _ = request(port, "GET", "/s2", headers={"Authorization": "Bearer x.y.z"})
        expect(st == 401, f"S2 with a garbage token -> {st}")
        st, body = request(
            port, "GET", "/s2", headers={"Authorization": f"Bearer {token}"}
        )
        expect(
            st == 200 and body == {"ok": True, "user_id": sub}, f"S2 -> {st} {body!r}"
        )
    if "s3" in impl.scenarios:
        st, body = request(port, "GET", "/s3/42")
        expect(
            st == 200 and body == {"id": 42, "name": "item-42", "value": 42},
            f"S3 -> {st} {body!r}",
        )
        st, _ = request(port, "GET", f"/s3/{common.SEED_ROWS + 1000}")
        expect(st == 404, f"S3 missing row -> {st}")
    if "s4" in impl.scenarios:
        st, body = request(port, "POST", "/s4", body=S4_BODY)
        expect(
            st == 201 and isinstance(body, dict) and isinstance(body.get("id"), int),
            f"S4 -> {st} {body!r}",
        )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _versions() -> dict[str, str]:
    names = [
        "sandloom",
        "fastapi",
        "starlette",
        "pydantic",
        "pydantic-core",
        "uvicorn",
        "uvloop",
        "httptools",
        "h11",
        "granian",
        "rloop",
        "litestar",
        "msgspec",
        "sqlalchemy",
        "asyncpg",
        "redis",
        "hiredis",
        "pyjwt",
        "orjson",
        "psutil",
    ]
    out = {}
    for name in names:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "missing"
    return out


def _cpu_model() -> str:
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


def _git_sha() -> str:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    with contextlib.suppress(Exception):
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
        ).stdout.strip()
    return "unknown"


def meta(args: argparse.Namespace, gens: dict[str, loadgen.LoadGen]) -> dict[str, Any]:
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    versions = {name: gen.version() for name, gen in gens.items()}
    return {
        "label": args.label,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": platform.python_version() + ("" if gil else "t (GIL disabled)"),
        "python_build": sys.version.replace("\n", " "),
        "gil_enabled": gil,
        "platform": platform.platform(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "git_sha": _git_sha(),
        "load_generator": ", ".join(versions.values()),
        "load_generators": versions,
        "versions": _versions(),
        "params": {
            "impls": args.impls,
            "servers": args.servers,
            "scenarios": args.scenarios,
            "loadgens": args.loadgen,
            "workers": args.workers,
            "duration_s": args.duration,
            "warmup_s": args.warmup,
            "reps": args.reps,
            "connections": args.connections,
            "client_threads": args.client_threads,
            "server_cpus": args.server_cpus,
            "client_cpus": args.client_cpus,
            "wrk2_scenarios": args.wrk2_scenarios,
            "rate": args.rate,
            "rate_fraction": args.rate_fraction,
            "rate_basis": args.rate_basis,
        },
    }


class Session:
    """One benchmark invocation: servers, generators, rows and failures."""

    def __init__(self, args: argparse.Namespace, gens: dict[str, loadgen.LoadGen]):
        self.args = args
        self.gens = gens
        self.token, self.sub = mint_token()
        self.impls = [IMPLS[n] for n in args.impls]
        self.results: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.base_env = {
            **os.environ,
            "DATABASE_URL": common.database_url(),
            "CACHE_URL": common.cache_url(),
            "BROKER_URL": common.broker_url(),
            "JWT_SECRET": common.jwt_secret(),
            "PYTHONUNBUFFERED": "1",
        }

    def spec(self, scenario: str, seconds: float, rate: int | None = None) -> Any:
        return loadgen.LoadSpec(
            port=self.args.port,
            scenario=scenario,
            token=self.token,
            seconds=seconds,
            connections=self.args.connections,
            threads=self.args.client_threads,
            client_cpus=self.args.client_cpus,
            rate=rate,
        )

    def rotated(self, rep: int) -> list[Impl]:
        k = rep % len(self.impls)
        return self.impls[k:] + self.impls[:k]

    def with_server(self, impl: Impl, server: str, key: dict[str, Any], body: Any):
        """Fresh server (empty write tables) + contract check, then ``body``."""
        asyncio.run(truncate_writes())
        srv = Server(
            [
                *_pin(self.args.server_cpus),
                *server_argv(server, impl.target, self.args.port, self.args.workers),
            ],
            {**self.base_env, **impl.env},
            self.args.port,
        )
        try:
            srv.start()
            contract_check(self.args.port, impl, self.token, self.sub)
            body(srv)
        except Exception as exc:  # noqa: BLE001 - record and continue
            self.failures.append({**key, "error": str(exc)[-3000:]})
            print(f"!! {impl.name} on {server}: {exc}", file=sys.stderr)
        finally:
            srv.stop()

    def measure(
        self,
        srv: Server,
        impl: Impl,
        key: dict[str, Any],
        scenario: str,
        gen: loadgen.LoadGen,
        rate: int | None = None,
    ) -> None:
        pid = srv.proc.pid  # type: ignore[union-attr]
        cpu0 = tree_cpu_seconds(pid)
        stats = gen.run(self.spec(scenario, self.args.duration, rate))
        cpu1 = tree_cpu_seconds(pid)
        ok = stats["ok"]
        row = {
            **key,
            "scenario": scenario,
            "loadgen": gen.name,
            **stats,
            "cpu_us_per_req": round((cpu1 - cpu0) / ok * 1e6, 1) if ok else None,
            "rss_mb": tree_rss_mb(pid),
        }
        if scenario == "s4":
            # The write tables were emptied before this server started and
            # only it writes to them: once in-flight requests settle, every
            # written row must have exactly one outbox row.
            time.sleep(1.0)
            counts = asyncio.run(count_writes())
            writes, events = counts["bench_writes"], counts[impl.outbox_table]
            row["s4_rows"], row["s4_outbox_rows"] = writes, events
            if impl.outbox_table == "outbox_messages":
                row["s4_outbox_delivered"] = counts["outbox_messages_delivered"]
            row["s4_outbox_ok"] = writes == events and writes >= ok
        self.results.append(row)
        _progress(row)

    # ---- phase 1: closed loop (max throughput) -------------------------- #

    def closed_loop(self, server: str, gens: list[loadgen.LoadGen], reps: int) -> None:
        for rep in range(reps):
            # Rotate generators per rep too, so neither always measures a
            # freshly warmed server first.
            order = gens[rep % len(gens) :] + gens[: rep % len(gens)]
            for impl in self.rotated(rep):
                scenarios = [s for s in self.args.scenarios if s in impl.scenarios]
                if not scenarios:
                    continue
                key = {
                    "impl": impl.name,
                    "server": server,
                    "workers": self.args.workers,
                    "rep": rep,
                }

                def body(
                    srv: Server,
                    impl: Impl = impl,
                    key: dict[str, Any] = key,
                    scenarios: list[str] = scenarios,
                    order: list[loadgen.LoadGen] = order,
                ) -> None:
                    for scenario in scenarios:
                        if self.args.warmup > 0:  # discarded, once per scenario
                            order[0].run(self.spec(scenario, self.args.warmup))
                        for gen in order:
                            self.measure(srv, impl, key, scenario, gen)

                self.with_server(impl, server, key, body)

    # ---- phase 2: open loop at a fixed rate (wrk2) ---------------------- #

    def rates(self, server: str, scenarios: list[str]) -> dict[tuple[str, str], int]:
        """Target rate per (impl, scenario) from THIS run's closed-loop rows."""
        if self.args.rate:
            return {
                (i.name, s): int(self.args.rate)
                for i in self.impls
                for s in scenarios
                if s in i.scenarios
            }
        best: dict[tuple[str, str], float] = {}
        for impl in self.impls:
            for s in scenarios:
                per_gen = []
                for g in loadgen.CLOSED_LOOP:
                    vals = [
                        r["rps"]
                        for r in self.results
                        if r["impl"] == impl.name
                        and r["server"] == server
                        and r["scenario"] == s
                        and r["loadgen"] == g
                        and r["workers"] == self.args.workers
                    ]
                    if vals:
                        per_gen.append(statistics.median(vals))
                if per_gen:
                    best[(impl.name, s)] = max(per_gen)
        frac = self.args.rate_fraction
        if self.args.rate_basis == "common":
            # One offered load per scenario: a fraction of the SLOWEST impl's
            # max, so every implementation is compared at the same rate.
            out = {}
            for s in scenarios:
                vals = [v for (_, sc), v in best.items() if sc == s]
                for name, sc in best:
                    if sc == s and vals:
                        out[(name, sc)] = max(1, int(min(vals) * frac))
            return out
        return {k: max(1, int(v * frac)) for k, v in best.items()}

    def open_loop(self, server: str, gen: loadgen.LoadGen) -> None:
        scenarios = [s for s in self.args.wrk2_scenarios if s in self.args.scenarios]
        rates = self.rates(server, scenarios)
        for rep in range(self.args.reps):
            for impl in self.rotated(rep):
                todo = [s for s in scenarios if (impl.name, s) in rates]
                if not todo:
                    continue
                key = {
                    "impl": impl.name,
                    "server": server,
                    "workers": self.args.workers,
                    "rep": rep,
                }

                def body(srv: Server, impl: Impl = impl, key: dict = key, todo=todo):
                    # wrk2's own ~10 s calibration window is the warm-up.
                    for scenario in todo:
                        rate = rates[(impl.name, scenario)]
                        self.measure(srv, impl, key, scenario, gen, rate=rate)

                self.with_server(impl, server, key, body)

    def run(self) -> dict[str, Any]:
        wanted = self.args.loadgen
        closed = [self.gens[g] for g in wanted if g in loadgen.CLOSED_LOOP]
        open_gen = self.gens.get("wrk2") if "wrk2" in wanted else None
        closed_reps = self.args.reps
        if open_gen and not closed and not self.args.rate:
            # wrk2 needs a max to take a fraction of: calibrate with one
            # closed-loop pass (wrk if present, else oha) in this same job.
            cal = "wrk" if "wrk" in self.gens else "oha"
            closed, closed_reps = [self.gens[cal]], 1
        for server in self.args.servers:
            if closed:
                self.closed_loop(server, closed, closed_reps)
            if open_gen:
                self.open_loop(server, open_gen)
        return {
            "meta": meta(self.args, self.gens),
            "results": self.results,
            "failures": self.failures,
        }


def _progress(row: dict[str, Any]) -> None:
    rate = f" @{row['target_rps']}/s" if row.get("target_rps") else ""
    print(
        f"{row['impl']:>17} {row['server']:>18} w{row['workers']} rep{row['rep']} "
        f"{row['scenario']} {row['loadgen']:>4}{rate}: {row['rps']:>9.1f} rps  "
        f"p99 {row['p99_ms']} ms  cpu {row['cpu_us_per_req']} us/req  "
        f"err {row['errors']}",
        flush=True,
    )


CHUNK = 6000


def print_json_chunks(doc: dict[str, Any], label: str) -> None:
    """Emit the JSON as ``BENCHJSON <label> i/n <chunk>`` log lines."""
    raw = json.dumps(doc, separators=(",", ":"))
    chunks = [raw[i : i + CHUNK] for i in range(0, len(raw), CHUNK)] or [""]
    for i, chunk in enumerate(chunks, 1):
        print(f"BENCHJSON {label} {i}/{len(chunks)} {chunk}")


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _loadgens(value: str) -> list[str]:
    names = _csv(value)
    if "all" in names:
        return list(loadgen.ALL)
    return names


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--impls", type=_csv, default=list(IMPLS))
    p.add_argument("--servers", type=_csv, default=list(SERVERS))
    p.add_argument("--scenarios", type=_csv, default=list(common.SCENARIOS))
    p.add_argument(
        "--loadgen",
        type=_loadgens,
        default=["oha"],
        help="comma list of oha,wrk,wrk2 or 'all'",
    )
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--duration", type=float, default=10.0, help="seconds per rep")
    p.add_argument("--warmup", type=float, default=3.0, help="seconds per scenario")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--connections", type=int, default=64)
    p.add_argument(
        "--client-threads",
        type=int,
        default=None,
        help="load generator threads (default: the client CPU set, else CPUs-1)",
    )
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--oha", default=shutil.which("oha") or "oha")
    p.add_argument("--wrk", default=shutil.which("wrk") or "wrk")
    p.add_argument("--wrk2", default=shutil.which("wrk2") or "wrk2")
    p.add_argument(
        "--wrk2-scenarios", type=_csv, default=["s2", "s3", "s4"]
    )  # headline scenarios
    p.add_argument("--rate", type=int, default=None, help="fixed wrk2 rate (req/s)")
    p.add_argument(
        "--rate-fraction",
        type=float,
        default=0.7,
        help="wrk2 rate = this fraction of the closed-loop max (default 0.7)",
    )
    p.add_argument(
        "--rate-basis",
        choices=["per-impl", "common"],
        default="per-impl",
        help="per-impl: each impl at a fraction of ITS max; common: every impl "
        "at a fraction of the slowest impl's max (same offered load)",
    )
    p.add_argument("--server-cpus", default=None, help="taskset list, Linux only")
    p.add_argument("--client-cpus", default=None, help="taskset list, Linux only")
    p.add_argument("--label", default=platform.python_version())
    p.add_argument("--json-out", type=Path, default=None)
    p.add_argument("--markdown-out", type=Path, default=None)
    p.add_argument("--print-json", action="store_true")
    args = p.parse_args(argv)
    for name in args.impls:
        if name not in IMPLS:
            p.error(f"unknown impl {name!r}; choose from {sorted(IMPLS)}")
    for name in args.servers:
        if name not in SERVERS:
            p.error(f"unknown server {name!r}; choose from {sorted(SERVERS)}")
    for name in [*args.scenarios, *args.wrk2_scenarios]:
        if name not in common.SCENARIOS:
            p.error(f"unknown scenario {name!r}; choose from {common.SCENARIOS}")
    for name in args.loadgen:
        if name not in loadgen.ALL:
            p.error(f"unknown load generator {name!r}; choose from {loadgen.ALL}")
    if args.client_threads is None:
        args.client_threads = loadgen.cpu_list_size(args.client_cpus) or max(
            1, (os.cpu_count() or 2) - 1
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    binaries = {"oha": args.oha, "wrk": args.wrk, "wrk2": args.wrk2}
    gens: dict[str, loadgen.LoadGen] = {}
    skipped = []
    needed = set(args.loadgen) | ({"wrk", "oha"} if "wrk2" in args.loadgen else set())
    for name in loadgen.ALL:
        if name not in needed:
            continue
        gen = loadgen.make(name, binaries)
        if gen.available():
            gens[name] = gen
        elif name in args.loadgen:
            skipped.append(name)
            print(f"!! load generator {name} not found ({binaries[name]}); skipped")
    args.loadgen = [g for g in args.loadgen if g in gens]
    if not args.loadgen:
        print("no load generator available", file=sys.stderr)
        return 1
    doc = Session(args, gens).run()
    doc["failures"] += [
        {"impl": "-", "server": "-", "workers": args.workers, "rep": "-",
         "error": f"load generator {name} not available — skipped"}
        for name in skipped
    ]  # fmt: skip
    if args.json_out:
        args.json_out.write_text(json.dumps(doc, indent=2))
    if args.print_json:
        print_json_chunks(doc, args.label)
    md = render.render([doc])
    if args.markdown_out:
        with args.markdown_out.open("a") as fh:
            fh.write(md + "\n")
    else:
        print(md)
    return 1 if doc["failures"] and not doc["results"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
