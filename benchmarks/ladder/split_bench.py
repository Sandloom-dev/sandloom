"""Topology bench: one workload against N app processes (monolith vs split).

Each service is its own uvicorn process (uvloop + httptools, 1 worker) with
its own env (SANDLOOM_MODULES, DATABASE_URL, SERVICE_BINDINGS). The client routes
every request DIRECTLY to the owning service by path (no gateway process, so
no proxy CPU is added): the first matching regex of the topology wins. The
same harness with a single service is the monolith control.

Metric per SUCCESSFUL op (closed loop): CPU of ALL app processes (+ each
service's share), Postgres and Redis container cgroup CPU; rps. Open loop at
a fixed rate: p50/p99 (CO-corrected, perf_counter), CPU per op. Errors are
counted per class by ``benchsuite.loadclient``; any error (warm-ups included)
makes the process exit 1 after printing its JSON.

    python ladder/split_bench.py --topology topo.json --mix mixed.json \\
        --header 'Authorization: Bearer …' --conc 64 --n 8000 --reps 3
    topo.json: [{"name", "env": {..}, "routes": ["^/auth", ...]}, ...]
    (ports are assigned here; "{port:<name>}" in env values is substituted)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import server_bench as sb  # noqa: E402
from darwin_rusage import rusage  # noqa: E402

from benchsuite import loadclient as lc  # noqa: E402


def _ru(pp: list[Any]) -> list[dict[str, float]]:
    """macOS: instructions / cycles of each app process (empty elsewhere)."""
    return [rusage(x.pid) or {} for x in pp]


def _ru_per_op(names: list[str], a: list[dict], b: list[dict], ok: int) -> dict:
    """{service: {instr_k, cycles_k, ghz}} per successful op; {} off macOS."""
    out: dict[str, dict[str, float]] = {}
    for n, x, y in zip(names, a, b, strict=True):
        if not x or not y:
            return {}
        cyc = y["cycles"] - x["cycles"]
        t = (y["user_time"] + y["system_time"]) - (x["user_time"] + x["system_time"])
        out[n] = {
            "instr_k": round((y["instructions"] - x["instructions"]) / ok / 1e3, 1),
            "cycles_k": round(cyc / ok / 1e3, 1),
            "ghz": round(cyc / t / 1e3, 3) if t else 0.0,
        }
    return out

ROUTES: list[tuple[re.Pattern[str], int]] = []  # (regex, service index)
EVERY: list[lc.RunStats] = []


def _router(wl: lc.Workload) -> Any:
    cache: dict[str, int] = {}

    def route(i: int) -> int:
        path = wl.path(i)
        k = cache.get(path)
        if k is None:
            for rx, idx in ROUTES:
                if rx.search(path):
                    k = cache[path] = idx
                    break
            else:
                raise RuntimeError(f"no route for {path}")
        return k

    return route


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    try:
        _run()
    finally:
        sys.stdout.flush()
    sys.exit(lc.exit_code(*EVERY))


def _run() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--topology", required=True)
    p.add_argument("--mix", required=True)
    p.add_argument("--env-file", default="")
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--header", action="append", default=[])
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--cwd", default=str(HERE.parent))
    p.add_argument("--conc", type=int, default=64)
    p.add_argument("--n", type=int, default=8000)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--warmup", type=int, default=1000)
    p.add_argument("--rate", type=float, default=0)
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--label", default="")
    a = p.parse_args()

    with open(a.mix) as fh:
        wl = lc.Workload.from_mix(json.load(fh), a.header)
    route = _router(wl)
    with open(a.topology) as fh:
        topo = json.load(fh)
    ports = {s["name"]: _free_port() for s in topo}
    file_env: dict[str, str] = {}
    if a.env_file:
        import shlex

        for line in open(a.env_file):
            if line.startswith("export "):
                k, v = line[7:].strip().split("=", 1)
                file_env[k] = shlex.split(v)[0] if v else ""
    for k, s in enumerate(topo):
        for rx in s["routes"]:
            ROUTES.append((re.compile(rx), k))
    procs: list[subprocess.Popen[bytes]] = []
    try:
        for s in topo:
            env = {
                **os.environ,
                **file_env,
                "OUTBOX_RELAY_ENABLED": "off",
                "SANDLOOM_ACCEPTED_RISKS": "relay_disabled",
                "PYTHONUNBUFFERED": "1",
                **dict(kv.split("=", 1) for kv in a.env),
            }
            for k, v in s["env"].items():
                for name, port in ports.items():
                    v = v.replace(f"{{port:{name}}}", str(port))
                env[k] = v
            cmd = [
                a.python,
                "-m",
                "uvicorn",
                "sandloom.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(ports[s["name"]]),
                "--loop",
                "uvloop",
                "--http",
                "httptools",
                "--no-access-log",
                "--log-level",
                "warning",
                # deploy/ GUNICORN_KEEPALIVE=75: a low-rate service's pooled
                # client connections must not hit uvicorn's 5 s idle close
                "--timeout-keep-alive",
                "75",
            ]
            procs.append(subprocess.Popen(cmd, env=env, cwd=a.cwd))
        for s in topo:
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    with socket.create_connection(
                        ("127.0.0.1", ports[s["name"]]), timeout=0.2
                    ):
                        break
                except OSError:
                    time.sleep(0.1)
        pp = [psutil.Process(x.pid) for x in procs]
        eps = [("127.0.0.1", ports[s["name"]]) for s in topo]
        names = [s["name"] for s in topo]
        import uvloop

        def drive(start: int, n: int, conc: int) -> lc.RunStats:
            st = r.run(
                lc.closed_loop(eps, wl, start, n, conc, route=route, timeout=a.timeout)
            )
            EVERY.append(st)
            return st

        def openl(rate: float, seconds: float, start: int = 0) -> lc.RunStats:
            st = r.run(
                lc.open_loop(
                    eps,
                    wl,
                    rate,
                    seconds,
                    start=start,
                    pool=128,
                    route=route,
                    timeout=a.timeout,
                )
            )
            EVERY.append(st)
            return st

        def containers() -> tuple[int | None, int | None]:
            return (
                sb._container_cpu_usec(sb.PG_CONTAINER),
                sb._container_cpu_usec(sb.REDIS_CONTAINER),
            )

        out: dict[str, Any] = {"label": a.label, "env": a.env, "services": names}
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as r:
            out["warmup"] = drive(0, a.warmup, min(a.conc, a.warmup)).as_dict()
            if a.rate:
                out["rate_warmup"] = openl(a.rate, 5).as_dict()
                p0, r0 = containers()
                t0 = [sb._tree_cpu(x) for x in pp]
                u0 = _ru(pp)
                st = openl(a.rate, a.seconds, start=10**7)
                u1 = _ru(pp)
                t1 = [sb._tree_cpu(x) for x in pp]
                p1, r1 = containers()
                ok = max(st.ok, 1)
                per = [(y - x) / ok * 1e6 for x, y in zip(t0, t1, strict=True)]
                out.update(
                    rate=a.rate,
                    seconds=a.seconds,
                    server_cpu_us=sum(per),
                    per_service_cpu_us=dict(
                        zip(names, [round(x, 1) for x in per], strict=True)
                    ),
                    pg_cpu_us=(p1 - p0) / ok if p0 and p1 else None,
                    redis_cpu_us=(r1 - r0) / ok if r0 and r1 else None,
                    rusage_per_op=_ru_per_op(names, u0, u1, ok),
                    **lc.open_result(st),
                )
            else:
                cpu, wall, pg, rd, persvc, rus = [], [], [], [], [], []
                total = lc.RunStats()
                for rep in range(a.reps):
                    p0, r0 = containers()
                    t0 = [sb._tree_cpu(x) for x in pp]
                    u0 = _ru(pp)
                    st = drive((rep + 1) * a.n, a.n, a.conc)
                    u1 = _ru(pp)
                    t1 = [sb._tree_cpu(x) for x in pp]
                    p1, r1 = containers()
                    total.merge(st)
                    ok = max(st.ok, 1)
                    per = [(y - x) / ok * 1e6 for x, y in zip(t0, t1, strict=True)]
                    persvc.append(per)
                    rus.append(_ru_per_op(names, u0, u1, ok))
                    cpu.append(sum(per))
                    wall.append(st.elapsed / ok * 1e6)
                    if p0 and p1:
                        pg.append((p1 - p0) / ok)
                    if r0 and r1:
                        rd.append((r1 - r0) / ok)
                out.update(
                    conc=a.conc,
                    n=a.n,
                    server_cpu_us=statistics.median(cpu),
                    cpu_all=[round(x, 1) for x in cpu],
                    per_service_cpu_us={
                        name: round(statistics.median(x[k] for x in persvc), 1)
                        for k, name in enumerate(names)
                    },
                    rps=1e6 / statistics.median(wall),
                    rps_all=[round(1e6 / w, 1) for w in wall],
                    pg_cpu_us=statistics.median(pg) if pg else None,
                    pg_all=[round(x, 1) for x in pg],
                    redis_cpu_us=statistics.median(rd) if rd else None,
                    redis_all=[round(x, 1) for x in rd],
                    rusage_per_op_all=rus,
                    **total.as_dict(),
                )
        out["errors_total"] = sum(s.errors for s in EVERY)
        out["rss_mb_per_process"] = {
            n: round(x.memory_info().rss / 2**20, 1)
            for n, x in zip(names, pp, strict=True)
        }
        print(json.dumps(out), flush=True)
    finally:
        for x in procs:
            x.terminate()
        for x in procs:
            try:
                x.wait(10)
            except subprocess.TimeoutExpired:
                x.kill()


if __name__ == "__main__":
    main()
