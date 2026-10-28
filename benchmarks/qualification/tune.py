"""Compare Docker settings with the historical single-worker read benchmark.

Only the isolated qualification app is recreated. Security and infrastructure
settings stay intact; the default app configuration is restored in finally.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
from datetime import UTC, datetime

import check
from load import run_wrk
from run import STATE, compose, token

GUNICORN = [
    "gunicorn",
    "-c",
    "python:sandloom.core.cli.gunicorn_conf",
    "benchsuite.apps.sandloom_app:app",
]
UVICORN = [
    "uvicorn",
    "benchsuite.apps.sandloom_app:app",
    "--host",
    "0.0.0.0",
    "--port",
    "2222",
    "--loop",
    "uvloop",
    "--http",
    "httptools",
    "--workers",
    "1",
    "--no-access-log",
    "--log-level",
    "warning",
]


def profiles():
    base = {
        "workers": 2,
        "cpus": 1,
        "connections": 32,
        "pool": 5,
        "overflow": 5,
        "command": GUNICORN,
        "planopt": False,
    }
    one = dict(base, workers=1)
    unlimited = dict(one, cpus=0)
    c64 = dict(unlimited, connections=64)
    pool = dict(c64, pool=10, overflow=20)
    quiet = dict(
        pool,
        command=[
            "gunicorn",
            "-c",
            "/benchmarks/qualification/gunicorn_noaccess.py",
            "benchsuite.apps.sandloom_app:app",
        ],
    )
    uvicorn = dict(quiet, command=UVICORN)
    return {
        "baseline": base,
        "one-worker": one,
        "no-quota": unlimited,
        "c64": c64,
        "pool30": pool,
        "no-access-log": quiet,
        "uvicorn": uvicorn,
        "baseline-planner": dict(base, planopt=True),
        "uvicorn-planner": dict(uvicorn, planopt=True),
    }


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def configure(profile, override):
    override.write_text(
        json.dumps(
            {
                "services": {
                    "app": {
                        "cpus": profile["cpus"],
                        "command": profile["command"],
                        "environment": {
                            "WEB_CONCURRENCY": str(profile["workers"]),
                            "DB_POOL_SIZE": str(profile["pool"]),
                            "DB_MAX_OVERFLOW": str(profile["overflow"]),
                            "PLANNER_OPTIMIZED_EXECUTOR": str(
                                profile["planopt"]
                            ).lower(),
                        },
                    }
                }
            }
        )
    )
    compose(
        "-f",
        str(override),
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "app",
        capture_output=True,
        text=True,
    )
    container = compose(
        "ps", "-q", "app", capture_output=True, text=True
    ).stdout.strip()
    info = json.loads(docker("inspect", container))[0]
    expected = profile["cpus"] * 1_000_000_000
    assert info["HostConfig"]["NanoCpus"] == expected, "CPU quota mismatch"
    try:
        cpu_max = docker("exec", container, "cat", "/sys/fs/cgroup/cpu.max")
    except subprocess.CalledProcessError:
        logs = subprocess.run(
            ["docker", "logs", "--tail", "20", container],
            capture_output=True,
            text=True,
        )
        print(logs.stdout + logs.stderr, flush=True)
        raise
    assert (cpu_max.split()[0] == "max") == (profile["cpus"] == 0), cpu_max
    return container, info["Image"], cpu_max


def cpu_stats(container):
    return {
        k: int(v)
        for k, v in (
            line.split()
            for line in docker(
                "exec", container, "cat", "/sys/fs/cgroup/cpu.stat"
            ).splitlines()
        )
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--profiles", default=",".join(profiles()))
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="collect later profiles after load errors; still exit nonzero if any fail",
    )
    args = parser.parse_args()
    if min(args.seconds, args.warmup, args.reps) < 1:
        parser.error("durations and repetitions must be positive")
    selected = args.profiles.split(",")
    if any(name not in profiles() for name in selected):
        parser.error("unknown profile")
    if not shutil.which("wrk"):
        parser.error("wrk is required")
    STATE.mkdir(mode=0o700, exist_ok=True)
    output = STATE / "tuning.json"
    override = STATE / "tuning.compose.json"
    output.unlink(missing_ok=True)
    report = {
        "at": datetime.now(UTC).isoformat(),
        "passed": False,
        "samples": [],
        "settings": profiles(),
        "scope": "Docker, same strict security; reads only",
    }
    try:
        for rep in range(args.reps):
            order = selected if rep % 2 == 0 else list(reversed(selected))
            for name in order:
                profile = profiles()[name]
                container, image, cpu_max = configure(profile, override)
                jwt = token()
                check.wait_ready(jwt)
                route = "/s5plan/" if profile["planopt"] else "/s5/"
                os.environ["SANDLOOM_QUALIFICATION_READ_ROUTE"] = route
                check.expect("own tenant", route + "1", jwt, 200)
                check.expect("foreign tenant", route + "5001", jwt, 404)
                check.expect("no JWT", route + "1", None, 401)
                suffix = f"-tuning-{name}-r{rep + 1}"
                warmup = run_wrk(
                    jwt, "read", profile["connections"], args.warmup, suffix + "-warmup"
                )
                before = cpu_stats(container)
                sample = run_wrk(
                    jwt, "read", profile["connections"], args.seconds, suffix
                )
                after = cpu_stats(container)
                delta = {k: after[k] - before[k] for k in after}
                sample.update(
                    name=name,
                    rep=rep + 1,
                    image=image,
                    cpu_max=cpu_max,
                    cpu_delta=delta,
                    cpu_us_per_good_request=delta["usage_usec"] / sample["ok"],
                    host_load=os.getloadavg(),
                    warmup=warmup,
                )
                report["samples"].append(sample)
                output.write_text(json.dumps(report, indent=2) + "\n")
                print(
                    json.dumps({k: v for k, v in sample.items() if k != "warmup"}),
                    flush=True,
                )
                sample["passed"] = all(
                    not (
                        window["http_errors"]
                        or window["invalid_bodies"]
                        or window["socket_errors"]
                    )
                    for window in (warmup, sample)
                )
                if not sample["passed"] and not args.keep_going:
                    raise AssertionError("load errors")
                check.expect("post-load foreign tenant", route + "5001", jwt, 404)
        report["medians"] = {
            name: statistics.median(
                s["goodput_rps"] for s in report["samples"] if s["name"] == name
            )
            for name in selected
        }
        report["passed"] = all(s["passed"] for s in report["samples"])
    finally:
        os.environ.pop("SANDLOOM_QUALIFICATION_READ_ROUTE", None)
        compose(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "app",
            capture_output=True,
            text=True,
        )
        check.wait_ready(token())
        report["default_app_restored"] = True
        output.write_text(json.dumps(report, indent=2) + "\n")
        override.unlink(missing_ok=True)
    print(json.dumps(report["medians"]), flush=True)
    if not report["passed"]:
        raise SystemExit(
            "Load errors recorded; failing samples are not capacity results"
        )


if __name__ == "__main__":
    main()
