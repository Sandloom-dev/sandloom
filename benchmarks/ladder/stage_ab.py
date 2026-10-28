"""Interleaved A/B(/C…) over a real socket: the same variants on several checkouts.

    STAGES=base=/path/a,c1=/path/b,c12=/path/c \\
        uv run python ladder/stage_ab.py socket [--rounds 3] [--variants uow,ro,fast,fn,s3]
    STAGES=... uv run python ladder/stage_ab.py latency --rates 530,850 [--variants uow,s3]

Each path is a checkout ROOT (it must contain ``benchmarks/.venv``). For every
round, every variant, every stage (in that nested order — stages alternate
back to back, so host drift hits all of them alike) it starts that checkout's
Sandloom app on uvicorn (uvloop + httptools, 1 worker, the default
``LingeringQueuePool``) with ``server_bench.py`` and measures server CPU µs per
request (closed loop, conc 64), or p50/p99 at fixed open-loop rates.

Variants: ``uow`` / ``ro`` / ``fast`` / ``fn`` = S5 (``benchsuite.s5 env``:
tenant_rls on, NON-superuser role, a tenant-A token); ``s3`` = the plain S3
read on the owner DSN (no JWT / tenant / RLS) as the control.
Results append to ``ladder/results/stage_<mode>.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
S5_ROUTES = {
    "uow": "/s5/",
    "ro": "/s5ro/",
    "fast": "/s5fast/",
    "fn": "/s5fn/",
    "plan": "/s5plan/",  # execution planner, general (ORM) executor
    "planopt": "/s5plan/",  # same route, PLANNER_OPTIMIZED_EXECUTOR=true
    "uow2": "/s5two/",  # TWO reads on the request UoW
    "plan2": "/s5plan2/",  # TWO declared reads, general executor (one read unit)
    "planopt2": "/s5plan2/",  # TWO declared reads, optimized executor
    # cost-attribution stages (DIAGNOSTIC ONLY, benchsuite.bench.api.diag_s5)
    "d0": "/d5/0/",
    "d1": "/d5/1/",
    "d2": "/d5/2/",
    "d3": "/d5/2/",
    "d4": "/d5/2/",
    "d5": "/d5/5/",
    "d6": "/s5/",
}
#: Requests per rep (3 reps) ≈ 20–30 s per run at the expected rps.
N_PER_REP = {
    "uow": 8000,
    "ro": 8000,
    "fast": 12000,
    "fn": 16000,
    "plan": 8000,
    "planopt": 12000,
    "uow2": 7000,
    "plan2": 7000,
    "planopt2": 10000,
    "s3": 16000,
    "d0": 14000,
    "d1": 12000,
    "d2": 12000,
    "d3": 10000,
    "d4": 9000,
    "d5": 9000,
    "d6": 9000,
}


def _stages() -> list[tuple[str, Path, list[str]]]:
    """``label=path[@KEY=VAL[@KEY=VAL…]]`` items: extra server env per stage."""
    out = []
    for item in os.environ["STAGES"].split(","):
        label, rest = item.split("=", 1)
        path, *env = rest.split("@")
        out.append((label, Path(path).resolve(), env))
    return out


def _s5(bench: Path, *args: str) -> str:
    return subprocess.run(
        [str(bench / ".venv/bin/python"), "-m", "benchsuite.s5", *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=bench,
    ).stdout


def run_one(
    label: str, root: Path, variant: str, extra: list[str], out: str, env: list[str]
) -> None:
    bench = root / "benchmarks"
    cmd = [
        sys.executable,
        str(HERE / "server_bench.py"),
        "--target",
        "benchsuite.apps.sandloom_app:app",
        "--cwd",
        str(bench),
        "--python",
        str(bench / ".venv/bin/python"),
    ]
    if variant == "s3":
        cmd += ["--path", "/s3/"]
    else:
        env_file = Path(tempfile.gettempdir()) / f"stage-{label}-{variant}.env"
        env_file.write_text(_s5(bench, "env", variant))
        token = _s5(bench, "token").strip()
        cmd += [
            "--env-file",
            str(env_file),
            "--header",
            f"Authorization: Bearer {token}",
            "--ids",
            "5000",
            "--path",
            S5_ROUTES[variant],
        ]
    for kv in env:
        cmd += ["--env", kv]
    r = subprocess.run([*cmd, *extra], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("{"):
            rec = {"stage": label, "variant": variant, **json.loads(line)}
            with (HERE / "results" / out).open("a") as f:
                f.write(json.dumps(rec) + "\n")
            keys = ("server_cpu_us", "rps", "rate", "achieved_rps", "p50_ms", "p99_ms")
            print(label, variant, {k: rec[k] for k in keys if k in rec}, flush=True)
    if r.returncode:
        print(label, variant, "FAILED", r.stderr[-2000:], file=sys.stderr, flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["socket", "latency"])
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--variants", default="uow,ro,fast,fn,s3")
    p.add_argument("--rates", default="")
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--scale", type=float, default=1.0, help="x N_PER_REP (smoke)")
    a = p.parse_args()
    stages = _stages()
    variants = a.variants.split(",")
    for rnd in range(a.rounds):
        # alternate the variant order too: drift hits every variant alike
        for variant in variants if rnd % 2 == 0 else variants[::-1]:
            order = stages if rnd % 2 == 0 else stages[::-1]  # alternate order too
            for label, root, env in order:
                if a.mode == "socket":
                    extra = ["--conc", "64", "--n", str(int(N_PER_REP[variant] * a.scale) // 64 * 64), "--reps", "3"]
                    run_one(label, root, variant, extra, "stage_socket.jsonl", env)
                else:
                    extra = ["--capacity", a.rates, "--seconds", str(a.seconds)]
                    run_one(label, root, variant, extra, "stage_latency.jsonl", env)


if __name__ == "__main__":
    main()
