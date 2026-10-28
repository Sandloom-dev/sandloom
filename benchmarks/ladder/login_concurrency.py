"""Password-login throughput vs ``PASSWORD_HASH_CONCURRENCY`` in a CPU quota.

The question (security review): ``PASSWORD_HASH_CONCURRENCY`` defaults to
``min(4, CPUs)`` PER PROCESS, and every argon2id hash of the default
``strong`` profile runs ``parallelism = 4`` lanes on their own threads. Under
gunicorn that multiplies: 2 workers x 2 concurrent hashes x 4 lanes = 16
runnable hash threads on a 2-vCPU quota. Does the limit change login
throughput, and what does the oversubscription cost the OTHER requests the
same workers serve?

For each limit the stand starts the platform app (identity + tenancy +
billing) under the framework's gunicorn config in a container with a CPU
quota (``--cpus``), the ``ladder/cg`` image (the exact benchmark lock) with
the checked-out ``src/`` and ``benchmarks/`` mounted read-only, then for
``--seconds``:

* a CLOSED loop of password logins (``--conc`` connections, seeded users
  with the default strong hash, ``ladder/write_login.py seed``);
* at the same time an OPEN loop of ``GET /auth/me`` at ``--read-rate``
  (a cheap strict read, coordinated-omission corrected latency).

It records login RPS and latency, the read's latency, and the container's
cgroup CPU (``usage_usec``, throttling). Run from ``benchmarks/`` with
``DATABASE_URL`` / ``CACHE_URL`` / ``BROKER_URL`` of the stand's own database
(localhost is rewritten to ``host.docker.internal``)::

    uv run --frozen python ladder/login_concurrency.py --limits 1,2,4 \\
        --workers 2 --cpus 2 --seconds 20 --out ladder/results/login_conc.json

``--limits default`` leaves ``PASSWORD_HASH_CONCURRENCY`` unset (the
framework's own default in that container). Ports: 18310 + index.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
IMAGE = "sandloom-ladder-cg:3.13"


def _docker_url(url: str) -> str:
    return url.replace("localhost", "host.docker.internal").replace(
        "127.0.0.1", "host.docker.internal"
    )


def _container_env(limit: str, workers: int) -> dict[str, str]:
    sys.path.insert(0, str(HERE.parent))
    sys.path.insert(0, str(HERE))
    from write_login import TWOFA_KEY

    from benchsuite import platform as plat

    env = dict(plat.server_env())
    env.update(
        {
            "DATABASE_URL": _docker_url(env["DATABASE_URL"]),
            "CACHE_URL": _docker_url(env["CACHE_URL"]),
            "BROKER_URL": _docker_url(env["BROKER_URL"]),
            "TWOFA_ENCRYPTION_KEY": TWOFA_KEY,
            "OUTBOX_RELAY_ENABLED": "off",
            "SANDLOOM_ACCEPTED_RISKS": "relay_disabled",
            "PASSWORD_HASH_PROFILE": "strong",
            "WEB_CONCURRENCY": str(workers),
            "GUNICORN_BIND": "0.0.0.0:8000",
            "PYTHONPATH": "/w/src:/w/benchmarks",
        }
    )
    if limit != "default":
        env["PASSWORD_HASH_CONCURRENCY"] = limit
    return env


def _start(name: str, port: int, cpus: float, env: dict[str, str]) -> None:
    args = ["docker", "run", "-d", "--rm", "--name", name, f"--cpus={cpus}"]
    args += ["--memory=3g", "-p", f"127.0.0.1:{port}:8000"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    args += [
        "-v",
        f"{ROOT / 'src'}:/w/src:ro",
        "-v",
        f"{ROOT / 'benchmarks'}:/w/benchmarks:ro",
        "-w",
        "/w/benchmarks",
        IMAGE,
        "gunicorn",
        "-c",
        "python:sandloom.core.cli.gunicorn_conf",
        "sandloom.main:app",
    ]
    subprocess.run(args, check=True, capture_output=True)


def _wait_ready(port: int, name: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                return
        except Exception:  # noqa: BLE001 - not up yet (or 503 while warming)
            time.sleep(0.5)
    logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
    raise RuntimeError(f"{name} not ready:\n{(logs.stdout + logs.stderr)[-3000:]}")


def _cpu_stat(name: str) -> dict[str, int]:
    out = subprocess.run(
        ["docker", "exec", name, "cat", "/sys/fs/cgroup/cpu.stat"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {k: int(v) for k, v in (line.split() for line in out.splitlines())}


def _login_raw(k: int) -> bytes:
    from write_login import PASSWORD, STRONG_USERS, strong_email

    body = json.dumps(
        {"email": strong_email(k % STRONG_USERS), "password": PASSWORD}
    ).encode()
    return (
        b"POST /auth/login HTTP/1.1\r\nHost: bench\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


async def _measure(port: int, args: argparse.Namespace, token: str) -> dict[str, Any]:
    from benchsuite import loadclient as lc

    ep = ("127.0.0.1", port)
    login = lc.RunStats()
    deadline = time.perf_counter() + args.seconds
    counter = [0]

    async def login_worker() -> None:
        slot = lc._Slot(ep)
        while time.perf_counter() < deadline:
            i = counter[0]
            counter[0] += 1
            login.planned += 1
            await slot.exchange(
                _login_raw(i), frozenset([200]), login, time.perf_counter(), 60
            )
        slot.drop()

    read_wl = lc.Workload(
        [lc.Entry("GET", "/auth/me", "", frozenset([200]))],
        [f"Authorization: Bearer {token}"],
    )
    t0 = time.perf_counter()
    reads, *_ = await asyncio.gather(
        lc.open_loop([ep], read_wl, args.read_rate, args.seconds, pool=64, timeout=60),
        *(login_worker() for _ in range(args.conc)),
    )
    elapsed = time.perf_counter() - t0
    login.elapsed = elapsed
    return {
        "login_rps": login.ok / elapsed,
        "login": {**login.as_dict(), **login.quantiles()},
        "read_rate": args.read_rate,
        "read": {**reads.as_dict(), **reads.quantiles()},
        "elapsed_s": elapsed,
    }


def _token() -> str:
    from benchsuite import platform as plat

    return plat.mint()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--limits", default="1,2,4")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--cpus", type=float, default=2.0)
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--warmup", type=float, default=5)
    p.add_argument("--conc", type=int, default=8)
    p.add_argument("--read-rate", type=float, default=50)
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    sys.path.insert(0, str(HERE.parent))
    sys.path.insert(0, str(HERE))
    results: list[dict[str, Any]] = []
    for rnd in range(a.rounds):
        limits = a.limits.split(",")
        if rnd % 2:
            limits.reverse()
        for idx, limit in enumerate(limits):
            name = f"sandloom-login-conc-{os.getpid()}-{idx}"
            port = 18310 + idx
            _start(name, port, a.cpus, _container_env(limit, a.workers))
            try:
                _wait_ready(port, name)
                token = _token()
                warm = argparse.Namespace(**{**vars(a), "seconds": a.warmup})
                asyncio.run(_measure(port, warm, token))
                before = _cpu_stat(name)
                res = asyncio.run(_measure(port, a, token))
                after = _cpu_stat(name)
            finally:
                subprocess.run(["docker", "stop", name], capture_output=True)
            secs = res["elapsed_s"]
            res.update(
                limit=limit,
                round=rnd,
                workers=a.workers,
                cpus=a.cpus,
                conc=a.conc,
                cpu_cores_used=(after["usage_usec"] - before["usage_usec"])
                / secs
                / 1e6,
                throttled_usec=after.get("throttled_usec", 0)
                - before.get("throttled_usec", 0),
                cpu_ms_per_login=(after["usage_usec"] - before["usage_usec"])
                / 1000
                / max(res["login"]["ok"], 1),
            )
            results.append(res)
            print(
                json.dumps(
                    {
                        k: res[k]
                        for k in ("limit", "round", "login_rps", "cpu_cores_used")
                    }
                    | {
                        "login_p50": res["login"]["p50_ms"],
                        "login_p99": res["login"]["p99_ms"],
                        "read_p50": res["read"]["p50_ms"],
                        "read_p99": res["read"]["p99_ms"],
                        "read_errors": res["read"]["errors"],
                    }
                ),
                flush=True,
            )
    Path(a.out).write_text(json.dumps({"results": results}, indent=1) + "\n")


if __name__ == "__main__":
    main()
