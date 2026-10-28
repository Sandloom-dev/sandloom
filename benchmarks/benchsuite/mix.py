"""Stand for the "typical SaaS" CPU-share MODEL (docs/design/execution-planner.md §17).

The model weights per-endpoint CPU costs by a SYNTHETIC traffic mix. This
stand measures the per-endpoint server CPU (µs/request) so the model's assumed
costs can be replaced by measured ones. It generates NO load by itself.

Platform only (identity + tenancy + billing, ``sandloom.main:app``)::

    # a separate database (DATABASE_URL = owner DSN; *_mix is appended)
    python -m benchsuite.mix seed          # migrations + one user, one org, membership
    python -m benchsuite.mix smoke         # every endpoint answers 200 (in-process)
    python -m benchsuite.mix env  > /tmp/mix.env      # server env (export lines)
    python -m benchsuite.mix commands      # the server_bench command per endpoint

FULL mix (``*-full``): the platform modules AND the example apps' modules
(helpdesk tickets; projecthub projects / tasks / taskboard / notifications;
linkly links) in ONE app process on ONE database (``*_mixfull``), so every row
of the §17 mix except token refresh / logout (4 %: each request would consume
a server-side refresh token) is MEASURED — reads, writes (with their events,
outbox and projection consumers) and the login (password hash included).
Every catalog request is measured once per planner mode (the same build with
``PLANNER_OPTIMIZED_EXECUTOR=false|true``): app-process CPU, and the Postgres
and Redis CONTAINER cgroup CPU (``LADDER_PG_CONTAINER`` /
``LADDER_REDIS_CONTAINER``; upper bounds if anything else uses them). The
report weights them by three FIXED profiles declared up front in
``PROFILES`` — ``mixed`` (the §17 model), ``read-heavy`` (90 % reads),
``write-heavy`` (55 % writes + 5 % login) — and prints CPU µs per operation
of each whole profile (app, Postgres, Redis), planner off vs on::

    python -m benchsuite.mix seed-full
    python -m benchsuite.mix smoke-full                       # every row once, in-process
    python -m benchsuite.mix env-full > /tmp/mixfull.env
    python -m benchsuite.mix commands-full > /tmp/mixfull.sh  # every row, off AND on
    sh /tmp/mixfull.sh | tee /tmp/mixfull.jsonl               # THE load run
    python -m benchsuite.mix report /tmp/mixfull.jsonl        # weighted CPU µs/op
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import jwt

from benchsuite import common

USER = uuid.UUID("00000000-0000-4000-8000-00000000a001")
ORG = uuid.UUID("00000000-0000-4000-8000-00000000b001")
PROJECT = uuid.UUID("00000000-0000-4000-8000-00000000c001")
LINK_CODE = "mixcode"
PASSWORD = "mix-password-0123456789"
#: Rows seeded per example list (tickets, tasks + taskboard rows, notifications).
LIST_ROWS = 30

#: (model row, weight % in the model mix, path) — platform endpoints only.
ENDPOINTS: list[tuple[str, int, str]] = [
    ("GET /auth/me", 14, "/auth/me"),
    ("GET /orgs/{id} (guard + org)", 8, f"/api/v1/orgs/{ORG}"),
    ("GET /orgs (my orgs)", 6, "/api/v1/orgs"),
    ("GET /orgs/{id}/members", 5, f"/api/v1/orgs/{ORG}/members"),
    ("billing GET subscription", 3, "/api/v1/billing/subscription"),
    ("billing GET credits", 2, "/api/v1/billing/credits"),
    ("health", 10, "/health/simple"),
]

#: The example apps' source roots (put on the server's PYTHONPATH, full mix).
EXAMPLES = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../examples"))
FULL_MODULES = [
    "sandloom_identity",
    "sandloom_tenancy",
    "sandloom_billing",
    "helpdesk.modules.tickets",
    "projecthub.modules.projects",
    "projecthub.modules.tasks",
    "projecthub.modules.taskboard",
    "projecthub.modules.notifications",
    "linkly.modules.links",
]
_TICKET = json.dumps({"subject": "mix {i}", "body": "b", "priority": "normal"})
_TASK = json.dumps({"tenant_id": str(ORG), "project_id": str(PROJECT), "title": "t"})
_LINK = json.dumps({"target_url": "https://example.com/{i}", "custom_code": "m{u}"})
_LOGIN = json.dumps({"email": "mix@example.com", "password": PASSWORD})

#: Every measured request of the full stand: key -> (method, path, JSON body
#: or "", accepted statuses). Each is measured ONCE per planner mode; the
#: profiles below only weight these measurements.
CATALOG: dict[str, tuple[str, str, str, str]] = {
    "me": ("GET", "/auth/me", "", "200"),
    "org": ("GET", f"/api/v1/orgs/{ORG}", "", "200"),
    "orgs": ("GET", "/api/v1/orgs", "", "200"),
    "members": ("GET", f"/api/v1/orgs/{ORG}/members", "", "200"),
    "tickets": ("GET", f"/api/v1/orgs/{ORG}/tickets?limit=50", "", "200"),
    "taskboard": (
        "GET",
        "/api/v1/taskboard/tasks?limit=50",
        "",
        "200",
    ),
    "notifications": ("GET", "/api/v1/notifications", "", "200"),
    "link": ("GET", f"/api/v1/links/{LINK_CODE}", "", "307"),
    "subscription": ("GET", "/api/v1/billing/subscription", "", "200"),
    "credits": ("GET", "/api/v1/billing/credits", "", "200"),
    "post_ticket": ("POST", f"/api/v1/orgs/{ORG}/tickets", _TICKET, "201"),
    "post_task": ("POST", "/api/v1/tasks", _TASK, "201"),
    "post_link": ("POST", "/api/v1/links", _LINK, "201"),
    "login": ("POST", "/auth/login", _LOGIN, "200"),
    "health": ("GET", "/health/simple", "", "200"),
}

#: FIXED workload profiles (weights in %, each sums to 100). Declared BEFORE
#: any measurement (2026-10-10) and not to be tuned afterwards, so the planner
#: cannot look good by a convenient mix:
#:
#: * ``mixed`` — the §17 model mix (rows it lumps together split evenly; its
#:   4 % refresh / logout row is not measurable here and is dropped, the other
#:   rows are renormalised from 96 to 100);
#: * ``read-heavy`` — 90 % reads / 10 % writes;
#: * ``write-heavy`` — 55 % writes + 5 % login / 40 % reads.
PROFILES: dict[str, dict[str, float]] = {
    "mixed": {
        "me": 14,
        "org": 8,
        "orgs": 6,
        "members": 5,
        "tickets": 12,
        "taskboard": 5,
        "notifications": 5,
        "link": 10,
        "subscription": 2.5,
        "credits": 2.5,
        "post_ticket": 5,
        "post_task": 5,
        "post_link": 5,
        "login": 1,
        "health": 10,
    },
    "read-heavy": {
        "me": 20,
        "org": 10,
        "orgs": 8,
        "members": 7,
        "tickets": 15,
        "taskboard": 8,
        "notifications": 7,
        "link": 10,
        "subscription": 3,
        "credits": 2,
        "post_ticket": 3,
        "post_task": 3,
        "post_link": 4,
    },
    "write-heavy": {
        "post_ticket": 20,
        "post_task": 20,
        "post_link": 15,
        "login": 5,
        "me": 10,
        "org": 5,
        "members": 5,
        "tickets": 10,
        "taskboard": 5,
        "link": 5,
    },
}
#: The §17 model row the stand does NOT measure (weight %, ``mixed`` only).
UNMEASURED = {"auth writes (refresh, logout)": 4}

#: FIXED open-loop rate per profile (requests/s, one worker), declared with
#: the profiles: below every configuration's capacity, so p50/p99 compare
#: latency at the SAME load (write-heavy is lower: 5 % logins hash a password).
PROFILE_RATES: dict[str, int] = {"mixed": 400, "read-heavy": 500, "write-heavy": 200}

#: The integration-run configurations: (name, checkout env var or "" for
#: this checkout, PLANNER_OPTIMIZED_EXECUTOR). Splits the gain into #38's
#: share and the executor's.
CONFIGS: list[tuple[str, str, str]] = [
    ("pre38", "MIX_PRE38_DIR", "false"),  # main before #38 (11281fc)
    ("main38", "MIX_MAIN38_DIR", "false"),  # #38 only (47cd7e5)
    ("planner_off", "", "false"),  # this build, planner OFF
    ("planner_on", "", "true"),  # this build, planner ON
]


def database_url(full: bool = False) -> str:
    """``DATABASE_URL`` with ``_mix`` (``_mixfull``) appended to the name."""
    parts = urlsplit(common.database_url())
    suffix = "_mixfull" if full else "_mix"
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") + suffix))


def server_env(full: bool = False) -> dict[str, str]:
    env = {
        "ENVIRONMENT": "dev",
        # explicit: the ladder runner exports SANDLOOM_MODULES=["benchsuite.bench"]
        "SANDLOOM_MODULES": '["sandloom_identity", "sandloom_tenancy", "sandloom_billing"]',
        "LOG_LEVEL": "WARNING",
        "RATE_LIMIT_ENABLED": "false",
        "DOCS_ENABLED": "false",
        "BILLING_PROVIDER": "fake",
        "DATABASE_URL": database_url(),
        "CACHE_URL": common.cache_url(),
        "BROKER_URL": common.broker_url(),
        "JWT_SECRET": common.jwt_secret(),
    }
    if full:
        env["SANDLOOM_MODULES"] = json.dumps(FULL_MODULES)
        env["DATABASE_URL"] = database_url(full=True)
        env["PYTHONPATH"] = os.pathsep.join(
            os.path.join(EXAMPLES, app) for app in ("helpdesk", "projecthub", "linkly")
        )
    return env


def mint(hours: int = 6, *, tenant: bool = False) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(USER),
        "token_type": "access",
        "iat": now,
        "nbf": now,
        "exp": now + hours * 3600,
        "jti": uuid.uuid4().hex,
        "email": "mix@example.com",
        "email_verified": True,
        "tenant_ids": [str(ORG)],
        "claims_sources": ["identity", "tenancy", "billing"],
    }
    if tenant:  # full mix: TenantScope routes (notifications) bind ORG
        claims["tenant_id"] = str(ORG)
    return jwt.encode(claims, common.jwt_secret(), algorithm="HS256")


def _asyncpg_dsn(full: bool = False) -> str:
    dsn = database_url(full).replace("postgresql+asyncpg://", "postgresql://", 1)
    return re.sub(r"([?&])ssl=", r"\1sslmode=", dsn)


async def _ensure_database(full: bool = False) -> None:
    dsn = _asyncpg_dsn(full)
    parts = urlsplit(dsn)
    name = parts.path.lstrip("/").split("?")[0]
    admin = urlunsplit(parts._replace(path="/postgres"))
    conn = await asyncpg.connect(admin)
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


def _migrate(full: bool = False) -> None:
    exe = shutil.which("sandloom-migrate", path=os.path.dirname(sys.executable))
    cmd = [exe] if exe else [sys.executable, "-m", "sandloom.core.cli.migrate"]
    env = {**os.environ, **server_env(full)}
    subprocess.run([*cmd, "upgrade-all", "--apply"], env=env, check=True)


async def seed(full: bool = False) -> None:
    await _ensure_database(full)
    _migrate(full)
    conn = await asyncpg.connect(_asyncpg_dsn(full))
    try:
        await conn.execute(
            """INSERT INTO users (id, email, full_name, is_active, is_superuser,
                   is_verified, created_at, updated_at)
               VALUES ($1, 'mix@example.com', 'Mix User', true, false, true,
                       now(), now())
               ON CONFLICT (id) DO NOTHING""",
            USER,
        )
        await conn.execute(
            """INSERT INTO tenancy_organizations
                   (id, name, slug, owner_id, is_active, created_at, updated_at)
               VALUES ($1, 'Mix Org', 'mix-org', $2, true, now(), now())
               ON CONFLICT (id) DO NOTHING""",
            ORG,
            USER,
        )
        await conn.execute(
            """INSERT INTO tenancy_members (id, organization_id, user_id, role, created_at)
               VALUES ($1, $2, $3, 'owner', now())
               ON CONFLICT (organization_id, user_id) DO NOTHING""",
            uuid.uuid4(),
            ORG,
            USER,
        )
        if full:
            await _seed_full(conn)
    finally:
        await conn.close()
    print(f"mix database ready: {urlsplit(database_url(full)).path.lstrip('/')}")


async def _seed_full(conn: asyncpg.Connection) -> None:
    """The example apps' rows and the user's password (the login row)."""
    os.environ.setdefault("JWT_SECRET", common.jwt_secret())  # sandloom's settings
    from sandloom.core.auth.hashing import hash_password

    await conn.execute(
        "UPDATE users SET hashed_password = $2 WHERE id = $1",
        USER,
        hash_password(PASSWORD),
    )
    if await conn.fetchval("SELECT count(*) FROM tickets_tickets") >= LIST_ROWS:
        return  # seeded before
    await conn.executemany(
        "INSERT INTO tickets_tickets (id, organization_id, created_by, subject, body,"
        " status, priority, created_at, updated_at) VALUES"
        " ($1, $2, $3, $4, 'b', 'open', 'normal', now(), now())",
        [(uuid.uuid4(), ORG, USER, f"ticket {i}") for i in range(LIST_ROWS)],
    )
    await conn.execute(
        "INSERT INTO projects_projects (id, tenant_id, name, status, version,"
        " created_at, updated_at) VALUES ($1, $2, 'Dev mix', 'active', 1, now(), now())"
        " ON CONFLICT (id) DO NOTHING",
        PROJECT,
        ORG,
    )
    await conn.execute(
        "INSERT INTO taskboard_project_snapshot (project_id, tenant_id, name, status,"
        " version) VALUES ($1, $2, 'Dev mix', 'active', 1) ON CONFLICT DO NOTHING",
        PROJECT,
        ORG,
    )
    tasks = [uuid.uuid4() for _ in range(LIST_ROWS)]
    await conn.executemany(
        "INSERT INTO tasks_tasks (id, tenant_id, project_id, title, status, version,"
        " created_at, updated_at) VALUES ($1, $2, $3, $4, 'active', 1, now(), now())",
        [(t, ORG, PROJECT, f"task {i}") for i, t in enumerate(tasks)],
    )
    await conn.executemany(
        "INSERT INTO taskboard_project_task_query_index (task_id, tenant_id,"
        " task_title, task_status, task_version, project_id, project_name,"
        " project_status, project_version, updated_at) VALUES"
        " ($1, $2, $3, 'active', 1, $4, 'Dev mix', 'active', 1, now())",
        [(t, ORG, f"task {i}", PROJECT) for i, t in enumerate(tasks)],
    )
    await conn.executemany(
        "INSERT INTO notifications_inapp (id, tenant_id, recipient_id, kind, title,"
        " body, created_at) VALUES ($1, $2, $3, 'mix', $4, 'b', now())",
        [(uuid.uuid4(), ORG, USER, f"note {i}") for i in range(LIST_ROWS)],
    )
    await conn.execute(
        "INSERT INTO links_shortlinks (id, code, target_url, clicks, created_at)"
        " VALUES ($1, $2, 'https://example.com/', 0, now()) ON CONFLICT DO NOTHING",
        uuid.uuid4(),
        LINK_CODE,
    )


async def smoke() -> None:
    os.environ.update(server_env())
    import httpx

    from sandloom.main import app

    headers = {"Authorization": f"Bearer {mint()}"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            bad = []
            for name, _w, path in ENDPOINTS:
                r = await c.get(path, headers=headers)
                print(f"{r.status_code} {name} {path}")
                if r.status_code != 200:
                    bad.append((name, r.status_code, r.text[:200]))
    if bad:
        raise SystemExit(f"mix smoke failed: {bad}")


async def smoke_full() -> None:
    """Every full-mix row once, in-process (no load), planner as configured."""
    os.environ.update(server_env(full=True))
    sys.path[:0] = os.environ["PYTHONPATH"].split(os.pathsep)
    import httpx

    from sandloom.main import app

    headers = {
        "Authorization": f"Bearer {mint(tenant=True)}",
        "Content-Type": "application/json",
    }
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            bad = []
            for i, (name, (method, path, body, expect)) in enumerate(CATALOG.items()):
                payload = (
                    body.replace("{i}", str(i)).replace("{u}", uuid.uuid4().hex[:8])
                    if body
                    else None
                )
                r = await c.request(method, path, headers=headers, content=payload)
                print(f"{r.status_code} {name} {method} {path}")
                if str(r.status_code) not in expect.split(","):
                    bad.append((name, r.status_code, r.text[:200]))
    if bad:
        raise SystemExit(f"full mix smoke failed: {bad}")


def commands() -> None:
    token = mint(hours=24)
    for name, weight, path in ENDPOINTS:
        print(
            f"# {name} (model weight {weight} %)\n"
            "uv run python ladder/server_bench.py --target sandloom.main:app "
            f"--env-file /tmp/mix.env --no-id --path {shlex.quote(path)} "
            f"--header {shlex.quote('Authorization: Bearer ' + token)} "
            "--conc 64 --n 8000 --reps 3"
        )


def commands_full(n: int = 6000) -> None:
    """A shell script: every catalog request with the planner OFF, then ON.

    Each run prints one JSON line labelled ``<key>|off|on`` with the app's,
    Postgres' and Redis' CPU µs per request (read by :func:`report`). Run from
    ``benchmarks/``; the Postgres / Redis containers are taken from
    ``LADDER_PG_CONTAINER`` / ``LADDER_REDIS_CONTAINER``.
    """
    token = mint(hours=24, tenant=True)
    print("set -e")
    for mode, flag in (("off", "false"), ("on", "true")):
        for key, (method, path, body, expect) in CATALOG.items():
            rows = max(n // 30, 128) if key == "login" else n  # ~40 ms hash
            parts = [
                "uv run python ladder/server_bench.py --target sandloom.main:app",
                "--env-file /tmp/mixfull.env",
                f"--env PLANNER_OPTIMIZED_EXECUTOR={flag}",
                f"--no-id --path {shlex.quote(path)} --method {method}",
                f"--expect {expect}",
                f"--header {shlex.quote('Authorization: Bearer ' + token)}",
                f"--label {shlex.quote(f'{key}|{mode}')}",
                f"--conc 64 --n {rows // 64 * 64} --reps 3",
            ]
            if body:
                parts.append(f"--body {shlex.quote(body)}")
            print(" ".join(parts))


_METRICS = (("app", "server_cpu_us"), ("pg", "pg_cpu_us"), ("redis", "redis_cpu_us"))


def report(path: str) -> None:
    """Per profile: CPU µs per operation (app, Postgres, Redis), off vs on."""
    m: dict[tuple[str, str], dict[str, float | None]] = {}
    for line in open(path):
        line = line.strip()
        if not line.startswith("{"):
            continue
        rec = json.loads(line)
        key, mode = rec.get("label", "").rsplit("|", 1)
        m[(key, mode)] = {name: rec.get(field) for name, field in _METRICS}
    print("## Per request (µs CPU)\n")
    print("| request | app off | app on | pg off | pg on | redis off | redis on |")
    print("|---|---|---|---|---|---|---|")

    def fmt(v: float | None) -> str:
        return "–" if v is None else f"{v:.0f}"

    for key in CATALOG:
        off, on = m.get((key, "off"), {}), m.get((key, "on"), {})
        cells = [fmt(x.get(n)) for n, _ in _METRICS for x in (off, on)]
        print(f"| {key} | " + " | ".join(cells) + " |")
    for profile, weights in PROFILES.items():
        total = sum(weights.values())
        print(f"\n## Profile {profile!r} (weights fixed in benchsuite.mix)\n")
        print("| CPU µs / operation | planner off | planner on | Δ |")
        print("|---|---|---|---|")
        for name, _ in _METRICS:
            mix: dict[str, float | None] = {}
            for mode in ("off", "on"):
                acc = 0.0
                for key, w in weights.items():
                    v = m.get((key, mode), {}).get(name)
                    if v is None:
                        acc = float("nan")
                        break
                    acc += w / total * v
                mix[mode] = None if acc != acc else acc
            off, on = mix["off"], mix["on"]
            delta = (
                f"{(on / off - 1) * 100:+.1f} %"
                if off is not None and on is not None and off
                else "–"
            )
            print(f"| {name} | {fmt(off)} | {fmt(on)} | {delta} |")
    print(f"\nnot measured (mixed profile only): {UNMEASURED}")


def write_profiles(out_dir: str) -> None:
    """One workload file per profile (server_bench --mix)."""
    os.makedirs(out_dir, exist_ok=True)
    for profile, weights in PROFILES.items():
        entries = [
            {
                "method": CATALOG[k][0],
                "path": CATALOG[k][1],
                "body": CATALOG[k][2],
                "expect": CATALOG[k][3],
                "weight": w,
            }
            for k, w in weights.items()
        ]
        with open(os.path.join(out_dir, f"{profile}.json"), "w") as fh:
            json.dump(entries, fh, indent=1)


def commands_integration(out_dir: str = "/tmp/mixprof", n: int = 8000) -> None:
    """Shell script: every (configuration x profile) as a WHOLE workload.

    Per run: closed loop (conc 64, 3 reps: app / Postgres / Redis CPU per
    operation, sustained rps, RSS per worker) and open loop at the profile's
    FIXED rate (p50 / p99, CPU per operation). Labels
    ``<profile>|<config>|closed|open`` (read by :func:`report_integration`).
    Older checkouts: ``git worktree add $MIX_PRE38_DIR 11281fc`` and
    ``git worktree add $MIX_MAIN38_DIR 47cd7e5``, then ``uv sync --frozen`` in
    their ``benchmarks/``.
    """
    token = mint(hours=24, tenant=True)
    header = shlex.quote("Authorization: Bearer " + token)
    print("set -e")
    print(f"uv run python -m benchsuite.mix profiles {shlex.quote(out_dir)}")
    for config, dir_var, flag in CONFIGS:
        where = ""
        if dir_var:
            print(f': "${{{dir_var}:?set {dir_var} to a checkout (see mix.py)}}"')
            where = (
                f' --cwd "${dir_var}/benchmarks"'
                f' --python "${dir_var}/benchmarks/.venv/bin/python"'
            )
        for profile in PROFILES:
            base = (
                "uv run python ladder/server_bench.py --target sandloom.main:app"
                f" --env-file /tmp/mixfull.env --env PLANNER_OPTIMIZED_EXECUTOR={flag}"
                f" --mix {shlex.quote(os.path.join(out_dir, profile + '.json'))}"
                f" --no-id --path / --header {header}{where}"
            )
            print(
                f"{base} --label {shlex.quote(f'{profile}|{config}|closed')}"
                f" --conc 64 --n {n // 64 * 64} --reps 3"
            )
            print(
                f"{base} --label {shlex.quote(f'{profile}|{config}|open')}"
                f" --rate {PROFILE_RATES[profile]} --seconds 30"
            )


def report_integration(path: str) -> None:
    """Per profile and configuration: CPU / op (app, PG, Redis), sustained
    rps, RSS per worker (closed loop) and p50 / p99 at the fixed rate."""
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line in open(path):
        line = line.strip()
        if line.startswith("{"):
            rec = json.loads(line)
            parts = rec.get("label", "").split("|")
            if len(parts) == 3:
                rows[tuple(parts)] = rec  # type: ignore[index]

    def f(v: Any, d: int = 0) -> str:
        return "–" if v is None else f"{v:.{d}f}"

    for profile in PROFILES:
        print(f"\n## {profile} (fixed rate {PROFILE_RATES[profile]} rps)\n")
        print(
            "| config | app µs/op | pg µs/op | redis µs/op | rps (closed) | "
            "RSS MB/worker | p50 ms | p99 ms | app µs/op @rate |"
        )
        print("|---|---|---|---|---|---|---|---|---|")
        for config, _d, _f in CONFIGS:
            c = rows.get((profile, config, "closed"), {})
            o = rows.get((profile, config, "open"), {})
            print(
                f"| {config} | {f(c.get('server_cpu_us'))} | {f(c.get('pg_cpu_us'))} "
                f"| {f(c.get('redis_cpu_us'))} | {f(c.get('rps'))} "
                f"| {f(c.get('rss_mb_per_worker'), 1)} | {f(o.get('p50_ms'), 2)} "
                f"| {f(o.get('p99_ms'), 2)} | {f(o.get('server_cpu_us'))} |"
            )


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        asyncio.run(seed())
    elif cmd == "seed-full":
        asyncio.run(seed(full=True))
    elif cmd == "smoke":
        asyncio.run(smoke())
    elif cmd == "smoke-full":
        asyncio.run(smoke_full())
    elif cmd == "env":
        for k, v in server_env().items():
            print(f"export {k}={shlex.quote(v)}")
    elif cmd == "env-full":
        for k, v in server_env(full=True).items():
            print(f"export {k}={shlex.quote(v)}")
    elif cmd == "token":
        print(mint())
    elif cmd == "commands":
        commands()
    elif cmd == "commands-full":
        commands_full()
    elif cmd == "report":
        report(sys.argv[2])
    elif cmd == "profiles":
        write_profiles(sys.argv[2] if len(sys.argv) > 2 else "/tmp/mixprof")
    elif cmd == "commands-integration":
        commands_integration()
    elif cmd == "report-integration":
        report_integration(sys.argv[2])
    elif cmd == "endpoints":
        print(json.dumps({"catalog": CATALOG, "profiles": PROFILES}, indent=1))
    else:
        raise SystemExit(
            "usage: python -m benchsuite.mix seed|smoke|env|token|commands"
            "|seed-full|smoke-full|env-full|commands-full|report <jsonl>"
            "|profiles [dir]|commands-integration|report-integration <jsonl>"
        )


if __name__ == "__main__":
    main()
