"""Interleaved Docker comparison of two built images with the same strict fixture."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess

import check
from load import application_cpu_limit, run_wrk
from run import STATE, compose, token

CURRENT = "sandloom-release:qualification"


def cpu_usage():
    lines = compose(
        "exec",
        "-T",
        "app",
        "cat",
        "/sys/fs/cgroup/cpu.stat",
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return int(dict(line.split() for line in lines)["usage_usec"])


def switch(image):
    os.environ["SANDLOOM_QUALIFICATION_IMAGE"] = image
    compose("up", "-d", "--no-deps", "--force-recreate", "app", capture_output=True)
    jwt = token()
    check.wait_ready(jwt)
    check.expect("A/B tenant isolation", "/s5/5001", jwt, 404)
    return jwt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="sandloom-release:baseline-1898eaa")
    args = parser.parse_args()
    output = STATE / "comparison.json"
    output.unlink(missing_ok=True)
    samples = []
    try:
        for round_id, order in enumerate(
            [
                [("baseline", args.baseline), ("current", CURRENT)],
                [("current", CURRENT), ("baseline", args.baseline)],
                [("baseline", args.baseline), ("current", CURRENT)],
            ],
            1,
        ):
            for lane, image in order:
                jwt = switch(image)
                with application_cpu_limit(True):
                    run_wrk(jwt, "read", 32, 5, f"-ab-warmup-{lane}-{round_id}")
                    before = cpu_usage()
                    sample = run_wrk(jwt, "read", 32, 20, f"-ab-{lane}-{round_id}")
                    after = cpu_usage()
                assert (
                    sample["http_errors"]
                    == sample["socket_errors"]
                    == sample["invalid_bodies"]
                    == 0
                ), sample
                sample.update(
                    lane=lane,
                    image=image,
                    round=round_id,
                    application_cpu_limit=1,
                    cgroup_cpu_us_per_good_request=(after - before) / sample["ok"],
                )
                samples.append(sample)
                print(json.dumps(sample), flush=True)
        medians = {
            lane: statistics.median(
                s["goodput_rps"] for s in samples if s["lane"] == lane
            )
            for lane in ("baseline", "current")
        }
        images = {
            image: subprocess.run(
                ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            for image in (args.baseline, CURRENT)
        }
        result = {
            "passed": True,
            "samples": samples,
            "median_goodput_rps": medians,
            "current_change_percent": (medians["current"] / medians["baseline"] - 1)
            * 100,
            "image_ids": images,
            "scope": "same Docker host, Python image, two workers, 1 CPU quota, RLS database, security Redis, route /s5, wrk c32; AB/BA/AB, 5s warmup + 20s per sample",
        }
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2), flush=True)
    finally:
        switch(CURRENT)
        os.environ.pop("SANDLOOM_QUALIFICATION_IMAGE", None)


if __name__ == "__main__":
    main()
