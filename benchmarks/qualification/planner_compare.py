"""Compare ORM and planned reads through the SAME protected HTTPS deployment.

Run without another load generator or fault injector. The order is ABBA at
each concurrency; both routes retain the same auth, security Redis and RLS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

import check
from edge_trace_client import collect
from external import remote
from load import run_wrk
from run import STATE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--ssh", required=True)
    parser.add_argument("--server-root", required=True)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--connections", default="64,128")
    args = parser.parse_args()
    url = urlparse(args.url)
    root = PurePosixPath(args.server_root)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or not root.is_absolute()
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.ssh)
    ):
        parser.error("use an HTTPS origin, SSH alias and absolute server directory")
    try:
        counts = [int(n) for n in args.connections.split(",")]
    except ValueError:
        parser.error("connections must be integers")
    if args.seconds < 10 or not counts or min(counts) < 4:
        parser.error("duration >=10s and connections >=4 required")
    check.BASE = args.url.rstrip("/")
    check.HTTP.addheaders = [("User-Agent", "wrk/4.2.0")]
    jwt = remote(
        args.ssh,
        [
            "sudo",
            "-n",
            "python3",
            str(root / "benchmarks/qualification/run.py"),
            "token",
        ],
    )
    image = remote(
        args.ssh,
        [
            "sudo",
            "-n",
            "docker",
            "inspect",
            "--format",
            "{{.Image}} {{.HostConfig.NanoCpus}}",
            "sandloom-release-qualification-app-1",
        ],
    )
    STATE.mkdir(mode=0o700, exist_ok=True)
    report = {
        "passed": False,
        "runtime": image,
        "url": check.BASE,
        "scope": "same image, 2 workers, ORM/planner HTTPS ABBA",
        "samples": [],
    }
    output = STATE / "planner-comparison.json"
    original = {
        k: os.environ.get(k)
        for k in ("SANDLOOM_QUALIFICATION_READ_ROUTE", "SANDLOOM_QUALIFICATION_USER_AGENT")
    }
    os.environ["SANDLOOM_QUALIFICATION_USER_AGENT"] = "wrk/4.2.0"
    try:
        for count in counts:
            for index, route in enumerate(("/s5/", "/s5plan/", "/s5plan/", "/s5/")):
                os.environ["SANDLOOM_QUALIFICATION_READ_ROUTE"] = route
                check.expect("own tenant", route + "1", jwt, 200)
                check.expect("foreign tenant", route + "5001", jwt, 404)
                suffix = f"-comparison-c{count}-{index}"
                warmup = run_wrk(jwt, "read", count, 3, suffix + "-warmup")
                sample = run_wrk(jwt, "read", count, args.seconds, suffix)
                collect(args.ssh, sample)
                sample.update(route=route, warmup=warmup)
                report["samples"].append(sample)
                output.write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(sample), flush=True)
        report["passed"] = all(
            not (s["http_errors"] or s["socket_errors"] or s["invalid_bodies"])
            for sample in report["samples"]
            for s in (sample, sample["warmup"])
        )
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        output.write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"]:
        raise SystemExit("load errors recorded; inspect planner-comparison.json")


if __name__ == "__main__":
    main()
