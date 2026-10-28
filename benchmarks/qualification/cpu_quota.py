"""Client HTTPS ABA comparison of app CPU quotas, preserving workers and pools."""

from __future__ import annotations

import argparse
import json
import os
import time

import check
from external import remote
from load import run_wrk
from run import STATE

APP = "sandloom-release-qualification-app-1"
DB = "sandloom-release-qualification-db-1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--ssh", default="oracle_f")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    if not args.url.startswith("https://") or args.seconds < 1:
        parser.error("HTTPS and positive duration required")
    check.BASE = args.url.rstrip("/")
    check.HTTP.addheaders = [("User-Agent", "wrk/4.2.0")]
    os.environ["SANDLOOM_QUALIFICATION_USER_AGENT"] = "wrk/4.2.0"
    STATE.mkdir(mode=0o700, exist_ok=True)

    def docker(*command):
        return remote(args.ssh, ["sudo", "-n", "docker", *command])

    original = int(docker("inspect", "--format", "{{.HostConfig.NanoCpus}}", APP)) / 1e9
    jwt = docker(
        "exec", APP, "python", "-c", "from benchsuite.s5 import mint;print(mint())"
    )
    config = json.loads(
        docker(
            "exec",
            APP,
            "python",
            "-c",
            "import json;from sandloom.core.config.settings import settings as s;"
            "print(json.dumps({k:getattr(s,k) for k in "
            "['db_pool_size','db_max_overflow','db_pool_timeout','contract_db_pool_size',"
            "'contract_db_max_overflow','db_pool_overflow_linger_seconds']}))",
        )
    )
    workers = docker("top", APP, "-eo", "pid,ppid,comm")
    maximum = docker(
        "exec",
        DB,
        "psql",
        "-U",
        "qualification_owner",
        "-d",
        "qualification",
        "-At",
        "-c",
        "SHOW max_connections",
    )
    report = {
        "passed": False,
        "config": config,
        "processes": workers,
        "postgres_max_connections": int(maximum),
        "samples": [],
    }
    output = STATE / "cpu-quota.json"

    def stats():
        return {
            k: int(v)
            for k, v in (
                line.split()
                for line in docker(
                    "exec", APP, "cat", "/sys/fs/cgroup/cpu.stat"
                ).splitlines()
            )
        }

    try:
        check.expect("quota test own tenant", "/s5/1", jwt, 200)
        check.expect("quota test foreign tenant", "/s5/5001", jwt, 404)
        for index, cpus in enumerate((2, 4, 2)):
            docker("update", "--cpus", str(cpus), APP)
            assert (
                int(docker("inspect", "--format", "{{.HostConfig.NanoCpus}}", APP))
                == cpus * 1e9
            )
            warm = run_wrk(jwt, "read", 128, 10, f"-quota-{index}-warm")
            before = stats()
            started = time.time()
            sample = run_wrk(jwt, "read", 128, args.seconds, f"-quota-{index}")
            after = stats()
            delta = {k: after[k] - before[k] for k in after}
            sample.update(
                cpus=cpus,
                cpu_delta=delta,
                warmup=warm,
                elapsed_seconds=time.time() - started,
                throttled_period_fraction=delta["nr_throttled"]
                / max(1, delta["nr_periods"]),
            )
            report["samples"].append(sample)
            output.write_text(json.dumps(report, indent=2) + "\n")
            print(
                json.dumps({k: v for k, v in sample.items() if k != "warmup"}),
                flush=True,
            )
        report["passed"] = all(
            not (s["http_errors"] or s["socket_errors"] or s["invalid_bodies"])
            for sample in report["samples"]
            for s in (sample, sample["warmup"])
        )
        check.expect("quota final isolation", "/s5/5001", jwt, 404)
    finally:
        docker("update", "--cpus", str(original), APP)
        report["restored_cpus"] = original
        output.write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"]:
        raise SystemExit("CPU-quota test recorded errors")


if __name__ == "__main__":
    main()
