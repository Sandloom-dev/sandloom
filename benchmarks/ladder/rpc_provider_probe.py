"""RPC provider probe: does the provider do the same work per call on each
``RPC_TRANSPORT`` at a FIXED rate, and what does it cost?

    python ladder/rpc_provider_probe.py --bench <build>/benchmarks \\
        --env-file int/main.env --topology int/topo_split.json \\
        --mix int/prof/single_tickets.json --transport aiohttp --rate 250 \\
        --seconds 30 --out res.jsonl [--probe] [--profile] [--warmup-conc 64]

The split topology of ``split_bench.py`` (same uvicorn flags, the client
routes each request to its owning service), the same warm-up (closed loop
``--warmup-conc``, 1,000 requests; then 5 s at the rate) and a measured
open-loop window at ``--rate``. Recorded per successful op, per process:
user / sys CPU (µs), voluntary / involuntary context switches; the Postgres
and Redis container CPU; ``pg_stat_database`` deltas of every split database
(transactions, NEW sessions, tuples); established TCP connections to every
service before / after the window.

``--probe`` loads ``probe_site/sitecustomize.py`` into every service (work
COUNTERS: provider requests / responses / ``data_received`` calls / transport
writes / bytes / header sets / connections, asyncpg calls and connects,
Redis commands, RPC proxy calls, client attempts and new client
connections; SQLAlchemy pool states). ``--profile`` adds a cProfile of the
provider's main thread over the window, on THREAD CPU time. Both perturb CPU:
compare CPU only from runs without them.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time

import asyncpg
import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--bench", required=True, help="the build's benchmarks/ dir")
ap.add_argument("--env-file", required=True)
ap.add_argument("--topology", required=True)
ap.add_argument("--mix", required=True)
ap.add_argument("--db-prefix", default="bench_oa_split")
ap.add_argument("--pg", default="postgresql://test_user:test_password@localhost:5433/")
ap.add_argument("--provider", default="tenancy")
ap.add_argument("--transport", required=True)
ap.add_argument("--rate", type=float, default=250)
ap.add_argument("--seconds", type=float, default=30)
ap.add_argument("--warmup-conc", type=int, default=64)
ap.add_argument("--env", action="append", default=[])
ap.add_argument("--probe", action="store_true")
ap.add_argument("--profile", action="store_true")
ap.add_argument("--work-dir", default="/tmp/rpc_provider_probe")
ap.add_argument("--out", required=True)
ap.add_argument("--meta", default="{}", help="extra JSON merged into the record")
a = ap.parse_args()

sys.path.insert(0, a.bench)
sys.path.insert(0, os.path.join(a.bench, "ladder"))
import server_bench as sb  # noqa: E402
from darwin_rusage import rusage  # noqa: E402

from benchsuite import loadclient as lc  # noqa: E402
from benchsuite import mix  # noqa: E402

PY = os.path.join(a.bench, ".venv/bin/python")
os.makedirs(a.work_dir, exist_ok=True)
for f in glob.glob(f"{a.work_dir}/p.*"):
    os.remove(f)

with open(a.mix) as fh:
    wl = lc.Workload.from_mix(
        json.load(fh), ["Authorization: Bearer " + mix.mint(hours=24, tenant=True)]
    )
topo = json.load(open(a.topology))
names = [s["name"] for s in topo]
routes = [(re.compile(rx), k) for k, s in enumerate(topo) for rx in s["routes"]]
DBS = [f"{a.db_prefix}_{n}" for n in names]


def route(i: int) -> int:
    path = wl.path(i)
    for rx, k in routes:
        if rx.search(path):
            return k
    raise KeyError(path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


ports = {n: free_port() for n in names}
file_env: dict[str, str] = {}
for line in open(a.env_file):
    if line.startswith("export "):
        k, v = line[7:].strip().split("=", 1)
        file_env[k] = shlex.split(v)[0] if v else ""


async def pgstat() -> dict:
    out = {}
    for db in DBS:
        c = await asyncpg.connect(a.pg + db)
        try:
            r = await c.fetchrow(
                "SELECT xact_commit, xact_rollback, sessions, tup_returned,"
                " tup_fetched, tup_inserted, tup_updated, tup_deleted, blks_hit"
                " FROM pg_stat_database WHERE datname = current_database()"
            )
            out[db] = dict(r)
        finally:
            await c.close()
    return out


def established(port: int) -> int | None:
    """Server-side ESTABLISHED TCP connections of a listening port."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP@127.0.0.1:{port}", "-sTCP:ESTABLISHED"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:  # noqa: BLE001
        return None
    return sum(1 for ln in out.splitlines()[1:] if f"127.0.0.1:{port}->" in ln)


def snap(pp: list[psutil.Process]) -> dict:
    s = {}
    for n, p in zip(names, pp, strict=True):
        t, cs = p.cpu_times(), p.num_ctx_switches()
        s[n] = {"user": t.user, "sys": t.system, "vcs": cs.voluntary,
                "ics": cs.involuntary, "threads": p.num_threads()}
        s[n]["ru"] = rusage(p.pid) or {}
    return s


def counters(pp: list[psutil.Process]) -> dict:
    for p in pp:
        os.kill(p.pid, signal.SIGUSR1)
    out = {}
    for n, p in zip(names, pp, strict=True):
        f = f"{a.work_dir}/p.{p.pid}.json"
        for _ in range(100):
            if os.path.exists(f):
                break
            time.sleep(0.1)
        time.sleep(0.1)
        out[n] = json.load(open(f))
        os.remove(f)
    return out


procs: list[subprocess.Popen[bytes]] = []
try:
    for s in topo:
        env = {**os.environ, **file_env, "OUTBOX_RELAY_ENABLED": "off",
               "SANDLOOM_ACCEPTED_RISKS": "relay_disabled", "PYTHONUNBUFFERED": "1", "PLANNER_OPTIMIZED_EXECUTOR": "true",
               "RPC_TRANSPORT": a.transport, **dict(kv.split("=", 1) for kv in a.env)}
        if a.probe or a.profile:
            env["PYTHONPATH"] = os.pathsep.join(
                [os.path.join(HERE, "probe_site"), file_env.get("PYTHONPATH", "")]
            )
            env["SANDLOOM_ANOM_PROBE"] = f"{a.work_dir}/p"
        for k, v in s["env"].items():
            for n, port in ports.items():
                v = v.replace(f"{{port:{n}}}", str(port))
            env[k] = v
        cmd = [PY, "-m", "uvicorn", "sandloom.main:app", "--host", "127.0.0.1",
               "--port", str(ports[s["name"]]), "--loop", "uvloop", "--http",
               "httptools", "--no-access-log", "--log-level", "warning",
               "--timeout-keep-alive", "75"]
        procs.append(subprocess.Popen(cmd, env=env, cwd=a.bench))
    for s in topo:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", ports[s["name"]]), 0.2):
                    break
            except OSError:
                time.sleep(0.1)
    pp = [psutil.Process(x.pid) for x in procs]
    prov = pp[names.index(a.provider)]
    eps = [("127.0.0.1", ports[n]) for n in names]
    import uvloop

    rec: dict = {"transport": a.transport, "mix": os.path.basename(a.mix),
                 "rate": a.rate, "seconds": a.seconds, "probe": a.probe,
                 "profile": a.profile, "warmup_conc": a.warmup_conc, "env": a.env,
                 **json.loads(a.meta)}
    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as r:
        w1 = r.run(lc.closed_loop(eps, wl, 0, 1000, a.warmup_conc, route=route, timeout=30))
        w2 = r.run(lc.open_loop(eps, wl, a.rate, 5, start=0, pool=128, route=route,
                                timeout=30))
        rec["conns_before"] = {n: established(p) for n, p in ports.items()}
        c0 = counters(pp) if a.probe else None
        if a.profile:
            os.kill(prov.pid, signal.SIGUSR2)
        g0 = r.run(pgstat())
        pg0 = sb._container_cpu_usec(sb.PG_CONTAINER)
        rd0 = sb._container_cpu_usec(sb.REDIS_CONTAINER)
        s0 = snap(pp)
        st = r.run(lc.open_loop(eps, wl, a.rate, a.seconds, start=10**7, pool=128,
                                route=route, timeout=30))
        s1 = snap(pp)
        pg1 = sb._container_cpu_usec(sb.PG_CONTAINER)
        rd1 = sb._container_cpu_usec(sb.REDIS_CONTAINER)
        g1 = r.run(pgstat())
        if a.profile:
            os.kill(prov.pid, signal.SIGUSR2)
            time.sleep(1.5)
            src = f"{a.work_dir}/p.{prov.pid}.prof"
            if os.path.exists(src):
                dst = a.out.rsplit(".", 1)[0] + f".{int(time.time())}.{a.transport}.prof"
                os.replace(src, dst)
                rec["prof"] = os.path.abspath(dst)
        c1 = counters(pp) if a.probe else None
        rec["conns_after"] = {n: established(p) for n, p in ports.items()}
    ok = max(st.ok, 1)
    rec.update(lc.open_result(st))
    rec.update(ok=st.ok, errors_total=w1.errors + w2.errors + st.errors)
    rec["per_op"] = {
        n: {k: (s1[n][k] - s0[n][k]) / ok * (1e6 if k in ("user", "sys") else 1)
            for k in ("user", "sys", "vcs", "ics")}
        for n in names
    }
    rec["threads"] = {n: s1[n]["threads"] for n in names}
    # macOS: instructions / cycles / P-core share / wakeups per op
    rec["rusage_per_op"] = {
        n: {k: (s1[n]["ru"][k] - s0[n]["ru"].get(k, 0)) / ok for k in s1[n]["ru"]}
        for n in names
    }
    rec["pg_cpu_us"] = (pg1 - pg0) / ok if pg0 and pg1 else None
    rec["redis_cpu_us"] = (rd1 - rd0) / ok if rd0 and rd1 else None
    rec["pg_per_op"] = {db: {k: (g1[db][k] - g0[db][k]) / ok for k in g0[db]}
                        for db in DBS}
    if c0 is not None and c1 is not None:
        rec["counters_per_op"] = {
            n: {k: round((c1[n]["c"].get(k, 0) - c0[n]["c"].get(k, 0)) / ok, 4)
                for k in sorted(set(c0[n]["c"]) | set(c1[n]["c"]))
                if c1[n]["c"].get(k, 0) != c0[n]["c"].get(k, 0)}
            for n in names
        }
        rec["request_heads"] = {n: c1[n]["heads"][:1] for n in names}
        rec["pools_before"] = {n: c0[n].get("pools") for n in names}
        rec["pools_after"] = {n: c1[n].get("pools") for n in names}
    with open(a.out, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(json.dumps({k: rec.get(k) for k in ("transport", "mix", "ok", "errors_total",
                                               "per_op", "conns_after", "pg_cpu_us")}),
          flush=True)
    sys.exit(1 if rec["errors_total"] else 0)
finally:
    for x in procs:
        x.terminate()
    for x in procs:
        try:
            x.wait(10)
        except subprocess.TimeoutExpired:
            x.kill()
