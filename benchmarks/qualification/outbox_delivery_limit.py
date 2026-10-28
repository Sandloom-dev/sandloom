"""Outbox delivery limit on the isolated Oracle qualification deployment.

Ramps FIXED arrival rates of the class-F diagnostic write ``/dw/7`` (strict
revocation, fail-closed ``idempotency_guard``, one insert + ``publish_event``
+ ``record_audit``; every request a unique ``Idempotency-Key``) and, per rate,
samples the transactional outbox and the Redis Streams bus while the load runs
and while it drains:

* pending outbox rows (undelivered, not dead), dead rows, oldest pending age;
* the event stream's length (XLEN), its consumer groups' pending / lag and the
  Streams DLQ length (``<channel>.dlq``);
* the app container's CPU (cgroup ``cpu.stat``) over the load window.

A rate is SUSTAINABLE when the outbox does not grow during the second half of
the load (slope <= 2 % of the rate), successful goodput is >= 98 % of the
offered rate, the pending count at the end of the load
is at most two seconds of events, the queue drains within ``--drain-timeout``
and no row was dead-lettered / no DLQ entry appeared. The highest sustainable
rate is the delivery limit ("knee"). Run it on the CURRENT image ("before"),
deploy the candidate image, run it again with another ``--label`` ("after").

It never reconfigures, restarts or reseeds anything: it only sends the
synthetic workload to the isolated fixture and reads PostgreSQL / Redis
through ``docker exec`` (read-only commands). Same host lock as
``oracle_perf.py`` so the two never overlap::

    sudo python3 benchmarks/qualification/outbox_delivery_limit.py \\
      --fixture $FIXTURE/benchmarks/qualification \\
      --source $SOURCE \\
      --selected $FIXTURE/.state/selected-compose-files.txt \\
      --out $OUTPUT/outbox-limit-before --label before \\
      --rates 150,200,250,300,350,400,450 --seconds 60

``--bearer-file`` (a file holding a synthetic qualification JWT) skips the
token minting through the fixture's ``run.token()``. Output: ``results.jsonl``
(one record per rate, with every sample) and ``summary.md`` (the table).
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

PROJECT = "sandloom-release-qualification"
APP = PROJECT + "-app-1"
DB_CONTAINER = PROJECT + "-db-1"
EVENT_CHANNEL = "bench.item.created.v1"
ROUTE = "/dw/7"


def command(args, **kwargs):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, **kwargs
    ).stdout.strip()


def app_environment():
    data = json.loads(command(["docker", "inspect", APP]))[0]
    if data["Config"]["Labels"].get("com.docker.compose.project") != PROJECT:
        raise RuntimeError(
            "refusing a container outside the isolated qualification project"
        )
    return dict(s.split("=", 1) for s in data["Config"]["Env"]), data


def bus_location(env):
    """(container, db index, key prefix) of the Streams bus the app publishes to."""
    url = None
    try:
        url = json.loads(env.get("INFRA") or "{}")["eventbus"]["default"]["url"]
    except (KeyError, TypeError, ValueError):
        url = None
    url = url or env.get("CACHE_URL") or "redis://cache:6379/0"
    parts = urlsplit(url)
    db = (parts.path or "/0").lstrip("/") or "0"
    return f"{PROJECT}-{parts.hostname}-1", db, env.get("EVENT_STREAM_PREFIX", "")


class Probe:
    """Read-only samplers (``docker exec`` psql / redis-cli)."""

    def __init__(self, args, env):
        self.args = args
        self.redis_container, self.redis_db, prefix = bus_location(env)
        self.stream = prefix + args.channel
        self.dlq = self.stream + ".dlq"

    def sql(self, query):
        return command(
            [
                "docker",
                "exec",
                DB_CONTAINER,
                "psql",
                "-U",
                self.args.db_user,
                "-d",
                self.args.database,
                "-At",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                query,
            ]
        )

    def redis(self, *argv):
        return command(
            [
                "docker",
                "exec",
                self.redis_container,
                "redis-cli",
                "-n",
                self.redis_db,
                "--raw",
                *argv,
            ]
        )

    def groups(self):
        """Consumer groups of the stream: {name: {pending, lag}}."""
        try:
            raw = self.redis("XINFO", "GROUPS", self.stream).splitlines()
        except subprocess.CalledProcessError:
            return {}
        # --raw prints each group as flat "key\nvalue" pairs starting at "name"
        out, cur = {}, {}
        for key, value in zip(raw[::2], raw[1::2], strict=False):
            if key == "name":
                cur = {}
                out[value] = cur
            cur[key] = value
        return {
            name: {
                "pending": int(g.get("pending", 0) or 0),
                "lag": int(g["lag"])
                if g.get("lag", "").lstrip("-").isdigit()
                else None,
            }
            for name, g in out.items()
        }

    def sample(self):
        outbox = json.loads(
            self.sql(
                """SELECT json_build_object(
            'pending',count(*) FILTER (WHERE delivered_at IS NULL AND dead_at IS NULL),
            'dead',count(*) FILTER (WHERE dead_at IS NOT NULL),
            'total',count(*),
            'oldest_pending_s',COALESCE(EXTRACT(epoch FROM (now() AT TIME ZONE 'utc')
                -(min(emitted_at) FILTER (WHERE delivered_at IS NULL AND dead_at IS NULL))),0))
            FROM outbox_messages"""
            )
        )
        outbox["xlen"] = int(self.redis("XLEN", self.stream) or 0)
        outbox["dlq"] = int(self.redis("XLEN", self.dlq) or 0)
        outbox["groups"] = self.groups()
        outbox["at_epoch"] = time.time()
        return outbox


class Monitor:
    def __init__(self, probe, every):
        self.probe, self.every = probe, every
        self.samples = []
        self.stop = threading.Event()
        self.error = None

    def __enter__(self):
        self.samples.append(self.probe.sample())
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def run(self):
        while not self.stop.wait(self.every):
            try:
                self.samples.append(self.probe.sample())
            except Exception as e:  # noqa: BLE001 - recorded, invalidates the cell
                self.error = f"{type(e).__name__}: {e}"
                break

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()


def app_cpu_usec(data):
    pid = data["State"]["Pid"]
    relative = Path(f"/proc/{pid}/cgroup").read_text().split("0::", 1)[1].strip()
    stat = Path("/sys/fs/cgroup") / relative.lstrip("/") / "cpu.stat"
    return int(
        dict(line.split() for line in stat.read_text().splitlines())["usage_usec"]
    )


def slope(points):
    if len(points) < 2:
        return 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs) or 1.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den


def sustainable(record):
    """A drained queue alone cannot prove the offered arrival rate was held.

    open_loop waits for every scheduled request, including those queued for a
    free connection. Above capacity it can finish without HTTP errors while
    taking much longer than the arrival window. Allow 2% timing noise, but
    never call that overloaded window sustainable.
    """
    rate = record["rate"]
    return bool(
        not record["errors"]
        and not record["monitor_error"]
        and record["goodput_rps"] >= 0.98 * rate
        and record["drain_s"] is not None
        and record["pending_slope_per_s"] <= max(1.0, 0.02 * rate)
        and record["pending_at_load_end"] <= max(20, 2 * rate)
        and record["dead_delta"] == 0
        and record["dlq_delta"] == 0
    )


def bearer(args):
    if args.bearer_file:
        return Path(args.bearer_file).read_text().strip()
    selected = Path(args.selected).read_text().strip()
    os.environ["SANDLOOM_QUALIFICATION_ENV_FILES"] = str(
        Path(args.fixture).resolve() / ".state/https.env"
    )
    os.environ["SANDLOOM_QUALIFICATION_COMPOSE_FILES"] = selected
    sys.path.insert(0, str(Path(args.fixture).resolve()))
    import run  # the fixture's own helper: mints a synthetic tenant token

    return run.token()


def one_rate(args, probe, lc, token, rate):
    prefix = "outbox-limit-" + uuid.uuid4().hex[:10]

    class Financial(lc.Workload):
        def request(self, i):
            return lc.Workload(
                [
                    lc.Entry(
                        "POST",
                        ROUTE,
                        json.dumps({"name": f"{prefix}-{i}", "value": 7}),
                        frozenset([201]),
                    )
                ],
                [
                    "Authorization: Bearer " + token,
                    f"Idempotency-Key: {prefix}-{i}",
                ],
            ).request(i)

    workload = Financial([lc.Entry("POST", ROUTE, "", frozenset([201]))])
    _env, data = app_environment()
    with Monitor(probe, args.sample) as monitor:
        cpu0, t0 = app_cpu_usec(data), time.monotonic()
        stats = asyncio.run(
            lc.open_loop(
                [(args.host, args.port)],
                workload,
                rate,
                args.seconds,
                pool=args.pool,
                timeout=10,
            )
        )
        load_wall = time.monotonic() - t0
        app_cpu = (app_cpu_usec(data) - cpu0) / 1e6
        load_end = time.time()
        load_samples = len(monitor.samples)
        drained_at = None
        while time.time() - load_end < args.drain_timeout:
            last = monitor.samples[-1]
            prev = monitor.samples[-2] if len(monitor.samples) > 1 else None
            if (
                last["at_epoch"] > load_end
                and last["pending"] == 0
                and prev is not None
                and prev["total"] == last["total"]
            ):
                drained_at = last["at_epoch"]
                break
            time.sleep(0.5)
    samples = monitor.samples
    first, final = samples[0], samples[-1]
    start = first["at_epoch"]
    during = samples[:load_samples]
    half = [
        (s["at_epoch"] - start, s["pending"])
        for s in during
        if s["at_epoch"] - start >= args.seconds / 2
    ]
    trend = slope(half)
    pending_end = during[-1]["pending"] if during else 0
    groups_end = final.get("groups", {})
    record = {
        "section": "outbox-delivery-limit",
        "label": args.label,
        "request_prefix": prefix,
        "rate": rate,
        "seconds": args.seconds,
        **lc.open_result(stats),
        "goodput_rps": stats.ok / stats.elapsed if stats.elapsed else 0.0,
        "load_wall_s": load_wall,
        "app_cpu_cores": app_cpu / load_wall if load_wall else None,
        "app_cpu_ms_per_write": app_cpu / stats.ok * 1000 if stats.ok else None,
        "max_pending": max(s["pending"] for s in samples),
        "pending_at_load_end": pending_end,
        "pending_slope_per_s": trend,
        "oldest_pending_s_max": max(s["oldest_pending_s"] for s in samples),
        "drain_s": (drained_at - load_end) if drained_at else None,
        "dead_delta": final["dead"] - first["dead"],
        "dlq_delta": final["dlq"] - first["dlq"],
        "xlen_delta": final["xlen"] - first["xlen"],
        "group_pending_end": {k: v["pending"] for k, v in groups_end.items()},
        "group_lag_end": {k: v["lag"] for k, v in groups_end.items()},
        "monitor_error": monitor.error,
        "samples": samples,
    }
    record["sustainable"] = sustainable(record)
    return record


COLUMNS = [
    ("rate", "rate/s"),
    ("goodput_rps", "writes/s"),
    ("errors", "errors"),
    ("max_pending", "max pending"),
    ("pending_at_load_end", "pending@end"),
    ("pending_slope_per_s", "slope/s"),
    ("drain_s", "drain s"),
    ("dead_delta", "dead"),
    ("dlq_delta", "DLQ"),
    ("app_cpu_cores", "app cores"),
    ("app_cpu_ms_per_write", "app ms/write"),
    ("sustainable", "ok"),
]


def table(records):
    def fmt(v):
        if isinstance(v, float):
            return f"{v:.2f}" if v < 100 else f"{v:.0f}"
        return "-" if v is None else str(v)

    lines = [
        "| " + " | ".join(h for _k, h in COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
    ]
    for r in records:
        lines.append("| " + " | ".join(fmt(r.get(k)) for k, _h in COLUMNS) + " |")
    ok = [r["rate"] for r in records if r["sustainable"]]
    knee = max(ok) if ok else None
    lines.append("")
    lines.append(f"Delivery limit (highest sustainable rate): {knee}")
    return "\n".join(lines), knee


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixture", type=Path, help="fixture dir with run.py")
    p.add_argument("--source", required=True, type=Path, help="export with benchmarks/")
    p.add_argument("--selected", type=Path, help="original compose selection file")
    p.add_argument("--bearer-file", type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--label", required=True)
    p.add_argument("--rates", default="150,200,250,300,350,400,450")
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--drain-timeout", type=int, default=180)
    p.add_argument("--settle", type=int, default=15, help="pause between rates")
    p.add_argument("--sample", type=float, default=2.0)
    p.add_argument("--pool", type=int, default=128)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18222)
    p.add_argument("--database", default="qualification")
    p.add_argument("--db-user", default="qualification_owner")
    p.add_argument("--channel", default=EVENT_CHANNEL)
    a = p.parse_args()
    rates = [float(r) for r in a.rates.split(",") if r.strip()]
    if not rates or min(rates) <= 0 or a.seconds < 5:
        p.error("positive rates and --seconds >= 5 required")
    if not a.bearer_file and not (a.fixture and a.selected):
        p.error("--bearer-file or --fixture + --selected required")
    a.out.mkdir(parents=True, mode=0o700, exist_ok=True)
    if (a.out / "results.jsonl").exists():
        p.error("use a new result directory; measured cells are never overwritten")
    sys.path.insert(0, str(a.source.resolve() / "benchmarks"))
    from benchsuite import loadclient as lc

    with open("/tmp/sandloom-oracle-perf.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env, _data = app_environment()
        probe = Probe(a, env)
        probe.sample()  # fail fast: database / Redis reachable
        token = bearer(a)
        records = []
        for rate in rates:
            record = one_rate(a, probe, lc, token, rate)
            record["at"] = datetime.now(UTC).isoformat()
            records.append(record)
            with (a.out / "results.jsonl").open("a") as f:
                f.write(json.dumps(record) + "\n")
            (a.out / "results.jsonl").chmod(0o600)
            print(
                json.dumps({k: record.get(k) for k, _h in COLUMNS}),
                flush=True,
            )
            time.sleep(a.settle)
        text, knee = table(records)
        (a.out / "summary.md").write_text(
            f"# Outbox delivery limit ({a.label})\n\n{text}\n"
        )
        print(text, flush=True)
        if any(r["monitor_error"] for r in records):
            raise SystemExit("a sampler failed; see results.jsonl")
        if knee is None:
            raise SystemExit("no sustainable rate in this ramp")


if __name__ == "__main__":
    main()
