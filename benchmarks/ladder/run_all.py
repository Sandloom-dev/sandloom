"""Run the whole ladder; every measurement in a fresh process.

    cd benchmarks
    export DATABASE_URL=... CACHE_URL=... BROKER_URL=...
    uv run python -m benchsuite.setup_db          # once
    uv run python ladder/run_all.py rt alloc calls memray   # deterministic
    ladder/cg/run_cg.sh $(uv run python ladder/run_all.py rungs)  # instructions
    uv run python ladder/run_all.py cpu cpu64 cpuuv socket       # timing

Results append to ``ladder/results/<phase>.jsonl``; ``render.py`` builds the
tables from them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "results"
LADDER = str(HERE / "ladder.py")
SERVER = str(HERE / "server_bench.py")

RUNGS = [
    "L0",
    "L0r",
    "L0t",
    "L0s",
    "L0s1",
    "L1",
    "L2",
    "L2c",
    "L2nc",
    "L2t",
    "L2a",
    "L2A",
    "L2E",
    "L3",
    "L3s",
    "L3p",
    "L3c",
    "L3t",
    "L3g",
    "L4",
    "L4t",
    "L4v",
    "L4c",
    "L4ta",
    "L4o",
    "L5e",
    "L5",
    "L6r",
    "L6",
    "L6R",
    "L6R0",
    "L6fr",
    "L6fa",
    "L7u",
    "L7a",
    "L7A",
    "L7p",
]
#: L6 minus one middleware each (instructions + allocations only).
MW_RUNGS = [
    f"L6-{m}"
    for m in (
        "RpcFastPathMiddleware",
        "ContractReadScopeMiddleware",
        "CORSMiddleware",
        "Mask5xxMiddleware",
        "RequestIDMiddleware",
        "SecurityHeadersMiddleware",
        "IdempotencyCaptureMiddleware",
        "ProcessTimeMiddleware",
        "BodySizeLimitMiddleware",
        "RouteErrorMiddleware",
    )
]
CONC_RUNGS = [
    "L0",
    "L2A",
    "L3",
    "L4",
    "L5",
    "L6",
    "L6R",
    "L6fa",
    "L7u",
    "L7a",
    "L7A",
    "L7p",
]
UV_RUNGS = ["L0", "L3", "L5", "L6", "L6R", "L6fa", "L7A"]

SOCKET = [
    # (app, path, loop, http, conc)
    *[
        (app, path, "uvloop", "httptools", c)
        for c in (1, 64)
        for app, path in (
            ("sandloom", "/s3/"),
            ("fastapi", "/s3/"),
            ("fast", "/fast/uow/"),
            ("fast", "/fast/core/"),
            ("fast", "/fast/core2/"),
            ("fast", "/fast/pg/"),
        )
    ],
    ("sandloom", "/s3/", "asyncio", "httptools", 64),
    ("sandloom", "/s3/", "asyncio", "h11", 64),
    ("fastapi", "/s3/", "asyncio", "h11", 64),
]


CHAIN = [
    "L0",
    "L0t",
    "L2t",
    "L3t",
    "L4t",
    "L5",
    "L6",
    "L6R",
    "L6fa",
    "L7A",
]
PROF_RUNGS = ["L0", "L0t", "L5", "L6", "L6R", "L6r", "L6fa", "L6fr"]

LATENCY_TARGETS = [
    ("sandloom", "/s3/", []),  # L6
    ("fast", "/ro/", ["--env", "DB_READ_ONLY_SESSIONS=true"]),  # L6R (declared RO)
    ("fast", "/fast/core2/", []),  # L7 (Variant A)
]


def _run(cmd: list[str], phase: str, extra: dict | None = None) -> None:
    print("::", " ".join(cmd[2:]), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE.parent)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    if r.returncode != 0 or not lines:
        print(r.stderr[-2000:], file=sys.stderr)
        return
    if phase != "capacity":
        lines = lines[-1:]
    OUT.mkdir(exist_ok=True)
    for line in lines:
        rec = json.loads(line)
        rec.update(extra or {})
        with (OUT / f"{phase}.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
    print("   ", {k: rec[k] for k in list(rec)[:14] if k not in ("desc",)}, flush=True)


def main() -> None:
    global RUNGS, CONC_RUNGS, UV_RUNGS, CHAIN
    py = sys.executable
    only = os.environ.get("LADDER_ONLY")  # e.g. "L2A,L7A": re-run a subset
    if only:
        keep = set(only.split(","))
        RUNGS = [r for r in RUNGS if r in keep]
        CONC_RUNGS = [r for r in CONC_RUNGS if r in keep]
        UV_RUNGS = [r for r in UV_RUNGS if r in keep]
        CHAIN = [r for r in CHAIN if r in keep]
    for phase in sys.argv[1:]:
        if phase == "rungs":
            print(" ".join(RUNGS))
        elif phase == "mwrungs":
            print(" ".join(MW_RUNGS))
        elif phase == "mwmemray":
            for r in MW_RUNGS:
                _run([py, LADDER, "memray", r, "--n", "1000", "--warmup", "500"], phase)
        elif phase == "rt":
            for r in RUNGS:
                _run([py, LADDER, "rt", r, "--n", "200", "--warmup", "50"], phase)
        elif phase == "alloc":
            for r in RUNGS:
                _run([py, LADDER, "alloc", r, "--n", "2000", "--warmup", "500"], phase)
        elif phase == "calls":
            for r in RUNGS:
                _run([py, LADDER, "calls", r, "--n", "1000", "--warmup", "500"], phase)
        elif phase == "memray":
            for r in RUNGS:
                _run([py, LADDER, "memray", r, "--n", "1000", "--warmup", "500"], phase)
        elif phase == "cpu":
            for r in RUNGS:
                _run([py, LADDER, "cpu", r, "--n", "5000", "--reps", "5"], phase)
        elif phase == "cpu64":
            for r in CONC_RUNGS:
                _run(
                    [
                        py,
                        LADDER,
                        "cpu",
                        r,
                        "--n",
                        "12800",
                        "--reps",
                        "5",
                        "--conc",
                        "64",
                        "--warmup",
                        "1280",
                    ],
                    phase,
                )
        elif phase == "cpuuv":
            for r in UV_RUNGS:
                _run(
                    [
                        py,
                        LADDER,
                        "cpu",
                        r,
                        "--n",
                        "5000",
                        "--reps",
                        "5",
                        "--loop",
                        "uvloop",
                    ],
                    phase,
                )
        elif phase == "socket":
            for app, path, loop, http, conc in SOCKET:
                n = 5000 if conc == 1 else 12800
                _run(
                    [
                        py,
                        SERVER,
                        "--app",
                        app,
                        "--path",
                        path,
                        "--loop",
                        loop,
                        "--http",
                        http,
                        "--conc",
                        str(conc),
                        "--n",
                        str(n),
                        "--reps",
                        "5",
                    ],
                    phase,
                )
            _run(
                [
                    py,
                    SERVER,
                    "--app",
                    "sandloom",
                    "--conc",
                    "64",
                    "--n",
                    "12800",
                    "--reps",
                    "5",
                    "--relay",
                    "on",
                ],
                phase,
            )
        elif phase in ("chain", "chain64", "chainuv", "chainuv64"):
            # Interleaved rounds over the main chain (drift hits every rung
            # alike); user/sys CPU split recorded per run.
            conc = "64" if phase.endswith("64") else "1"
            loop = "uvloop" if "uv" in phase else "asyncio"
            n = "12800" if conc == "64" else "4000"
            for rnd in range(int(os.environ.get("LADDER_ROUNDS", "3"))):
                for r in CHAIN:
                    _run(
                        [
                            py,
                            LADDER,
                            "cpu",
                            r,
                            "--n",
                            n,
                            "--reps",
                            "3",
                            "--conc",
                            conc,
                            "--loop",
                            loop,
                            "--warmup",
                            "1280" if conc == "64" else "500",
                        ],
                        phase,
                        {"round": rnd},
                    )
        elif phase == "prof":
            for r in PROF_RUNGS:
                _run([py, LADDER, "prof", r, "--n", "3000", "--warmup", "500"], phase)
        elif phase == "socket2":
            for conc in (1, 64):
                n = "5000" if conc == 1 else "12800"
                for app, path, env in LATENCY_TARGETS:
                    _run(
                        [
                            py,
                            SERVER,
                            "--app",
                            app,
                            "--path",
                            path,
                            "--conc",
                            str(conc),
                            "--n",
                            n,
                            "--reps",
                            "5",
                            *env,
                        ],
                        phase,
                    )
        elif phase == "socket3":
            # fast paths re-run with the SANDLOOM-shaped lingering pool, conc 64
            for app, path in (
                ("fast", "/fast/pg/"),
                ("fast", "/fast/core2/"),
                ("sandloom", "/s3/"),
            ):
                _run(
                    [
                        py,
                        SERVER,
                        "--app",
                        app,
                        "--path",
                        path,
                        "--conc",
                        "64",
                        "--n",
                        "12800",
                        "--reps",
                        "5",
                    ],
                    phase,
                )
        elif phase == "capacity":
            # (1) fixed rates, Sandloom current vs declared read-only,
            # app vs PostgreSQL CPU side by side; (2) worker scaling ramp.
            for app, path, env in LATENCY_TARGETS[:2]:
                _run(
                    [
                        py,
                        SERVER,
                        "--app",
                        app,
                        "--path",
                        path,
                        "--capacity",
                        "980,1570,1860",
                        "--seconds",
                        "30",
                        *env,
                    ],
                    phase,
                )
            ramps = {1: "1500,2000,2500", 2: "2500,3500,4500", 4: "4000,5500,7000,8500"}
            for workers, ramp in ramps.items():
                _run(
                    [
                        py,
                        SERVER,
                        "--app",
                        "sandloom",
                        "--workers",
                        str(workers),
                        "--capacity",
                        ramp,
                        "--seconds",
                        "20",
                    ],
                    phase,
                )
        elif phase == "latency":
            # Open loop at fixed rates (default: 50% / 80% / 95% of L6's max).
            rates = os.environ.get("LADDER_RATES", "980,1570,1860").split(",")
            for rate in rates:
                for app, path, env in LATENCY_TARGETS:
                    _run(
                        [
                            py,
                            SERVER,
                            "--app",
                            app,
                            "--path",
                            path,
                            "--rate",
                            rate,
                            "--seconds",
                            "30",
                            *env,
                        ],
                        phase,
                    )
        else:
            raise SystemExit(f"unknown phase {phase}")


if __name__ == "__main__":
    main()
