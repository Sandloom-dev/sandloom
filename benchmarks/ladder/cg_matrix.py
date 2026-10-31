"""Instructions per request (cachegrind) for a matrix of builds × rungs.

    python ladder/cg_matrix.py <plan.json> <out.jsonl> [--only LANE,...]

Noise-insensitive replacement for the wall-clock A/Bs on a loaded host: every
job runs ``ladder/cg/cgdriver.py`` in the ``sandloom-ladder-cg`` image
(``Ir(K ops) − Ir(0 ops)`` / K, setup and teardown cancel out) with

* the measured BUILD's ``src/`` and ``examples/`` mounted read-only, and
* ONE harness ``benchmarks/`` (this checkout, or ``plan["harness"]``) for
  every build, so the rungs / workloads are byte-identical across builds.

plan.json::

    {"image": "sandloom-ladder-cg:3.13-rf", "harness": "<benchmarks dir>",
     "k": 200, "pg": "postgresql://user:pw@localhost:5433",
     "lanes": {"<lane>": {"build": "<export root>", "redis": [1, 2],
                          "reset": ["<db>", ...],
                          "env": {...}, "jobs": [{"name": ..., "rung": "MIX",
                                                  "env": {...}}, ...]}}}

Lanes run concurrently (one job at a time per lane, so a lane's databases
are never shared between running jobs); before EVERY job the lane's
databases are reset to the seeded state (``benchsuite.dataset.reset``) and
its Redis dbs flushed. ``DATABASE_URL`` / ``CACHE_URL`` / ``BROKER_URL`` may
use ``localhost`` (rewritten to ``host.docker.internal``). Result lines:
``{"lane", "job", "rung", "ir_per_op", "rpc_per_op"?, "commit", "env"}``.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from benchsuite import dataset  # noqa: E402

LOCK = threading.Lock()


def _fix(url: str) -> str:
    return url.replace("localhost", "host.docker.internal").replace(
        "127.0.0.1", "host.docker.internal"
    )


def _commit(root: str) -> str:
    p = os.path.join(root, "COMMIT")
    return open(p).read().strip() if os.path.exists(p) else "?"


def _job(plan: dict, lane: str, spec: dict, job: dict, out: str) -> None:
    build = job.get("build", spec["build"])
    harness = plan.get("harness") or os.path.dirname(HERE)
    env = {**spec.get("env", {}), **job.get("env", {})}
    tag = f"{lane}.{job['name']}".replace("/", "_")
    with tempfile.TemporaryDirectory() as td:
        cmd = [
            "docker",
            "run",
            "--rm",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{build}/src:/w/src:ro",
            "-v",
            f"{harness}:/w/benchmarks:ro",
            "-v",
            f"{build}/examples:/w/examples:ro",
            "-v",
            f"{td}:/out",
            "-e",
            "PYTHONPATH=/w/src:/w/benchmarks:/w/examples/helpdesk"
            ":/w/examples/projecthub:/w/examples/linkly",
            "-e",
            f"CG_K={plan.get('k', 200)}",
            "-e",
            f"LADDER_MIX_TAG={tag}",
        ]
        for k, v in env.items():
            cmd += ["-e", f"{k}={_fix(v) if k.endswith('_URL') else v}"]
        cmd += [
            plan["image"],
            "python",
            "/w/benchmarks/ladder/cg/cgdriver.py",
            job["rung"],
        ]
        # reset the lane's databases + flush its Redis dbs right before the job
        dsns = [f"{plan['pg']}/{d}" for d in spec.get("reset", [])]
        if dsns:
            asyncio.run(dataset.reset(dsns))
        dataset.flush_redis(
            plan.get("redis_container", "sandloom_redis_bench"), spec.get("redis", [])
        )
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True)
        m = re.search(r"^JSON (.*)$", p.stdout, re.M)
        res = json.loads(m.group(1)) if m else {}
        ir = res.get(job["rung"])
        rec = {
            "lane": lane,
            "job": job["name"],
            "rung": job["rung"],
            "ir_per_op": ir,
            "commit": _commit(build),
            "env": job.get("env", {}),
            "seconds": round(time.time() - t0),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        rpc = os.path.join(td, f"{tag}.rpc")
        if os.path.exists(rpc):
            vals = [float(x) for x in open(rpc).read().split()]
            if len(vals) == 2:
                rec["rpc_per_op"] = (vals[1] - vals[0]) / plan.get("k", 200)
        if ir is None:
            rec["error"] = (p.stdout + p.stderr)[-3000:]
    with LOCK:
        with open(out, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(
            f"[{rec['ts']}] {lane} {job['name']}: "
            f"{ir if ir is None else f'{ir:,.0f}'} instr/op"
            f"{' rpc/op=' + str(rec['rpc_per_op']) if 'rpc_per_op' in rec else ''}"
            f" ({rec['seconds']} s){' ERROR' if ir is None else ''}",
            flush=True,
        )


def _lane(plan: dict, lane: str, out: str, done: set) -> None:
    spec = plan["lanes"][lane]
    for job in spec["jobs"]:
        if (lane, job["name"]) in done:
            continue
        _job(plan, lane, spec, job, out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("out")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    plan = json.load(open(a.plan))
    lanes = [x for x in plan["lanes"] if not a.only or x in a.only.split(",")]
    done = set()
    if os.path.exists(a.out):  # resume: skip jobs with a result
        for line in open(a.out):
            r = json.loads(line)
            if r.get("ir_per_op") is not None:
                done.add((r["lane"], r["job"]))
    with cf.ThreadPoolExecutor(len(lanes)) as ex:
        for f in [ex.submit(_lane, plan, x, a.out, done) for x in lanes]:
            f.result()


if __name__ == "__main__":
    main()
