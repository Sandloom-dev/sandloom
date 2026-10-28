"""Collect an on-CPU Python profile of the isolated ordinary ORM read path.

Linux host only: run as an operator permitted to ptrace this stack's worker.
Install py-spy separately; never add the profiler to the production image.
"""

from __future__ import annotations

import argparse
import json
import subprocess

import check
from load import run_wrk
from run import STATE, compose, token
from tune import configure, docker, profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiler", required=True)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--reps", type=int, default=3)
    args = parser.parse_args()
    override = STATE / "profile.compose.json"
    report = {"passed": False, "samples": [], "profiled": None}
    try:
        container, image, cpu_max = configure(profiles()["no-quota"], override)
        jwt = token()
        check.wait_ready(jwt)
        processes = docker("top", container, "-eo", "pid,ppid,comm").splitlines()[1:]
        master = int(processes[0].split()[0])
        workers = [int(p.split()[0]) for p in processes if int(p.split()[1]) == master]
        assert len(workers) == 1, workers
        report.update(image=image, cpu_max=cpu_max)
        run_wrk(jwt, "read", 32, 5, "-profile-warmup")
        # Unprofiled repetitions establish whether sampling changes the symptom.
        for rep in range(args.reps):
            sample = run_wrk(jwt, "read", 32, args.seconds, f"-profile-control-{rep}")
            report["samples"].append(sample)
            print(json.dumps(sample), flush=True)
        output = STATE / "orm-cpu.raw"
        profiler = subprocess.Popen(
            [
                args.profiler,
                "record",
                "--pid",
                str(workers[0]),
                "--gil",
                "--rate",
                "100",
                "--duration",
                str(args.seconds),
                "--format",
                "raw",
                "--output",
                str(output),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            report["profiled"] = run_wrk(
                jwt, "read", 32, args.seconds, "-profile-sampled"
            )
        finally:
            _, stderr = profiler.communicate(timeout=20)
            if profiler.returncode:
                raise RuntimeError(stderr)
        report["passed"] = all(
            not (s["socket_errors"] or s["http_errors"] or s["invalid_bodies"])
            for s in [*report["samples"], report["profiled"]]
        )
    finally:
        compose("up", "-d", "--no-deps", "--force-recreate", "app", capture_output=True)
        check.wait_ready(token())
        STATE.joinpath("profile-read.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        override.unlink(missing_ok=True)
    if not report["passed"]:
        raise SystemExit("Read-path errors captured")


if __name__ == "__main__":
    main()
