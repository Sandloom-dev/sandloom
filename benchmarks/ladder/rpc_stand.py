"""RPC stand over a REAL loopback socket: caller CPU vs provider CPU per call.

Starts a provider process (uvicorn, one worker, ``sandloom.main:app`` with
``benchsuite.rpcstand.server_env``) and calls ONE contract method from THIS
process through the production proxy path (``RemoteServiceProxy`` on the
shared pooled RPC client of ``--transport`` = ``RPC_TRANSPORT``, keep-alive),
by ``--conc`` concurrent callers (default 1 = sequential).

Per call it reports:

* ``client_cpu_us`` — this process's CPU (``process_time``): proxy, codec,
  pair-MAC (+ command signature) and response-binding check, the transport, the event loop and socket syscalls;
* ``server_cpu_us`` — the provider process's CPU (psutil user+system):
  uvicorn + the RPC fast path + pair-MAC verification and replay guard +
  contract runtime (+ UoW/DB
  client work for R2+);
* ``pg_cpu_us`` / ``redis_cpu_us`` — container cgroup CPU (UPPER bounds on a
  shared container; ``--no-containers`` to skip);
* ``wall_us``.

    # DATABASE_URL / CACHE_URL / BROKER_URL as for ladder.py; seed first:
    #   python -m benchsuite.platform seed
    uv run python ladder/rpc_stand.py R0 --n 5000 --reps 5
    uv run python ladder/rpc_stand.py R0 --tenant      # + tenant in the context
    uv run python ladder/rpc_stand.py R3o              # provider planner OPTIMIZED
    uv run python ladder/rpc_stand.py R0 --n 3 --reps 1 --warmup 2 --no-containers
                                                       # functional smoke only
    uv run python ladder/rpc_stand.py R3o --conc 64 --transport aiohttp
                                                       # the transport A/B rows

``new_connections_per_call``: connections the caller opened per call
(httpx: counted in httpcore's pool; aiohttp / lean: the
``sandloom_rpc_client_connections_opened_total`` metric). ``RPC_STAND_PROFILE=<file>``
dumps a cProfile (CPU timer with ``RPC_STAND_PROFILE_CPU=1``) of one extra,
unmeasured rep.

Rungs = ``benchsuite.rpcstand.METHODS`` (R0 R2 R2q R3 R3o R3u R4 R4r).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from server_bench import (  # noqa: E402
    PG_CONTAINER,
    REDIS_CONTAINER,
    _container_cpu_usec,
    _free_port,
    _rss_per_worker,
)


def main() -> None:
    from benchsuite import rpcstand

    p = argparse.ArgumentParser()
    p.add_argument("rung", choices=list(rpcstand.METHODS))
    p.add_argument("--tenant", action="store_true", help="bind a tenant (signed ctx)")
    p.add_argument("--n", type=int, default=5000)
    p.add_argument("--reps", type=int, default=5)
    p.add_argument("--warmup", type=int, default=1000)
    p.add_argument("--loop", default="uvloop", choices=["asyncio", "uvloop"])
    p.add_argument("--http", default="httptools", choices=["h11", "httptools"])
    p.add_argument("--env", action="append", default=[], help="KEY=VAL (provider)")
    p.add_argument("--label", default="")
    p.add_argument("--conc", type=int, default=1, help="concurrent callers")
    p.add_argument(
        "--transport",
        default="httpx",
        choices=["httpx", "aiohttp", "lean"],
        help="the caller's RPC_TRANSPORT (main before the switch: httpx only)",
    )
    p.add_argument(
        "--keepalive",
        type=int,
        default=None,
        help="httpx only: override max_keepalive_connections (prod: 20)",
    )
    p.add_argument("--containers", action=argparse.BooleanOptionalAction, default=True)
    a = p.parse_args()
    m = rpcstand.METHODS[a.rung]
    env = rpcstand.server_env(planner_opt=m.planner_opt)
    os.environ.update(env)  # the caller's settings (RPC keys, prod checks) too
    os.environ["RPC_TRANSPORT"] = a.transport  # read when sandloom is imported
    port = _free_port()
    srv_env = {
        **os.environ,
        **env,
        "PYTHONUNBUFFERED": "1",
        **dict(kv.split("=", 1) for kv in a.env),
    }
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "sandloom.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--loop",
        a.loop,
        "--http",
        a.http,
        "--no-access-log",
        "--log-level",
        "warning",
    ]
    srv = subprocess.Popen(cmd, env=srv_env, cwd=HERE.parent)
    try:
        res = _drive(a, m, port, srv.pid)
    finally:
        srv.terminate()
        srv.wait(10)
    print(json.dumps(res))


def _drive(a: argparse.Namespace, m, port: int, pid: int) -> dict:
    import socket

    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    proc = psutil.Process(pid)
    loop_factory = asyncio.new_event_loop
    if a.loop == "uvloop":
        import uvloop

        loop_factory = uvloop.new_event_loop

    from sandloom.core import rpc
    from sandloom.core.tenant import bind_tenant
    from benchsuite.rpcstand import ORG, STAND_SERVICE

    if a.transport != "httpx":
        from sandloom.core.config.settings import settings

        if getattr(settings, "rpc_transport", None) != a.transport:
            raise SystemExit(f"this build has no RPC_TRANSPORT={a.transport}")
    if a.keepalive is not None:  # the same client _rpc_client builds, other limit
        import httpx

        # The pooled client lives in sandloom.core.rpc.client (older builds: the
        # sandloom.core.rpc module itself).
        pool = sys.modules.get("sandloom.core.rpc.client", rpc)
        pool._rpc_client_instance = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=100, max_keepalive_connections=a.keepalive
            )
        )
        pool._rpc_client_kind = "httpx"  # (main: attribute absent, harmless)
    opened = _connection_counter(a.transport)

    proxy = rpc.RemoteServiceProxy(
        service_name=m.service,
        # The provider process runs with the SAME env (keys included): the
        # stand calls itself across the socket, pinned to its service id.
        binding={"base_url": f"http://127.0.0.1:{port}", "service_id": STAND_SERVICE},
        default_timeout_seconds=10,
    )  # client=None: the shared pooled client, as in production
    call = getattr(proxy, m.method)
    nonce = uuid.uuid4().hex[:12]

    async def one(i: int) -> None:
        args, kwargs = m.args(i, nonce)
        if a.tenant:
            with bind_tenant(ORG):
                got = await call(*args, **kwargs)
        else:
            got = await call(*args, **kwargs)
        if got != "owner":
            raise RuntimeError(f"{m.method}: unexpected {got!r}")

    async def batch(start: int, n: int) -> None:
        if a.conc <= 1:
            for i in range(start, start + n):
                await one(i)
            return
        nxt = [start]

        async def worker() -> None:
            while nxt[0] < start + n:
                i = nxt[0]
                nxt[0] += 1
                await one(i)

        await asyncio.gather(*(worker() for _ in range(a.conc)))

    cpu, srv_cpu, wall, pg, redis, conns = [], [], [], [], [], []
    with asyncio.Runner(loop_factory=loop_factory) as r:
        r.run(batch(0, a.warmup))
        for rep in range(a.reps):
            p0 = _container_cpu_usec(PG_CONTAINER) if a.containers else None
            q0 = _container_cpu_usec(REDIS_CONTAINER) if a.containers else None
            o0 = opened()
            s0, c0, w0 = proc.cpu_times(), time.process_time(), time.perf_counter()
            r.run(batch((rep + 1) * a.n, a.n))
            s1, c1, w1 = proc.cpu_times(), time.process_time(), time.perf_counter()
            p1 = _container_cpu_usec(PG_CONTAINER) if a.containers else None
            q1 = _container_cpu_usec(REDIS_CONTAINER) if a.containers else None
            conns.append((opened() - o0) / a.n)
            cpu.append((c1 - c0) / a.n * 1e6)
            srv_cpu.append(((s1.user + s1.system) - (s0.user + s0.system)) / a.n * 1e6)
            wall.append((w1 - w0) / a.n * 1e6)
            if p0 is not None and p1 is not None:
                pg.append((p1 - p0) / a.n)
            if q0 is not None and q1 is not None:
                redis.append((q1 - q0) / a.n)
        prof_out = os.environ.get("RPC_STAND_PROFILE")
        if prof_out:  # one extra, profiled rep (never part of the CPU numbers)
            import cProfile

            pr = (
                cProfile.Profile(time.process_time)  # CPU, not wall
                if os.environ.get("RPC_STAND_PROFILE_CPU")
                else cProfile.Profile()
            )
            pr.enable()
            r.run(batch((a.reps + 1) * a.n, a.n))
            pr.disable()
            pr.dump_stats(prof_out)
        r.run(rpc.close_rpc_client())
    return {
        "stand": "rpc_socket",
        "rung": a.rung,
        "tenant": a.tenant,
        "service": m.service,
        "method": m.method,
        "desc": m.desc,
        "n": a.n,
        "reps": a.reps,
        "loop": a.loop,
        "http": a.http,
        "env": a.env,
        "label": a.label,
        "conc": a.conc,
        "transport": a.transport,
        "keepalive": a.keepalive,
        "sandloom_build": _sandloom_build(),
        "new_connections_per_call": statistics.median(conns),
        "client_cpu_us": statistics.median(cpu),
        "server_cpu_us": statistics.median(srv_cpu),
        "total_cpu_us": statistics.median(
            c + s for c, s in zip(cpu, srv_cpu, strict=True)
        ),
        "wall_us": statistics.median(wall),
        "client_cpu_all": [round(x, 1) for x in cpu],
        "server_cpu_all": [round(x, 1) for x in srv_cpu],
        "pg_cpu_us": statistics.median(pg) if pg else None,
        "redis_cpu_us": statistics.median(redis) if redis else None,
        "server_rss_mb": _rss_per_worker(proc, 1),
    }


def _connection_counter(transport: str):
    """() -> connections the caller has opened so far."""
    if transport == "httpx":
        import httpcore._async.connection_pool as cp

        opened = [0]
        create = cp.AsyncConnectionPool.create_connection

        def counting(self, origin):  # noqa: ANN001, ANN202
            opened[0] += 1
            return create(self, origin)

        cp.AsyncConnectionPool.create_connection = counting
        return lambda: opened[0]
    from prometheus_client import REGISTRY

    def total() -> float:
        return sum(
            s.value
            for metric in REGISTRY.collect()
            if metric.name == "sandloom_rpc_client_connections_opened"
            for s in metric.samples
            if s.name.endswith("_total") and s.labels.get("transport") == transport
        )

    return total


def _sandloom_build() -> str:
    """The measured framework checkout (git rev of the sandloom package)."""
    import sandloom

    root = Path(sandloom.__file__).resolve().parents[3]
    try:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - an export without .git
        return (
            (root / "BUILD").read_text().strip() if (root / "BUILD").exists() else "?"
        )


if __name__ == "__main__":
    main()
