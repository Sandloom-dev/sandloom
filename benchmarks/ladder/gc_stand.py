"""GC wall-clock stand: gunicorn + 2 uvicorn workers under fixed-rate load.

DIAGNOSTIC ONLY. The cachegrind ladder counts instructions and cannot see GC
PAUSES: a full collection is a few hundred ms of one worker doing nothing
else, which shows up as tail latency and event-loop lag, not as a mean. This
stand runs the SHIPPED gunicorn config (``sandloom.core.cli.gunicorn_conf``,
wrapped by ``ladder/gc_stand_conf.py`` for the per-worker GC probe) with the
S5 production env (tenant RLS role, strict revocation, Streams bus, relay on
in the elected worker) and drives open-loop (coordinated-omission corrected)
windows:

* ``plan`` — ``GET /s5plan/{id}`` (declared ReadOperation, general executor)
* ``s5``   — ``GET /s5/{id}`` (ORM ``DbSession.get``)
* ``f``    — ``POST /dw/7`` (class-F write: strict revocation, RLS insert,
  event + audit, fail-closed idempotency; a unique ``Idempotency-Key`` each)
* ``soak`` — the three mixed 4:4:2 for ``--soak`` seconds (RSS growth)

Per window it reports client p50/p99/p99.9, per-generation collection counts,
pause sums / max / p99 (elapsed, ``perf_counter``) and GC thread-CPU, the GC
share of worker CPU, and worker RSS at the window edges.

    source <env with DATABASE_URL/CACHE_URL/BROKER_URL/S5_APP_ROLE>
    python -m benchsuite.setup_db            # once, as the DB owner
    uv run python ladder/gc_stand.py --variants baseline,freeze \\
        --rates plan=900,s5=700,f=300 --seconds 30 --soak 120 --reps 2

Variants (``VARIANTS``) are environment overlays for the server: ``GCSTAND_*``
(stand-local, see ``gc_stand_conf.py``) or the framework's ``GC_*`` settings.
Results append to ``ladder/results/gc_stand.jsonl``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import psutil

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
sys.path.insert(0, str(BENCH))
from benchsuite import common, s5  # noqa: E402
from benchsuite import loadclient as lc  # noqa: E402

VARIANTS: dict[str, dict[str, str]] = {
    "baseline": {},
    "freeze_early": {"GCSTAND_FREEZE": "early"},
    "freeze": {"GCSTAND_FREEZE": "startup"},
    "thr10k": {"GCSTAND_THRESHOLDS": "10000,10,10"},
    "thr50k": {"GCSTAND_THRESHOLDS": "50000,20,10"},
    "freeze_thr2k": {"GCSTAND_FREEZE": "startup", "GCSTAND_THRESHOLDS": "2000,10,10"},
    "freeze_thr10k": {"GCSTAND_FREEZE": "startup", "GCSTAND_THRESHOLDS": "10000,10,10"},
    "freeze_thr10k_100": {
        "GCSTAND_FREEZE": "startup",
        "GCSTAND_THRESHOLDS": "10000,10,100",
    },
    "freeze_thr50k": {"GCSTAND_FREEZE": "startup", "GCSTAND_THRESHOLDS": "50000,20,10"},
    "freeze_thr50k_100": {
        "GCSTAND_FREEZE": "startup",
        "GCSTAND_THRESHOLDS": "50000,50,100",
    },
    "freeze_periodic": {"GCSTAND_FREEZE": "startup", "GCSTAND_PERIODIC": "0.5,30"},
    # the KERNEL path (sandloom.core.gc_tuning via the shipped gunicorn hook);
    # run with --sandloom-src pointing at a checkout that has it
    "kernel_off": {
        "GC_FREEZE_AFTER_STARTUP": "false",
        "GC_THRESHOLDS": "",
        "GC_METRICS_ENABLED": "false",
    },
    "kernel_freeze_only": {"GC_THRESHOLDS": ""},
    "kernel_default": {},
    "kernel_10k": {"GC_THRESHOLDS": "10000,10,100"},
}
for _spec in os.environ.get("GCSTAND_EXTRA_VARIANTS", "").split(";"):
    # name=K=V,K=V ; ...  (ad-hoc variants without editing this file)
    if "=" in _spec:
        _name, _rest = _spec.split("=", 1)
        VARIANTS[_name.strip()] = dict(kv.split("=", 1) for kv in _rest.split("|"))

IDS = common.S5_ROWS_PER_TENANT


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Workload(lc.Workload):
    """Reads with ids 1..5000 (tenant A); F writes with a unique key each."""

    def __init__(self, kind: str, bearer: str, prefix: str) -> None:
        self.kind = kind
        self.auth = f"Authorization: Bearer {bearer}\r\n"
        self.prefix = prefix
        super().__init__([lc.Entry("GET", "/", "", frozenset([200]))])

    def _kind(self, i: int) -> str:
        if self.kind != "soak":
            return self.kind
        return ("plan", "s5", "plan", "s5", "f")[i % 5]

    def request(self, i: int) -> tuple[bytes, frozenset[int]]:
        kind = self._kind(i)
        if kind == "f":
            headers = self.auth + f"Idempotency-Key: {self.prefix}-{i}\r\n"
            body = f'{{"name":"gc-{self.prefix}-{i}","value":7}}'
            return lc._raw("POST", "/dw/7", body, i, headers), frozenset([201])
        route = "/s5plan/" if kind == "plan" else "/s5/"
        return lc._raw("GET", f"{route}{i % IDS + 1}", "", i, self.auth), frozenset(
            [200]
        )


class _RssSampler(threading.Thread):
    def __init__(self, procs: list[psutil.Process]) -> None:
        super().__init__(daemon=True)
        self.procs = procs
        self.samples: list[tuple[float, list[float]]] = []
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(1.0):
            try:
                rss = [p.memory_info().rss / 2**20 for p in self.procs]
            except psutil.Error:
                continue
            self.samples.append((time.time(), rss))


def _wait_ready(port: int, bearer: str, deadline: float) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/s5plan/1",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.3)
    raise SystemExit("server did not become ready")


def _cpu(procs: list[psutil.Process]) -> list[float]:
    out = []
    for p in procs:
        t = p.cpu_times()
        out.append(t.user + t.system)
    return out


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _gc_window(dumps: list[dict[str, Any]], t0: float, t1: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    cpu_total = 0.0
    for gen in (0, 1, 2):
        walls: list[float] = []
        cpus: list[float] = []
        for d in dumps:
            ev = d["events"]
            for t, w, c, g in zip(
                ev["t_end"], ev["wall"], ev["cpu"], ev["gen"], strict=True
            ):
                if g == gen and t0 <= t < t1:
                    walls.append(w)
                    cpus.append(c)
        cpu_total += sum(cpus)
        out[f"gen{gen}"] = {
            "n": len(walls),
            "pause_sum_ms": round(sum(walls) * 1e3, 1),
            "pause_mean_ms": round(statistics.fmean(walls) * 1e3, 3) if walls else None,
            "pause_p99_ms": round((_pct(walls, 0.99) or 0) * 1e3, 3),
            "pause_max_ms": round(max(walls) * 1e3, 3) if walls else None,
            "cpu_s": round(sum(cpus), 3),
            # thread CPU per collection: immune to the OS descheduling the
            # worker mid-collection (wall >> CPU on a shared, loaded host)
            "cpu_mean_ms": round(statistics.fmean(cpus) * 1e3, 3) if cpus else None,
            "cpu_p99_ms": round((_pct(cpus, 0.99) or 0) * 1e3, 3),
            "cpu_max_ms": round(max(cpus) * 1e3, 3) if cpus else None,
        }
    out["gc_cpu_s"] = round(cpu_total, 3)
    return out


def run_variant(
    name: str, a: argparse.Namespace, rates: dict[str, float], rep: int
) -> list[dict[str, Any]]:
    port = _free_port()
    probe_dir = tempfile.mkdtemp(prefix=f"gcstand-{name}-")
    bearer = s5.mint()
    env = {
        **os.environ,
        **s5.server_env("plan"),
        "BENCH_WRITE_DIAG": "1",
        # the Oracle qualification stand's shape (benchmarks/qualification/compose.yml)
        "SANDLOOM_MODULES": a.modules,
        "MODULE_REQUIRED": a.modules,
        "TWOFA_ENCRYPTION_KEY": os.environ.get(
            "TWOFA_ENCRYPTION_KEY", "gcstand-2fa-key-0123456789abcdef0123456789abcdef"
        ),
        "INTERNAL_API_TOKEN": "gcstand-internal-token-0123456789abcdef0123456789",
        "AUTH_REVOCATION_FAIL_CLOSED": "true",
        "SANDLOOM_ACCEPTED_RISKS": "local_storage_in_prod",
        "DB_POOL_SIZE": "5",
        "DB_MAX_OVERFLOW": "5",
        "CONTRACT_DB_POOL_SIZE": "2",
        "CONTRACT_DB_MAX_OVERFLOW": "2",
        "OUTBOX_RELAY_INTERVAL_SECONDS": "1",
        "WEB_CONCURRENCY": str(a.workers),
        "GUNICORN_BIND": f"127.0.0.1:{port}",
        "GCSTAND_OUT": probe_dir,
        # --sandloom-src: a SNAPSHOT of src, so editing the checkout while
        # a long matrix runs never changes the code later variants measure
        "PYTHONPATH": os.pathsep.join(
            [*([a.sandloom_src] if a.sandloom_src else []), str(HERE), str(BENCH)]
        ),
        "PYTHONUNBUFFERED": "1",
        **VARIANTS[name],
    }
    for k in list(env):
        if k.startswith(("GCSTAND_", "GC_")) and k not in VARIANTS[name]:
            if k not in ("GCSTAND_OUT", "GCSTAND_EXTRA_VARIANTS"):
                env.pop(k)
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    gunicorn = str(Path(sys.executable).with_name("gunicorn"))
    srv = subprocess.Popen(
        [gunicorn, "-c", "python:gc_stand_conf", "benchsuite.apps.sandloom_app:app"],
        env=env,
        cwd=BENCH,
        stdout=subprocess.DEVNULL if not a.verbose else None,
        stderr=subprocess.DEVNULL if not a.verbose else None,
    )
    rows: list[dict[str, Any]] = []
    try:
        _wait_ready(port, bearer, time.time() + 90)
        master = psutil.Process(srv.pid)
        workers = master.children()
        if len(workers) != a.workers:
            time.sleep(2)
            workers = master.children()
        sampler = _RssSampler(workers)
        sampler.start()
        prefix = uuid.uuid4().hex[:10]
        ep = [("127.0.0.1", port)]
        windows: list[
            tuple[
                str,
                float,
                float,
                Any,
                list[float],
                list[float],
                list[float],
                list[float],
                float,
            ]
        ] = []
        import uvloop

        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as r:
            warm = _Workload("soak", bearer, prefix + "w")
            r.run(lc.open_loop(ep, warm, a.warm_rate, a.warmup, pool=a.pool))
            kinds = [k for k in ("plan", "s5", "f") if k in rates]
            if a.soak:
                kinds.append("soak")
            start = 10**6
            for kind in kinds:
                rate = rates.get(kind) or rates["soak"]
                seconds = a.soak if kind == "soak" else a.seconds
                wl = _Workload(kind, bearer, prefix)
                rss0 = [p.memory_info().rss / 2**20 for p in workers]
                c0 = _cpu(workers)
                t0 = time.time()
                st = r.run(
                    lc.open_loop(ep, wl, rate, seconds, start=start, pool=a.pool)
                )
                t1 = time.time()
                load = os.getloadavg()
                c1 = _cpu(workers)
                rss1 = [p.memory_info().rss / 2**20 for p in workers]
                start += 10**6
                windows.append((kind, t0, t1, st, c0, c1, rss0, rss1, rate, load))
                time.sleep(a.settle)
        sampler.stop.set()
    finally:
        srv.send_signal(signal.SIGTERM)
        try:
            srv.wait(60)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait()
    dumps = [json.loads(p.read_text()) for p in sorted(Path(probe_dir).glob("*.json"))]
    if len(dumps) != a.workers:
        print(f"WARNING: {len(dumps)} probe dumps for {a.workers} workers", flush=True)
    for kind, t0, t1, st, c0, c1, rss0, rss1, rate, load in windows:
        gcw = _gc_window(dumps, t0, t1)
        worker_cpu = sum(c1) - sum(c0)
        row = {
            "variant": name,
            "env": VARIANTS[name],
            "rep": rep,
            "window": kind,
            "rate": rate,
            "seconds": round(t1 - t0, 2),
            "t0": t0,
            "t1": t1,
            "probe_dir": probe_dir,
            # host load at the window's end: wall-clock tails on a shared
            # host track it (filter with --max-load / at analysis time)
            "loadavg": load,
            "python": sys.version.split()[0],
            "sandloom_src": a.sandloom_src or "checkout",
            **lc.open_result(st),
            "worker_cpu_s": [round(b - x, 2) for x, b in zip(c0, c1, strict=True)],
            "cpu_us_per_ok": round(worker_cpu / max(st.ok, 1) * 1e6, 1),
            "gc_cpu_us_per_ok": round(gcw["gc_cpu_s"] / max(st.ok, 1) * 1e6, 1),
            "gc_share_of_cpu": round(gcw["gc_cpu_s"] / worker_cpu, 4)
            if worker_cpu
            else None,
            "rss_mb": [[round(x, 1) for x in rss0], [round(x, 1) for x in rss1]],
            **gcw,
        }
        row.pop("latencies", None)
        rows.append(row)
    if rows:
        rows[-1]["rss_series_mb"] = (
            [
                [round(t - sampler.samples[0][0], 1), [round(x, 1) for x in rss]]
                for t, rss in sampler.samples[:: max(1, len(sampler.samples) // 60)]
            ]
            if sampler.samples
            else []
        )
        rows[-1]["probe"] = [
            {
                k: d.get(k)
                for k in ("after_import", "after_startup", "at_exit", "maxrss")
            }
            for d in dumps
        ]
    return rows


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--variants", default="baseline,freeze")
    p.add_argument("--rates", default="plan=900,s5=700,f=300,soak=600")
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--soak", type=float, default=0)
    p.add_argument("--warmup", type=float, default=10)
    p.add_argument("--warm-rate", type=float, default=300)
    p.add_argument("--settle", type=float, default=2)
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--pool", type=int, default=256)
    p.add_argument("--out", default=str(HERE / "results" / "gc_stand.jsonl"))
    p.add_argument("--label", default="")
    p.add_argument("--modules", default='["sandloom_identity", "benchsuite.bench"]')
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--sandloom-src", default="", help="framework src dir to run")
    p.add_argument(
        "--max-load",
        type=float,
        default=0,
        help="wait (up to --max-load-wait s) for the 1-min load average to "
        "drop below this before each variant (shared hosts)",
    )
    p.add_argument("--max-load-wait", type=float, default=900)
    a = p.parse_args()
    rates = {k: float(v) for k, v in (kv.split("=") for kv in a.rates.split(","))}
    names = a.variants.split(",")
    for n in names:
        if n not in VARIANTS:
            raise SystemExit(f"unknown variant {n!r}; one of {sorted(VARIANTS)}")
    for rep in range(a.reps):
        # alternate the order every repetition (drift / thermal / neighbours)
        order = names if rep % 2 == 0 else list(reversed(names))
        for name in order:
            while os.path.exists("/tmp/sandloom-ci-local.lock"):  # local CI owns the CPUs
                time.sleep(15)
            deadline = time.time() + a.max_load_wait
            while a.max_load and os.getloadavg()[0] > a.max_load:
                if time.time() > deadline:
                    print(f"WARNING: load {os.getloadavg()[0]:.1f} > {a.max_load}")
                    break
                time.sleep(10)
            rows = run_variant(name, a, rates, rep)
            with open(a.out, "a") as fh:
                for row in rows:
                    row["label"] = a.label
                    fh.write(json.dumps(row) + "\n")
            for row in rows:
                print(
                    f"{name:>18} r{rep} {row['window']:>4} @{row['rate']:.0f}: "
                    f"ok={row['ok']} err={row['errors']} "
                    f"p50={row['p50_ms']} p99={row['p99_ms']} p999={row['p999_ms']} "
                    f"| gc0={row['gen0']['n']} gc1={row['gen1']['n']} "
                    f"gc2={row['gen2']['n']} g2max={row['gen2']['pause_max_ms']}ms "
                    f"gcshare={row['gc_share_of_cpu']} cpu/ok={row['cpu_us_per_ok']} "
                    f"rss={row['rss_mb'][1]}",
                    flush=True,
                )


if __name__ == "__main__":
    main()
