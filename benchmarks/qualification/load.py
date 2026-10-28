"""Authenticated wrk measurements against the strict Docker fixture (no faults)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import check
from run import ROOT, STATE, compose, token


@contextmanager
def application_cpu_limit(one_cpu):
    container = compose(
        "ps", "-q", "app", capture_output=True, text=True
    ).stdout.strip()
    if not container or "\n" in container:
        raise RuntimeError("expected exactly one running qualification app container")
    original = (
        int(
            subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.HostConfig.NanoCpus}}",
                    container,
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        / 1_000_000_000
    )
    if one_cpu:
        subprocess.run(
            ["docker", "update", "--cpus", "1", container],
            check=True,
            capture_output=True,
        )
    try:
        yield 1 if one_cpu else original
    finally:
        if one_cpu:
            subprocess.run(
                ["docker", "update", "--cpus", str(original), container],
                check=True,
                capture_output=True,
            )


@contextmanager
def optimized_planner(enabled):
    if enabled:
        os.environ["SANDLOOM_QUALIFICATION_PLANNER_OPTIMIZED"] = "true"
        os.environ["SANDLOOM_QUALIFICATION_READ_ROUTE"] = "/s5plan/"
    try:
        if enabled:
            compose(
                "up", "-d", "--no-deps", "--force-recreate", "app", capture_output=True
            )
        yield
    finally:
        if enabled:
            os.environ.pop("SANDLOOM_QUALIFICATION_PLANNER_OPTIMIZED", None)
            os.environ.pop("SANDLOOM_QUALIFICATION_READ_ROUTE", None)
            compose(
                "up", "-d", "--no-deps", "--force-recreate", "app", capture_output=True
            )
            check.wait_ready(token())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument(
        "--one-cpu",
        action="store_true",
        help="measure reads/writes at 32 connections with one application CPU",
    )
    parser.add_argument(
        "--planopt",
        action="store_true",
        help="measure only /s5plan reads using the opt-in prototype executor",
    )
    args = parser.parse_args()
    if args.seconds < 1:
        parser.error("--seconds must be positive")
    if shutil.which("wrk") is None:
        raise SystemExit("Install wrk first: brew install wrk")
    with optimized_planner(args.planopt):
        with application_cpu_limit(args.one_cpu) as cpu_limit:
            measure(args.seconds, args.one_cpu, cpu_limit, args.planopt)


def run_wrk(jwt, profile, connections, seconds, suffix=""):
    executable = shutil.which(os.environ.get("SANDLOOM_QUALIFICATION_WRK", "wrk"))
    if executable is None:
        raise RuntimeError("selected wrk executable was not found")
    executable = str(Path(executable).resolve())
    executable_hash = hashlib.sha256(Path(executable).read_bytes()).hexdigest()
    env = dict(
        os.environ,
        SANDLOOM_QUALIFICATION_TOKEN=jwt,
        SANDLOOM_QUALIFICATION_PROFILE=profile,
        SANDLOOM_QUALIFICATION_RUN=uuid.uuid4().hex[:12],
    )
    command = [
        executable,
        "-t",
        "4",
        "-c",
        str(connections),
        "-d",
        f"{seconds}s",
        "--timeout",
        "10s",
        "--latency",
        "-s",
        str(ROOT / "load.lua"),
        check.BASE,
    ]
    print(
        f"Running {profile}: connections={connections}, duration={seconds}s",
        flush=True,
    )
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, check=True
    )
    name = f"{profile}-c{connections}-{seconds}s{suffix}"
    STATE.joinpath(name + ".txt").write_text(result.stdout + result.stderr)
    if hashlib.sha256(Path(executable).read_bytes()).hexdigest() != executable_hash:
        raise RuntimeError(
            "wrk executable changed during the measurement; raw log retained"
        )
    line = next(
        line for line in result.stdout.splitlines() if line.startswith("QUALIFICATION ")
    )
    sample = json.loads(line.removeprefix("QUALIFICATION "))
    for line in result.stdout.splitlines():
        if line.startswith("EDGE_COLOS "):
            sample["edge_colos"] = json.loads(line.removeprefix("EDGE_COLOS "))
        elif line.startswith("EDGE_INGRESSES "):
            sample["edge_ingresses"] = json.loads(line.removeprefix("EDGE_INGRESSES "))
    sample.update(
        profile=profile,
        connections=connections,
        seconds=seconds,
        raw=name + ".txt",
        wrk_executable=executable,
        wrk_sha256=executable_hash,
    )
    return sample


def measure(seconds, one_cpu, cpu_limit, planopt=False):
    suffix = ("-1cpu" if one_cpu else "") + ("-planopt" if planopt else "")
    output = STATE / f"load{suffix}.json"
    output.unlink(missing_ok=True)
    jwt = token()
    check.wait_ready(jwt)
    read_route = os.environ.get("SANDLOOM_QUALIFICATION_READ_ROUTE", "/s5/")
    check.expect("pre-load isolation", read_route + "5001", jwt, 404)
    results = []
    plan = [("read", 8, 10)]  # warm-up; excluded from the reported measurements
    if one_cpu:
        plan += [("read", 32, seconds)]
        if not planopt:
            plan += [("write", 32, seconds)]
    else:
        plan += [("read", n, seconds) for n in (8, 32, 128)]
        if not planopt:
            plan += [("write", 32, seconds)]
    for profile, connections, seconds in plan:
        sample = run_wrk(jwt, profile, connections, seconds, suffix)
        results.append(sample)
        print(json.dumps(sample), flush=True)
    check.expect("post-load isolation", read_route + "5001", jwt, 404)
    check.wait_ready(jwt)
    # Give post-commit relays a bounded opportunity to drain before counting.
    import time

    for _ in range(60):
        if (
            check.sql("SELECT count(*) FROM outbox_messages WHERE delivered_at IS NULL")
            == "0"
        ):
            break
        time.sleep(1)
    else:
        raise AssertionError("post-load outbox backlog did not drain")
    totals = check.sql(
        "SELECT (SELECT count(*) FROM bench_tenant_writes),"
        "(SELECT count(*) FROM audit_log WHERE action='bench.write.created'),"
        "(SELECT count(*) FROM outbox_messages WHERE payload->>'name' LIKE 'q-%')"
    )
    assert len(set(totals.split("|"))) == 1, totals
    stats = compose("ps", "--format", "json", capture_output=True, text=True).stdout
    STATE.joinpath("containers.jsonl").write_text(stats)
    passed = all(
        not (s["http_errors"] or s["invalid_bodies"] or s["socket_errors"])
        for s in results
    )
    result = {
        "passed": passed,
        "application_cpu_limit": cpu_limit,
        "read_route": read_route,
        "planner_optimized_executor": planopt,
        "at": datetime.now(UTC).isoformat(),
        "host": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "cpus": os.cpu_count(),
        },
        "warmup": results[0],
        "samples": results[1:],
        "write_audit_outbox_counts": totals,
        "scope": "local Docker Desktop, diagnostic fixture, loopback HTTP, no TLS; no cross-host SLA",
    }
    output.write_text(json.dumps(result, indent=2) + "\n")
    if not passed:
        raise AssertionError(
            "Load produced errors; inspect .state/load.json and raw wrk output"
        )


if __name__ == "__main__":
    main()
