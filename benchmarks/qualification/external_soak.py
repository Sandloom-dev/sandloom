"""Coordinate a bounded server fault soak with client-side HTTPS wrk windows."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
import urllib.request
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
    parser.add_argument("--seconds", type=int, default=7200)
    parser.add_argument("--fault-interval", type=int, default=600)
    parser.add_argument("--connections", type=int, default=32)
    parser.add_argument(
        "--read-route", choices=("/s5/", "/s5plan/"), default="/s5plan/"
    )
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
    if not 180 <= args.seconds <= 14400 or args.connections < 4:
        parser.error("duration 180..14400 and connections >=4 required")
    check.BASE = args.url.rstrip("/")
    check.HTTP.addheaders = [("User-Agent", "wrk/4.2.0")]
    os.environ["SANDLOOM_QUALIFICATION_USER_AGENT"] = "wrk/4.2.0"
    os.environ["SANDLOOM_QUALIFICATION_READ_ROUTE"] = args.read_route
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
    check.expect("HTTPS own tenant", "/s5/1", jwt, 200)
    check.expect("HTTPS foreign tenant", "/s5/5001", jwt, 404)
    check.expect("HTTPS no token", "/s5/1", None, 401)
    check.expect("HTTPS planned own tenant", "/s5plan/1", jwt, 200)
    check.expect("HTTPS planned foreign tenant", "/s5plan/5001", jwt, 404)
    with check.HTTP.open(
        urllib.request.Request(
            check.BASE + "/s5/1", headers={"Authorization": f"Bearer {jwt}"}
        ),
        timeout=12,
    ) as response:
        assert response.headers.get("CF-Cache-Status") not in (
            "HIT",
            "STALE",
            "UPDATING",
        )
        response.read()
    state = root / "benchmarks/qualification/.state"
    remote(
        args.ssh,
        [
            "sudo",
            "-n",
            "rm",
            "-f",
            str(state / "soak-progress.json"),
            str(state / "production-soak.json"),
        ],
    )
    overrides = ":".join(
        str(root / "benchmarks/qualification" / name)
        for name in (
            "compose.production.yml",
            "compose.https.yml",
            "compose.cloudflare.yml",
        )
    )
    command = [
        "sudo",
        "-n",
        "env",
        "SANDLOOM_QUALIFICATION_ENV_FILES=" + str(state / "https.env"),
        "SANDLOOM_QUALIFICATION_COMPOSE_FILES=" + overrides,
        "python3",
        str(root / "benchmarks/qualification/production_soak.py"),
        "--seconds",
        str(args.seconds),
        "--fault-interval",
        str(args.fault_interval),
    ]
    STATE.mkdir(mode=0o700, exist_ok=True)
    output = STATE / "external-soak.json"
    report = {
        "passed": False,
        "url": check.BASE,
        "requested_seconds": args.seconds,
        "connections": args.connections,
        "read_route": args.read_route,
        "samples": [],
    }
    log = (STATE / "production-soak-server.log").open("w")
    process = subprocess.Popen(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=30",
            args.ssh,
            shlex.join(command),
        ],
        stdout=log,
        stderr=log,
    )
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError(
                    "server soak failed during preparation; inspect server log"
                )
            try:
                progress = json.loads(
                    remote(
                        args.ssh,
                        ["sudo", "-n", "cat", str(state / "soak-progress.json")],
                    )
                )
                break
            except subprocess.CalledProcessError:
                time.sleep(1)
        else:
            raise RuntimeError("server soak did not start")
        report["started_epoch"] = progress["started_epoch"]
        # Bound clock uncertainty using an RTT bracket, not an assumed clock match.
        t0 = time.time()
        server_now = float(remote(args.ssh, ["date", "+%s.%N"]))
        t1 = time.time()
        offset = server_now - (t0 + t1) / 2
        uncertainty = (
            t1 - t0
        ) / 2 + 1  # wrk's error timestamps have one-second resolution
        report.update(
            clock_offset_seconds=offset, clock_uncertainty_seconds=uncertainty
        )
        end = progress["started_epoch"] + args.seconds - offset
        while time.time() < end:
            if process.poll() is not None:
                raise RuntimeError("server soak ended early; inspect server log")
            seconds = min(20, max(1, int(end - time.time())))
            started = time.time()
            sample = run_wrk(
                jwt,
                "read",
                args.connections,
                seconds,
                f"-external-soak-{len(report['samples']):04}",
            )
            collect(args.ssh, sample)
            sample.update(started_epoch=started, finished_epoch=time.time())
            report["samples"].append(sample)
            output.write_text(json.dumps(report, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "window": len(report["samples"]),
                        "elapsed": round(
                            time.time() + offset - progress["started_epoch"]
                        ),
                        "rps": sample["goodput_rps"],
                        "http_errors": sample["http_errors"],
                        "socket_errors": sample["socket_errors"],
                    }
                ),
                flush=True,
            )
        process.wait(timeout=120)
        if process.returncode:
            raise RuntimeError("server soak failed; inspect server log")
        server = json.loads(
            remote(args.ssh, ["sudo", "-n", "cat", str(state / "production-soak.json")])
        )
        report["server"] = server
        faults = server["faults"]
        unexpected = []
        for index, sample in enumerate(report["samples"]):
            overlaps = any(
                sample["started_epoch"] + offset
                <= f["tolerated_until_epoch"] + uncertainty
                and sample["finished_epoch"] + offset
                >= f["started_epoch"] - uncertainty
                for f in faults
            )
            sample["fault_overlap"] = overlaps
            bad_http = []
            for key, count in sample["error_times"].items():
                epoch, status = (int(part) for part in key.split(":"))
                expected = status >= 500 and any(
                    f["started_epoch"] - uncertainty
                    <= epoch + offset
                    <= f["tolerated_until_epoch"] + uncertainty
                    for f in faults
                )
                if not expected:
                    bad_http.append({"epoch": epoch, "status": status, "count": count})
            if (
                bad_http
                or sample["invalid_bodies"]
                or (sample["socket_errors"] and not overlaps)
            ):
                unexpected.append(
                    {
                        "window": index,
                        "http": bad_http,
                        "socket_errors": sample["socket_errors"],
                        "invalid_bodies": sample["invalid_bodies"],
                    }
                )
        report["unexpected_errors"] = unexpected
        report["passed"] = server["passed"] and not unexpected
        check.expect("HTTPS final isolation", "/s5/5001", jwt, 404)
        check.wait_ready(jwt)
    finally:
        # SSH disconnect must not silently leave an unobserved fault injector.
        if process.poll() is None or "server" not in report:
            try:
                remote(
                    args.ssh,
                    [
                        "sudo",
                        "-n",
                        "python3",
                        "-c",
                        "import pathlib,os,signal,sys; p=pathlib.Path(sys.argv[1]); "
                        "pid=p.read_text().strip() if p.exists() else ''; "
                        "cmd=pathlib.Path('/proc')/pid/'cmdline'; "
                        "live=cmd.read_bytes().split(bytes([0])) if pid.isdecimal() and cmd.exists() else []; "
                        "expected=sys.argv[2].encode(); "
                        "os.kill(int(pid),signal.SIGINT) if expected in live else None",
                        str(state / "soak.pid"),
                        str(root / "benchmarks/qualification/production_soak.py"),
                    ],
                )
                if process.poll() is None:
                    process.wait(timeout=45)
            finally:
                if process.poll() is None:
                    process.terminate()
        log.close()
        report["finished_epoch"] = time.time()
        output.write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"]:
        raise SystemExit(
            "Unexplained errors or server invariants failed; inspect external-soak.json"
        )


if __name__ == "__main__":
    main()
