"""Tail attribution of the class-F write (``/dw/7``) under a fixed arrival rate.

DIAGNOSTIC ONLY. Averages say where CPU goes; this stand says where a SLOW
request waits. It serves the S5 write stand like the Oracle qualification
does — gunicorn with the framework's config, 2 workers in a 2-CPU quota
container, ``DB_POOL_SIZE=5`` / ``DB_MAX_OVERFLOW=5``, the outbox relay on
(prod), a separate security Redis and broker Redis with
``appendonly yes / appendfsync always`` (its own containers, started here) —
with ``ladder/tail_probe.py`` timing every wait of every request (pool
checkout, BEGIN, statements, COMMIT, each Redis instance, event-loop lag).

Run (from ``benchmarks/``; ``DATABASE_URL`` = the stand's OWNER DSN, the app
logs in as ``S5_APP_ROLE``; ``python -m benchsuite.setup_db`` first)::

    # the stand's own PostgreSQL on the stand network (never a shared one)
    docker run -d --rm --name sandloom-wltail-pg -e POSTGRES_USER=... \\
        -e POSTGRES_PASSWORD=... -p 127.0.0.1:5442:5432 postgres:16-alpine
    docker network create sandloom-wltail-net
    docker network connect sandloom-wltail-net sandloom-wltail-pg
    DATABASE_URL=postgresql+asyncpg://...@localhost:5442/sandloom_bench_tail \\
        uv run --frozen python -m benchsuite.setup_db
    uv run --frozen python ladder/write_tail.py run --out /tmp/tail \\
        --pg-host sandloom-wltail-pg:5432 --redis disk --fractions 0.55,0.8 \\
        [--backend <snapshot>] [--split-idempotency]
    uv run --frozen python ladder/write_tail.py report /tmp/tail

The load client runs in a container on the same network (Docker Desktop's
host port forwarding would add milliseconds to every request). ``--redis``
picks the security / broker durability: ``disk`` (Oracle: AOF, appendfsync
always), ``tmpfs`` (the same, fsync ~free) or ``none``.

``run`` measures the capacity (closed loop, 64 connections), then open-loop
phases at the given fractions of it, each request tagged so the server
records split by phase. ``report`` prints, per phase: client and server
latency quantiles, the mean of each wait over all requests, over the median
band (p45-p55) and over the slowest 1 % (>= p99) — which phase grows in the
tail — and the post-commit dispatcher's rounds.
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
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
IMAGE = "sandloom-ladder-cg:3.13"
APP = "sandloom-wltail-app"
REDIS = {
    "security": ("sandloom-wltail-security", 6391),
    "broker": ("sandloom-wltail-broker", 6392),
    "cache": ("sandloom-wltail-cache", 6393),
    # only with --split-idempotency: idempotency records on their own
    # (also appendfsync always) Redis, away from the revocation / block marks
    "idem": ("sandloom-wltail-idem", 6394),
}
#: Every container of the stand shares this network: container-to-container
#: round trips (Docker Desktop's host port forwarding adds milliseconds).
NET = "sandloom-wltail-net"
PORT = 18330


def _docker_url(url: str, pg_host: str) -> str:
    """``url`` with its host:port replaced by ``pg_host`` (on :data:`NET`)."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    user = parts.netloc.rsplit("@", 1)[0]
    return urlunsplit(parts._replace(netloc=f"{user}@{pg_host}"))


def _network() -> None:
    subprocess.run(["docker", "network", "create", NET], capture_output=True)


def _start_redis(mode: str) -> None:
    """The security and broker Redis: ``disk`` = AOF + appendfsync always on
    the container's disk (Oracle's setting), ``tmpfs`` = the same on tmpfs
    (fsync ~free: isolates what fsync costs), ``none`` = no AOF."""
    _network()
    for role, (name, port) in REDIS.items():
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        args = ["docker", "run", "-d", "--rm", "--name", name, "--network", NET]
        args += ["-p", f"127.0.0.1:{port}:6379"]
        if role == "cache":  # the plain cache: no persistence (as on Oracle)
            args += ["redis:7-alpine", "redis-server", "--save", ""]
            subprocess.run(args, check=True, capture_output=True)
            continue
        if mode == "tmpfs":
            args += ["--tmpfs", "/data"]
        args += ["redis:7-alpine", "redis-server"]
        if mode == "none":
            args += ["--appendonly", "no", "--save", ""]
        else:
            args += ["--appendonly", "yes", "--appendfsync", "always"]
        args += ["--maxmemory", "256mb", "--maxmemory-policy", "noeviction"]
        subprocess.run(args, check=True, capture_output=True)


def _env(
    workers: int, pool: int, overflow: int, pg_host: str, split: bool = False
) -> dict[str, str]:
    sys.path.insert(0, str(HERE.parent))
    from benchsuite import writes

    env = dict(writes.server_env())
    infra = json.loads(env.get("INFRA") or "{}")
    infra["cache"] = {
        "security": {
            "driver": "redis",
            "url": f"redis://{REDIS['security'][0]}:6379/0",
        },
        "idem": {"driver": "redis", "url": f"redis://{REDIS['idem'][0]}:6379/0"},
    }
    infra["eventbus"] = {
        "default": {
            "driver": "streams",
            "url": f"redis://{REDIS['broker'][0]}:6379/0",
        }
    }
    env.update(
        {
            "DATABASE_URL": _docker_url(env["DATABASE_URL"], pg_host),
            "CACHE_URL": f"redis://{REDIS['cache'][0]}:6379/0",
            "BROKER_URL": f"redis://{REDIS['broker'][0]}:6379/1",
            "INFRA": json.dumps(infra),
            "SECURITY_CACHE_INSTANCE": "security",
            "AUTH_REVOCATION_FAIL_CLOSED": "true",
            "OUTBOX_RELAY_ENABLED": "auto",
            "OUTBOX_RELAY_INTERVAL_SECONDS": "1",
            "DB_POOL_SIZE": str(pool),
            "DB_MAX_OVERFLOW": str(overflow),
            "CONTRACT_DB_POOL_SIZE": "2",
            "CONTRACT_DB_MAX_OVERFLOW": "2",
            "WEB_CONCURRENCY": str(workers),
            "GUNICORN_BIND": "0.0.0.0:8000",
            "TAIL_OUT": "/out",
            "TAIL_IDEMPOTENCY_INSTANCE": "idem" if split else "",
            "PYTHONPATH": "/w/src:/w/benchmarks:/w/benchmarks/ladder",
        }
    )
    return env


def _start_app(out: Path, cpus: float, env: dict[str, str], backend: Path) -> None:
    subprocess.run(["docker", "rm", "-f", APP], capture_output=True)
    args = ["docker", "run", "-d", "--name", APP, f"--cpus={cpus}", "--memory=3g"]
    args += ["--network", NET]
    args += ["-p", f"127.0.0.1:{PORT}:8000"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    args += [
        "-v", f"{backend / 'src'}:/w/src:ro",
        "-v", f"{ROOT / 'benchmarks'}:/w/benchmarks:ro",
        "-v", f"{out}:/out",
        "-w", "/w/benchmarks",
        IMAGE, "gunicorn", "-c", "python:sandloom.core.cli.gunicorn_conf", "tail_probe:app",
    ]  # fmt: skip
    subprocess.run(args, check=True, capture_output=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2):
                return
        except Exception:  # noqa: BLE001 - starting (or 503 while warming)
            time.sleep(0.5)
    logs = subprocess.run(["docker", "logs", APP], capture_output=True, text=True)
    raise RuntimeError((logs.stdout + logs.stderr)[-4000:])


def _workload(label: str, token: str) -> Any:
    from benchsuite import loadclient as lc

    nonce = os.urandom(6).hex()

    class Financial(lc.Workload):
        def request(self, i: int) -> Any:
            body = json.dumps({"name": f"tail-{nonce}-{i}", "value": 7})
            return lc.Workload(
                [lc.Entry("POST", "/dw/7", body, frozenset([201]))],
                [
                    f"Authorization: Bearer {token}",
                    f"Idempotency-Key: {label}-{nonce}-{i}",
                    f"x-tail-label: {label}",
                ],
            ).request(i)

    return Financial([lc.Entry("POST", "/dw/7", "", frozenset([201]))])


def _cpu_stat() -> dict[str, int]:
    out = subprocess.run(
        ["docker", "exec", APP, "cat", "/sys/fs/cgroup/cpu.stat"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {k: int(v) for k, v in (line.split() for line in out.splitlines())}


def _load_in_container(out: Path, *args: str) -> dict[str, Any]:
    """Run ``write_tail.py load`` in a container ON the stand network (the
    client never crosses Docker Desktop's host port forwarding)."""
    name = args[args.index("--label") + 1]
    subprocess.run(
        [
            "docker", "run", "--rm", "--network", NET,
            "-v", f"{ROOT / 'benchmarks'}:/w/benchmarks:ro",
            "-v", f"{out}:/out", "-w", "/w/benchmarks",
            "-e", "PYTHONPATH=/w/benchmarks",
            "-e", f"JWT_SECRET={os.environ.get('JWT_SECRET', '')}",
            IMAGE, "python", "ladder/write_tail.py", "load",
            "--target", f"{APP}:8000", "--json", f"/out/load-{name}.json", *args,
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return json.loads((out / f"load-{name}.json").read_text())


def load(a: argparse.Namespace) -> None:
    """(in the load container) one closed- or open-loop phase -> JSON."""
    sys.path.insert(0, str(HERE.parent))
    if not os.environ.get("JWT_SECRET"):
        os.environ.pop("JWT_SECRET", None)
    from benchsuite import loadclient as lc
    from benchsuite import s5

    host, port = a.target.rsplit(":", 1)
    ep = [(host, int(port))]
    wl = _workload(a.label, s5.mint())
    if a.rate:
        stats = asyncio.run(
            lc.open_loop(ep, wl, a.rate, a.seconds, pool=256, timeout=30)
        )
    else:
        stats = asyncio.run(lc.closed_loop(ep, wl, 0, a.n, a.conc, timeout=30))
    res = {
        **lc.open_result(stats),
        "ok": stats.ok,
        "errors": stats.errors,
        "elapsed": stats.elapsed,
        "client_latencies_ms": [round(x * 1e3, 3) for x in sorted(stats.latencies)],
    }
    Path(a.json).write_text(json.dumps(res) + "\n")


def run(a: argparse.Namespace) -> None:
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    _start_redis(a.redis)
    env = _env(a.workers, a.pool, a.overflow, a.pg_host, a.split_idempotency)
    _start_app(out, a.cpus, env, Path(a.backend).resolve())
    summary: dict[str, Any] = {"args": vars(a), "phases": {}}
    try:
        _load_in_container(out, "--label", "warm", "--n", "2000", "--conc", "32")
        before = _cpu_stat()
        cap = _load_in_container(
            out, "--label", "cap", "--n", str(a.cap_n), "--conc", "64"
        )
        after = _cpu_stat()
        capacity = cap["ok"] / cap["elapsed"]
        summary["capacity_rps"] = capacity
        summary["capacity_cpu"] = _cpu_delta(before, after, cap["elapsed"])
        print(f"capacity {capacity:.1f} RPS (closed loop, 64 connections)", flush=True)
        for frac in [float(f) for f in a.fractions.split(",")]:
            label = f"r{int(round(frac * 100))}"
            rate = capacity * frac
            before = _cpu_stat()
            res = _load_in_container(
                out,
                "--label", label, "--rate", f"{rate:.3f}", "--seconds", str(a.seconds),
            )  # fmt: skip
            after = _cpu_stat()
            res.update(
                rate=rate, fraction=frac, cpu=_cpu_delta(before, after, a.seconds)
            )
            summary["phases"][label] = res
            lat = res["client_latencies_ms"]
            print(
                f"{label}: rate {rate:.0f}/s ok {res['ok']} errors {res['errors']} "
                f"p50 {_q(lat, 0.5)} ms p99 {_q(lat, 0.99)} ms cpu {res['cpu']}",
                flush=True,
            )
            time.sleep(3)  # let the outbox drain between phases
    finally:
        subprocess.run(["docker", "stop", "-t", "30", APP], capture_output=True)
        logs = subprocess.run(["docker", "logs", APP], capture_output=True, text=True)
        (out / "app.log").write_text((logs.stdout + logs.stderr)[-200000:])
        subprocess.run(["docker", "rm", "-f", APP], capture_output=True)
        if not a.keep_redis:
            for name, _port in REDIS.values():
                subprocess.run(["docker", "stop", name], capture_output=True)
    (out / "client.json").write_text(json.dumps(summary) + "\n")


def _cpu_delta(
    before: dict[str, int], after: dict[str, int], seconds: float
) -> dict[str, float]:
    return {
        "cores": round((after["usage_usec"] - before["usage_usec"]) / seconds / 1e6, 3),
        "throttled_periods": after.get("nr_throttled", 0)
        - before.get("nr_throttled", 0),
        "periods": after.get("nr_periods", 0) - before.get("nr_periods", 0),
        "throttled_ms": round(
            (after.get("throttled_usec", 0) - before.get("throttled_usec", 0)) / 1e3, 1
        ),
    }


def _q(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


PHASES = (
    "pool",
    "pg_begin",
    "pg_stmt",
    "pg_commit",
    "redis:security",
    "redis:idem",
    "redis:broker",
    "redis:cache",
)


def _lag_blame(
    lags: dict[int, list[tuple[float, float]]],
    events: dict[int, list[tuple[float, str, float]]],
    spike: float = 0.02,
) -> dict[str, Any]:
    """Loop-lag samples, and what overlapped each spike (>= ``spike`` s):
    a GC collection or a relay pass that ended inside the stalled window."""
    all_lag = [lag for v in lags.values() for _t, lag in v]
    blame: dict[str, int] = {}
    spikes = 0
    for pid, samples in lags.items():
        evs = sorted(events.get(pid, []))
        for t, lag in samples:
            if lag < spike:
                continue
            spikes += 1
            hit = sorted(
                {
                    kind.split(":")[0] if kind.startswith("relay") else kind
                    for end, kind, dur in evs
                    if t - lag - 0.01 <= end <= t + 0.001
                    and not kind.startswith("relay_cpu")
                }
            )
            key = "+".join(hit) or "unexplained"
            blame[key] = blame.get(key, 0) + 1
    gc_stats: dict[str, list[float]] = {}
    for evs in events.values():
        for _end, kind, dur in evs:
            gc_stats.setdefault(kind, []).append(dur)
    return {
        "samples": len(all_lag),
        "p50_ms": round(_q(all_lag, 0.5) * 1e3, 3),
        "p99_ms": round(_q(all_lag, 0.99) * 1e3, 3),
        "max_ms": round(max(all_lag, default=0) * 1e3, 3),
        "spikes_ge_20ms": spikes,
        "spike_overlaps": dict(sorted(blame.items(), key=lambda kv: -kv[1])),
        "events": {
            k: {
                "n": len(v),
                "p50_ms": round(_q(v, 0.5) * 1e3, 3),
                "p99_ms": round(_q(v, 0.99) * 1e3, 3),
                "max_ms": round(max(v) * 1e3, 3),
            }
            for k, v in sorted(gc_stats.items())
        },
    }


def report(a: argparse.Namespace) -> dict[str, Any]:
    out = Path(a.dir)
    reqs: list[dict[str, Any]] = []
    dispatch: list[dict[str, Any]] = []
    lags: dict[int, list[tuple[float, float]]] = {}
    events: dict[int, list[tuple[float, str, float]]] = {}
    for f in sorted(out.glob("tail-*.jsonl")):
        pid = int(f.stem.split("-")[1])
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            if rec["kind"] == "req":
                rec["pid"] = pid
                reqs.append(rec)
            elif rec["kind"] == "dispatch":
                dispatch.append(rec)
            elif rec["kind"] == "events":
                events.setdefault(pid, []).extend(tuple(e) for e in rec["events"])
            else:
                lags.setdefault(pid, []).extend(tuple(s) for s in rec["samples"])
    for v in lags.values():
        v.sort()
    client = json.loads((out / "client.json").read_text())

    def lag_in(rec: dict[str, Any]) -> float:
        samples = lags.get(rec["pid"], [])
        t0, t1 = rec["t0"], rec["t0"] + rec["total"]
        worst = 0.0
        for t, lag in samples:  # small lists per run; linear is fine
            if t0 <= t <= t1 + 0.005:
                worst = max(worst, lag)
        return worst

    result: dict[str, Any] = {"capacity_rps": client.get("capacity_rps")}
    for label, ph in client["phases"].items():
        rs = [r for r in reqs if r["label"] == label and r["status"] == 201]
        totals = [r["total"] for r in rs]
        p45, p55, p99 = _q(totals, 0.45), _q(totals, 0.55), _q(totals, 0.99)
        band = [r for r in rs if p45 <= r["total"] <= p55]
        tail = [r for r in rs if r["total"] >= p99]

        def means(group: list[dict[str, Any]]) -> dict[str, float]:
            if not group:
                return {}
            row = {
                p: statistics.fmean(r["ph"].get(p, 0.0) for r in group) * 1e3
                for p in PHASES
            }
            row["total"] = statistics.fmean(r["total"] for r in group) * 1e3
            waits = sum(row[p] for p in PHASES)
            row["cpu_and_loop_queue"] = row["total"] - waits
            row["loop_lag_max"] = statistics.fmean(lag_in(r) for r in group) * 1e3
            return {k: round(v, 3) for k, v in row.items()}

        cl = ph["client_latencies_ms"]
        result[label] = {
            "rate": round(ph["rate"], 1),
            "ok": ph["ok"] if "ok" in ph else len(cl),
            "cpu": ph.get("cpu"),
            "client_ms": {
                "p50": _q(cl, 0.5),
                "p90": _q(cl, 0.9),
                "p99": _q(cl, 0.99),
                "max": max(cl) if cl else None,
            },
            "server_ms": {
                "p50": round(_q(totals, 0.5) * 1e3, 3),
                "p90": round(_q(totals, 0.9) * 1e3, 3),
                "p99": round(p99 * 1e3, 3),
            },
            "mean_all": means(rs),
            "median_band": means(band),
            "slowest_1pct": means(tail),
            "phase_p99_ms": {
                p: round(_q([r["ph"].get(p, 0.0) for r in rs], 0.99) * 1e3, 3)
                for p in PHASES
            },
        }
    result["loop_lag"] = _lag_blame(lags, events)
    if dispatch:
        tot = [d["total"] for d in dispatch]
        result["dispatch"] = {
            "rounds": len(dispatch),
            "jobs_per_round_mean": statistics.fmean(d["jobs"] for d in dispatch),
            "round_ms_p50": round(_q(tot, 0.5) * 1e3, 3),
            "round_ms_p99": round(_q(tot, 0.99) * 1e3, 3),
            "mean_phases_ms": {
                p: round(
                    statistics.fmean(d["ph"].get(p, 0.0) for d in dispatch) * 1e3, 3
                )
                for p in PHASES
            },
        }
    print(json.dumps(result, indent=1))
    (out / "report.json").write_text(json.dumps(result, indent=1) + "\n")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--fractions", default="0.55,0.8")
    r.add_argument("--seconds", type=float, default=30)
    r.add_argument("--cap-n", type=int, default=6000)
    r.add_argument("--cpus", type=float, default=2)
    r.add_argument("--workers", type=int, default=2)
    r.add_argument("--pool", type=int, default=5)
    r.add_argument("--overflow", type=int, default=5)
    r.add_argument("--keep-redis", action="store_true")
    r.add_argument("--redis", choices=["disk", "tmpfs", "none"], default="disk")
    r.add_argument(
        "--split-idempotency",
        action="store_true",
        help="EXPERIMENT: idempotency records on their own Redis (probe patch)",
    )
    r.add_argument(
        "--pg-host",
        required=True,
        help="host:port of the PostgreSQL container on the stand network "
        f"({NET}; docker network connect {NET} <container>)",
    )
    r.add_argument(
        "--backend",
        default=str(ROOT),
        help="the repository root to serve (a snapshot for a before/after run)",
    )
    ld = sub.add_parser("load", help="(inside the load container)")
    ld.add_argument("--target", required=True)
    ld.add_argument("--label", required=True)
    ld.add_argument("--json", required=True)
    ld.add_argument("--rate", type=float, default=0)
    ld.add_argument("--seconds", type=float, default=30)
    ld.add_argument("--n", type=int, default=2000)
    ld.add_argument("--conc", type=int, default=64)
    rep = sub.add_parser("report")
    rep.add_argument("dir")
    a = p.parse_args()
    if a.cmd == "run":
        run(a)
        report(argparse.Namespace(dir=a.out))
    elif a.cmd == "load":
        load(a)
    else:
        report(a)


if __name__ == "__main__":
    main()
