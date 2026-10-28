"""Run read-only wrk from this machine against a protected HTTPS deployment."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import resource
import shlex
import shutil
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import urlparse

import check
from load import run_wrk
from run import STATE


def remote(alias, command):
    return subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            alias,
            shlex.join(command),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="https://test.example.com")
    parser.add_argument("--ssh", required=True, help="configured SSH alias")
    parser.add_argument("--server-root", required=True, help="remote fixture directory")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--connections", default="8,32,64")
    args = parser.parse_args()
    url = urlparse(args.url)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        parser.error("--url must be an HTTPS origin without credentials or a path")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.ssh):
        parser.error("--ssh must be an SSH config alias")
    root = PurePosixPath(args.server_root)
    if not root.is_absolute():
        parser.error("--server-root must be absolute")
    try:
        connections = [int(n) for n in args.connections.split(",")]
    except ValueError:
        parser.error("--connections must contain integers")
    if args.seconds < 1 or not connections or min(connections) < 4:
        parser.error("positive duration and at least four connections are required")
    if not shutil.which("wrk"):
        parser.error("wrk is required")
    STATE.mkdir(mode=0o700, exist_ok=True)
    output = STATE / "external.json"
    output.unlink(missing_ok=True)
    # Synthetic signing material stays on the server; never print the bearer.
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
    runtime = remote(
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
    ).split()
    keepalive = int(
        remote(
            args.ssh,
            [
                "sudo",
                "-n",
                "docker",
                "exec",
                "sandloom-release-qualification-app-1",
                "python",
                "-c",
                "from sandloom.core.cli.gunicorn_conf import keepalive; print(keepalive)",
            ],
        )
    )
    other_tenant_jwt = remote(
        args.ssh,
        [
            "sudo",
            "-n",
            "docker",
            "exec",
            "sandloom-release-qualification-app-1",
            "python",
            "-c",
            "from benchsuite.s5 import mint; "
            "print(mint('5e000000-0000-4000-8000-00000000000b'))",
        ],
    )
    check.BASE = args.url.rstrip("/")
    user_agent = "wrk/4.2.0"
    check.HTTP.addheaders = [("User-Agent", user_agent)]
    report = {
        "at": datetime.now(UTC).isoformat(),
        "passed": False,
        "scope": "external client -> public HTTPS -> Coolify Traefik -> app; no SSH tunnel",
        "url": check.BASE,
        "image": runtime[0],
        "application_cpu_limit": int(runtime[1]) / 1_000_000_000,
        "application_keepalive_seconds": keepalive,
        "client": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "cpus": os.cpu_count(),
            "user_agent": user_agent,
        },
        "samples": [],
    }
    previous_route = os.environ.pop("SANDLOOM_QUALIFICATION_READ_ROUTE", None)
    previous_agent = os.environ.get("SANDLOOM_QUALIFICATION_USER_AGENT")
    os.environ["SANDLOOM_QUALIFICATION_USER_AGENT"] = user_agent
    try:
        # urllib verifies the HTTPS certificate before load begins.
        check.wait_ready(jwt)
        check.expect("external own tenant", "/s5/1", jwt, 200)
        check.expect("same URL with another tenant", "/s5/1", other_tenant_jwt, 404)
        check.expect("external foreign tenant", "/s5/5001", jwt, 404)
        check.expect("external missing JWT", "/s5/1", None, 401)
        with check.HTTP.open(
            urllib.request.Request(
                check.BASE + "/s5/1", headers={"Authorization": f"Bearer {jwt}"}
            ),
            timeout=12,
        ) as response:
            report["edge_headers"] = {
                key: response.headers.get(key)
                for key in ("Server", "CF-Cache-Status", "Age", "Cache-Control")
            }
            response.read()
        assert report["edge_headers"]["CF-Cache-Status"] not in (
            "HIT",
            "STALE",
            "UPDATING",
        ), "CDN cache would invalidate framework capacity measurement"
        report["warmup"] = run_wrk(jwt, "read", connections[0], 10, "-external-warmup")
        for count in connections:
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            sample = run_wrk(jwt, "read", count, args.seconds, "-external")
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            sample["client_cpu_seconds"] = (after.ru_utime + after.ru_stime) - (
                before.ru_utime + before.ru_stime
            )
            report["samples"].append(sample)
            output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(sample), flush=True)
        check.expect("post-load external foreign tenant", "/s5/5001", jwt, 404)
        check.wait_ready(jwt)
        report["passed"] = all(
            not (s["http_errors"] or s["invalid_bodies"] or s["socket_errors"])
            for s in [report["warmup"], *report["samples"]]
        )
        report["checks"] = check.RESULTS
    finally:
        if previous_route is not None:
            os.environ["SANDLOOM_QUALIFICATION_READ_ROUTE"] = previous_route
        if previous_agent is None:
            os.environ.pop("SANDLOOM_QUALIFICATION_USER_AGENT", None)
        else:
            os.environ["SANDLOOM_QUALIFICATION_USER_AGENT"] = previous_agent
        output.write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"]:
        raise SystemExit("External load errors recorded; inspect .state/external.json")


if __name__ == "__main__":
    main()
