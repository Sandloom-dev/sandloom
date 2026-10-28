"""Monolith -> microservices WITH its data: the end-to-end extraction scenario.

1. A monolith (identity + tenancy + billing in one process, one database,
   Redis Streams bus, outbox relay on) is filled with a small SaaS dataset
   through its public API: users, organizations, invitations + members,
   subscriptions, credit balances and idempotent credit spends.
2. Every read the dataset supports is recorded (status + exact body).
3. Writes stop (the monolith is shut down). Events that were committed but not
   dispatched yet are left in the outbox ("in flight").
4. ``sandloom-migrate extract-module`` moves tenancy and billing into their own
   databases (dry-run, refusal without ``--i-have-stopped-writes``, apply,
   idempotent re-run, ``--verify``).
5. identity / tenancy / billing start as three services wired by
   ``SERVICE_BINDINGS``; identity keeps the old database.
6. Every recorded read is replayed: the answers must be IDENTICAL (same ids,
   same bodies — the data moved, it was not recreated), tokens minted by the
   monolith keep working, and the business invariants hold: per-table row
   counts and checksums, credit balances, members, and every in-flight event
   reaches the bus exactly once (relayed by the NEW owner, never re-sent by
   the old database).
7. A credit-spend workload runs through identity -> billing (internal RPC)
   while the billing service is SIGKILLed and restarted. Every spend is
   retried with its idempotency key until it settles: no credit is lost or
   double-spent, each settled key has exactly one ledger row and exactly one
   ``billing.credits.spent.v1`` event, and balance = start - settled spends.
8. ``--drop-source`` removes the moved tables from the old database; identity
   keeps serving.

Opt-in (slow, ~1 min): ``SANDLOOM_EXTRACTION_E2E=1``. Needs PostgreSQL + Redis
(``.env.test`` / ``SANDLOOM_TEST_*`` / ``E2E_*`` overrides).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from sandloom.core.contract_testing import rpc_env
from sandloom.core.testing import worker_env_values
from sandloom.core.testing.served import (
    ReservedPort,
    SpawnedServer,
    SpawnedServerError,
    probe,
    spawn_served,
)

pytestmark = [pytest.mark.e2e, pytest.mark.slow, pytest.mark.extraction_e2e]

BACKEND = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
RUN = uuid.uuid4().hex[:8]
PREFIX = f"xe2e-{RUN}:"
INTERNAL_TOKEN = f"extract-internal-{uuid.uuid4().hex}"
JWT_SECRET = f"extract-jwt-{uuid.uuid4().hex}"
PASSWORD = "Extract-Passw0rd!x"
I, T, B = "sandloom_identity", "sandloom_tenancy", "sandloom_billing"  # noqa: E741
PROBE = "extract_probe"
STARTUP_TIMEOUT = 90.0

#: Contracts a process binds for each module hosted by a peer.
PEER_CONTRACTS: dict[str, dict[str, dict[str, Any]]] = {
    I: {},
    T: {"tenancy.membership.v1": {}, "tenancy.claims.v1": {}},
    B: {
        "billing.entitlements.v1": {},
        "billing.credits.v1": {},
        "billing.facts.v1": {},
        "billing.claims.v1": {"critical": False},
    },
}


# ---------------------------------------------------------------------------
# infrastructure
# ---------------------------------------------------------------------------


def _env_base() -> dict[str, str]:
    values = worker_env_values(BACKEND / ".env.test")
    for key in ("DATABASE_URL", "CACHE_URL", "BROKER_URL"):
        if os.environ.get(f"E2E_{key}"):
            values[key] = os.environ[f"E2E_{key}"]
    return values


def _reachable(url: str, default_port: int) -> bool:
    parsed = urlparse(url.replace("+asyncpg", ""))
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or default_port), timeout=1.5
        ):
            return True
    except OSError:
        return False


def _db_url(base: str, name: str) -> str:
    return urlparse(base)._replace(path=f"/{name}").geturl()


def _sync(url: str) -> str:
    return urlparse(url)._replace(scheme="postgresql").geturl().split("?")[0]


def _psql(url: str, sql: str, params: Any = None) -> list[Any]:
    import psycopg2

    conn = psycopg2.connect(_sync(url))
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
        conn.commit()
        return list(rows)
    finally:
        conn.close()


def _admin(base: str, statements: list[str]) -> None:
    import psycopg2

    conn = psycopg2.connect(_sync(_db_url(base, "postgres")))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    finally:
        conn.close()


@dataclass
class Proc:
    name: str
    modules: tuple[str, ...]
    db_url: str
    #: Held by the harness for the whole test (a restarted child gets the same
    #: socket back) and handed to each child — no bind race.
    listener: ReservedPort = field(default_factory=ReservedPort)
    bindings: dict[str, Any] = field(default_factory=dict)
    #: RPC_ALLOWED_CALLS of the whole deployment (identical everywhere).
    allowed: dict[str, Any] = field(default_factory=dict)
    served: SpawnedServer | None = None
    log_path: Path | None = None

    @property
    def proc(self) -> subprocess.Popen[bytes] | None:
        return self.served.proc if self.served is not None else None

    @property
    def url(self) -> str:
        return self.listener.url

    def env(self, base: dict[str, str]) -> dict[str, str]:
        env = {**os.environ, **base}
        env.update(
            {
                "ENVIRONMENT": "test",
                "DATABASE_URL": self.db_url,
                "SANDLOOM_MODULES": json.dumps([*self.modules, PROBE]),
                "SERVICE_BINDINGS": json.dumps(self.bindings),
                "SANDLOOM_SERVICE_NAME": f"xe2e-{self.name}",
                # internal RPC is pair-MAC authenticated (ADR-0004)
                **rpc_env(
                    f"xe2e-{self.name}",
                    peers=[f"xe2e-{n}" for n in ("identity", "tenancy", "billing")],
                    allowed_calls=self.allowed,
                ),
                "EVENT_BUS_BACKEND": "streams",
                "EVENT_STREAM_PREFIX": PREFIX,
                "OUTBOX_RELAY_ENABLED": "on",
                "INTERNAL_API_TOKEN": INTERNAL_TOKEN,
                "JWT_SECRET": JWT_SECRET,
                "JWT_PREVIOUS_SECRETS": "[]",
                "RATE_LIMIT_ENABLED": "false",
                "NOTIFIER_BACKEND": "log",
                "BILLING_PROVIDER": "fake",
                "SERVICE_RPC_TIMEOUT_SECONDS": "5",
                "DB_POOL_SIZE": "2",
                "DB_MAX_OVERFLOW": "4",
                "CONTRACT_DB_POOL_SIZE": "2",
                "CONTRACT_DB_MAX_OVERFLOW": "2",
                "LOG_FORMAT": "text",
                "PYTHONPATH": os.pathsep.join(
                    [str(HERE), str(BACKEND / "src"), os.environ.get("PYTHONPATH", "")]
                ),
            }
        )
        for key in ("MODULE_REQUIRED", "INFRA", "ACCESS"):
            env.pop(key, None)
        return env

    def start(self, base: dict[str, str], log_dir: Path) -> None:
        self.listener.reopen()
        self.log_path = log_dir / f"{self.name}.log"
        with self.log_path.open("ab") as log:
            self.served = spawn_served(
                self.listener,
                "sandloom.main:app",
                env=self.env(base),
                label=self.name,
                cwd=BACKEND,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # its own process group: we kill only it
            )

    def logs(self) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        return self.log_path.read_text(errors="replace")[-6000:]

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)
        self.listener.close()

    def kill(self) -> None:
        assert self.proc is not None
        os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=10)
        # Down means REFUSED (not queued for the next child): release the
        # port; start() re-reserves the same one or fails loudly.
        self.listener.close()


def _wait_ready(procs: list[Proc]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    pending = list(procs)
    while pending and time.monotonic() < deadline:
        for proc in list(pending):
            assert proc.served is not None
            try:  # identity (spawn nonce), not merely "something answers"
                if probe(proc.served):
                    pending.remove(proc)
            except SpawnedServerError as exc:
                raise AssertionError(f"{exc}\n{proc.logs()}") from None
        time.sleep(0.2)
    if pending:
        raise AssertionError(f"did not start: {pending[0].name}\n{pending[0].logs()}")


def _cli(
    base: dict[str, str], mono_url: str, *args: str
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **base}
    env.update(
        {
            "ENVIRONMENT": "test",
            "DATABASE_URL": mono_url,
            "SANDLOOM_MODULES": json.dumps([I, T, B]),
            "EVENT_BUS_BACKEND": "streams",
            "EVENT_STREAM_PREFIX": PREFIX,
            "JWT_SECRET": JWT_SECRET,
            "INTERNAL_API_TOKEN": INTERNAL_TOKEN,
            "BILLING_PROVIDER": "fake",
        }
    )
    for key in ("MODULE_REQUIRED", "INFRA", "SERVICE_BINDINGS"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "sandloom.core.cli.migrate", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _ok(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    return result.stdout


# ---------------------------------------------------------------------------
# the scenario
# ---------------------------------------------------------------------------

USERS = [f"u{i}" for i in range(6)]


def _email(user: str) -> str:
    return f"{user}-{RUN}@extract.example"


@dataclass
class World:
    """Ids and tokens of the seeded dataset."""

    tokens: dict[str, str] = field(default_factory=dict)
    user_ids: dict[str, str] = field(default_factory=dict)
    orgs: dict[str, str] = field(default_factory=dict)  # owner user -> org id
    members: dict[str, list[str]] = field(default_factory=dict)  # org -> users


class Router:
    """The gateway: routes a path to the process hosting its module."""

    def __init__(self, procs: dict[str, Proc]) -> None:
        self.procs = procs

    def url(self, path: str) -> str:
        if path.startswith("/auth"):
            module = I
        elif path.startswith(("/api/v1/orgs", "/api/v1/invitations")):
            module = T
        elif path.startswith("/api/v1/billing"):
            module = B
        else:
            module = PROBE
        return self.procs[module].url + path


async def _login(client: httpx.AsyncClient, router: Router, user: str) -> str:
    resp = await client.post(
        router.url("/auth/login"), json={"email": _email(user), "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(router: Router, mono_url: str) -> World:
    world = World()
    async with httpx.AsyncClient(timeout=30) as client:
        for user in USERS:
            resp = await client.post(
                router.url("/auth/register"),
                json={"email": _email(user), "full_name": user, "password": PASSWORD},
            )
            assert resp.status_code == 202, resp.text
        _psql(mono_url, "UPDATE users SET is_verified = true")
        for user in USERS:
            world.tokens[user] = await _login(client, router, user)
            me = await client.get(
                router.url("/auth/me"), headers=_auth(world.tokens[user])
            )
            world.user_ids[user] = str(me.json()["id"])
        # Three organizations, each owner invites two others.
        for idx, owner in enumerate(USERS[:3]):
            resp = await client.post(
                router.url("/api/v1/orgs"),
                json={"name": f"Org {idx}", "slug": f"org-{idx}-{RUN}"},
                headers=_auth(world.tokens[owner]),
            )
            assert resp.status_code == 201, resp.text
            org = str(resp.json()["id"])
            world.orgs[owner] = org
            world.members[org] = [owner]
        for idx, owner in enumerate(USERS[:3]):
            org = world.orgs[owner]
            world.tokens[owner] = await _login(client, router, owner)
            for guest in (USERS[3 + idx], USERS[(idx + 1) % 3]):
                resp = await client.post(
                    router.url(f"/api/v1/orgs/{org}/invitations"),
                    json={"email": _email(guest), "role": "member"},
                    headers=_auth(world.tokens[owner]),
                )
                assert resp.status_code == 201, resp.text
                accept = await client.post(
                    router.url("/api/v1/invitations/accept"),
                    json={"token": resp.json()["token"]},
                    headers=_auth(world.tokens[guest]),
                )
                assert accept.status_code == 200, accept.text
                world.members[org].append(guest)
        # A pending (not accepted) invitation as well.
        await client.post(
            router.url(f"/api/v1/orgs/{world.orgs[USERS[0]]}/invitations"),
            json={"email": f"pending-{RUN}@extract.example", "role": "admin"},
            headers=_auth(world.tokens[USERS[0]]),
        )
        # Subscriptions + credits for two orgs (the PSP webhook's effect).
        for owner in USERS[:2]:
            org = world.orgs[owner]
            cid = str(uuid.uuid4())
            _psql(
                mono_url,
                "INSERT INTO billing_customers (id, organization_id) VALUES (%s, %s)",
                (cid, org),
            )
            _psql(
                mono_url,
                "INSERT INTO billing_subscriptions (customer_id, price_id, status, "
                "current_period_end, cancel_at_period_end) "
                "VALUES (%s, 1, 'active', now() + interval '30 days', false)",
                (cid,),
            )
            _psql(
                mono_url,
                "INSERT INTO billing_credit_balances (customer_id, amount) "
                "VALUES (%s, 1000)",
                (cid,),
            )
            _psql(
                mono_url,
                "INSERT INTO billing_credit_ledger (customer_id, delta, reason, "
                "operation_key, balance_after) VALUES (%s, 1000, 'grant', %s, 1000)",
                (cid, f"grant-{org}"),
            )
        # Fresh tokens carrying every claim (memberships, subscription).
        for user in USERS:
            world.tokens[user] = await _login(client, router, user)
        # Idempotent spends through the contract (local in the monolith).
        for owner in USERS[:2]:
            org = world.orgs[owner]
            for n in range(8):
                body = {
                    "tenant_type": "organization",
                    "tenant_id": org,
                    "amount": n + 1,
                    "key": f"mono-{org}-{n}",
                }
                first = await client.post(router.url("/_x/spend"), json=body)
                again = await client.post(router.url("/_x/spend"), json=body)
                assert first.status_code == 200, first.text
                assert again.json() == first.json()  # replay, not a 2nd spend
    return world


def _reads(world: World) -> list[tuple[str, str, str | None]]:
    """(label, path, token user) of every read the dataset supports."""
    reads: list[tuple[str, str, str | None]] = [
        ("plans", "/api/v1/billing/plans", None)
    ]
    for user in USERS:
        reads += [
            (f"me-{user}", "/auth/me", user),
            (f"orgs-{user}", "/api/v1/orgs", user),
        ]
        for org in world.orgs.values():
            reads += [
                (f"org-{user}-{org}", f"/api/v1/orgs/{org}", user),
                (f"members-{user}-{org}", f"/api/v1/orgs/{org}/members", user),
                (f"invites-{user}-{org}", f"/api/v1/orgs/{org}/invitations", user),
                (
                    f"sub-{user}-{org}",
                    f"/api/v1/billing/subscription?org_id={org}",
                    user,
                ),
                (
                    f"credits-{user}-{org}",
                    f"/api/v1/billing/credits?org_id={org}",
                    user,
                ),
            ]
    for org in world.orgs.values():
        reads.append((f"balance-{org}", f"/_x/balance/organization/{org}", None))
    return reads


async def _record(router: Router, world: World) -> dict[str, tuple[int, Any]]:
    out: dict[str, tuple[int, Any]] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for label, path, user in _reads(world):
            headers = _auth(world.tokens[user]) if user else {}
            resp = await client.get(router.url(path), headers=headers)
            body = resp.json() if resp.content else None
            out[label] = (resp.status_code, body)
    return out


def _table_fingerprints(url: str, tables: list[str]) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for table in tables:
        cols = [
            r[0]
            for r in _psql(
                url,
                "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) "
                "AND attnum > 0 AND NOT attisdropped ORDER BY attname",
                (table,),
            )
        ]
        row = "md5(ROW(" + ", ".join(f'"{c}"' for c in cols) + ")::text)"
        count, digest = _psql(
            url,
            f"SELECT count(*), coalesce(sum(('x' || substr({row}, 1, 15))"
            f"::bit(60)::bigint), 0)::text FROM {table}",
        )[0]
        out[table] = (int(count), str(digest))
    return out


TENANCY_TABLES = [
    "tenancy_organizations",
    "tenancy_members",
    "tenancy_invitations",
    "tenancy_member_versions",
]
BILLING_TABLES = [
    "billing_products",
    "billing_prices",
    "billing_customers",
    "billing_subscriptions",
    "billing_credit_balances",
    "billing_credit_ledger",
    "billing_stripe_events",
]


def _stream_ids(cache_url: str, channel: str) -> list[str]:
    import redis

    client = redis.Redis.from_url(cache_url)
    try:
        entries = client.xrange(f"{PREFIX}{channel}")
    finally:
        client.close()
    ids = []
    for _entry_id, fields in entries:
        data = fields.get(b"data")
        if data:
            ids.append(str(json.loads(data).get("id")))
    return ids


def _cleanup_redis(cache_url: str) -> None:
    import redis

    client = redis.Redis.from_url(cache_url)
    try:
        keys = list(client.scan_iter(match=f"{PREFIX}*", count=500))
        if keys:
            client.delete(*keys)
    finally:
        client.close()


@pytest.fixture
def infra(tmp_path: Path) -> Iterator[dict[str, Any]]:
    if os.environ.get("SANDLOOM_EXTRACTION_E2E", "").strip() != "1":
        pytest.skip("opt-in: set SANDLOOM_EXTRACTION_E2E=1 (slow end-to-end scenario)")
    base = _env_base()
    db, cache = base.get("DATABASE_URL", ""), base.get("CACHE_URL", "")
    if not (_reachable(db, 5432) and _reachable(cache, 6379)):
        pytest.fail(f"PostgreSQL / Redis unreachable ({db} / {cache})")
    names = {
        k: f"sandloom_test_extract_{RUN}_{k}" for k in ("mono", "tenancy", "billing")
    }
    _admin(
        db,
        [f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)' for n in names.values()]
        + [f'CREATE DATABASE "{n}"' for n in names.values()],
    )
    urls = {k: _db_url(db, n) for k, n in names.items()}
    _psql(urls["mono"], 'CREATE EXTENSION IF NOT EXISTS "citext"')
    _psql(urls["mono"], 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    procs: list[Proc] = []
    try:
        yield {"base": base, "urls": urls, "procs": procs, "logs": tmp_path}
    finally:
        for proc in procs:
            proc.stop()
        _cleanup_redis(cache)
        _admin(
            db, [f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)' for n in names.values()]
        )


def test_monolith_to_microservices_with_data(infra: dict[str, Any]) -> None:
    base: dict[str, str] = infra["base"]
    urls: dict[str, str] = infra["urls"]
    procs: list[Proc] = infra["procs"]
    logs: Path = infra["logs"]
    mono_url = urls["mono"]
    cache_url = base["CACHE_URL"]

    # -- 1. the monolith with a dataset ------------------------------------
    _ok(_cli(base, mono_url, "upgrade-all", "--apply"))
    _psql(
        mono_url,
        "INSERT INTO billing_products (name, type, is_active) "
        "VALUES ('Pro', 'subscription', true)",
    )
    _psql(
        mono_url,
        "INSERT INTO billing_prices (product_id, amount, currency, "
        "recurring_interval, stripe_price_id) "
        "VALUES (1, 1000, 'usd', 'month', 'price_x_pro')",
    )
    mono = Proc("monolith", (I, T, B), mono_url)
    procs.append(mono)
    mono.start(base, logs)
    _wait_ready([mono])
    router = Router({I: mono, T: mono, B: mono, PROBE: mono})
    world = asyncio.run(_seed(router, mono_url))

    # -- 2. every answer of the monolith -----------------------------------
    before = asyncio.run(_record(router, world))
    assert before["plans"][0] == 200
    assert sum(1 for s, _ in before.values() if s == 200) > 50

    # -- 3. stop writes; leave committed-but-undispatched events ------------
    mono.stop()
    in_flight: dict[str, str] = {}
    for src in ("billing.credits.spent.v1", "tenancy.%"):
        rows = _psql(
            mono_url,
            "SELECT channel, payload::text FROM outbox_messages "
            "WHERE channel LIKE %s ORDER BY emitted_at DESC LIMIT 2",
            (src,),
        )
        for real_channel, payload in rows:
            event_id = str(uuid.uuid4())
            _psql(
                mono_url,
                "INSERT INTO outbox_messages (id, channel, schema_version, payload, "
                "emitted_at, delivery_attempts) "
                "VALUES (%s, %s, 'v1', %s::json, now() AT TIME ZONE 'utc', 0)",
                (event_id, real_channel, payload),
            )
            in_flight[event_id] = real_channel
    assert len(in_flight) >= 3, "the monolith published no tenancy/billing events"
    mono_tables = _table_fingerprints(mono_url, TENANCY_TABLES + BILLING_TABLES)

    # -- 4. extract tenancy and billing ------------------------------------
    for short in ("tenancy", "billing"):
        target = urls[short]
        dry = _ok(
            _cli(
                base,
                mono_url,
                "extract-module",
                short,
                "--from",
                "default",
                "--to",
                target,
            )
        )
        assert "DRY-RUN" in dry
        assert not _psql(
            target, "SELECT 1 FROM pg_tables WHERE tablename LIKE %s", (f"{short}_%",)
        )
        refused = _cli(
            base,
            mono_url,
            "extract-module",
            short,
            "--from",
            "default",
            "--to",
            target,
            "--apply",
        )
        assert refused.returncode == 1 and "stopped-writes" in refused.stderr
        applied = _ok(
            _cli(
                base,
                mono_url,
                "extract-module",
                short,
                "--from",
                "default",
                "--to",
                target,
                "--apply",
                "--i-have-stopped-writes",
            )
        )
        assert "APPLIED" in applied and "PROBLEM" not in applied
        rerun = _ok(
            _cli(
                base,
                mono_url,
                "extract-module",
                short,
                "--from",
                "default",
                "--to",
                target,
                "--apply",
                "--i-have-stopped-writes",
            )
        )
        assert "handed off 0 outbox row(s)" in rerun  # idempotent
        _ok(
            _cli(
                base,
                mono_url,
                "extract-module",
                short,
                "--from",
                "default",
                "--to",
                target,
                "--verify",
            )
        )

    # The stale copies are fenced in the old database.
    import psycopg2

    with pytest.raises(psycopg2.Error) as err:
        _psql(mono_url, "UPDATE tenancy_members SET role = role")
    assert err.value.pgcode == "25006"

    moved = _table_fingerprints(urls["tenancy"], TENANCY_TABLES)
    moved.update(_table_fingerprints(urls["billing"], BILLING_TABLES))
    assert moved == mono_tables

    # -- 5. the same product as three services ------------------------------
    identity = Proc("identity", (I,), mono_url)
    tenancy = Proc("tenancy", (T,), urls["tenancy"])
    billing = Proc("billing", (B,), urls["billing"])
    services = {I: identity, T: tenancy, B: billing}
    for module, proc in services.items():
        proc.bindings = {
            contract: {
                "base_url": services[peer].url,
                "service_id": f"xe2e-{services[peer].name}",
                **extra,
            }
            for peer in services
            if peer != module
            for contract, extra in PEER_CONTRACTS[peer].items()
        }
    allowed = {f"xe2e-{p.name}": sorted(p.bindings) for p in services.values()}
    for proc in services.values():
        proc.allowed = allowed
        procs.append(proc)
        proc.start(base, logs)
    _wait_ready(list(services.values()))
    router = Router({I: identity, T: tenancy, B: billing, PROBE: identity})

    # -- 6. identical answers + invariants ----------------------------------
    after = asyncio.run(_record(router, world))
    diffs = {k: (before[k], after.get(k)) for k in before if before[k] != after.get(k)}
    assert not diffs, json.dumps(diffs, indent=1, default=str)[:4000]

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pending = [
            r[0]
            for url in (urls["tenancy"], urls["billing"])
            for r in _psql(
                url,
                "SELECT id FROM outbox_messages WHERE delivered_at IS NULL "
                "AND dead_at IS NULL",
            )
        ]
        if not pending:
            break
        time.sleep(0.5)
    assert not pending, f"in-flight events not relayed by the new owners: {pending}"
    for event_id, channel in in_flight.items():
        assert _stream_ids(cache_url, channel).count(event_id) == 1, (event_id, channel)
    assert not _psql(
        mono_url,
        "SELECT id FROM outbox_messages WHERE delivered_at IS NULL AND "
        "(channel LIKE 'tenancy.%%' OR channel LIKE 'billing.%%')",
    )

    # -- 7. kill billing while spends run; recover; invariants --------------
    orgs = [world.orgs[u] for u in USERS[:2]]
    start = {
        org: int(
            _psql(
                urls["billing"],
                "SELECT b.amount FROM billing_credit_balances b JOIN billing_customers "
                "c ON c.id = b.customer_id WHERE c.organization_id = %s",
                (org,),
            )[0][0]
        )
        for org in orgs
    }
    rng = random.Random(7)
    work = [
        {
            "tenant_type": "organization",
            "tenant_id": orgs[i % 2],
            "amount": rng.randint(1, 5),
            "key": f"kill-{RUN}-{i}",
        }
        for i in range(200)
    ]
    settled: dict[str, int] = {}
    stuck: set[str] = set()
    interrupted: list[str] = []
    at_kill: list[int] = []
    workload_started = _psql(urls["billing"], "SELECT now() AT TIME ZONE 'utc'")[0][0]

    async def _spend_until_settled(
        client: httpx.AsyncClient, body: dict[str, Any]
    ) -> None:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                resp = await client.post(router.url("/_x/spend"), json=body)
            except httpx.HTTPError:
                interrupted.append(body["key"])
                await asyncio.sleep(0.2)
                continue
            if resp.status_code == 200:
                settled[body["key"]] = body["amount"]
                return
            assert resp.status_code in (502, 503, 504), resp.text
            interrupted.append(body["key"])
            await asyncio.sleep(0.2)
        stuck.add(body["key"])

    async def _workload() -> None:
        sem = asyncio.Semaphore(4)
        async with httpx.AsyncClient(timeout=15) as client:

            async def one(body: dict[str, Any]) -> None:
                async with sem:
                    await _spend_until_settled(client, body)

            async def chaos() -> None:
                while len(settled) < 20:  # kill in the middle of the workload
                    await asyncio.sleep(0.01)
                at_kill.append(len(settled))
                billing.kill()
                await asyncio.sleep(1.5)
                billing.start(base, logs)
                await asyncio.to_thread(_wait_ready, [billing])

            await asyncio.gather(chaos(), *(one(b) for b in work))

    asyncio.run(_workload())
    assert len(settled) + len(stuck) == len(work)
    # The kill really hit the workload: spends were settled before it, others
    # failed while billing was down and were retried to completion.
    assert 0 < at_kill[0] < len(work), at_kill
    assert interrupted, "the kill did not interrupt any spend"
    print(
        f"\nworkload: settled={len(settled)} stuck={len(stuck)} "
        f"settled-before-kill={at_kill[0]} failed-attempts={len(interrupted)}"
    )
    # A spend in flight at the kill keeps its key reserved ("in progress", 503)
    # until the pending reservation expires (<= 5 min) — never re-applied.
    assert len(stuck) <= 4, stuck

    keys = [b["key"] for b in work]
    ledger = _psql(
        urls["billing"],
        "SELECT operation_key, count(*), -sum(delta) FROM billing_credit_ledger "
        "WHERE operation_key = ANY(%s) GROUP BY operation_key",
        (keys,),
    )
    applied_keys = {k: int(amount) for k, n, amount in ledger if n == 1}
    assert len(applied_keys) == len(ledger), "a spend was applied twice"
    for key in settled:
        assert applied_keys.get(key) == settled[key], key  # settled => applied once
    for org in orgs:
        spent = sum(
            applied_keys.get(b["key"], 0) for b in work if b["tenant_id"] == org
        )
        balance = int(
            _psql(
                urls["billing"],
                "SELECT b.amount FROM billing_credit_balances b JOIN billing_customers "
                "c ON c.id = b.customer_id WHERE c.organization_id = %s",
                (org,),
            )[0][0]
        )
        assert balance == start[org] - spent, (org, balance, start[org], spent)
        ledger_total = int(
            _psql(
                urls["billing"],
                "SELECT sum(l.delta) FROM billing_credit_ledger l JOIN "
                "billing_customers c ON c.id = l.customer_id "
                "WHERE c.organization_id = %s",
                (org,),
            )[0][0]
        )
        assert ledger_total == balance  # the ledger explains every credit
    # Exactly one billing.credits.spent.v1 event per applied spend (same
    # tenant / amount / resulting balance as its ledger row).
    events = sorted(
        (str(t), int(a), int(b))
        for t, a, b in _psql(
            urls["billing"],
            "SELECT payload::jsonb ->> 'tenant_id', "
            "(payload::jsonb -> 'data' ->> 'amount')::int, "
            "(payload::jsonb -> 'data' ->> 'balance')::int FROM outbox_messages "
            "WHERE channel = 'billing.credits.spent.v1' AND emitted_at >= %s",
            (workload_started,),
        )
    )
    spends = sorted(
        (str(org), int(-delta), int(after))
        for org, delta, after in _psql(
            urls["billing"],
            "SELECT c.organization_id, l.delta, l.balance_after FROM "
            "billing_credit_ledger l JOIN billing_customers c ON c.id = "
            "l.customer_id WHERE l.operation_key = ANY(%s)",
            (keys,),
        )
    )
    assert events == spends
    members = {
        str(org): int(n)
        for org, n in _psql(
            urls["tenancy"],
            "SELECT organization_id, count(*) FROM tenancy_members GROUP BY 1",
        )
    }
    assert members == {org: len(m) for org, m in world.members.items()}

    # -- 8. cleanup of the old database ------------------------------------
    for short in ("tenancy", "billing"):
        dropped = _ok(
            _cli(
                base,
                mono_url,
                "extract-module",
                short,
                "--from",
                "default",
                "--to",
                urls[short],
                "--drop-source",
                "--apply",
                "--i-have-stopped-writes",
            )
        )
        assert "APPLIED" in dropped
    assert not _psql(
        mono_url,
        "SELECT tablename FROM pg_tables WHERE tablename LIKE 'tenancy_%%' "
        "OR tablename LIKE 'billing_%%'",
    )

    async def _me() -> int:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                router.url("/auth/me"), headers=_auth(world.tokens[USERS[0]])
            )
            return resp.status_code

    assert asyncio.run(_me()) == 200
