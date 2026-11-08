"""PoC stand of tm kb-poc-authz: the perf-dx stand (tm kb-perf-dx-baseline,
``_evidence/perf-dx/authz_stand.py``) with the same method, extended:

* ``KB_VARIANT``: ``authzstand`` (kernel atoms, base), ``authzpa2`` (atoms
  generated next to the contract stubs), ``authzpa3`` (parameterized facts);
* ``KB_REPO``: the checkout whose ``benchmarks/`` the stand uses;
* ``KB_PROVIDER_PYTHON``: the interpreter of the split provider process (the
  consumer is this interpreter, e.g. a venv without tenancy / billing);
* topologies ``split-tenancy-down`` (membership bound to a closed port) and
  ``split-nobind`` (no binding at all);
* subjects ``member`` (role member in ORG) and ``ORG2`` (USER admin, canceled
  subscription); the ``decisions`` command prints status and code per case.

Original docstring:

Authorization baseline stand (tm kb-perf-dx-baseline).

Measures, per policy, one authenticated GET of ``sandloom.main:app`` whose
route is guarded by ``require(policy)`` (module ``stand/authzstand``), in a
monolith (identity + tenancy + billing + the stand in one process) and split
(the stand + identity in this process, tenancy + billing in a uvicorn
provider process reached over internal RPC through ``SERVICE_BINDINGS``).

    # env: DATABASE_URL (owner DSN of the seeded db), CACHE_URL / BROKER_URL
    python authz_stand.py run    --topology mono|split --n 2000 --reps 3 --out x.json
    python authz_stand.py counts --topology mono|split --m 20 --out y.json
    python authz_stand.py cg     --case role.admin --n 200     # under valgrind

``run``: in-process ASGI calls at concurrency 1; per op ``perf_counter``
latency and (macOS) retired instructions of this process (and of the
provider process when split), interleaved over cases and repetitions.
``counts``: database statements (PostgreSQL ``log_statement=all`` between
marker statements, split by database user), Redis commands (``MONITOR``,
split by db index) and internal-RPC requests (the provider's access log)
per request. ``cg``: N requests of one case; ``cg_authz.sh`` runs it under
cachegrind with N=0 and N=K and divides the difference by K.

Nothing here is imported by the framework; it only reads it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("KB_REPO") or HERE.parents[3])
VARIANT = os.environ.get("KB_VARIANT", "authzstand")
sys.path.insert(0, str(HERE / "stand"))
sys.path.insert(0, str(REPO / "benchmarks"))
sys.path.insert(0, str(REPO / "benchmarks" / "ladder"))

USER = uuid.UUID("00000000-0000-4000-8000-00000000a001")
ORG = uuid.UUID("00000000-0000-4000-8000-00000000b001")
OUTSIDER = uuid.UUID("00000000-0000-4000-8000-00000000a0ff")
MEMBER = uuid.UUID("00000000-0000-4000-8000-00000000a002")
ORG2 = uuid.UUID("00000000-0000-4000-8000-00000000b002")
JWT_SECRET = "kb-perf-dx-stand-secret-0123456789abcdef0123456789abcdef"
CONSUMER, PROVIDER = "consumer", "provider"
#: module_router() mounts a module under /api/v1/<module>.
PREFIX = f"/api/v1/{VARIANT}"
CONTRACTS: tuple[str, ...] = (
    "tenancy.membership.v1",
    "billing.entitlements.v1",
    "billing.facts.v1",
)
if VARIANT == "authzpa3":
    CONTRACTS = (*CONTRACTS, "pa3.facts.v1")
TOPOLOGIES = ["mono", "split", "split-tenancy-down", "split-nobind"]
_B = {"x-tenant-id": str(ORG)}

#: (case id, path, subject, expected status, extra headers)
CASES: list[tuple[str, str, str, int, dict[str, str]]] = [
    ("control", "/azs/control", "admin", 200, {}),
    ("role.admin", "/azs/role", "admin", 200, {}),
    ("role.user", "/azs/role", "user", 403, {}),
    ("perm.admin", "/azs/perm", "admin", 200, {}),
    ("subscribed", "/azs/subscribed", "admin", 200, {}),
    ("subactive", "/azs/subactive", "admin", 200, {}),
    ("credits", "/azs/credits", "admin", 200, {}),
    ("role_or_credits.admin", "/azs/role_or_credits", "admin", 200, {}),
    ("role_or_credits.user", "/azs/role_or_credits", "user", 200, {}),
    ("orgrole", f"/azs/orgrole/{ORG}", "admin", 200, {}),
    ("orgrole_and_subactive", f"/azs/orgrole_and_subactive/{ORG}", "admin", 200, {}),
    (
        "orgrole_and_subactive.outsider",
        f"/azs/orgrole_and_subactive/{ORG}",
        "outsider",
        403,
        {},
    ),
    ("bound.control", "/azs/bound/control", "admin", 200, {"x-tenant-id": str(ORG)}),
    (
        "bound.orgrole_and_subactive",
        "/azs/bound/orgrole_and_subactive",
        "admin",
        200,
        {"x-tenant-id": str(ORG)},
    ),
    # PoC kb-poc-authz: the decisive shape's other subjects (Z-1)
    ("bound.member", "/azs/bound/orgrole_and_subactive", "member", 403, _B),
    (
        "bound.admin_inactive",
        "/azs/bound/orgrole_and_subactive",
        "admin",
        402,
        {"x-tenant-id": str(ORG2)},
    ),
    ("bound.outsider", "/azs/bound/orgrole_and_subactive", "outsider", 403, _B),
    ("bound.no_tenant", "/azs/bound/orgrole_and_subactive", "admin", 400, {}),
    # fail-closed cases (Z-9)
    ("or_sub_user.admin", f"/azs/orgrole_or_subactive_user/{ORG}", "admin", 200, {}),
    ("not_member.admin", f"/azs/not_member/{ORG}", "admin", 403, {}),
    # past_due claim (Z-14): allowed in the grace window unless
    # BILLING_DENY_ON_PAST_DUE (the owner's rule, read through its fact)
    ("subscribed.past_due", "/azs/subscribed", "pastdue", 200, {}),
]
if VARIANT == "authzpa3":
    CASES = [c for c in CASES if c[0].startswith("bound.") and c[0] != "bound.control"]


# --------------------------------------------------------------------------- #
# Environment (built BEFORE sandloom is imported: settings are read at import)
# --------------------------------------------------------------------------- #
def _dsn_as(user: str | None) -> str:
    dsn = os.environ["DATABASE_URL"]
    if user is None:
        return dsn
    return re.sub(r"//[^@/]+@", f"//{user}:kbpw@", dsn, count=1)


def _redis(db: int) -> str:
    base = os.environ.get("CACHE_URL", "redis://127.0.0.1:6379/3")
    return re.sub(r"/\d+$", f"/{db}", base)


def _common_env() -> dict[str, str]:
    return {
        "ENVIRONMENT": "dev",
        "LOG_LEVEL": "WARNING",
        "RATE_LIMIT_ENABLED": "false",
        "DOCS_ENABLED": "false",
        "BILLING_PROVIDER": "fake",
        "OUTBOX_RELAY_ENABLED": "off",
        "JWT_SECRET": JWT_SECRET,
        "SANDLOOM_ACCEPTED_RISKS": "shared_hs_secret_split,relay_disabled",
    }


def consumer_env(topology: str, provider_url: str | None) -> dict[str, str]:
    from benchsuite import rpckeys

    if topology == "mono":
        modules = [
            "sandloom_identity",
            "sandloom_tenancy",
            "sandloom_billing",
            VARIANT,
            *(["pa3"] if VARIANT == "authzpa3" else []),
        ]
        return {
            **_common_env(),
            "SANDLOOM_MODULES": json.dumps(modules),
            "DATABASE_URL": _dsn_as(os.environ.get("KB_APP_DB_USER")),
            "CACHE_URL": _redis(3),
            "BROKER_URL": _redis(4),
        }
    modules = ["sandloom_identity", VARIANT]
    bindings = {
        c: {"base_url": provider_url, "service_id": PROVIDER} for c in CONTRACTS
    }
    if topology == "split-tenancy-down":
        bindings["tenancy.membership.v1"] = {
            "base_url": f"http://127.0.0.1:{_free_port()}",
            "service_id": PROVIDER,
        }
    if topology == "split-nobind":
        bindings = {}
    return {
        **_common_env(),
        "SANDLOOM_MODULES": json.dumps(modules),
        "DATABASE_URL": _dsn_as(os.environ.get("KB_APP_DB_USER")),
        "CACHE_URL": _redis(3),
        "BROKER_URL": _redis(4),
        "SERVICE_BINDINGS": json.dumps(bindings),
        "RPC_TRANSPORT": os.environ.get("RPC_TRANSPORT", "aiohttp"),
        **rpckeys.env(
            CONSUMER, peers=[PROVIDER], allowed_calls={CONSUMER: list(CONTRACTS)}
        ),
    }


def provider_env() -> dict[str, str]:
    from benchsuite import rpckeys

    modules = ["sandloom_tenancy", "sandloom_billing"]
    if VARIANT == "authzpa3":
        modules.append("pa3")
    # KB_PROVIDER_DENY: contracts the provider refuses to this consumer
    # (an RPC authorization failure, not an outage)
    deny = set(filter(None, os.environ.get("KB_PROVIDER_DENY", "").split(",")))
    allowed = [c for c in CONTRACTS if c not in deny]
    return {
        **_common_env(),
        # KB_PROVIDER_PYTHONPATH: a changed provider package shadowing the
        # installed one (a provider older or newer than the consumer's stub)
        "PYTHONPATH": os.pathsep.join(
            filter(None, [os.environ.get("KB_PROVIDER_PYTHONPATH"), str(HERE / "stand")])
        ),
        "SANDLOOM_MODULES": json.dumps(modules),
        "DATABASE_URL": _dsn_as(os.environ.get("KB_PROVIDER_DB_USER")),
        "CACHE_URL": _redis(5),
        "BROKER_URL": _redis(6),
        **rpckeys.env(PROVIDER, peers=[CONSUMER], allowed_calls={CONSUMER: allowed}),
    }


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_provider(
    *, access_log: bool, log_path: Path
) -> tuple[subprocess.Popen[bytes], str]:
    port = _free_port()
    env = {**os.environ, **provider_env(), "PYTHONUNBUFFERED": "1"}
    cmd = [
        os.environ.get("KB_PROVIDER_PYTHON", sys.executable),
        "-m",
        "uvicorn",
        "sandloom.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--loop",
        "uvloop",
        "--http",
        "httptools",
        "--log-level",
        "info" if access_log else "warning",
    ]
    if not access_log:
        cmd.append("--no-access-log")
    log = log_path.open("wb")
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"provider exited: {log_path.read_text()[-3000:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc, f"http://127.0.0.1:{port}"
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise SystemExit("provider did not start")


# --------------------------------------------------------------------------- #
# Tokens and in-process ASGI calls
# --------------------------------------------------------------------------- #
def mint(subject: str) -> str:
    import jwt

    now = int(time.time())
    sub = {"outsider": OUTSIDER, "member": MEMBER}.get(subject, USER)
    status = "past_due" if subject == "pastdue" else "active"
    claims: dict[str, Any] = {
        "sub": str(sub),
        "token_type": "access",
        "iat": now,
        "nbf": now,
        "exp": now + 6 * 3600,
        "jti": uuid.uuid4().hex,
        "tenant_ids": [str(ORG), str(ORG2)],
        "subscription_status": status,
        "claims_sources": ["identity", "tenancy", "billing"],
        "claims_sources_failed": [],
    }
    if subject == "admin":
        claims["roles"] = ["admin"]
        claims["permissions"] = ["perm.stand.read"]
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def make_op(
    app: Any, path: str, token: str, extra: dict[str, str], expect: int | None
) -> Any:
    headers = [
        (b"host", b"127.0.0.1:8000"),
        (b"authorization", f"Bearer {token}".encode()),
        *((k.encode(), v.encode()) for k, v in extra.items()),
    ]
    base = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    req = {"type": "http.request", "body": b"", "more_body": False}

    async def receive() -> Any:
        return req

    async def op() -> Any:
        out: list[Any] = []

        async def send(msg: Any) -> None:
            out.append(msg)

        scope = dict(base)
        scope["state"] = {}
        await app(scope, receive, send)
        if expect is None:  # decisions: report, do not assert
            return out[0]["status"], b"".join(m.get("body", b"") for m in out[1:])
        if out[0]["status"] != expect:
            raise RuntimeError(f"{path}: {out[0]['status']} != {expect}: {out[1:]}")
        return None

    return op


class Lifespan:
    def __init__(self, app: Any) -> None:
        self.cm = app.router.lifespan_context(app)

    async def __aenter__(self) -> Lifespan:
        await self.cm.__aenter__()
        return self

    async def aclose(self) -> None:
        await self.cm.__aexit__(None, None, None)


def _poc_switches() -> None:
    """Set the candidate's PoC switches (K5, K6, witness) from KB_POC_* before
    the app is built; the kernel itself reads no environment for them. A base
    tree has no such modules: nothing to set."""
    flag = {k: os.environ.get(k) == "1" for k in ("KB_POC_K5", "KB_POC_K6", "KB_POC_WITNESS")}
    try:
        import sandloom.shared.authz.contract_check as cc
    except ImportError:
        return
    import sandloom.core.rpc.readiness as readiness
    import sandloom.shared.authz.deps as deps

    cc._K5 = flag["KB_POC_K5"]
    cc._WITNESS = flag["KB_POC_WITNESS"]
    deps._K6 = flag["KB_POC_K6"]
    readiness._K6_SEED = flag["KB_POC_K6"]


async def build_app(topology: str, provider_url: str | None) -> tuple[Any, Lifespan]:
    os.environ.update(consumer_env(topology, provider_url))
    _poc_switches()
    from sandloom.main import app

    life = await Lifespan(app).__aenter__()
    return app, life


def _ops(
    app: Any, selected: list[str] | None, *, check: bool = True
) -> dict[str, Any]:
    tokens = {s: mint(s) for s in ("admin", "user", "outsider", "member", "pastdue")}
    return {
        cid: make_op(app, PREFIX + path, tokens[subj], extra, expect if check else None)
        for cid, path, subj, expect, extra in CASES
        if selected is None or cid in selected
    }


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[k]


# --------------------------------------------------------------------------- #
# run: latency + instructions
# --------------------------------------------------------------------------- #
async def cmd_run(a: argparse.Namespace) -> None:
    from darwin_rusage import rusage

    proc = None
    url = None
    if a.topology != "mono":
        proc, url = start_provider(
            access_log=False, log_path=Path(a.out).with_suffix(".provider.log")
        )
    try:
        app, life = await build_app(a.topology, url)
        ops = _ops(app, a.cases.split(",") if a.cases else None)
        results: dict[str, list[dict[str, float]]] = {c: [] for c in ops}
        order = list(ops)
        for rep in range(a.reps):
            seq = order[rep % len(order) :] + order[: rep % len(order)]
            if rep % 2:
                seq.reverse()
            for cid in seq:
                op = ops[cid]
                for _ in range(a.warmup):
                    await op()
                lat: list[float] = []
                r0 = rusage(os.getpid())
                p0 = rusage(proc.pid) if proc else None
                for _ in range(a.n):
                    t = time.perf_counter()
                    await op()
                    lat.append((time.perf_counter() - t) * 1e6)
                r1 = rusage(os.getpid())
                p1 = rusage(proc.pid) if proc else None
                row = {
                    "p50_us": _pct(lat, 0.50),
                    "p95_us": _pct(lat, 0.95),
                    "p99_us": _pct(lat, 0.99),
                    "mean_us": statistics.fmean(lat),
                }
                if r0 and r1:
                    row["instr_k"] = (
                        (r1["instructions"] - r0["instructions"]) / a.n / 1e3
                    )
                if p0 and p1:
                    row["provider_instr_k"] = (
                        (p1["instructions"] - p0["instructions"]) / a.n / 1e3
                    )
                results[cid].append(row)
                print(cid, rep, {k: round(v, 1) for k, v in row.items()}, flush=True)
        await life.aclose()
    finally:
        if proc:
            proc.terminate()
            proc.wait(10)
    import platform

    doc = {
        "meta": {
            "topology": a.topology,
            "n": a.n,
            "warmup": a.warmup,
            "reps": a.reps,
            "python": sys.version.split()[0],
            "machine": platform.machine(),
            "platform": platform.platform(),
            "git": subprocess.run(
                ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "rpc_transport": os.environ.get("RPC_TRANSPORT", "aiohttp")
            if a.topology != "mono"
            else None,
        },
        "results": results,
    }
    Path(a.out).write_text(json.dumps(doc, indent=1))


# --------------------------------------------------------------------------- #
# counts: DB statements, Redis commands, RPC requests per request
# --------------------------------------------------------------------------- #
PG_CONTAINER = os.environ.get("KB_PG_CONTAINER", "kb-pocauthz-pg")
_STMT = re.compile(
    r"^\S+ \S+ \S+ \[\d+\] (\S+)@\S+ LOG:  (statement|execute [^:]*): (.*)$"
)


async def _pg_admin() -> Any:
    import asyncpg

    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    return await asyncpg.connect(dsn)


async def cmd_counts(a: argparse.Namespace) -> None:
    import redis.asyncio as aioredis

    adm = await _pg_admin()
    for role in ("kb_consumer", "kb_provider"):
        if not await adm.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role):
            await adm.execute(f"CREATE ROLE {role} LOGIN SUPERUSER PASSWORD 'kbpw'")
    os.environ["KB_APP_DB_USER"] = "kb_consumer"
    os.environ["KB_PROVIDER_DB_USER"] = "kb_provider"
    plog = Path(a.out).with_suffix(".provider.log")
    proc = None
    url = None
    if a.topology != "mono":
        proc, url = start_provider(access_log=True, log_path=plog)
    mon = aioredis.from_url(_redis(14))  # MONITOR is server-wide; db 0 unused
    marker = aioredis.from_url(_redis(15))
    seen: list[str] = []

    async def monitor() -> None:
        async with mon.monitor() as m:
            async for cmd in m.listen():
                seen.append(f"{cmd.get('db')} {cmd.get('command')}")

    mtask = asyncio.create_task(monitor())
    await asyncio.sleep(0.5)
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 1))
    try:
        app, life = await build_app(a.topology, url)
        ops = _ops(app, a.cases.split(",") if a.cases else None)
        await adm.execute("ALTER SYSTEM SET log_statement = 'all'")
        await adm.execute("SELECT pg_reload_conf()")
        await asyncio.sleep(0.5)
        windows: list[tuple[str, str, int, int, int]] = []

        def plines() -> int:
            return len(plog.read_text().splitlines()) if proc else 0

        for cid, op in ops.items():
            for phase, reps in (("first", 1), ("steady", a.m)):
                if phase == "steady":
                    for _ in range(3):
                        await op()
                await asyncio.sleep(0.2)
                l0, s0 = plines(), len(seen)
                await adm.execute(f"SELECT 'kbmark start {cid} {phase}'")
                await marker.echo(f"kbmark start {cid} {phase}")
                for _ in range(reps):
                    await op()
                await adm.execute(f"SELECT 'kbmark end {cid} {phase}'")
                await marker.echo(f"kbmark end {cid} {phase}")
                await asyncio.sleep(0.3)
                windows.append((cid, phase, reps, l0, plines()))
                seen.append(f"#window {cid} {phase} {s0}")
        await adm.execute("ALTER SYSTEM RESET log_statement")
        await adm.execute("SELECT pg_reload_conf()")
        await life.aclose()
    finally:
        mtask.cancel()
        if proc:
            proc.terminate()
            proc.wait(10)
    logs = subprocess.run(
        ["docker", "logs", "--since", since, PG_CONTAINER],
        capture_output=True,
        text=True,
    ).stderr.splitlines()
    plog_lines = plog.read_text().splitlines() if proc else []
    out: dict[str, Any] = {}
    # PostgreSQL statements between markers, per user
    current: str | None = None
    pg: dict[str, dict[str, list[str]]] = {}
    for line in logs:
        m = _STMT.match(line)
        if not m:
            continue
        user, _kind, text = m.groups()
        mk = re.search(r"kbmark (start|end) (\S+) (\S+)'", text)
        if mk:
            current = f"{mk.group(2)} {mk.group(3)}" if mk.group(1) == "start" else None
            continue
        if current is not None:
            pg.setdefault(current, {}).setdefault(user, []).append(text[:160])
    # Redis commands between ECHO markers, per db index
    rd: dict[str, dict[str, list[str]]] = {}
    current = None
    for entry in seen:
        mk = re.search(r"ECHO kbmark (start|end) (\S+) (\S+)", entry)
        if mk:
            current = f"{mk.group(2)} {mk.group(3)}" if mk.group(1) == "start" else None
            continue
        if current is not None and not entry.startswith("#"):
            db, cmd = entry.split(" ", 1)
            rd.setdefault(current, {}).setdefault(f"db{db}", []).append(
                cmd.split(" ")[0]
            )
    for cid, phase, reps, l0, l1 in windows:
        key = f"{cid} {phase}"
        rpc = [ln for ln in plog_lines[l0:l1] if "/_sandloom/rpc/" in ln]
        out[key] = {
            "requests": reps,
            "pg_statements_per_req": {
                u: len(v) / reps for u, v in sorted(pg.get(key, {}).items())
            },
            "pg_sample": {
                u: v[: max(1, len(v) // reps)] for u, v in pg.get(key, {}).items()
            },
            "redis_commands_per_req": {
                d: len(v) / reps for d, v in sorted(rd.get(key, {}).items())
            },
            "redis_sample": {
                d: v[: max(1, len(v) // reps)] for d, v in rd.get(key, {}).items()
            },
            "rpc_requests_per_req": len(rpc) / reps,
            "rpc_sample": [
                re.sub(r'^.*"((GET|POST) [^ ]+)[^"]*" (\d+).*$', r"\1 \3", ln)
                for ln in rpc[: max(1, len(rpc) // reps)]
            ],
        }
        print(
            key,
            json.dumps({k: v for k, v in out[key].items() if not k.endswith("sample")}),
            flush=True,
        )
    Path(a.out).write_text(
        json.dumps({"topology": a.topology, "m": a.m, "windows": out}, indent=1)
    )
    await adm.close()


# --------------------------------------------------------------------------- #
# decisions: status, body and the provider calls (in order) per case
# --------------------------------------------------------------------------- #
async def cmd_decisions(a: argparse.Namespace) -> None:
    plog = Path(a.out).with_suffix(".provider.log")
    proc = None
    url = None
    if a.topology != "mono":
        proc, url = start_provider(access_log=True, log_path=plog)
    out: dict[str, Any] = {}
    try:
        app, life = await build_app(a.topology, url)
        ops = _ops(app, a.cases.split(",") if a.cases else None, check=False)
        expected = {c[0]: c[3] for c in CASES}
        for cid, op in ops.items():
            rows = []
            for _ in range(a.times):
                l0 = len(plog.read_text().splitlines()) if proc else 0
                status, body = await op()
                await asyncio.sleep(0.15)
                lines = plog.read_text().splitlines()[l0:] if proc else []
                rpc = [
                    re.sub(r'^.*"((GET|POST) [^ ]+)[^"]*" (\d+).*$', r"\1 \3", ln)
                    for ln in lines
                    if "/_sandloom/rpc/" in ln
                ]
                try:
                    detail = json.loads(body)
                except ValueError:
                    detail = body.decode(errors="replace")[:200]
                rows.append({"status": status, "body": detail, "rpc": rpc})
            out[cid] = {"expected_today": expected[cid], "runs": rows}
            print(cid, json.dumps(out[cid]), flush=True)
        core = getattr(app.state, "core", None)
        metrics = {
            name: core.get_metric(name) if core is not None else None
            for name in (
                "access_denied_total",
                "access_indeterminate_total",
                "access_denied_policy_misconfigured_total",
                "access_indeterminate_branch_total",
            )
        }
        out["_metrics"] = metrics
        print("metrics", json.dumps(metrics), flush=True)
        await life.aclose()
    finally:
        if proc:
            proc.terminate()
            proc.wait(10)
    Path(a.out).write_text(
        json.dumps({"variant": VARIANT, "topology": a.topology, "cases": out}, indent=1)
    )


# --------------------------------------------------------------------------- #
# cg: N requests of one case (run under valgrind by cg_authz.sh)
# --------------------------------------------------------------------------- #
async def cmd_cg(a: argparse.Namespace) -> None:
    app, life = await build_app("mono", None)
    op = _ops(app, [a.case])[a.case]
    for _ in range(20):
        await op()
    for _ in range(a.n):
        await op()
    await life.aclose()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--topology", choices=TOPOLOGIES, default="mono")
    r.add_argument("--n", type=int, default=2000)
    r.add_argument("--warmup", type=int, default=200)
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--cases", default="")
    r.add_argument("--out", required=True)
    c = sub.add_parser("counts")
    c.add_argument("--topology", choices=TOPOLOGIES, default="mono")
    c.add_argument("--m", type=int, default=20)
    c.add_argument("--cases", default="")
    c.add_argument("--out", required=True)
    d = sub.add_parser("decisions")
    d.add_argument("--topology", choices=TOPOLOGIES, default="mono")
    d.add_argument("--times", type=int, default=2)
    d.add_argument("--cases", default="")
    d.add_argument("--out", required=True)
    g = sub.add_parser("cg")
    g.add_argument("--case", required=True)
    g.add_argument("--n", type=int, default=0)
    a = ap.parse_args()
    fn = {
        "run": cmd_run,
        "counts": cmd_counts,
        "cg": cmd_cg,
        "decisions": cmd_decisions,
    }[a.cmd]
    try:
        import uvloop

        uvloop.run(fn(a))
    except ImportError:
        asyncio.run(fn(a))


if __name__ == "__main__":
    main()
