"""Integration-run driver: interleaves configurations per round.

    INT_SP=<stand root> python ladder/run_int.py \\
        matrix|login|scaling|topology|rpc --rounds 3 --out FILE [--start K]

Stand layout under ``$INT_SP``: ``b/<build>/`` = a ``git archive`` export of
each measured build with its own ``benchmarks/.venv`` (``uv sync --frozen``);
``int/<build>.env`` = its ``mix env-full`` (own database, own Redis db pair);
``int/topo_{mono,split}.json`` = topologies for ``split_bench``. The client
(this harness) runs from ITS OWN checkout, so harness changes never touch the
measured server code.

Before EVERY server start the run's databases are reset to the seeded state
(``benchsuite.dataset.reset``) and its Redis dbs flushed; the row counts of
every table are then recorded (``rows``) and compared with the FIRST snapshot
of that database in this output file (``dataset_baseline.json`` next to it): a mismatch,
a non-empty Redis db, a non-zero exit or any request error marks the record
``"invalid": true`` (with the reason) — invalid runs are kept, never dropped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from benchsuite import dataset, mix  # noqa: E402

SP = os.environ["INT_SP"]
B = f"{SP}/b"
INT = f"{SP}/int"
PG = os.environ.get("INT_PG", "postgresql://test_user:test_password@localhost:5433/")
REDIS = os.environ.get("INT_REDIS_CONTAINER", "sandloom_redis_bench")
PY = sys.executable
BUILDS = {  # build -> (db, redis dbs, env file)
    "pre38": ("bench_int_pre38_mixfull", (1, 2), f"{INT}/pre38.env"),
    "main38": ("bench_int_main38_mixfull", (3, 4), f"{INT}/main38.env"),
    "main": ("bench_int_main_mixfull", (5, 6), f"{INT}/main.env"),
}
CONFIGS = {  # config -> (build, PLANNER_OPTIMIZED_EXECUTOR)
    "pre38": ("pre38", "false"),
    "main38": ("main38", "false"),
    "planner_off": ("main", "false"),
    "planner_on": ("main", "true"),
}
SPLIT = ("identity", "tenancy", "billing", "apps")
SPLIT_DBS = [f"bench_int_split_{s}" for s in SPLIT]
SPLIT_REDIS = (7, 8)
#: measured profiles: the fixed three + login-excluded variants (the login
#: row dropped, the other weights unchanged) so the argon2 login cost is
#: MEASURED, not subtracted as a constant
PROFILES = [
    "mixed",
    "read-heavy",
    "write-heavy",
    "mixed-nologin",
    "write-heavy-nologin",
]
RATES = {
    **mix.PROFILE_RATES,
    "mixed-nologin": mix.PROFILE_RATES["mixed"],
    "write-heavy-nologin": mix.PROFILE_RATES["write-heavy"],
}
PROF = f"{INT}/prof"
#: --profiles: re-run only these profiles of the given rounds (a round whose
#: block was measured while a foreign workload ran is re-run as a whole block)
ONLY_PROFILES: list[str] = []


def write_profiles() -> None:
    os.makedirs(PROF, exist_ok=True)
    weights = dict(mix.PROFILES)
    weights["mixed-nologin"] = {
        k: w for k, w in mix.PROFILES["mixed"].items() if k != "login"
    }
    weights["write-heavy-nologin"] = {
        k: w for k, w in mix.PROFILES["write-heavy"].items() if k != "login"
    }
    weights["login-only"] = {"login": 1}
    for key in ("subscription", "credits", "tickets", "org"):
        weights[f"single_{key}"] = {key: 1}
    for name, ws in weights.items():
        entries = [
            {
                "method": mix.CATALOG[k][0],
                "path": mix.CATALOG[k][1],
                "body": mix.CATALOG[k][2],
                "expect": mix.CATALOG[k][3],
                "weight": w,
            }
            for k, w in ws.items()
        ]
        with open(f"{PROF}/{name}.json", "w") as fh:
            json.dump(entries, fh, indent=1)


def _token() -> str:
    return mix.mint(hours=24, tenant=True)


def _baseline_path(out: str) -> str:
    """One baseline per results directory, shared by every section."""
    return os.path.join(os.path.dirname(os.path.abspath(out)), "dataset_baseline.json")


def _prepare(dbs: list[str], redis_dbs: tuple[int, ...], out: str) -> dict:
    """Reset + flush, then verify the dataset equals its first snapshot."""
    dsns = [PG + d for d in dbs]
    asyncio.run(dataset.reset(dsns))
    sizes = dataset.flush_redis(REDIS, list(redis_dbs))
    rows = asyncio.run(dataset.row_counts(dsns))
    path = _baseline_path(out)
    base = json.load(open(path)) if os.path.exists(path) else {}
    new = {db: c for db, c in rows.items() if db not in base}
    if new:
        base.update(new)
        with open(path, "w") as fh:
            json.dump(base, fh, indent=1, sort_keys=True)
    diff = dataset.diff_counts({db: base[db] for db in rows}, rows)
    return {
        "rows": rows,
        "rows_diff": {db: {t: list(v) for t, v in d.items()} for db, d in diff.items()},
        "redis_dbsize": sizes,
    }


def _host() -> dict:
    ps = subprocess.run(
        ["ps", "-Ao", "pcpu,comm", "-r"], capture_output=True, text=True
    ).stdout.splitlines()[1:6]
    return {
        "load1": round(os.getloadavg()[0], 2),
        "top": [x.strip()[:70] for x in ps],
        "foreign_pytest": _foreign_pytest(),
    }


def _foreign_pytest() -> int:
    """pytest processes on the host (another agent's tests): a round measured
    while one ran is re-run."""
    args = subprocess.run(["ps", "-Ao", "args"], capture_output=True, text=True).stdout
    return sum(
        1 for line in args.splitlines() if "pytest" in line and "run_int" not in line
    )


def _wait_quiet(quiet_s: float = 20.0) -> float:
    """Block while a foreign pytest runs on the host (it loads the shared
    Postgres container too); returns the seconds waited."""
    t0 = time.time()
    calm_since = None
    while True:
        if _foreign_pytest():
            calm_since = None
        elif calm_since is None:
            calm_since = time.time()
        elif time.time() - calm_since >= quiet_s:
            return round(time.time() - t0, 1)
        time.sleep(2)


def _run(cmd: list[str], meta: dict, out: str, prep: dict) -> dict:
    waited = _wait_quiet()
    h0 = _host()
    h0["waited_for_quiet_s"] = waited
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(HERE))
    lines = [x for x in p.stdout.splitlines() if x.startswith("{")]
    rec: dict = json.loads(lines[-1]) if lines else {}
    reasons = []
    if p.returncode != 0:
        reasons.append(f"exit {p.returncode}")
    if not lines:
        reasons.append("no result")
    if rec.get("errors_total"):
        reasons.append(f"{rec['errors_total']} request errors")
    if prep["rows_diff"]:
        reasons.append("dataset differs from its first snapshot")
    if any(prep["redis_dbsize"].values()):
        reasons.append("redis not empty")
    if reasons:
        rec["invalid"] = True
        rec["invalid_reasons"] = reasons
        rec["stderr"] = p.stderr[-3000:]
    rec.update(meta)
    rec.update(
        rows_diff=prep["rows_diff"],
        rows_total={db: sum(c.values()) for db, c in prep["rows"].items()},
        redis_dbsize=prep["redis_dbsize"],
        host_before=h0,
        load1_after=round(os.getloadavg()[0], 2),
        foreign_pytest_after=_foreign_pytest(),
        started=time.strftime("%H:%M:%S", time.localtime(t0)),
        duration_s=round(time.time() - t0, 1),
    )
    with open(out, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    tag = " ".join(f"{k}={meta[k]}" for k in meta)
    print(
        f"[{rec['started']}] {tag} -> cpu={rec.get('server_cpu_us')} pg={rec.get('pg_cpu_us')}"
        f" rps={rec.get('rps')} p50={rec.get('p50_ms')} p99={rec.get('p99_ms')}"
        f" errors={rec.get('errors_total')} invalid={reasons or False} load={h0['load1']}",
        flush=True,
    )
    return rec


def _sb(
    build: str, flag: str, profile: str, mode: str, label: str, extra=None
) -> list[str]:
    _db, _r, envf = BUILDS[build]
    cmd = [
        PY,
        "ladder/server_bench.py",
        "--target",
        "sandloom.main:app",
        "--env-file",
        envf,
        "--env",
        f"PLANNER_OPTIMIZED_EXECUTOR={flag}",
        "--mix",
        f"{PROF}/{profile}.json",
        "--no-id",
        "--path",
        "/",
        "--header",
        "Authorization: Bearer " + _token(),
        "--cwd",
        f"{B}/{build}/benchmarks",
        "--python",
        f"{B}/{build}/benchmarks/.venv/bin/python",
        "--label",
        label,
    ]
    if mode == "closed":
        cmd += ["--conc", "64", "--n", str(8000 // 64 * 64), "--reps", "3"]
    elif mode == "login4":
        cmd += ["--conc", "4", "--n", "240", "--reps", "3", "--warmup", "40"]
    elif mode == "login1":
        cmd += ["--conc", "1", "--n", "120", "--reps", "3", "--warmup", "10"]
    else:
        cmd += ["--rate", str(RATES[profile]), "--seconds", "30"]
    return cmd + (extra or [])


def _order(names: list, rnd: int) -> list:
    k = rnd % len(names)
    order = names[k:] + names[:k]
    return order[::-1] if rnd % 2 else order


def matrix(rounds: list[int], out: str) -> None:
    for rnd in rounds:
        order = _order(list(CONFIGS), rnd)
        for profile in (
            PROFILES[rnd % len(PROFILES) :] + PROFILES[: rnd % len(PROFILES)]
        ):
            if ONLY_PROFILES and profile not in ONLY_PROFILES:
                continue
            for slot, config in enumerate(order):
                build, flag = CONFIGS[config]
                db, rds, _e = BUILDS[build]
                for mode in ("closed", "open"):
                    prep = _prepare([db], rds, out)
                    _run(
                        _sb(build, flag, profile, mode, f"{profile}|{config}|{mode}"),
                        {
                            "section": "matrix",
                            "round": rnd,
                            "profile": profile,
                            "config": config,
                            "mode": mode,
                            "slot": slot,
                        },
                        out,
                        prep,
                    )


def login(rounds: list[int], out: str) -> None:
    """POST /auth/login only, closed loop at conc 1 and conc 4: the measured
    cost of one login (argon2 verify + route + token issue) per config."""
    for rnd in rounds:
        for slot, config in enumerate(_order(list(CONFIGS), rnd)):
            build, flag = CONFIGS[config]
            db, rds, _e = BUILDS[build]
            for mode in ("login1", "login4") if rnd % 2 == 0 else ("login4", "login1"):
                prep = _prepare([db], rds, out)
                _run(
                    _sb(build, flag, "login-only", mode, f"login-only|{config}|{mode}"),
                    {
                        "section": "login",
                        "round": rnd,
                        "profile": "login-only",
                        "config": config,
                        "mode": mode,
                        "slot": slot,
                    },
                    out,
                    prep,
                )


def scaling(rounds: list[int], out: str) -> None:
    pool = [
        "--env",
        "DB_POOL_SIZE=10",
        "--env",
        "DB_MAX_OVERFLOW=5",
        "--env",
        "CONTRACT_DB_POOL_SIZE=2",
        "--env",
        "CONTRACT_DB_MAX_OVERFLOW=2",
    ]
    for rnd in rounds:
        for w in [1, 2, 4] if rnd % 2 == 0 else [4, 2, 1]:
            for mode in ("closed", "open"):
                db, rds, _e = BUILDS["main"]
                prep = _prepare([db], rds, out)
                _run(
                    _sb(
                        "main",
                        "true",
                        "mixed",
                        mode,
                        f"mixed|planner_on|w{w}|{mode}",
                        extra=pool + ["--workers", str(w)],
                    ),
                    {
                        "section": "scaling",
                        "round": rnd,
                        "workers": w,
                        "mode": mode,
                        "pool": "10+5, contract 2+2 per worker",
                    },
                    out,
                    prep,
                )


def _split_cmd(
    topo: str, flag: str, profile: str, mode: str, label: str, n: int = 8000
) -> list[str]:
    cmd = [
        PY,
        "ladder/split_bench.py",
        "--topology",
        f"{INT}/topo_{topo}.json",
        "--mix",
        f"{PROF}/{profile}.json",
        "--env-file",
        BUILDS["main"][2],
        "--env",
        f"PLANNER_OPTIMIZED_EXECUTOR={flag}",
        "--header",
        "Authorization: Bearer " + _token(),
        "--python",
        f"{B}/main/benchmarks/.venv/bin/python",
        "--cwd",
        f"{B}/main/benchmarks",
        "--label",
        label,
    ]
    if mode == "closed":
        cmd += ["--conc", "64", "--n", str(n // 64 * 64), "--reps", "3"]
    else:
        cmd += ["--rate", str(RATES[profile]), "--seconds", "30"]
    return cmd


def _topo_prepare(topo: str, out: str) -> dict:
    if topo == "mono":
        return _prepare([BUILDS["main"][0]], BUILDS["main"][1], out)
    return _prepare(SPLIT_DBS, SPLIT_REDIS, out)


CELLS = [("mono", "false"), ("mono", "true"), ("split", "false"), ("split", "true")]


def topology(rounds: list[int], out: str) -> None:
    for rnd in rounds:
        for slot, (topo, flag) in enumerate(_order(CELLS, rnd)):
            cfg = "planner_on" if flag == "true" else "planner_off"
            for mode in ("closed", "open"):
                prep = _topo_prepare(topo, out)
                _run(
                    _split_cmd(topo, flag, "mixed", mode, f"mixed|{topo}|{cfg}|{mode}"),
                    {
                        "section": "topology",
                        "round": rnd,
                        "topology": topo,
                        "config": cfg,
                        "mode": mode,
                        "slot": slot,
                    },
                    out,
                    prep,
                )


def rpc(rounds: list[int], out: str) -> None:
    """Single requests that cross tenancy.membership.v1: billing + helpdesk."""
    for rnd in rounds:
        for key in ("subscription", "credits", "tickets", "org"):
            for topo, flag in CELLS if rnd % 2 == 0 else CELLS[::-1]:
                cfg = "planner_on" if flag == "true" else "planner_off"
                prep = _topo_prepare(topo, out)
                _run(
                    _split_cmd(
                        topo,
                        flag,
                        f"single_{key}",
                        "closed",
                        f"{key}|{topo}|{cfg}",
                        n=4000,
                    ),
                    {
                        "section": "rpc",
                        "round": rnd,
                        "request": key,
                        "topology": topo,
                        "config": cfg,
                    },
                    out,
                    prep,
                )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("section")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--out", required=True)
    p.add_argument("--profiles", default="", help="matrix: comma-separated subset")
    a = p.parse_args()
    write_profiles()
    ONLY_PROFILES.extend(x for x in a.profiles.split(",") if x)
    rr = list(range(a.start, a.start + a.rounds))
    {
        "matrix": matrix,
        "login": login,
        "scaling": scaling,
        "topology": topology,
        "rpc": rpc,
    }[a.section](rr, a.out)
