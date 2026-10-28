"""Per-request CPU of the S5 reads in a REAL server process (Linux container).

The in-process ladder rungs drive the ASGI app directly; the Oracle RPS
qualification runs gunicorn + uvicorn workers (uvloop + httptools, one access
log line per request). This script serves the same app the same way, in ONE
uvicorn process configured like a ``uvicorn_worker.UvicornWorker`` under
``sandloom.core.cli.gunicorn_conf`` (``log_config=None``, ``uvicorn.access`` at
INFO to stdout, propagation off), with the qualification environment
(``PRODUCTION_PROFILE=multi_tenant_strict``, a separate security Redis,
fail-closed revocation, identity loaded, RLS), and measures it:

    # inside the ladder/cg image (+ py-spy), from /w/benchmarks
    python ladder/server_profile.py cg   uow|planopt --k 300 --k2 900
    python ladder/server_profile.py spy  uow|planopt --seconds 20 --conc 64

``cg``  : the server under cachegrind for K and K2 requests (closed loop,
          conc 8): ``(Ir(K2) - Ir(K)) / (K2 - K)`` = user-space instructions
          per request of the whole server (uvloop, httptools, uvicorn, the
          app, asyncpg, redis-py, logging); startup cancels out.
``spy`` : py-spy (``--native``, non-idle samples only) on the server under a
          closed-loop load at ``--conc``; writes the raw stacks, and the
          server's user / system CPU per request from ``/proc``.

Env (the ladder's): DATABASE_URL (owner DSN; the app connects as
S5_APP_ROLE), CACHE_URL, BROKER_URL, SECURITY_CACHE_URL (another Redis
SERVER: the strict profile refuses a shared one).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

ROUTES = {"uow": "/s5/", "planopt": "/s5plan/"}


def _fernet_key() -> str:
    key = os.environ.get("SERVER_PROFILE_TWOFA_KEY")
    if not key:
        from cryptography.fernet import Fernet

        key = os.environ["SERVER_PROFILE_TWOFA_KEY"] = Fernet.generate_key().decode()
    return key


def server_env(variant: str) -> dict[str, str]:
    from benchsuite import s5

    env = s5.server_env(variant)
    env.update(
        {
            "PRODUCTION_PROFILE": "multi_tenant_strict",
            "SANDLOOM_MODULES": '["sandloom_identity", "benchsuite.bench"]',
            "MODULE_REQUIRED": '["sandloom_identity", "benchsuite.bench"]',
            "SECURITY_CACHE_INSTANCE": "security",
            "INFRA": json.dumps(
                {
                    "database": {"default": {"tenant_rls": True}},
                    "cache": {
                        "security": {
                            "driver": "redis",
                            "url": os.environ["SECURITY_CACHE_URL"],
                        }
                    },
                }
            ),
            "AUTH_REVOCATION_FAIL_CLOSED": "true",
            "INTERNAL_API_TOKEN": "server-profile-internal-token-0123456789abcdef",
            "TWOFA_ENCRYPTION_KEY": _fernet_key(),
            "RATE_LIMIT_ENABLED": "false",
            "LOG_LEVEL": "WARNING",
            "OUTBOX_RELAY_ENABLED": "off",
            "DB_POOL_SIZE": "5",
            "DB_MAX_OVERFLOW": "5",
            "CONTRACT_DB_POOL_SIZE": "2",
            "CONTRACT_DB_MAX_OVERFLOW": "2",
            "SANDLOOM_ACCEPTED_RISKS": "relay_disabled",
        }
    )
    return env


def serve(port: int) -> None:
    """One uvicorn process configured like the gunicorn UvicornWorker."""
    import logging

    import uvicorn

    access = logging.getLogger("uvicorn.access")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    access.handlers = [handler]
    access.setLevel(logging.INFO)
    access.propagate = False
    from benchsuite.apps.sandloom_app import app

    profile_out = os.environ.get("SERVER_PROFILE_CPROFILE")
    if profile_out:
        # SIGUSR1 starts a cProfile of the serving thread, SIGUSR2 dumps it
        import cProfile

        prof = cProfile.Profile()

        def _start(*_a: Any) -> None:
            prof.enable()

        def _dump(*_a: Any) -> None:
            prof.disable()
            prof.dump_stats(profile_out)

        signal.signal(signal.SIGUSR1, _start)
        signal.signal(signal.SIGUSR2, _dump)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        loop="uvloop",
        http="httptools",
        log_config=None,
        access_log=True,
        timeout_keep_alive=120,
    )
    uvicorn.Server(config).run()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(port: int, proc: subprocess.Popen[bytes], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise RuntimeError(f"server exited with {proc.returncode}: {err[-3000:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("server did not start")


def _workload(variant: str) -> Any:
    from benchsuite import common, s5
    from benchsuite import loadclient as lc

    token = s5.mint()
    n = common.S5_ROWS_PER_TENANT

    def target(e: Any, i: int) -> str:
        return f"{e.target}{i % n + 1}"

    entry = lc.Entry("GET", ROUTES[variant], "", lc.parse_expect("200"))
    return lc.Workload([entry], [f"Authorization: Bearer {token}"], target)


def _drive(port: int, workload: Any, start: int, n: int, conc: int) -> Any:
    from benchsuite import loadclient as lc

    stats = asyncio.run(
        lc.closed_loop([("127.0.0.1", port)], workload, start, n, conc, timeout=60)
    )
    if stats.ok != (n // conc) * conc:
        raise RuntimeError(f"{stats.ok} ok of {n}: {stats.errors_by_class}")
    return stats


def _start(variant: str, port: int, prefix: list[str]) -> subprocess.Popen[bytes]:
    env = dict(os.environ, **server_env(variant), PYTHONHASHSEED="0")
    return subprocess.Popen(
        [*prefix, sys.executable, __file__, "serve", variant, "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _stop(proc: subprocess.Popen[bytes]) -> str:
    proc.send_signal(signal.SIGINT)
    _out, err = proc.communicate(timeout=600)
    return err.decode(errors="replace")


def run_cg(variant: str, ks: list[int], conc: int) -> dict[str, Any]:
    import re

    workload = _workload(variant)
    irs: dict[int, int] = {}
    for k in ks:
        port = _free_port()
        proc = _start(
            variant,
            port,
            [
                "valgrind",
                "--tool=cachegrind",
                "--cache-sim=no",
                "--cachegrind-out-file=/dev/null",
            ],
        )
        _wait_port(port, proc, 600)
        _drive(port, workload, 0, 40, 4)  # warm-up: pools, caches, prepared stmts
        if k:
            _drive(port, workload, 40, k, conc)
        err = _stop(proc)
        m = re.search(r"I\s+refs:\s+([\d,]+)", err)
        if not m:
            raise RuntimeError(err[-3000:])
        irs[k] = int(m.group(1).replace(",", ""))
    k1, k2 = ks
    return {
        "variant": variant,
        "conc": conc,
        "ir": irs,
        "instr_per_request": (irs[k2] - irs[k1]) / (k2 - k1),
    }


def _proc_cpu(pid: int) -> tuple[float, float]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    tick = os.sysconf("SC_CLK_TCK")
    return int(fields[11]) / tick, int(fields[12]) / tick  # utime, stime


def run_perf(
    variant: str, seconds: float, conc: int, out: str, rate: int
) -> dict[str, Any]:
    """``perf record -g`` on the server (Python perf trampolines on): every
    sample carries the symbolized C frames AND the Python frames, user and
    kernel; exported with ``perf script`` for ``layer_attribution.py perf``."""
    os.environ["PYTHONPERFSUPPORT"] = "1"
    workload = _workload(variant)
    port = _free_port()
    proc = _start(variant, port, [])
    try:
        _wait_port(port, proc, 120)
        _drive(port, workload, 0, 4000, conc)
        u0, s0 = _proc_cpu(proc.pid)
        t0 = time.perf_counter()
        stats = _drive(port, workload, 0, int(seconds * 300), conc)
        el = time.perf_counter() - t0
        u1, s1 = _proc_cpu(proc.pid)
        cpu = {
            "requests": stats.ok,
            "rps": stats.ok / el,
            "utilization": (u1 - u0 + s1 - s0) / el,
            "user_us_per_req": (u1 - u0) / stats.ok * 1e6,
            "sys_us_per_req": (s1 - s0) / stats.ok * 1e6,
        }
        data = out.replace(".txt", ".data")
        rec = subprocess.Popen(
            [
                "perf",
                "record",
                "-F",
                str(rate),
                "-g",
                "-p",
                str(proc.pid),
                "-o",
                data,
                "--",
                "sleep",
                str(int(seconds)),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.5)
        u0, s0 = _proc_cpu(proc.pid)
        stats = _drive(port, workload, 0, int(seconds * 400), conc)
        u1, s1 = _proc_cpu(proc.pid)
        _o, rec_err = rec.communicate(timeout=seconds + 120)
        if rec.returncode != 0:
            raise RuntimeError(f"perf record failed: {rec_err.decode()[-2000:]}")
        cpu["profiled_requests"] = stats.ok
        cpu["profiled_us_per_req"] = (u1 - u0 + s1 - s0) / stats.ok * 1e6
        with open(out, "w") as fh:
            subprocess.run(
                ["perf", "script", "-i", data, "-F", "tid,ip,sym,dso"],
                stdout=fh,
                stderr=subprocess.DEVNULL,
                check=True,
            )
    finally:
        _stop(proc)
    return {"variant": variant, "conc": conc, "cpu": cpu, "stacks": out}


def run_spy(
    variant: str, seconds: float, conc: int, out: str, rate: int
) -> dict[str, Any]:
    workload = _workload(variant)
    port = _free_port()
    proc = _start(variant, port, [])
    try:
        _wait_port(port, proc, 120)
        _drive(port, workload, 0, 4000, conc)  # warm-up
        # CPU per request without the profiler attached
        u0, s0 = _proc_cpu(proc.pid)
        t0 = time.perf_counter()
        stats = _drive(port, workload, 0, int(seconds * 400), conc)
        el = time.perf_counter() - t0
        u1, s1 = _proc_cpu(proc.pid)
        cpu = {
            "requests": stats.ok,
            "rps": stats.ok / el,
            "user_us_per_req": (u1 - u0) / stats.ok * 1e6,
            "sys_us_per_req": (s1 - s0) / stats.ok * 1e6,
        }
        spy = subprocess.Popen(
            [
                "py-spy",
                "record",
                "--pid",
                str(proc.pid),
                "--native",
                "--rate",
                str(rate),
                "--duration",
                str(int(seconds)),
                "--format",
                "raw",
                "--output",
                out,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(1)
        _drive(port, workload, 0, int(seconds * 500), conc)
        _out, spy_err = spy.communicate(timeout=seconds + 120)
        if spy.returncode != 0:
            raise RuntimeError(
                f"py-spy failed: {spy_err.decode(errors='replace')[-2000:]}"
            )
    finally:
        _stop(proc)
    return {"variant": variant, "conc": conc, "cpu": cpu, "stacks": out}


def _tree_cpu(pid: int) -> float:
    import psutil

    t = psutil.Process(pid).cpu_times()
    return float(t.user + t.system)


def run_cprof(variant: str, n: int, conc: int, out: str) -> dict[str, Any]:
    """Host fallback (no ptrace sampler): CPU per request unprofiled, then a
    cProfile of the same load; per-call profiler overhead is calibrated from
    the difference (see the report)."""
    import psutil

    workload = _workload(variant)
    port = _free_port()
    os.environ["SERVER_PROFILE_CPROFILE"] = out
    proc = _start(variant, port, [])
    try:
        _wait_port(port, proc, 120)
        _drive(port, workload, 0, 4000, conc)
        ps = psutil.Process(proc.pid)
        t0, w0 = ps.cpu_times(), time.perf_counter()
        stats = _drive(port, workload, 0, n, conc)
        t1, w1 = ps.cpu_times(), time.perf_counter()
        plain = {
            "requests": stats.ok,
            "rps": stats.ok / (w1 - w0),
            "utilization": (t1.user + t1.system - t0.user - t0.system) / (w1 - w0),
            "user_us_per_req": (t1.user - t0.user) / stats.ok * 1e6,
            "sys_us_per_req": (t1.system - t0.system) / stats.ok * 1e6,
        }
        proc.send_signal(signal.SIGUSR1)
        time.sleep(0.5)
        t0, w0 = ps.cpu_times(), time.perf_counter()
        stats = _drive(port, workload, 0, n, conc)
        t1, w1 = ps.cpu_times(), time.perf_counter()
        proc.send_signal(signal.SIGUSR2)
        time.sleep(2)
        profiled = {
            "requests": stats.ok,
            "utilization": (t1.user + t1.system - t0.user - t0.system) / (w1 - w0),
            "user_us_per_req": (t1.user - t0.user) / stats.ok * 1e6,
            "sys_us_per_req": (t1.system - t0.system) / stats.ok * 1e6,
        }
    finally:
        _stop(proc)
    return {
        "variant": variant,
        "conc": conc,
        "plain": plain,
        "profiled": profiled,
        "stats_file": out,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("mode", choices=["serve", "cg", "spy", "perf", "cprof", "env"])
    p.add_argument("variant", choices=list(ROUTES))
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--n", type=int, default=20000)
    p.add_argument("--k", type=int, default=300)
    p.add_argument("--k2", type=int, default=900)
    p.add_argument("--conc", type=int, default=0)
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--rate", type=int, default=997)
    p.add_argument("--out", default="/out/stacks.raw")
    a = p.parse_args()
    if a.mode == "serve":
        serve(a.port)
    elif a.mode == "env":
        print(json.dumps(server_env(a.variant), indent=1))
    elif a.mode == "cg":
        print(json.dumps(run_cg(a.variant, [a.k, a.k2], a.conc or 8)))
    elif a.mode == "perf":
        out = a.out.replace(".raw", f".{a.variant}.perf.txt")
        print(json.dumps(run_perf(a.variant, a.seconds, a.conc or 64, out, a.rate)))
    elif a.mode == "cprof":
        out = a.out.replace(".raw", f".{a.variant}.prof")
        print(json.dumps(run_cprof(a.variant, a.n, a.conc or 64, out)))
    else:
        out = a.out.replace(".raw", f".{a.variant}.raw")
        print(json.dumps(run_spy(a.variant, a.seconds, a.conc or 64, out, a.rate)))


if __name__ == "__main__":
    main()
