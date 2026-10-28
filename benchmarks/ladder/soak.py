"""Soak stand: merged BEGIN (``DB_RLS_MERGED_BEGIN``) ON vs OFF under faults.

DIAGNOSTIC ONLY. Two independent app instances (merged ON / OFF), each with
its OWN database and Redis db pair, run SIMULTANEOUSLY under the same load:
the secure S5 read (``/s5``), the planner read (``/s5plan``), a list page
(``/s5list``), a write with outbox + audit (``/dw/3``) and an UPDATE
(``/dw/4c``), for two tenants, with a deliberately tiny pool so every
connection is reused thousands of times. Faults:

* client-side aborts of 5-10 % of requests at random points (the server
  wrapper ``soak_app.py`` turns a disconnect into a real task cancellation);
* ``pg_terminate_backend`` of a random app connection of each database every
  ``--kill-every`` seconds.

Every ``--tick`` seconds the invariants are checked; the first violation
ABORTS the run with diagnostics (``pg_stat_activity``, server state, log
tail). At the end, after a quiescence period: pool drained, no idle-in-tx
session, the outbox fully delivered, and write atomicity (every write row has
exactly one outbox row and one audit row; every id a 201 returned exists for
the tenant that wrote it; counter sums within [acknowledged, acknowledged +
uncertain]).

    # once, as the DB owner (see STANDS.md "C. Soak")
    python ladder/soak.py setup
    # the soak (stops at the targets or --max-minutes)
    python ladder/soak.py run --out ladder/results/soak.jsonl
    # interleaved A/B without faults (normal pool): CPU (app + PG backends), p99
    python ladder/soak.py ab --out ladder/results/soak_ab.jsonl

Env: ``SOAK_SRV`` = the ``benchmarks/`` dir of a ``git archive`` export with
its own ``.venv`` (the measured server code), ``SOAK_PG`` (owner DSN prefix),
``SOAK_REDIS_PORT`` / ``SOAK_REDIS_CONTAINER`` / ``SOAK_PG_CONTAINER``,
``S5_APP_ROLE`` / ``S5_APP_PASSWORD``. The client half
(``soak.py client``) is spawned by ``run`` / ``ab``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

SRV = os.environ.get("SOAK_SRV", os.path.dirname(HERE))
SRV_PY = os.path.join(SRV, ".venv", "bin", "python")
PG = os.environ.get("SOAK_PG", "postgresql://test_user:test_password@localhost:5433/")
REDIS_PORT = int(os.environ.get("SOAK_REDIS_PORT", "6390"))
REDIS_CONTAINER = os.environ.get("SOAK_REDIS_CONTAINER", "sandloom_redis_bench")
PG_CONTAINER = os.environ.get("SOAK_PG_CONTAINER", "sandloom_postgres_test")
ROLE = os.environ.setdefault("S5_APP_ROLE", "soak_app")
PASSWORD = os.environ.setdefault(
    "S5_APP_PASSWORD", "soak-app-local-password-0123456789"
)

#: config -> (database, redis cache db, redis broker db, DB_RLS_MERGED_BEGIN, port)
CONFIGS = {
    "on": ("sandloom_test_soak_on", 10, 11, "true", 18710),
    "off": ("sandloom_test_soak_off", 12, 13, "false", 18711),
}

TENANTS = (
    "5e000000-0000-4000-8000-00000000000a",
    "5e000000-0000-4000-8000-00000000000b",
)
ITEMS = 5_000  # common.S5_ROWS_PER_TENANT
COUNTERS = 1_000  # setup_db.WRITE_COUNTERS_PER_TENANT
#: route -> weight. *_x are cross-tenant probes (must be 404).
MIX = {
    "s5": 30,
    "s5_x": 8,
    "plan": 10,
    "list": 7,
    "w3": 30,
    "w4": 10,
    "w4_x": 5,
}
WRITES = ("w3", "w4")
#: fault phases (cycled every --phase-seconds): (name, abort rate, delay as a
#: multiple of the route's rolling median latency, drawn uniformly in [lo, hi])
PHASES = (
    ("early", 0.07, (0.0, 0.6)),
    ("spread", 0.07, (0.0, 1.5)),
    ("heavy", 0.10, (0.2, 1.2)),
)

#: an ERROR log line of the server is EXPECTED iff it is caused by a
#: terminated / closed connection (the injected faults). Anything else aborts.
EXPECTED_ERRORS = re.compile(
    r"AdminShutdown|terminating connection due to administrator command|"
    r"ConnectionDoesNotExistError|connection was closed in the middle|"
    r"connection is closed|ConnectionResetError|underlying connection is closed|"
    r"CancelledError|"
    # a backend killed while its connection sat IDLE in the pool: asyncpg
    # reads the FATAL (57P01) and stays in ERROR_CONSUME (state 2) waiting for
    # a ReadyForQuery that never comes; the NEXT statement on it fails with
    # this, SQLAlchemy invalidates it. Seen equally with merged BEGIN ON and
    # OFF, never without terminations (soak diagnosis runs, STANDS.md).
    r"cannot switch to state \d+; another operation \(2\) is in progress"
)
#: matched against the record's message + the FINAL exception text (never the
#: stack frames: tenant_rls is on the stack of every first statement)
FATAL_ERRORS = re.compile(
    r"another operation \((?!2\))\d+\) is in progress|"
    r"already inside a transaction|already in transaction|QueuePool limit|"
    r"current transaction is aborted|InFailedSQLTransaction|NonTenantStatement|"
    r"MissingTenant|TenantMismatch|TenantRLSConfigError"
)


def _final_exception(exc_info: str) -> str:
    """The exception lines after the last stack frame of a traceback."""
    lines = exc_info.splitlines()
    tail: list[str] = []
    for line in reversed(lines):
        if line.startswith("  ") or line.startswith("Traceback"):
            break
        tail.append(line)
    return "\n".join(reversed(tail))


# ===================================================================== client


def _mint(tenant: str) -> str:
    from benchsuite import s5

    return s5.mint(tenant, hours=6)


class Client:
    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.rng = random.Random(a.seed)
        self.tokens = [_mint(t) for t in TENANTS]
        self.routes = list(MIX)
        self.weights = [MIX[r] for r in self.routes]
        self.stop = False
        self.t0 = time.time()
        self.status: Counter[str] = Counter()
        self.aborts: Counter[str] = Counter()
        self.conn_errors: Counter[str] = Counter()
        self.server_errors: Counter[str] = Counter()
        self.server_errors_by_status: Counter[int] = Counter()
        self.violations: list[str] = []
        self.violation_count = 0
        self.ok = 0
        self.lat: deque[tuple[float, str, float]] = deque()
        self.all_lat: dict[str, list[float]] = defaultdict(list)
        self.median: dict[str, float] = dict.fromkeys(MIX, 0.004)
        self.ids: dict[int, list[int]] = {0: [], 1: []}
        self.acked: Counter[str] = Counter()  # f"{route}:{tenant}" 2xx
        self.uncertain: Counter[str] = Counter()  # aborted / 5xx / dropped writes

    def phase(self) -> tuple[str, float, tuple[float, float]]:
        if self.a.cancel_rate == 0:
            return ("none", 0.0, (0.0, 0.0))
        idx = int((time.time() - self.t0) // self.a.phase_seconds) % len(PHASES)
        return PHASES[idx]

    def build(self, route: str, ti: int, i: int) -> tuple[bytes, Any]:
        own, other = ti, 1 - ti
        tok = self.tokens[ti]
        method, body, meta = "GET", b"", None
        if route == "s5":
            rid = own * ITEMS + self.rng.randint(1, ITEMS)
            path, meta = f"/s5/{rid}", rid
        elif route == "s5_x":
            path = f"/s5/{other * ITEMS + self.rng.randint(1, ITEMS)}"
        elif route == "plan":
            rid = own * ITEMS + self.rng.randint(1, ITEMS)
            path, meta = f"/s5plan/{rid}", rid
        elif route == "list":
            path = "/s5list?limit=20"
        elif route == "w3":
            method, path = "POST", "/dw/3"
            body = json.dumps({"name": f"s{os.urandom(5).hex()}", "value": i}).encode()
        elif route == "w4":
            method = "POST"
            path = f"/dw/4c/{own * COUNTERS + self.rng.randint(1, COUNTERS)}"
            body = b"{}"
        else:  # w4_x
            method = "POST"
            path = f"/dw/4c/{other * COUNTERS + self.rng.randint(1, COUNTERS)}"
            body = b"{}"
        head = (
            f"{method} {path} HTTP/1.1\r\nHost: soak\r\n"
            f"Authorization: Bearer {tok}\r\nContent-Length: {len(body)}\r\n"
        )
        if body:
            head += "Content-Type: application/json\r\n"
        return (head + "\r\n").encode() + body, meta

    def check(self, route: str, ti: int, meta: Any, status: int, body: bytes) -> None:
        key = f"{route}:{ti}"
        self.status[f"{route}:{status}"] += 1
        if status >= 500:
            self.server_errors[route] += 1
            self.server_errors_by_status[status] += 1
            if route in WRITES:
                self.uncertain[key] += 1
            return
        bad = None
        lo, hi = ti * ITEMS + 1, (ti + 1) * ITEMS
        if route in ("s5_x", "w4_x"):
            if status != 404:
                bad = f"cross-tenant {route} tenant={ti} -> {status} {body[:200]!r}"
        elif route in ("s5", "plan"):
            if status != 200 or json.loads(body)["id"] != meta:
                bad = f"{route} tenant={ti} id={meta} -> {status} {body[:200]!r}"
        elif route == "list":
            ids = (
                [it["id"] for it in json.loads(body)["items"]] if status == 200 else []
            )
            if status != 200 or not ids or any(not lo <= x <= hi for x in ids):
                bad = f"list tenant={ti} -> {status} ids={ids[:5]}..{ids[-3:]}"
        elif route == "w3":
            if status != 201:
                bad = f"w3 tenant={ti} -> {status} {body[:200]!r}"
            else:
                self.ids[ti].append(json.loads(body)["id"])
        elif route == "w4":
            if status != 200:
                bad = f"w4 tenant={ti} -> {status} {body[:200]!r}"
        if bad:
            self.violation_count += 1
            if len(self.violations) < 20:
                self.violations.append(bad)
        else:
            self.ok += 1
            if route in WRITES:
                self.acked[key] += 1

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", self.a.port)

    @staticmethod
    async def _read(r: asyncio.StreamReader) -> tuple[int, bytes, bool]:
        head = await r.readuntil(b"\r\n\r\n")
        status = int(head[9:12])
        low = head.lower()
        m = re.search(rb"content-length:\s*(\d+)", low)
        body = await r.readexactly(int(m.group(1))) if m else b""
        return status, body, b"connection: close" in low

    async def worker(self, wid: int) -> None:
        conn = None
        i = wid
        while not self.stop:
            i += self.a.conc
            route = self.rng.choices(self.routes, self.weights)[0]
            ti = self.rng.randrange(2)
            raw, meta = self.build(route, ti, i)
            name, rate, (lo, hi) = self.phase()
            delay = None
            if rate and self.rng.random() < rate:
                delay = self.rng.uniform(lo, hi) * self.median[route]
            try:
                if conn is None:
                    conn = await self._open()
                r, w = conn
                t = time.perf_counter()
                w.write(raw)
                try:
                    status, body, close = await asyncio.wait_for(
                        self._read(r), delay if delay is not None else 30.0
                    )
                except TimeoutError:
                    if delay is None:
                        raise
                    w.transport.abort()  # the client gives up mid-request
                    conn = None
                    self.aborts[name] += 1
                    if route in WRITES:
                        self.uncertain[f"{route}:{ti}"] += 1
                    continue
                ms = time.perf_counter() - t
            except (OSError, asyncio.IncompleteReadError, TimeoutError) as error:
                self.conn_errors[type(error).__name__] += 1
                if route in WRITES:
                    self.uncertain[f"{route}:{ti}"] += 1
                if conn is not None:
                    conn[1].transport.abort()
                conn = None
                await asyncio.sleep(0.01)
                continue
            if close:
                w.transport.abort()
                conn = None
            self.check(route, ti, meta, status, body)
            if status < 500:
                now = time.time()
                self.lat.append((now, route, ms))
                if self.a.keep_latencies:
                    self.all_lat[route].append(ms)
        if conn is not None:
            conn[1].close()

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        while self.lat and self.lat[0][0] < now - 5:
            self.lat.popleft()
        by: dict[str, list[float]] = defaultdict(list)
        for _t, route, ms in self.lat:
            by[route].append(ms)
        win = {}
        for route, xs in by.items():
            xs.sort()
            self.median[route] = xs[len(xs) // 2]
            win[route] = [
                round(xs[len(xs) // 2] * 1e3, 3),
                round(xs[min(len(xs) - 1, int(len(xs) * 0.99))] * 1e3, 3),
                len(xs),
            ]
        return {
            "t": round(now - self.t0, 1),
            "phase": self.phase()[0],
            "ok": self.ok,
            "status": dict(self.status),
            "aborts": dict(self.aborts),
            "aborts_total": sum(self.aborts.values()),
            "conn_errors": dict(self.conn_errors),
            "server_errors": dict(self.server_errors),
            "server_errors_total": sum(self.server_errors.values()),
            # 500 = a masked failure; 503 = DatabaseUnavailable (the request's
            # connection died mid-flight: outcome unknown, clean rollback)
            "server_errors_by_status": {
                str(k): v for k, v in self.server_errors_by_status.items()
            },
            "violation_count": self.violation_count,
            "violations": self.violations,
            "win_ms": win,
        }

    def final(self) -> dict[str, Any]:
        out = self.snapshot()
        out["ids"] = self.ids
        out["acked"] = dict(self.acked)
        out["uncertain"] = dict(self.uncertain)
        if self.a.keep_latencies:
            pooled = sorted(x for xs in self.all_lat.values() for x in xs)
            out["n"] = len(pooled)
            if pooled:
                out["p50_ms"] = round(pooled[len(pooled) // 2] * 1e3, 3)
                out["p99_ms"] = round(pooled[int(len(pooled) * 0.99)] * 1e3, 3)
        return out


def _dump(path: str, data: dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


async def _client_main(a: argparse.Namespace) -> None:
    c = Client(a)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: setattr(c, "stop", True))
    workers = [asyncio.ensure_future(c.worker(k)) for k in range(a.conc)]
    end = time.time() + a.duration if a.duration else None
    while not c.stop and (end is None or time.time() < end):
        await asyncio.sleep(1.0)
        _dump(a.stats, c.snapshot())
    c.stop = True
    await asyncio.wait(workers, timeout=40)
    _dump(a.stats, c.final())


def client(a: argparse.Namespace) -> None:
    import uvloop

    uvloop.run(_client_main(a))


# =============================================================== orchestrator


def _db_dsn(db: str) -> str:
    return f"{PG}{db}"


def server_env(cfg: str, *, pool: int, overflow: int, relay: str) -> dict[str, str]:
    """The S5 + write-stand production env of ``cfg`` (benchsuite.writes)."""
    db, rcache, rbroker, merged, _port = CONFIGS[cfg]
    saved = dict(os.environ)
    os.environ["DATABASE_URL"] = _db_dsn(db).replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    os.environ["CACHE_URL"] = f"redis://localhost:{REDIS_PORT}/{rcache}"
    os.environ["BROKER_URL"] = f"redis://localhost:{REDIS_PORT}/{rbroker}"
    try:
        from benchsuite import writes

        env = writes.server_env()
    finally:
        os.environ.clear()
        os.environ.update(saved)
    if "ssl=" not in env["DATABASE_URL"]:
        env["DATABASE_URL"] += "?ssl=disable"
    env.update(
        DB_RLS_MERGED_BEGIN=merged,
        DB_POOL_SIZE=str(pool),
        DB_MAX_OVERFLOW=str(overflow),
        DB_POOL_TIMEOUT="10",
        OUTBOX_RELAY_ENABLED=relay,
        SANDLOOM_ACCEPTED_RISKS="relay_disabled",
        EVENT_STREAM_MAXLEN="20000",
    )
    # A/B knobs for both servers: SOAK_SERVER_<SETTING>=value is passed on as
    # <SETTING>=value (e.g. SOAK_SERVER_DB_DEAD_CONNECTION_RETRY=false).
    env.update(
        {
            k.removeprefix("SOAK_SERVER_"): v
            for k, v in os.environ.items()
            if k.startswith("SOAK_SERVER_")
        }
    )
    return env


class Server:
    def __init__(self, cfg: str, logdir: str, **kw: Any) -> None:
        self.cfg = cfg
        self.port = CONFIGS[cfg][4]
        self.log = os.path.join(logdir, f"server_{cfg}.log")
        env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
            "PYTHONPATH": f"{SRV}:{HERE}",
            **server_env(cfg, **kw),
        }
        cmd = [
            SRV_PY, "-m", "uvicorn", "soak_app:app", "--host", "127.0.0.1",
            "--port", str(self.port), "--loop", "uvloop", "--http", "httptools",
            "--no-access-log", "--log-level", "warning",
        ]  # fmt: skip
        self.fh = open(self.log, "w")
        self.proc = subprocess.Popen(
            cmd, cwd=SRV, env=env, stdout=self.fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )  # fmt: skip
        self.log_pos = 0

    async def state(self) -> dict[str, Any]:
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            w.write(b"GET /_soak/state HTTP/1.1\r\nHost: s\r\n\r\n")
            _st, body, _c = await asyncio.wait_for(Client._read(r), 10)
            return json.loads(body)
        finally:
            w.close()

    async def wait_ready(self) -> dict[str, Any]:
        for _ in range(120):
            try:
                return await self.state()
            except OSError as error:
                if self.proc.poll() is not None:
                    raise SystemExit(f"server {self.cfg} died: {self.log}") from error
                await asyncio.sleep(0.5)
        raise SystemExit(f"server {self.cfg} not ready")

    def cpu(self) -> float:
        import psutil

        t = psutil.Process(self.proc.pid).cpu_times()
        return t.user + t.system

    def new_log(self) -> list[str]:
        with open(self.log, errors="replace") as fh:
            fh.seek(self.log_pos)
            data = fh.read()
            self.log_pos = fh.tell()
        return [x for x in data.splitlines() if x.strip()]

    def stop(self) -> None:
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)  # our own process group
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.fh.close()


def _classify(lines: list[str]) -> tuple[Counter[str], list[str]]:
    """Group server log records; return (buckets, unexpected records)."""
    buckets: Counter[str] = Counter()
    bad: list[str] = []
    for line in lines:
        try:
            rec = json.loads(line)
            level = rec.get("level", rec.get("levelname", ""))
            text = f"{rec.get('message', '')}\n" + _final_exception(
                str(rec.get("exc_info", ""))
            )
            msg = str(rec.get("message", rec.get("msg", "")))[:80]
        except ValueError:  # traceback lines in text mode / uvicorn output
            level, text, msg = ("ERROR" if "Error" in line else "INFO"), line, line[:80]
        if str(level).upper() not in ("ERROR", "CRITICAL", "WARNING"):
            continue
        if FATAL_ERRORS.search(text):
            buckets[f"FATAL {msg}"] += 1
            bad.append(text[:3000])
        elif EXPECTED_ERRORS.search(text):
            buckets[f"expected {str(level).upper()} {msg}"] += 1
        elif str(level).upper() == "WARNING":
            buckets[f"warning {msg}"] += 1
        else:
            buckets[f"UNEXPECTED {msg}"] += 1
            bad.append(text[:3000])
    return buckets, bad


async def _pg(db: str) -> Any:
    import asyncpg

    return await asyncpg.connect(_db_dsn(db))


async def _reset(cfg: str) -> None:
    db, rc, rb, _m, _p = CONFIGS[cfg]
    conn = await _pg(db)
    try:
        await conn.execute(
            "TRUNCATE bench_tenant_order_lines, bench_tenant_orders, "
            "bench_tenant_writes, outbox_messages, audit_log RESTART IDENTITY"
        )
        await conn.execute("UPDATE bench_tenant_counters SET value = 0")
    finally:
        await conn.close()
    for n in (rc, rb):
        subprocess.run(
            ["docker", "exec", REDIS_CONTAINER, "redis-cli", "-n", str(n), "FLUSHDB"],
            capture_output=True,
            check=True,
        )


DB_PROBE = """
SELECT
  (SELECT count(*) FROM pg_stat_activity WHERE datname = $1 AND usename = $2) AS backends,
  (SELECT count(*) FROM pg_stat_activity WHERE datname = $1 AND usename = $2
     AND state LIKE 'idle in transaction%' AND state_change < now() - make_interval(secs => $3))
     AS idle_in_tx_stale,
  (SELECT count(*) FROM pg_stat_activity WHERE datname = $1 AND usename = $2
     AND state LIKE 'idle in transaction%') AS idle_in_tx_now,
  (SELECT xact_commit + xact_rollback FROM pg_stat_database WHERE datname = $1) AS xacts,
  (SELECT sessions FROM pg_stat_database WHERE datname = $1) AS sessions,
  (SELECT count(*) FROM outbox_messages WHERE delivered_at IS NULL AND dead_at IS NULL)
     AS outbox_pending,
  (SELECT count(*) FROM outbox_messages WHERE delivered_at IS NULL AND dead_at IS NULL
     AND emitted_at < (now() AT TIME ZONE 'utc') - make_interval(secs => $4)) AS outbox_stuck,
  (SELECT count(*) FROM outbox_messages WHERE dead_at IS NOT NULL) AS outbox_dead,
  (SELECT count(*) FROM outbox_messages) AS outbox_total
"""


async def _db_probe(conn: Any, db: str, grace: float, stuck: float) -> dict[str, Any]:
    row = await conn.fetchrow(DB_PROBE, db, ROLE, grace, stuck)
    return dict(row)


async def _activity(conn: Any, db: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT pid, state, now() - state_change AS age, backend_xid, "
        "left(query, 160) AS query, wait_event FROM pg_stat_activity "
        "WHERE datname = $1",
        db,
    )
    return [{k: str(v) for k, v in dict(r).items()} for r in rows]


async def _terminator(
    cfg: str, every: float, counts: Counter[str], stop: asyncio.Event
) -> None:
    db = CONFIGS[cfg][0]
    conn = await _pg(db)
    rng = random.Random(hash(cfg))
    try:
        while not stop.is_set():
            await asyncio.sleep(rng.uniform(0.5, 1.5) * every)
            killed = await conn.fetchval(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND usename = $2 AND pid <> pg_backend_pid() "
                "ORDER BY random() LIMIT 1",
                db,
                ROLE,
            )
            counts["terminated" if killed else "none_found"] += 1
    finally:
        await conn.close()


def _spawn_client(
    cfg: str, stats: str, a: argparse.Namespace, **kw: Any
) -> subprocess.Popen[bytes]:
    cmd = [
        sys.executable, os.path.abspath(__file__), "client", "--port",
        str(CONFIGS[cfg][4]), "--stats", stats, "--seed", str(kw.get("seed", 1)),
        "--conc", str(kw.get("conc", a.conc)),
        "--cancel-rate", str(kw.get("cancel_rate", 1)),
        "--phase-seconds", str(getattr(a, "phase_seconds", 120)),
        "--duration", str(kw.get("duration", 0)),
    ]  # fmt: skip
    if kw.get("keep_latencies"):
        cmd.append("--keep-latencies")
    return subprocess.Popen(cmd, start_new_session=True)


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _foreign_load() -> list[str]:
    args = subprocess.run(["ps", "-Ao", "args"], capture_output=True, text=True).stdout
    return [
        x[:120]
        for x in args.splitlines()
        if ("run_int.py" in x or "server_bench" in x or "pytest" in x)
        and "soak" not in x
    ]


async def _integrity(cfg: str, final: dict[str, Any]) -> dict[str, Any]:
    """Write atomicity + tenant ownership after quiescence (owner DSN)."""
    db = CONFIGS[cfg][0]
    conn = await _pg(db)
    out: dict[str, Any] = {}
    try:
        rows = await conn.fetch(
            "SELECT id, tenant_id::text AS t FROM bench_tenant_writes"
        )
        writes = {r["id"]: r["t"] for r in rows}
        outbox = Counter(
            int(r["i"])
            for r in await conn.fetch(
                "SELECT payload->>'id' AS i FROM outbox_messages"  # only /dw/3 publishes
            )
        )
        audit = Counter(
            int(r["i"])
            for r in await conn.fetch(
                "SELECT resource_id AS i FROM audit_log "
                "WHERE resource_type = 'bench_tenant_write'"
            )
        )
        problems = []
        if set(outbox) != set(writes) or any(v != 1 for v in outbox.values()):
            problems.append(
                f"outbox vs writes: {len(set(outbox) - set(writes))} orphan events, "
                f"{len(set(writes) - set(outbox))} writes without event, "
                f"{sum(1 for v in outbox.values() if v != 1)} duplicated"
            )
        if set(audit) != set(writes) or any(v != 1 for v in audit.values()):
            problems.append(
                f"audit vs writes: {len(set(audit) - set(writes))} orphan, "
                f"{len(set(writes) - set(audit))} missing"
            )
        acked = final.get("acked", {})
        unc = final.get("uncertain", {})
        for ti, tenant in enumerate(TENANTS):
            ids = final.get("ids", {}).get(str(ti), [])
            wrong = [i for i in ids if writes.get(i) != tenant]
            if wrong:
                problems.append(f"tenant {ti}: {len(wrong)} acked ids missing/foreign")
            n = sum(1 for t in writes.values() if t == tenant)
            lo = acked.get(f"w3:{ti}", 0)
            hi = lo + unc.get(f"w3:{ti}", 0)
            if not lo <= n <= hi:
                problems.append(f"tenant {ti}: {n} write rows not in [{lo}, {hi}]")
            total = await conn.fetchval(
                "SELECT coalesce(sum(value), 0) FROM bench_tenant_counters "
                "WHERE tenant_id = $1::uuid",
                tenant,
            )
            lo4 = acked.get(f"w4:{ti}", 0)
            hi4 = lo4 + unc.get(f"w4:{ti}", 0)
            if not lo4 <= total <= hi4:
                problems.append(
                    f"tenant {ti}: counter sum {total} not in [{lo4}, {hi4}]"
                )
            out[f"tenant{ti}"] = {
                "write_rows": n,
                "w3_acked": lo,
                "w3_uncertain": hi - lo,
                "counter_sum": int(total),
                "w4_acked": lo4,
                "w4_uncertain": hi4 - lo4,
            }
        out["writes"] = len(writes)
        out["outbox_rows"] = sum(outbox.values())
        out["audit_rows"] = sum(audit.values())
        out["problems"] = problems
    finally:
        await conn.close()
    return out


async def run(a: argparse.Namespace) -> int:
    logdir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(logdir, exist_ok=True)
    out = open(a.out, "a")

    def emit(rec: dict[str, Any]) -> None:
        out.write(json.dumps(rec, default=str) + "\n")
        out.flush()

    for cfg in CONFIGS:
        await _reset(cfg)
    servers = {
        c: Server(c, logdir, pool=a.pool, overflow=a.overflow, relay="on")
        for c in CONFIGS
    }
    stop = asyncio.Event()
    kills: dict[str, Counter[str]] = {c: Counter() for c in CONFIGS}
    clients: dict[str, subprocess.Popen[bytes]] = {}
    tasks: list[asyncio.Task[Any]] = []
    violation: dict[str, Any] | None = None
    t0 = time.time()
    mon = {c: await _pg(CONFIGS[c][0]) for c in CONFIGS}
    base: dict[str, dict[str, Any]] = {}
    try:
        for c, s in servers.items():
            st = await s.wait_ready()
            base[c] = {
                "state": st,
                "db": await _db_probe(mon[c], CONFIGS[c][0], 2, 120),
            }
            emit({"event": "start", "cfg": c, **base[c], "foreign": _foreign_load()})
        for c in CONFIGS:
            clients[c] = _spawn_client(
                c,
                os.path.join(logdir, f"client_{c}.json"),
                a,
                seed=7,
                cancel_rate=a.cancel_rate,
            )
            tasks.append(
                asyncio.ensure_future(
                    _terminator(c, a.kill_every, kills[c], stop)
                    if a.kill_every
                    else asyncio.sleep(0)
                )
            )
        log_buckets = {c: Counter() for c in CONFIGS}
        lat_base: dict[str, dict[str, float]] = {c: {} for c in CONFIGS}
        done_reason = "max-minutes"
        while time.time() - t0 < a.max_minutes * 60:
            await asyncio.sleep(a.tick)
            reached = []
            for c, s in servers.items():
                db = CONFIGS[c][0]
                if s.proc.poll() is not None:
                    violation = {"cfg": c, "what": "server exited"}
                    break
                st = await s.state()
                dbp = await _db_probe(mon[c], db, a.idle_grace, a.stuck_seconds)
                cl = _read_json(os.path.join(logdir, f"client_{c}.json"))
                buckets, bad = _classify(s.new_log())
                log_buckets[c].update(buckets)
                reuses = st["checkouts"] - st["connects"]
                rec = {
                    "event": "tick",
                    "cfg": c,
                    "t": round(time.time() - t0, 1),
                    "phase": cl.get("phase"),
                    "ok": cl.get("ok"),
                    "aborts_client": cl.get("aborts_total"),
                    "cancelled_server": st["cancelled"],
                    "terminations": kills[c]["terminated"],
                    "reuses": reuses,
                    "connects": st["connects"],
                    "invalidations": st["invalidations"],
                    "merged": {
                        "ok": st["merged_ok"],
                        "err": st["merged_err"],
                        "noop": st["merged_noop"],
                    },
                    "probe_ok": st["probe_ok"],
                    "pool": st["pool"],
                    "inflight": st["inflight"],
                    "server_5xx": cl.get("server_errors_total"),
                    "server_5xx_by_status": cl.get("server_errors_by_status"),
                    "dead_conn_replaced": st.get("dead_conn_replaced"),
                    "conn_errors": cl.get("conn_errors"),
                    "db": dbp,
                    "win_ms": cl.get("win_ms"),
                    "violations": cl.get("violation_count"),
                }
                emit(rec)
                # ---- invariants
                why = []
                if cl.get("violation_count"):
                    why.append(f"client invariant: {cl.get('violations')}")
                if dbp["idle_in_tx_stale"]:
                    why.append(
                        f"{dbp['idle_in_tx_stale']} sessions idle in tx > {a.idle_grace}s"
                    )
                if dbp["backends"] > a.pool + a.overflow + a.backend_slack:
                    why.append(f"{dbp['backends']} backends > pool bound")
                if st["pool"]["checkedout"] > a.pool + a.overflow:
                    why.append("pool checked-out above its bound")
                if st["inflight"] > a.conc + 4:
                    why.append(
                        f"{st['inflight']} in-flight tasks > concurrency (stuck tasks)"
                    )
                if bad:
                    why.append(f"unexpected server errors: {bad[:3]}")
                if dbp["outbox_stuck"] or dbp["outbox_dead"]:
                    why.append(
                        f"outbox stuck={dbp['outbox_stuck']} dead={dbp['outbox_dead']}"
                    )
                if (cl.get("server_errors_total") or 0) > kills[c]["terminated"] + 10:
                    why.append("5xx count exceeds what the terminations explain")
                if c == "on" and (not st["probe_ok"] or not st["merged_setting"]):
                    why.append("merged BEGIN fast path disabled on ON")
                if (
                    c == "on"
                    and rec["t"] > 30
                    and st["merged_ok"] <= base[c]["state"]["merged_ok"]
                ):
                    why.append("ON instance never used the merged BEGIN")
                if c == "off" and st["merged_calls"]:
                    why.append("OFF instance ran the merged BEGIN")
                for route, (p50, _p99, n) in (cl.get("win_ms") or {}).items():
                    if rec["t"] < 90 and n >= 50:
                        lat_base[c][route] = min(lat_base[c].get(route, 1e9), p50)
                    elif route in lat_base[c] and p50 > 4 * lat_base[c][route]:
                        rec.setdefault("latency_drift", {})[route] = [
                            lat_base[c][route],
                            p50,
                        ]
                if "latency_drift" in rec:
                    emit(
                        {
                            "event": "warn",
                            "cfg": c,
                            "latency_drift": rec["latency_drift"],
                        }
                    )
                if why and a.keep_going:
                    emit(
                        {
                            "event": "violation_kept_going",
                            "cfg": c,
                            "t": rec["t"],
                            "why": why,
                        }
                    )
                    why = []
                if why:
                    violation = {"cfg": c, "t": rec["t"], "why": why, "state": st}
                    break
                if (
                    st["cancelled"] >= a.target_cancels
                    and kills[c]["terminated"] >= a.target_kills
                    and reuses >= a.target_reuses
                ):
                    reached.append(c)
            if violation:
                break
            print(
                f"[{time.time() - t0:6.0f}s] "
                + " | ".join(
                    f"{c}: cancel={_read_json(os.path.join(logdir, f'client_{c}.json')).get('aborts_total')}"
                    f" kills={kills[c]['terminated']}"
                    for c in CONFIGS
                ),
                flush=True,
            )
            if len(reached) == len(CONFIGS):
                done_reason = "targets reached"
                break
        stop.set()
        if violation:
            c = violation["cfg"]
            violation["pg_stat_activity"] = await _activity(mon[c], CONFIGS[c][0])
            violation["log_tail"] = servers[c].new_log()[-50:]
            emit({"event": "VIOLATION", **violation})
            print("VIOLATION:", json.dumps(violation, default=str)[:4000], flush=True)
        for p in clients.values():
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
        for p in clients.values():
            p.wait(60)
        await asyncio.gather(*tasks, return_exceptions=True)
        # ---- quiescence checks
        await asyncio.sleep(a.quiesce)
        summary: dict[str, Any] = {
            "event": "summary",
            "reason": "violation" if violation else done_reason,
            "minutes": round((time.time() - t0) / 60, 1),
        }
        for c, s in servers.items():
            db = CONFIGS[c][0]
            final = _read_json(os.path.join(logdir, f"client_{c}.json"))
            for _ in range(int(a.drain_seconds)):  # the relay delivers what is left
                dbp = await _db_probe(mon[c], db, 1, 0)
                if not dbp["outbox_pending"]:
                    break
                await asyncio.sleep(1)
            st = await s.state()
            buckets, bad = _classify(s.new_log())
            log_buckets[c].update(buckets)
            integ = await _integrity(c, final)
            checks = {
                "pool_checkedout_0": st["pool"]["checkedout"] == 0,
                "inflight_0": st["inflight"] == 0,
                "idle_in_tx_0": dbp["idle_in_tx_now"] == 0,
                "outbox_all_delivered": dbp["outbox_pending"] == 0
                and not dbp["outbox_dead"],
                "integrity": not integ["problems"],
                "no_unexpected_errors": not bad
                and not any(
                    k.startswith(("FATAL", "UNEXPECTED")) for k in log_buckets[c]
                ),
                "no_client_violations": not final.get("violation_count"),
            }
            summary[c] = {
                "checks": checks,
                "ok": all(checks.values()),
                "requests_ok": final.get("ok"),
                "status": final.get("status"),
                "client_aborts": final.get("aborts"),
                "server_cancelled": st["cancelled"],
                "server_cancelled_by_path": st["cancelled_by_path"],
                "terminations": kills[c]["terminated"],
                "checkouts": st["checkouts"],
                "connects": st["connects"],
                "reuses": st["checkouts"] - st["connects"],
                "invalidations": st["invalidations"],
                "merged": {
                    "calls": st["merged_calls"],
                    "ok": st["merged_ok"],
                    "noop": st["merged_noop"],
                    "err": st["merged_err"],
                },
                "probe_ok": st["probe_ok"],
                "server_5xx": final.get("server_errors"),
                "server_5xx_by_status": final.get("server_errors_by_status"),
                "dead_conn_replaced": st.get("dead_conn_replaced"),
                "conn_errors": final.get("conn_errors"),
                "db_final": dbp,
                "integrity": integ,
                "log_buckets": dict(log_buckets[c].most_common(25)),
            }
        emit(summary)
        print(json.dumps(summary, default=str, indent=1)[:6000], flush=True)
        return 1 if violation or not all(summary[c]["ok"] for c in CONFIGS) else 0
    finally:
        stop.set()
        for p in clients.values():
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
        for s in servers.values():
            s.stop()
        for conn in mon.values():
            await conn.close()
        out.close()


# ======================================================================== A/B


def _pg_backend_ticks(pids: list[int]) -> int:
    if not pids:
        return 0
    script = (
        "for p in "
        + " ".join(map(str, pids))
        + "; do cat /proc/$p/stat 2>/dev/null; echo; done"
    )
    out = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "sh", "-c", script],
        capture_output=True, text=True, timeout=20,
    ).stdout  # fmt: skip
    total = 0
    for line in out.splitlines():
        if ")" not in line:
            continue
        f = line.rsplit(")", 1)[1].split()
        total += int(f[11]) + int(f[12])  # utime, stime (fields 14, 15)
    return total


async def _pids(conn: Any, db: str) -> list[int]:
    return [
        r["pid"]
        for r in await conn.fetch(
            "SELECT pid FROM pg_stat_activity WHERE datname = $1 AND usename = $2",
            db,
            ROLE,
        )
    ]


async def ab(a: argparse.Namespace) -> int:
    logdir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(logdir, exist_ok=True)
    for cfg in CONFIGS:
        await _reset(cfg)
    servers = {c: Server(c, logdir, pool=10, overflow=20, relay="off") for c in CONFIGS}
    mon = {c: await _pg(CONFIGS[c][0]) for c in CONFIGS}
    rows: list[dict[str, Any]] = []
    try:
        for s in servers.values():
            await s.wait_ready()
        order = ["on", "off"] * 1 + ["off", "on"]  # ABBA, repeated
        plan = ["on", "off"] + order * a.blocks  # first pair = warm-up
        for k, c in enumerate(plan):
            s = servers[c]
            db = CONFIGS[c][0]
            stats = os.path.join(logdir, f"ab_{c}.json")
            pids = await _pids(mon[c], db)
            pg0, cpu0, x0 = _pg_backend_ticks(pids), s.cpu(), time.time()
            p = _spawn_client(
                c,
                stats,
                a,
                seed=100 + k,
                cancel_rate=0,
                duration=a.slot,
                keep_latencies=True,
            )
            await asyncio.get_running_loop().run_in_executor(None, p.wait)
            cpu1, x1 = s.cpu(), time.time()
            pids2 = await _pids(mon[c], db)
            pg1 = _pg_backend_ticks(pids2)
            fin = _read_json(stats)
            n = fin.get("ok", 0) + fin.get("server_errors_total", 0)
            rec = {
                "event": "ab_slot",
                "slot": k,
                "cfg": c,
                "warmup": k < 2,
                "n": n,
                "rps": round(n / (x1 - x0), 1),
                "server_cpu_us": round((cpu1 - cpu0) / n * 1e6, 1) if n else None,
                "pg_cpu_us": round((pg1 - pg0) * 1e4 / n, 1) if n else None,
                "pg_backends": [len(pids), len(pids2)],
                "p50_ms": fin.get("p50_ms"),
                "p99_ms": fin.get("p99_ms"),
                "errors": fin.get("server_errors_total"),
                "violations": fin.get("violation_count"),
                "foreign": _foreign_load(),
                "load1": round(os.getloadavg()[0], 2),
            }
            rows.append(rec)
            with open(a.out, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            print(json.dumps(rec), flush=True)
        summary: dict[str, Any] = {"event": "ab_summary"}
        for c in CONFIGS:
            rs = [r for r in rows if r["cfg"] == c and not r["warmup"]]
            summary[c] = {
                key: round(statistics.median(r[key] for r in rs), 2)
                for key in ("rps", "server_cpu_us", "pg_cpu_us", "p50_ms", "p99_ms")
            }
            summary[c]["slots"] = len(rs)
            summary[c]["errors"] = sum(r["errors"] or 0 for r in rs)
        for key in ("server_cpu_us", "pg_cpu_us", "p50_ms", "p99_ms"):
            summary[f"on_vs_off_{key}_pct"] = round(
                (summary["on"][key] / summary["off"][key] - 1) * 100, 1
            )
        with open(a.out, "a") as fh:
            fh.write(json.dumps(summary) + "\n")
        print(json.dumps(summary, indent=1), flush=True)
        return 0
    finally:
        for s in servers.values():
            s.stop()
        for conn in mon.values():
            await conn.close()


def setup() -> None:
    """Create both databases with the S5 + write-stand schema (owner DSN)."""
    for db, *_ in CONFIGS.values():
        env = {
            **os.environ,
            "DATABASE_URL": _db_dsn(db).replace(
                "postgresql://", "postgresql+asyncpg://", 1
            ),
            "CACHE_URL": f"redis://localhost:{REDIS_PORT}/{CONFIGS['on'][1]}",
        }
        subprocess.run(
            [SRV_PY, "-m", "benchsuite.setup_db"], cwd=SRV, env=env, check=True
        )


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("client")
    c.add_argument("--port", type=int, required=True)
    c.add_argument("--stats", required=True)
    c.add_argument("--seed", type=int, default=1)
    c.add_argument("--conc", type=int, default=12)
    c.add_argument("--cancel-rate", type=float, default=1)  # 0 = no aborts
    c.add_argument("--phase-seconds", type=float, default=120)
    c.add_argument("--duration", type=float, default=0)
    c.add_argument("--keep-latencies", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--conc", type=int, default=12)
    r.add_argument("--pool", type=int, default=3)
    r.add_argument("--overflow", type=int, default=1)
    r.add_argument("--kill-every", type=float, default=2.5)
    r.add_argument("--tick", type=float, default=5)
    r.add_argument("--phase-seconds", type=float, default=120)
    r.add_argument("--idle-grace", type=float, default=3)
    r.add_argument("--stuck-seconds", type=float, default=120)
    r.add_argument("--max-minutes", type=float, default=30)
    r.add_argument("--target-cancels", type=int, default=5000)
    r.add_argument("--target-kills", type=int, default=300)
    r.add_argument("--target-reuses", type=int, default=1_000_000)
    r.add_argument("--quiesce", type=float, default=5)
    r.add_argument(
        "--keep-going",
        action="store_true",
        help="diagnosis: log violations, never abort",
    )
    r.add_argument("--cancel-rate", type=float, default=1, help="0 = no client aborts")
    r.add_argument("--backend-slack", type=int, default=2)
    r.add_argument("--drain-seconds", type=float, default=90)
    b = sub.add_parser("ab")
    b.add_argument("--out", required=True)
    b.add_argument("--conc", type=int, default=8)
    b.add_argument("--slot", type=float, default=30)
    b.add_argument("--blocks", type=int, default=2)
    sub.add_parser("setup")
    a = p.parse_args()
    if a.cmd == "client":
        client(a)
    elif a.cmd == "run":
        sys.exit(asyncio.run(run(a)))
    elif a.cmd == "ab":
        sys.exit(asyncio.run(ab(a)))
    else:
        setup()


if __name__ == "__main__":
    main()
