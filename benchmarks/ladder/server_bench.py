"""L6 over a real socket: uvicorn (one worker) + a keep-alive HTTP/1.1 client.

The metric is the SERVER process's CPU time per request (psutil user+system
delta over a batch of N requests), which does not depend on how fast the
client is. Wall time / rps are reported as secondary numbers.

    uv run python ladder/server_bench.py --app sandloom --path /s3/ \
        --loop uvloop --http httptools --conc 1 --n 5000 --reps 5

``--app``: ``sandloom`` (benchsuite Sandloom app), ``fastapi`` (bare FastAPI app),
``fast`` (Sandloom app + ladder/fastpath.py prototypes; use ``--path /fast/core/``).
Env: DATABASE_URL / CACHE_URL / BROKER_URL as for ladder.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
import ladder  # noqa: E402  - sets the Sandloom env defaults

sys.path.insert(0, str(HERE.parent))
from benchsuite import loadclient as lc  # noqa: E402

TARGETS = {
    "sandloom": "benchsuite.apps.sandloom_app:app",
    "fastapi": "benchsuite.apps.fastapi_app:app",
    "fast": "fastpath:app",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


#: --mix FILE: a WORKLOAD of several requests — a JSON list of
#: ``{"method", "path", "body", "expect", "weight"}`` scheduled by
#: ``benchsuite.loadclient.schedule`` (deterministic smooth weighted
#: round-robin). Each request is checked against ITS OWN entry's ``expect``.
#: Without --mix: one path (``--path`` + an id, or verbatim with --no-id;
#: ``{id}`` in --path is replaced by the id), --method / --body / --expect.
WORKLOAD: lc.Workload | None = None
TIMEOUT = 30.0


def _path_workload(a: argparse.Namespace) -> lc.Workload:
    ids = a.ids

    def target(e: lc.Entry, i: int) -> str:
        if a.no_id:
            return e.target
        rid = (i % ids) + 1 if ids else ladder.IDS[i & 4095]
        if "{id}" in e.target:
            return e.target.replace("{id}", str(rid))
        return f"{e.target}{rid}"

    entry = lc.Entry(a.method.upper(), a.path, a.body, lc.parse_expect(a.expect))
    return lc.Workload([entry], a.header, target)


def _drive(port: int, start: int, n: int, conc: int) -> lc.RunStats:
    assert WORKLOAD is not None
    return lc.closed_loop(
        [("127.0.0.1", port)], WORKLOAD, start, n, conc, timeout=TIMEOUT
    )


def _open_loop(port: int, rate: float, seconds: float, start: int = 0):
    assert WORKLOAD is not None
    return lc.open_loop(
        [("127.0.0.1", port)], WORKLOAD, rate, seconds, start=start, timeout=TIMEOUT
    )


PG_CONTAINER = os.environ.get("LADDER_PG_CONTAINER", "sandloom_postgres_test")
REDIS_CONTAINER = os.environ.get("LADDER_REDIS_CONTAINER", "sandloom_redis_bench")


def _pg_cpu_usec() -> int | None:
    """Postgres container cgroup CPU usage (µs). NOTE: the container is shared
    with other databases/agents, so this is an upper bound for our load."""
    return _container_cpu_usec(PG_CONTAINER)


def _container_cpu_usec(container: str) -> int | None:
    """A container's cgroup CPU usage (µs), or None (no docker / no container)."""
    if not container:
        return None
    try:
        out = subprocess.run(
            ["docker", "exec", container, "cat", "/sys/fs/cgroup/cpu.stat"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        return int(out.split("usage_usec", 1)[1].split()[0])
    except Exception:  # noqa: BLE001
        return None


def _rss_per_worker(proc: psutil.Process, workers: int) -> float:
    """Resident memory (MB) per serving process after the run."""
    procs = [proc, *proc.children(recursive=True)]
    if workers > 1:  # the master only supervises: count the workers
        procs = procs[1:] or procs
    rss = []
    for p in procs:
        try:
            rss.append(p.memory_info().rss)
        except psutil.Error:
            pass
    return round(sum(rss) / max(len(rss), 1) / 2**20, 1)


def _tree_cpu(proc: psutil.Process) -> float:
    total = 0.0
    for p in [proc, *proc.children(recursive=True)]:
        try:
            t = p.cpu_times()
            total += t.user + t.system
        except psutil.Error:
            pass
    return total


async def _pg_active_sampler(stop: asyncio.Event, out: list[int]) -> None:
    import asyncpg

    from benchsuite import common

    conn = await asyncpg.connect(common.asyncpg_dsn())
    dbname = await conn.fetchval("SELECT current_database()")
    try:
        while not stop.is_set():
            out.append(
                await conn.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = $1 "
                    "AND state = 'active' AND pid <> pg_backend_pid()",
                    dbname,
                )
            )
            try:
                await asyncio.wait_for(stop.wait(), 0.2)
            except TimeoutError:
                pass
    finally:
        await conn.close()


async def _capacity_point(
    proc: psutil.Process, port: int, rate: float, seconds: float
) -> tuple[dict[str, Any], lc.RunStats]:
    stop = asyncio.Event()
    active: list[int] = []
    sampler = asyncio.ensure_future(_pg_active_sampler(stop, active))
    import resource

    c0 = resource.getrusage(resource.RUSAGE_SELF)
    a0, g0, w0 = _tree_cpu(proc), _pg_cpu_usec(), time.perf_counter()
    stats = await _open_loop(port, rate, seconds)
    a1, g1, w1 = _tree_cpu(proc), _pg_cpu_usec(), time.perf_counter()
    c1 = resource.getrusage(resource.RUSAGE_SELF)
    stop.set()
    await sampler
    el = w1 - w0
    res = lc.open_result(stats)
    res.update(
        {
            "rate": rate,
            "app_cores": (a1 - a0) / el,
            "app_cpu_us_per_req": (a1 - a0) / max(stats.ok, 1) * 1e6,
            "pg_cores": (g1 - g0) / 1e6 / el
            if g0 is not None and g1 is not None
            else None,
            "client_cores": ((c1.ru_utime + c1.ru_stime) - (c0.ru_utime + c0.ru_stime))
            / el,
            "pg_active_mean": statistics.mean(active) if active else None,
            "pg_active_max": max(active) if active else None,
        }
    )
    return res, stats


def _per_ok(
    delta: float | None, stats: lc.RunStats, scale: float = 1.0
) -> float | None:
    """A CPU delta per SUCCESSFUL operation (µs)."""
    if delta is None:
        return None
    return delta / max(stats.ok, 1) * scale


#: every RunStats of the process (warm-ups included): any error fails the run
EVERY: list[lc.RunStats] = []


def main() -> None:
    try:
        _run()
    finally:
        sys.stdout.flush()
    sys.exit(lc.exit_code(*EVERY))


def _errors_total() -> dict[str, Any]:
    return {"errors_total": sum(s.errors for s in EVERY)}


def _run() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--app", default="sandloom", choices=list(TARGETS))
    p.add_argument("--path", default="/s3/")
    p.add_argument("--loop", default="uvloop", choices=["asyncio", "uvloop"])
    p.add_argument("--http", default="httptools", choices=["h11", "httptools"])
    p.add_argument("--conc", type=int, default=1)
    p.add_argument("--n", type=int, default=5000)
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--warmup", type=int, default=1000)
    p.add_argument("--relay", default="off")
    p.add_argument("--env", action="append", default=[], help="KEY=VAL for the server")
    p.add_argument("--rate", type=float, default=0, help="open-loop rps (latency mode)")
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--target", default="", help="module:app (overrides --app)")
    p.add_argument("--cwd", default="", help="server working dir (other checkout)")
    p.add_argument("--python", default="", help="server interpreter")
    p.add_argument("--env-file", default="", help="'export K=V' lines (s5 env)")
    p.add_argument("--header", action="append", default=[], help="'Name: value'")
    p.add_argument("--ids", type=int, default=0, help="ids 1..N instead of 1..9999")
    p.add_argument("--no-id", action="store_true", help="GET --path verbatim")
    p.add_argument("--capacity", default="", help="comma-separated open-loop rates")
    p.add_argument("--method", default="GET", help="HTTP method (with --body)")
    p.add_argument("--body", default="", help="JSON body; {i}=index, {u}=random hex")
    p.add_argument("--expect", default="200", help="accepted statuses, e.g. 201,307")
    p.add_argument("--timeout", type=float, default=30.0, help="per-request seconds")
    p.add_argument("--label", default="", help="free-form tag copied to the output")
    p.add_argument(
        "--mix",
        default="",
        help="workload JSON (list of method/path/body/expect/weight)",
    )
    p.add_argument(
        "--containers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="sample the Postgres / Redis container cgroup CPU per rep "
        "(LADDER_PG_CONTAINER / LADDER_REDIS_CONTAINER)",
    )
    a = p.parse_args()

    global WORKLOAD, TIMEOUT
    TIMEOUT = a.timeout
    if a.mix:
        with open(a.mix) as fh:
            WORKLOAD = lc.Workload.from_mix(json.load(fh), a.header)
    else:
        WORKLOAD = _path_workload(a)
    file_env: dict[str, str] = {}
    if a.env_file:
        import shlex

        for line in open(a.env_file):
            if line.startswith("export "):
                k, v = line[7:].strip().split("=", 1)
                file_env[k] = shlex.split(v)[0] if v else ""
    port = _free_port()
    env = {
        **os.environ,
        **file_env,
        "OUTBOX_RELAY_ENABLED": a.relay,
        # a prod stand may run without the relay (a.relay=off): accepted
        "SANDLOOM_ACCEPTED_RISKS": "relay_disabled",
        "PYTHONUNBUFFERED": "1",
        **dict(kv.split("=", 1) for kv in a.env),
    }
    cmd = [
        a.python or sys.executable,
        "-m",
        "uvicorn",
        a.target or TARGETS[a.app],
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--loop",
        a.loop,
        "--http",
        a.http,
        "--no-access-log",
        "--log-level",
        "warning",
        "--app-dir",
        str(HERE),
        "--workers",
        str(a.workers),
    ]
    if a.cwd:
        cmd = [c for c in cmd if c not in ("--app-dir", str(HERE))]
    meta = {"app": a.app, "path": a.path, "env": a.env, "label": a.label}
    every = EVERY
    srv = subprocess.Popen(cmd, env=env, cwd=a.cwd or HERE.parent)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        proc = psutil.Process(srv.pid)
        import uvloop

        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as r:
            warm = r.run(_drive(port, 0, a.warmup, min(a.conc, a.warmup)))
            every.append(warm)
            meta["warmup"] = warm.as_dict()
            if a.capacity:
                every.append(r.run(_open_loop(port, 500 * a.workers, 3)))
                for rate in (float(x) for x in a.capacity.split(",")):
                    pt, st = r.run(_capacity_point(proc, port, rate, a.seconds))
                    every.append(st)
                    print(
                        json.dumps(
                            {
                                **meta,
                                "workers": a.workers,
                                "seconds": a.seconds,
                                **pt,
                                **_errors_total(),
                            }
                        ),
                        flush=True,
                    )
                return
            if a.rate:
                wst = r.run(_open_loop(port, a.rate, 5))  # warm-up at rate
                every.append(wst)
                p0 = _container_cpu_usec(PG_CONTAINER) if a.containers else None
                r0 = _container_cpu_usec(REDIS_CONTAINER) if a.containers else None
                t0 = _tree_cpu(proc)
                st = r.run(_open_loop(port, a.rate, a.seconds, start=10**7))
                t1 = _tree_cpu(proc)
                p1 = _container_cpu_usec(PG_CONTAINER) if a.containers else None
                r1 = _container_cpu_usec(REDIS_CONTAINER) if a.containers else None
                every.append(st)
                print(
                    json.dumps(
                        {
                            **meta,
                            "rate": a.rate,
                            "seconds": a.seconds,
                            "workers": a.workers,
                            "rss_mb_per_worker": _rss_per_worker(proc, a.workers),
                            "server_cpu_us": _per_ok(t1 - t0, st, 1e6),
                            "pg_cpu_us": _per_ok(
                                None if p0 is None or p1 is None else p1 - p0, st
                            ),
                            "redis_cpu_us": _per_ok(
                                None if r0 is None or r1 is None else r1 - r0, st
                            ),
                            "rate_warmup": wst.as_dict(),
                            **lc.open_result(st),
                            **_errors_total(),
                        }
                    ),
                    flush=True,
                )
                return
            cpu, wall = [], []
            pg: list[float] = []
            redis: list[float] = []
            reps: list[lc.RunStats] = []
            for rep in range(a.reps):
                p0 = _container_cpu_usec(PG_CONTAINER) if a.containers else None
                r0 = _container_cpu_usec(REDIS_CONTAINER) if a.containers else None
                t0 = _tree_cpu(proc)
                st = r.run(_drive(port, (rep + 1) * a.n, a.n, a.conc))
                t1 = _tree_cpu(proc)
                p1 = _container_cpu_usec(PG_CONTAINER) if a.containers else None
                r1 = _container_cpu_usec(REDIS_CONTAINER) if a.containers else None
                reps.append(st)
                cpu.append((t1 - t0) / max(st.ok, 1) * 1e6)
                wall.append(st.elapsed / max(st.ok, 1) * 1e6)
                if p0 is not None and p1 is not None:
                    pg.append((p1 - p0) / max(st.ok, 1))
                if r0 is not None and r1 is not None:
                    redis.append((r1 - r0) / max(st.ok, 1))
            every.extend(reps)
            total = lc.RunStats()
            for st in reps:
                total.merge(st)
            rss = _rss_per_worker(proc, a.workers)
    finally:
        srv.terminate()
        srv.wait(10)
    print(
        json.dumps(
            {
                **meta,
                "loop": a.loop,
                "http": a.http,
                "conc": a.conc,
                "n": a.n,
                "relay": a.relay,
                "workers": a.workers,
                "method": a.method,
                # per SUCCESSFUL operation; a run with errors is invalid anyway
                "server_cpu_us": statistics.median(cpu),
                "cpu_min": min(cpu),
                "cpu_max": max(cpu),
                "wall_us": statistics.median(wall),
                "rps": 1e6 / statistics.median(wall),
                "cpu_all": [round(x, 1) for x in cpu],
                "rps_all": [round(1e6 / w, 1) for w in wall],
                "rss_mb_per_worker": rss,
                # container cgroup CPU per request (µs): an UPPER bound when
                # anything else uses the same container during the run
                "pg_cpu_us": statistics.median(pg) if pg else None,
                "redis_cpu_us": statistics.median(redis) if redis else None,
                "pg_all": [round(x, 1) for x in pg],
                "redis_all": [round(x, 1) for x in redis],
                "pg_container": PG_CONTAINER if pg else None,
                "redis_container": REDIS_CONTAINER if redis else None,
                **total.as_dict(),
                **_errors_total(),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
