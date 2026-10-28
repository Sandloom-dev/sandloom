"""Split conformance matrix: every module subset behaves like the monolith.

The framework promise is "one codebase deploys as a monolith or as
microservices purely via configuration". This test checks it for EVERY
non-empty subset ``S`` of the shipped modules ``{identity, tenancy, billing}``
(7 subsets) in two variants:

- **(a) bound** — ``S`` runs in its own process and the missing modules run in
  a peer process (its own database), wired with ``SERVICE_BINDINGS`` over the
  real internal RPC. Every response of the request script (status +
  normalised body: ids, timestamps and secrets stripped) must be IDENTICAL to
  the monolith run (all three modules in one process).
- **(b) unbound** — ``S`` runs alone with no bindings. Every response must be
  identical to the monolith or an explicit ``404`` / ``501`` / ``503`` —
  never a ``500`` and never a different ``2xx`` body (no silent degradation).

``S = {identity, tenancy, billing}`` is the monolith itself. Deployments are
shared across cases: a split ``{X} | rest`` serves variant (a) of both ``{X}``
and its complement, so the matrix runs 15 deployments (21 uvicorn processes)
once per session on databases cloned from one template (``reset_schema`` +
``CREATE DATABASE ... TEMPLATE``) — in WAVES that each stay within
:data:`CONNECTION_BUDGET` PostgreSQL connections (every pool of every process
at its maximum + the script's own SQL), so one run never takes more than that
of the shared server; a server without that much room fails the run at once
with a clear message instead of "too many clients" mid-run. The deployments
the live tests use (:data:`LIVE_DEPLOYMENTS`) run in the last wave and stay up.

The script covers register / verify / login, ``/auth/me``, organizations,
members, invitations (create, list, accept), billing plans / subscription /
credits / checkout, and probe endpoints gated by ``OrgRole``,
``SubscriptionActive``, ``Credits``, ``Entitled(...)`` and ``Subscribed(of=Account.user())``
(``conformance_probe``, loaded in every process; probe requests are sent to
EVERY process of a deployment). Where a deployment has no identity, the
access token is minted with the monolith's claims for the same step (ids
remapped), i.e. "a token issued elsewhere".

Skips without PostgreSQL / Redis — unless ``SANDLOOM_E2E_REQUIRED=1`` (CI).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import httpx
import jwt
import pytest

from sandloom.core import rpc_binding
from sandloom.core.rpc import outbound as rpc_outbound
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.signing_keys import (
    generate_private_key,
    generate_x25519_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)
from sandloom.core.testing import worker_env_values
from sandloom.core.testing.connections import (
    TARGET_RUN_CONNECTIONS,
    ConnectionBudgetError,
    check_headroom,
    pool_connections,
)
from sandloom.core.testing.served import (
    ReservedPort,
    SpawnedServer,
    SpawnedServerError,
    probe,
    spawn_served,
)

pytestmark = [pytest.mark.split_conformance, pytest.mark.e2e, pytest.mark.slow]

BACKEND = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
RUN = uuid.uuid4().hex[:8]
INTERNAL_TOKEN = f"conformance-internal-{uuid.uuid4().hex}"
JWT_SECRET = f"conformance-jwt-{uuid.uuid4().hex}"
#: Access tokens are ASYMMETRIC (EdDSA) in every deployment of the matrix, as
#: in deploy/: a process hosting identity (the issuer) holds the private key
#: (+ JWT_SECRET for identity's emailed tokens); every other process verifies
#: with the public key only and could not mint.
JWT_KID = "conformance-jwt-1"
_JWT_KEY = generate_private_key("EdDSA")
JWT_PRIVATE_KEY = private_pem(_JWT_KEY)
JWT_PUBLIC_KEYS = json.dumps({JWT_KID: public_pem(_JWT_KEY)})
#: Internal RPC runs with per-pair request MACs (docs/adr/0004): every
#: process holds only its own X25519 key; each request is MACed with the
#: caller->provider pair key, each response with the provider->caller key,
#: and a provider allows
#: exactly what the caller's SERVICE_BINDINGS bind.
_DH_KEYS: dict[str, tuple[str, str]] = {}
#: ...plus its Ed25519 key, which signs its COMMAND calls on top of the MAC
#: (RPC_COMMANDS_REQUIRE_SIGNATURE=on, the prod default: a leaked provider
#: X25519 key must never forge an effect — ADR-0004, KCI).
_RPC_KEYS: dict[str, tuple[str, str]] = {}


def _rpc_key(service_id: str) -> tuple[str, str]:
    if service_id not in _RPC_KEYS:
        key = generate_private_key("EdDSA")
        _RPC_KEYS[service_id] = (private_pem(key), public_pem(key))
    return _RPC_KEYS[service_id]


def _dh_key(service_id: str) -> tuple[str, str]:
    if service_id not in _DH_KEYS:
        key = generate_x25519_key()
        _DH_KEYS[service_id] = (x25519_private_pem(key), x25519_public_pem(key))
    return _DH_KEYS[service_id]


def _rpc_identity_env(dep: Deployment, proc: Proc) -> dict[str, str]:
    def sid(p: Proc) -> str:
        return f"{dep.name}-{p.label}"

    keys = {sid(p): {"k1": _rpc_key(sid(p))[1]} for p in dep.procs}
    allowed = {}
    for p in dep.procs:
        peers = [q for q in dep.procs if q is not p]
        bound = _bindings(p, peers[0] if peers else None)
        if bound:
            allowed[sid(p)] = sorted(bound)
    return {
        "RPC_COMMANDS_REQUIRE_SIGNATURE": "on",
        "RPC_SERVICE_ID": sid(proc),
        "RPC_PRIVATE_KEY": _rpc_key(sid(proc))[0],
        "RPC_KEY_ID": "k1",
        "RPC_SERVICE_KEYS": json.dumps(keys),
        "RPC_ALLOWED_CALLS": json.dumps(allowed),
        "RPC_DH_PRIVATE_KEY": _dh_key(sid(proc))[0],
        "RPC_DH_KEY_ID": "d1",
        "RPC_SERVICE_DH_KEYS": json.dumps(
            {sid(p): {"d1": _dh_key(sid(p))[1]} for p in dep.procs}
        ),
    }


STARTUP_TIMEOUT = 90.0

I, T, B = "sandloom_identity", "sandloom_tenancy", "sandloom_billing"  # noqa: E741
ALL = (I, T, B)
SHORT = {I: "identity", T: "tenancy", B: "billing"}
PROBE = "conformance_probe"

#: Contracts a process binds for each module hosted by its peer (variant a):
#: what the loaded modules + the probe consume, as in deploy/.
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

#: Statuses an unbound subset may answer instead of the monolith's response.
EXPLICIT_DEGRADATION = frozenset({404, 501, 503})
#: Denials an unbound subset may answer when the request names an entity it
#: could not create (e.g. an organization id without tenancy deployed).
INPUT_DIVERGED_DENIALS = frozenset({401, 403, 404})


# ---------------------------------------------------------------------------
# infrastructure
# ---------------------------------------------------------------------------


def _test_env() -> dict[str, str]:
    # Worker-aware under pytest-xdist (own database / Redis DBs per worker).
    values = worker_env_values(BACKEND / ".env.test")
    for key in ("DATABASE_URL", "CACHE_URL", "BROKER_URL"):
        if os.environ.get(f"E2E_{key}"):
            values[key] = os.environ[f"E2E_{key}"]
    return values


def _unavailable(reason: str) -> NoReturn:
    if os.environ.get("SANDLOOM_E2E_REQUIRED", "").strip() == "1":
        pytest.fail(f"conformance infrastructure required but unavailable: {reason}")
    pytest.skip(reason)


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


async def _create_template(base_url: str, template: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    admin = create_async_engine(
        _db_url(base_url, "postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(
                text(f'DROP DATABASE IF EXISTS "{template}" WITH (FORCE)')
            )
            await conn.execute(text(f'CREATE DATABASE "{template}"'))
    finally:
        await admin.dispose()
    engine = create_async_engine(_db_url(base_url, template))
    try:
        async with engine.begin() as conn:
            for ext in ("citext", "uuid-ossp"):
                await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{ext}"'))
    finally:
        await engine.dispose()


def _migrate_template(base: dict[str, str], url: str) -> None:
    """The deployed schema: ``sandloom-migrate upgrade-all`` (core + module chains)."""
    env = {
        **os.environ,
        **base,
        "ENVIRONMENT": "test",
        "DATABASE_URL": url,
        "SANDLOOM_MODULES": json.dumps(list(ALL)),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sandloom.core.cli.migrate",
            "upgrade-all",
            "--apply",
        ],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]


async def _seed_template(url: str) -> None:
    """One subscription plan (price id 1) in every billing database."""
    await _sql(
        url,
        "INSERT INTO billing_products (id, name, type, is_active) "
        "VALUES (1, 'Pro', 'subscription', true)",
    )
    await _sql(
        url,
        "INSERT INTO billing_prices (id, product_id, amount, currency, "
        "recurring_interval, stripe_price_id) "
        "VALUES (1, 1, 1000, 'usd', 'month', 'price_conf_pro')",
    )


async def _clone_databases(base_url: str, template: str, names: list[str]) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    admin = create_async_engine(
        _db_url(base_url, "postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            for name in names:
                await conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                )
                await conn.execute(
                    text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
                )
    finally:
        await admin.dispose()


async def _drop_databases(base_url: str, names: list[str]) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    admin = create_async_engine(
        _db_url(base_url, "postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            for name in names:
                await conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                )
    finally:
        await admin.dispose()


async def _sql(url: str, statement: str, **params: Any) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


def _cleanup_streams(cache_url: str, prefix: str) -> None:
    import redis

    client = redis.Redis.from_url(cache_url)
    try:
        keys = list(client.scan_iter(match=f"{prefix}*", count=500))
        if keys:
            client.delete(*keys)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# deployments
# ---------------------------------------------------------------------------


@dataclass
class Proc:
    """One uvicorn process of a deployment."""

    deployment: str
    modules: tuple[str, ...]
    db_name: str
    #: The loopback port, held by the harness from allocation until the final
    #: stop and handed to the child as a listening socket — no TOCTOU window
    #: in which another process could take it.
    listener: ReservedPort = field(default_factory=ReservedPort)
    extra_env: dict[str, str] = field(default_factory=dict)
    served: SpawnedServer | None = None
    log_path: Path | None = None

    @property
    def proc(self) -> subprocess.Popen[bytes] | None:
        return self.served.proc if self.served is not None else None

    @property
    def url(self) -> str:
        return self.listener.url

    @property
    def label(self) -> str:
        return "+".join(SHORT[m] for m in self.modules)

    def start(self, env: dict[str, str], log_dir: Path) -> None:
        self.log_path = log_dir / f"{self.deployment}-{self.label}.log"
        with self.log_path.open("wb") as log:
            self.served = spawn_served(
                self.listener,
                "sandloom.main:app",
                env=env,
                label=f"{self.deployment}/{self.label}",
                cwd=BACKEND,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

    def logs(self) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        return self.log_path.read_text(errors="replace")[-8000:]

    def stop(self) -> None:
        if self.served is not None:
            self.served.stop()
        self.listener.close()


@dataclass
class Deployment:
    name: str
    procs: list[Proc]
    bound: bool

    def host(self, module: str) -> Proc | None:
        for proc in self.procs:
            if module in proc.modules:
                return proc
        return None

    def target(self, module: str) -> Proc:
        """The process a gateway routes ``module``'s routes to.

        A module absent from the deployment is sent to the first process (the
        route does not exist there: an explicit 404).
        """
        return self.host(module) or self.procs[0]


def _deployment_plan() -> dict[str, list[tuple[str, ...]]]:
    plan: dict[str, list[tuple[str, ...]]] = {"monolith": [ALL]}
    for x in ALL:
        rest = tuple(m for m in ALL if m != x)
        plan[f"split-{SHORT[x]}"] = [(x,), rest]  # default RPC_TRANSPORT
        # Every other RPC client is held to the same script, every run.
        for transport in OTHER_TRANSPORTS:
            plan[f"split-{SHORT[x]}-{transport}"] = [(x,), rest]
    for subset in _proper_subsets():
        plan[f"alone-{'+'.join(SHORT[m] for m in subset)}"] = [subset]
    # Not a subset case: identity | tenancy with revocation-write fault
    # injection in the tenancy process (test_revocation_across_services_*).
    plan[FAULT_DEPLOYMENT] = [(I,), (T,)]
    return plan


#: The fault-injection deployment (excluded from the script runs).
FAULT_DEPLOYMENT = "fault-revocation"

# ---------------------------------------------------------------------------
# connection budget: boot the matrix in waves
# ---------------------------------------------------------------------------

#: Pools of every matrix process. Overflow connections close as soon as they
#: are returned (no 30 s linger): a process holds what it uses right now.
PROC_POOLS: dict[str, str] = {
    "DB_POOL_SIZE": "1",
    "DB_MAX_OVERFLOW": "1",
    "CONTRACT_DB_POOL_SIZE": "1",
    "CONTRACT_DB_MAX_OVERFLOW": "1",
    "DB_POOL_OVERFLOW_LINGER_SECONDS": "0",
}
#: Connections one process can hold at most (request + contract pool).
PER_PROCESS_CONNECTIONS = pool_connections(PROC_POOLS)
#: What the matrix may hold at once on the shared server.
CONNECTION_BUDGET = TARGET_RUN_CONNECTIONS
#: Deployments the tests call live (they run in the last wave, kept up).
LIVE_DEPLOYMENTS = ("split-billing", FAULT_DEPLOYMENT)


def wave_connections(process_counts: list[int]) -> int:
    """Connections a wave of deployments (their process counts) can hold:
    every pool at its maximum, + one connection per deployment for the
    script's direct SQL steps, + one admin connection."""
    return sum(process_counts) * PER_PROCESS_CONNECTIONS + len(process_counts) + 1


def plan_waves(
    process_counts: dict[str, int], budget: int = CONNECTION_BUDGET
) -> list[list[str]]:
    """Deployments in boot waves, each within ``budget`` connections.

    The monolith runs in the first wave (the others are compared with its
    run); the live deployments go together into the LAST wave.
    """
    live = [n for n in LIVE_DEPLOYMENTS if n in process_counts]
    rest = [n for n in process_counts if n not in live]
    rest.sort(key=lambda n: n != "monolith")
    waves: list[list[str]] = []
    for name in rest:
        if (
            waves
            and wave_connections([process_counts[n] for n in [*waves[-1], name]])
            <= budget
        ):
            waves[-1].append(name)
        else:
            waves.append([name])
    if live:
        if (
            waves
            and wave_connections([process_counts[n] for n in [*waves[-1], *live]])
            <= budget
        ):
            waves[-1].extend(live)
        else:
            waves.append(live)
    for wave in waves:
        need = wave_connections([process_counts[n] for n in wave])
        assert need <= budget, (wave, need, budget)
    return waves


#: RPC_TRANSPORT of the default deployments, and the other selectable clients
#: (each gets its own copy of every bound split).
DEFAULT_TRANSPORT = "aiohttp"
OTHER_TRANSPORTS = ("httpx",)


def _transport_of(deployment: str) -> str:
    suffix = deployment.rsplit("-", 1)[-1]
    return suffix if suffix in OTHER_TRANSPORTS else DEFAULT_TRANSPORT


def _proper_subsets() -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for mask in range(1, 7):
        out.append(tuple(m for i, m in enumerate(ALL) if mask & (1 << i)))
    return out


def _bindings(
    proc: Proc, peer: Proc | None, peer_service_id: str | None = None
) -> dict[str, Any]:
    if peer is None:
        return {}
    pin = {"service_id": peer_service_id} if peer_service_id else {}
    out: dict[str, Any] = {}
    for module in peer.modules:
        for contract, extra in PEER_CONTRACTS[module].items():
            out[contract] = {"base_url": peer.url, **pin, **extra}
    return out


def _proc_env(
    base: dict[str, str], dep: Deployment, proc: Proc, base_db_url: str
) -> dict[str, str]:
    peers = [p for p in dep.procs if p is not proc]
    env = {**os.environ, **base}
    env.update(
        {
            "ENVIRONMENT": "test",
            "DATABASE_URL": _db_url(base_db_url, proc.db_name),
            "SANDLOOM_MODULES": json.dumps([*proc.modules, PROBE]),
            "SERVICE_BINDINGS": json.dumps(
                _bindings(
                    proc,
                    peers[0] if peers else None,
                    f"{dep.name}-{peers[0].label}" if peers else None,
                )
            ),
            "SANDLOOM_SERVICE_NAME": f"{dep.name}-{proc.label}",
            "EVENT_BUS_BACKEND": "streams",
            "EVENT_STREAM_PREFIX": f"conf-{RUN}-{dep.name}:",
            "INTERNAL_API_TOKEN": INTERNAL_TOKEN,
            "JWT_ALGORITHM": "EdDSA",
            "JWT_PUBLIC_KEYS": JWT_PUBLIC_KEYS,
            "JWT_KID": JWT_KID,
            # Issuer-only material; "" (= unset, also over a .env value) on a
            # verify-only process — the kernel refuses it there.
            "JWT_PRIVATE_KEY": JWT_PRIVATE_KEY if I in proc.modules else "",
            "JWT_SECRET": JWT_SECRET if I in proc.modules else "",
            "JWT_PREVIOUS_SECRETS": "[]",
            "RATE_LIMIT_ENABLED": "false",
            "NOTIFIER_BACKEND": "log",
            "BILLING_PROVIDER": "fake",
            "SERVICE_RPC_TIMEOUT_SECONDS": "10",
            "RPC_TRANSPORT": _transport_of(dep.name),
            **PROC_POOLS,
            "LOG_FORMAT": "text",
            "PYTHONPATH": os.pathsep.join(
                [str(HERE), str(BACKEND / "src"), os.environ.get("PYTHONPATH", "")]
            ),
        }
    )
    env.update(_rpc_identity_env(dep, proc))
    for key in ("MODULE_REQUIRED", "INFRA", "ACCESS"):
        env.pop(key, None)
    env.update(proc.extra_env)
    return env


def _wait_ready(procs: list[Proc]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    pending = list(procs)
    while pending and time.monotonic() < deadline:
        for proc in list(pending):
            assert proc.served is not None
            # Identity, not just "something answers": the port must be served
            # by THIS spawn (nonce), and a dead child fails at once.
            try:
                ready = probe(proc.served)
            except SpawnedServerError as exc:
                raise AssertionError(f"{exc}\n{proc.logs()}") from None
            if ready:
                pending.remove(proc)
        time.sleep(0.2)
    if pending:
        names = ", ".join(f"{p.deployment}/{p.label}" for p in pending)
        raise AssertionError(f"did not start: {names}\n{pending[0].logs()}")


# ---------------------------------------------------------------------------
# the request script
# ---------------------------------------------------------------------------

PASSWORD = "Conf-Passw0rd!x"
#: Per-run addresses: login lockout counters live in the shared cache.
EMAILS = {u: f"{u}-{RUN}@conformance.example" for u in ("alice", "bob")}


@dataclass(frozen=True)
class Step:
    name: str
    module: str  # I / T / B / PROBE (probe: sent to every process)
    method: str = "GET"
    path: str = ""
    body: dict[str, Any] | None = None
    auth: str | None = None  # token alias
    headers: dict[str, str] | None = None
    save: dict[str, str] | None = None  # alias -> response key
    login: tuple[str, str] | None = None  # (user, token alias)
    action: str | None = None  # "verify" | "activate" | "tick" | "past-skew"
    expect: int | None = None  # monolith status (script sanity)
    save_token: str | None = None  # token alias <- response access_token
    #: Modules whose earlier state change this step observes (e.g. a
    #: revocation by identity). An unbound subset lacking one cannot have made
    #: that change, so only "no 5xx" is checked there.
    needs: frozenset[str] = frozenset()


def _script() -> list[Step]:
    s: list[Step] = []
    for user in ("alice", "bob"):
        s.append(
            Step(
                f"register-{user}",
                I,
                "POST",
                "/auth/register",
                {"email": EMAILS[user], "full_name": user, "password": PASSWORD},
                expect=202,
            )
        )
    s.append(Step("verify-users", I, action="verify"))
    s += [
        Step("login-alice-1", I, login=("alice", "a1"), expect=200),
        Step("me-alice", I, "GET", "/auth/me", auth="a1", expect=200),
        Step("me-anonymous", I, "GET", "/auth/me", expect=401),
        Step("plans", B, "GET", "/api/v1/billing/plans", expect=200),
        Step(
            "sub-personal",
            B,
            "GET",
            "/api/v1/billing/subscription",
            auth="a1",
            expect=200,
        ),
        Step(
            "credits-personal",
            B,
            "GET",
            "/api/v1/billing/credits",
            auth="a1",
            expect=200,
        ),
        Step(
            "p-entitled-personal-1",
            PROBE,
            "GET",
            "/_conf/entitled/personal",
            auth="a1",
            expect=402,
        ),
        Step(
            "p-subscribed-1", PROBE, "GET", "/_conf/subscribed", auth="a1", expect=402
        ),
        Step(
            "create-org",
            T,
            "POST",
            "/api/v1/orgs",
            {"name": "Conformance Org", "slug": "conformance-org"},
            auth="a1",
            save={"org": "id"},
            expect=201,
        ),
        Step("list-orgs", T, "GET", "/api/v1/orgs", auth="a1", expect=200),
        Step("login-alice-2", I, login=("alice", "a2"), expect=200),
        Step("get-org", T, "GET", "/api/v1/orgs/{org}", auth="a2", expect=200),
        # Organization ownership is insufficient for the global owner console,
        # equally in the monolith and every bound/unbound module subset.
        Step(
            "platform-org-denied",
            T,
            "GET",
            "/api/v1/platform/tenancy/organizations",
            auth="a2",
            expect=403,
        ),
        Step(
            "platform-billing-denied",
            B,
            "GET",
            "/api/v1/platform/billing/customers",
            auth="a2",
            expect=403,
        ),
        Step(
            "platform-audit-denied",
            T,
            "GET",
            "/api/v1/platform/tenancy/audit",
            auth="a2",
            expect=403,
        ),
        Step(
            "members-1", T, "GET", "/api/v1/orgs/{org}/members", auth="a2", expect=200
        ),
        Step(
            "invite-bob",
            T,
            "POST",
            "/api/v1/orgs/{org}/invitations",
            {"email": EMAILS["bob"], "role": "member"},
            auth="a2",
            save={"invite_token": "token"},
            expect=201,
        ),
        Step(
            "invitations",
            T,
            "GET",
            "/api/v1/orgs/{org}/invitations",
            auth="a2",
            expect=200,
        ),
        Step("login-bob-1", I, login=("bob", "b1"), expect=200),
        Step(
            "p-member-bob-before",
            PROBE,
            "GET",
            "/_conf/member/{org}",
            auth="b1",
            expect=403,
        ),
        Step(
            "accept-invite",
            T,
            "POST",
            "/api/v1/invitations/accept",
            {"token": "{invite_token}"},
            auth="b1",
            expect=200,
        ),
        Step(
            "members-2", T, "GET", "/api/v1/orgs/{org}/members", auth="a2", expect=200
        ),
        Step("login-bob-2", I, login=("bob", "b2"), expect=200),
        Step(
            "sub-org",
            B,
            "GET",
            "/api/v1/billing/subscription?org_id={org}",
            auth="a2",
            expect=200,
        ),
        Step(
            "sub-single-org",
            B,
            "GET",
            "/api/v1/billing/subscription",
            auth="a2",
            expect=200,
        ),
        Step(
            "sub-foreign-org",
            B,
            "GET",
            "/api/v1/billing/subscription?org_id={foreign}",
            auth="a2",
            expect=403,
        ),
        Step(
            "credits-org-bob",
            B,
            "GET",
            "/api/v1/billing/credits?org_id={org}",
            auth="b2",
            expect=200,
        ),
        Step(
            "checkout-member-denied",
            B,
            "POST",
            "/api/v1/billing/checkout-session",
            {"price_id": 1, "org_id": "{org}"},
            auth="b2",
            headers={"Idempotency-Key": "conf-checkout-bob"},
            expect=403,
        ),
        Step(
            "checkout-owner",
            B,
            "POST",
            "/api/v1/billing/checkout-session",
            {"price_id": 1, "org_id": "{org}"},
            auth="a2",
            headers={"Idempotency-Key": "conf-checkout-alice"},
            expect=200,
        ),
        Step(
            "checkout-unknown-price",
            B,
            "POST",
            "/api/v1/billing/checkout-session",
            {"price_id": 999, "org_id": "{org}"},
            auth="a2",
            headers={"Idempotency-Key": "conf-checkout-404"},
            expect=404,
        ),
        Step(
            "checkout-owner-replay",  # same key: the stored response replays
            B,
            "POST",
            "/api/v1/billing/checkout-session",
            {"price_id": 1, "org_id": "{org}"},
            auth="a2",
            headers={"Idempotency-Key": "conf-checkout-alice"},
            expect=200,
        ),
        Step(
            "promote-bob",
            T,
            "PATCH",
            "/api/v1/orgs/{org}/members/{user:bob}",
            {"role": "admin"},
            auth="a2",
            expect=200,
        ),
        # A role change revokes the member's tokens (immediately, in the
        # shared cache): every process rejects bob's old token at once.
        Step(
            "p-member-bob-after-promote",
            PROBE,
            "GET",
            "/_conf/member/{org}",
            auth="b2",
            expect=401,
            needs=frozenset({T}),
        ),
        Step("past-promote-skew", T, action="past-skew"),
        Step("login-bob-2b", I, login=("bob", "b2"), expect=200),
        Step(
            "checkout-bob-admin",  # the new role is read live (over RPC)
            B,
            "POST",
            "/api/v1/billing/checkout-session",
            {"price_id": 1, "org_id": "{org}"},
            auth="b2",
            headers={"Idempotency-Key": "conf-checkout-bob-admin"},
            expect=200,
        ),
        Step(
            "p-orgrole-owner",
            PROBE,
            "GET",
            "/_conf/orgrole/{org}",
            auth="a2",
            expect=200,
        ),
        Step(
            "p-orgrole-member",
            PROBE,
            "GET",
            "/_conf/orgrole/{org}",
            auth="b2",
            expect=403,
        ),
        Step(
            "p-member-bob", PROBE, "GET", "/_conf/member/{org}", auth="b2", expect=200
        ),
        Step(
            "p-sub-active-1",
            PROBE,
            "GET",
            "/_conf/subscription-active/{org}",
            auth="a2",
            expect=402,
        ),
        Step(
            "p-credits-1", PROBE, "GET", "/_conf/credits/{org}", auth="a2", expect=402
        ),
        Step(
            "p-entitled-org-1",
            PROBE,
            "GET",
            "/_conf/entitled/org/{org}",
            auth="b2",
            expect=402,
        ),
        Step(
            "p-entitled-foreign",
            PROBE,
            "GET",
            "/_conf/entitled/org/{foreign}",
            auth="a2",
            expect=403,
        ),
        Step("activate-org", B, action="activate"),
        Step(
            "p-sub-active-2",
            PROBE,
            "GET",
            "/_conf/subscription-active/{org}",
            auth="a2",
            expect=200,
        ),
        Step(
            "p-credits-2", PROBE, "GET", "/_conf/credits/{org}", auth="a2", expect=200
        ),
        Step(
            "p-entitled-org-2",
            PROBE,
            "GET",
            "/_conf/entitled/org/{org}",
            auth="b2",
            expect=200,
        ),
        Step(
            "sub-org-active",
            B,
            "GET",
            "/api/v1/billing/subscription?org_id={org}",
            auth="b2",
            expect=200,
        ),
        Step(
            "credits-org",
            B,
            "GET",
            "/api/v1/billing/credits?org_id={org}",
            auth="a2",
            expect=200,
        ),
        Step(
            "p-entitled-personal-2",
            PROBE,
            "GET",
            "/_conf/entitled/personal",
            auth="a2",
            expect=402,
        ),
        Step("login-alice-3", I, login=("alice", "a3"), expect=200),
        Step(
            "p-subscribed-2", PROBE, "GET", "/_conf/subscribed", auth="a3", expect=402
        ),
        Step("me-alice-3", I, "GET", "/auth/me", auth="a3", expect=200),
        # Revocation is written by identity into the SHARED cache and must be
        # honoured by every process (the iat < mark rule needs a new second).
        Step("next-second", I, action="tick"),
        Step(
            "change-password",
            I,
            "POST",
            "/auth/change-password",
            {"current_password": PASSWORD, "new_password": PASSWORD + "2"},
            auth="a3",
            save_token="a4",
            expect=200,
        ),
        Step(
            "p-subscribed-revoked",
            PROBE,
            "GET",
            "/_conf/subscribed",
            auth="a3",
            expect=401,
            needs=frozenset({I}),
        ),
        Step(
            "sub-org-revoked",
            B,
            "GET",
            "/api/v1/billing/subscription?org_id={org}",
            auth="a2",
            expect=401,
            needs=frozenset({I}),
        ),
        Step(
            "members-revoked",
            T,
            "GET",
            "/api/v1/orgs/{org}/members",
            auth="a2",
            expect=401,
            needs=frozenset({I}),
        ),
        Step(
            "members-new-token",
            T,
            "GET",
            "/api/v1/orgs/{org}/members",
            auth="a4",
            expect=200,
            needs=frozenset({I}),
        ),
        Step(
            "p-orgrole-new-token",
            PROBE,
            "GET",
            "/_conf/orgrole/{org}",
            auth="a4",
            expect=200,
            needs=frozenset({I}),
        ),
        Step(
            "sub-org-new-token",
            B,
            "GET",
            "/api/v1/billing/subscription?org_id={org}",
            auth="a4",
            expect=200,
            needs=frozenset({I}),
        ),
        Step(
            "refresh-bob",
            I,
            "POST",
            "/auth/refresh",
            {"refresh_token": "{tok:b2:refresh}"},
            save_token="b3",
            expect=200,
        ),
        Step(
            "p-member-bob-refreshed",
            PROBE,
            "GET",
            "/_conf/member/{org}",
            auth="b3",
            expect=200,
            needs=frozenset({I}),
        ),
        Step(
            "p-entitled-org-refreshed",
            PROBE,
            "GET",
            "/_conf/entitled/org/{org}",
            auth="b3",
            expect=200,
            needs=frozenset({I}),
        ),
        # Removal in tenancy: bob's token is rejected on the NEXT request,
        # whichever service serves it (probes hit every process).
        Step(
            "remove-bob",
            T,
            "DELETE",
            "/api/v1/orgs/{org}/members/{user:bob}",
            auth="a4",
            expect=204,
            needs=frozenset({I}),
        ),
        Step(
            "p-member-bob-removed",
            PROBE,
            "GET",
            "/_conf/member/{org}",
            auth="b3",
            expect=401,
            needs=frozenset({I, T}),
        ),
        Step(
            "me-bob-removed",
            I,
            "GET",
            "/auth/me",
            auth="b3",
            expect=401,
            needs=frozenset({I, T}),
        ),
        Step(
            "orgs-bob-removed",
            T,
            "GET",
            "/api/v1/orgs",
            auth="b3",
            expect=401,
            needs=frozenset({I, T}),
        ),
        Step(
            "credits-bob-removed",
            B,
            "GET",
            "/api/v1/billing/credits?org_id={org}",
            auth="b3",
            expect=401,
            needs=frozenset({I, T}),
        ),
    ]
    return s


# ---------------------------------------------------------------------------
# running a script
# ---------------------------------------------------------------------------

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_SECRET_KEYS = frozenset({"access_token", "refresh_token", "token", "url"})


def normalise(value: Any) -> Any:
    """Strip ids, timestamps and secrets so two deployments compare."""
    if isinstance(value, dict):
        return {
            k: "<secret>" if k in _SECRET_KEYS and v else normalise(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [normalise(v) for v in value]
    if isinstance(value, str):
        if _TS.match(value):
            return "<ts>"
        return _UUID.sub("<uuid>", value)
    return value


@dataclass
class Result:
    step: str
    target: str  # process label
    status: int
    body: Any
    #: Aliases this deployment had to invent for the request (their creating
    #: step answered no id here, e.g. no tenancy -> no organization).
    invented: frozenset[str] = frozenset()

    @property
    def key(self) -> tuple[int, Any]:
        return (self.status, self.body)


@dataclass
class RunState:
    deployment: Deployment
    ids: dict[str, str] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    claims: dict[str, dict[str, Any]] = field(default_factory=dict)  # per step
    results: list[Result] = field(default_factory=list)
    invented: set[str] = field(default_factory=set)

    def ident(self, alias: str) -> str:
        if alias not in self.ids:  # absent here (e.g. no tenancy): unknown id
            self.ids[alias] = str(uuid.uuid4())
            self.invented.add(alias)
        return self.ids[alias]

    def invented_in(self, *templates: Any) -> frozenset[str]:
        text = json.dumps(templates)
        return frozenset(a for a in self.invented if "{" + a + "}" in text)


def _fill(template: Any, state: RunState) -> Any:
    if isinstance(template, str):
        template = re.sub(
            r"\{tok:([\w:]+)\}",
            lambda m: state.tokens.get(m.group(1), "missing-token"),
            template,
        )
        return re.sub(r"\{([\w:]+)\}", lambda m: state.ident(m.group(1)), template)
    if isinstance(template, dict):
        return {k: _fill(v, state) for k, v in template.items()}
    return template


def _remap(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_remap(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _remap(v, mapping) for k, v in value.items()}
    return value


def _mint_like(monolith: RunState, state: RunState, step: str, user: str) -> str:
    """A token "issued elsewhere": the monolith's claims, ids remapped."""
    if step not in monolith.claims:
        failed = [r for r in monolith.results if r.step == step]
        raise AssertionError(f"monolith {step} failed: {failed}")
    claims = dict(monolith.claims[step])
    mapping = {mono_id: state.ident(alias) for alias, mono_id in monolith.ids.items()}
    claims = _remap(claims, mapping)
    claims["sub"] = state.ident(f"user:{user}")
    now = int(time.time())
    claims.update({"iat": now, "exp": now + 900, "jti": uuid.uuid4().hex})
    return jwt.encode(claims, _JWT_KEY, algorithm="EdDSA", headers={"kid": JWT_KID})


async def _send(
    client: httpx.AsyncClient, proc: Proc, step: Step, state: RunState
) -> Result:
    headers = dict(step.headers or {})
    if step.auth is not None and step.auth in state.tokens:
        headers["Authorization"] = f"Bearer {state.tokens[step.auth]}"
    if "Idempotency-Key" in headers:  # one key per deployment (shared cache)
        headers["Idempotency-Key"] += f"-{RUN}-{state.deployment.name}"
    resp = await client.request(
        step.method,
        f"{proc.url}{_fill(step.path, state)}",
        json=_fill(step.body, state) if step.body is not None else None,
        headers=headers,
    )
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    if resp.status_code < 300 and step.save and isinstance(body, dict):
        for alias, key in step.save.items():
            if key in body:
                state.ids[alias] = str(body[key])
    if resp.status_code < 300 and step.save_token and isinstance(body, dict):
        state.tokens[step.save_token] = body["access_token"]
    return Result(
        step.name,
        proc.label,
        resp.status_code,
        normalise(body),
        state.invented_in(step.path, step.body),
    )


async def _action(step: Step, state: RunState, base_db_url: str) -> None:
    dep = state.deployment
    if step.action == "tick":
        await asyncio.sleep(1.05)
    elif step.action == "past-skew":
        # A revocation mark is ``now + AUTH_REVOCATION_CLOCK_SKEW_SECONDS``
        # (2 s): a token minted blind (no issuance check, whole-second iat —
        # "issued elsewhere") must wait for the mark's second to pass.
        await asyncio.sleep(3.2)
    elif step.action == "verify":
        proc = dep.host(I)
        if proc is not None:
            await _sql(
                _db_url(base_db_url, proc.db_name),
                "UPDATE users SET is_verified = true",
            )
    elif step.action == "activate":
        proc = dep.host(B)
        if proc is None:
            return
        url = _db_url(base_db_url, proc.db_name)
        org = state.ident("org")
        await _sql(
            url,
            "INSERT INTO billing_customers (id, organization_id) "
            "VALUES (CAST(:cid AS uuid), CAST(:org AS uuid)) "
            "ON CONFLICT (organization_id) DO NOTHING",
            cid=str(uuid.uuid4()),
            org=org,
        )
        await _sql(
            url,
            # origin 'intent': an AUTHORIZED subscription (a row without an
            # origin is intent-less and grants nothing - ADR-0003 item 9).
            "INSERT INTO billing_subscriptions (customer_id, price_id, status, "
            "current_period_end, cancel_at_period_end, origin) "
            "SELECT id, 1, 'active', now() + interval '30 days', false, 'intent' "
            "FROM billing_customers WHERE organization_id = CAST(:org AS uuid)",
            org=org,
        )
        await _sql(
            url,
            "INSERT INTO billing_credit_balances (customer_id, amount) "
            "SELECT id, 100 FROM billing_customers "
            "WHERE organization_id = CAST(:org AS uuid) "
            "ON CONFLICT (customer_id) DO UPDATE SET amount = 100",
            org=org,
        )


async def run_script(
    dep: Deployment, base_db_url: str, monolith: RunState | None
) -> RunState:
    state = RunState(dep)
    async with httpx.AsyncClient(timeout=30) as client:
        for step in _script():
            if step.action is not None:
                await _action(step, state, base_db_url)
                continue
            if step.login is not None:
                user, alias = step.login
                proc = dep.target(I)
                resp = await client.post(
                    f"{proc.url}/auth/login",
                    json={"email": EMAILS[user], "password": PASSWORD},
                )
                body: Any = resp.json() if resp.content else None
                state.results.append(
                    Result(step.name, proc.label, resp.status_code, normalise(body))
                )
                if resp.status_code == 200:
                    token = body["access_token"]
                    state.tokens[f"{alias}:refresh"] = body["refresh_token"]
                    claims = jwt.decode(token, options={"verify_signature": False})
                    state.ids[f"user:{user}"] = claims["sub"]
                elif monolith is not None:
                    token = _mint_like(monolith, state, step.name, user)
                    claims = jwt.decode(token, options={"verify_signature": False})
                else:  # monolith login must work (checked by expect)
                    continue
                state.tokens[alias] = token
                state.claims[step.name] = claims
                continue
            procs = dep.procs if step.module == PROBE else [dep.target(step.module)]
            for proc in procs:
                state.results.append(await _send(client, proc, step, state))
    return state


# ---------------------------------------------------------------------------
# fixture: boot the whole matrix once
# ---------------------------------------------------------------------------


@dataclass
class Matrix:
    deployments: dict[str, Deployment]
    runs: dict[str, RunState]
    ready: dict[str, int]  # "<deployment>/<proc>" -> /ready status
    ready_checks: dict[str, Any] = field(default_factory=dict)

    def logs(self, dep: str) -> str:
        return "\n".join(
            f"--- {p.label} ---\n{p.logs()}" for p in self.deployments[dep].procs
        )


@pytest.fixture(scope="session")
def matrix(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Matrix]:
    base = _test_env()
    db_url = base.get("DATABASE_URL", "")
    cache_url = base.get("CACHE_URL", "")
    if not db_url or not cache_url:
        _unavailable("DATABASE_URL / CACHE_URL not configured")
    if not _reachable(db_url, 5432):
        _unavailable(f"PostgreSQL unavailable at {urlparse(db_url).netloc}")
    if not _reachable(cache_url, 6379):
        _unavailable(f"Redis unavailable at {urlparse(cache_url).netloc}")

    base_name = urlparse(db_url).path.lstrip("/") or "sandloom_test"
    template = f"{base_name}_conf_tpl"
    deployments: dict[str, Deployment] = {}
    for i, (name, groups) in enumerate(_deployment_plan().items()):
        procs = [
            Proc(name, modules, f"{base_name}_conf_{i}_{j}")
            for j, modules in enumerate(groups)
        ]
        deployments[name] = Deployment(name, procs, bound=len(procs) > 1)
    for proc in deployments[FAULT_DEPLOYMENT].procs:
        proc.extra_env["CONF_FAULT_INJECTION"] = "1"
    db_names = [p.db_name for d in deployments.values() for p in d.procs]
    try:
        asyncio.run(_create_template(db_url, template))
        _migrate_template(base, _db_url(db_url, template))
        asyncio.run(_seed_template(_db_url(db_url, template)))
        asyncio.run(_clone_databases(db_url, template, db_names))
    except Exception as exc:  # noqa: BLE001 - no CREATEDB privilege etc.
        _unavailable(f"cannot create conformance databases: {exc}")

    log_dir = tmp_path_factory.mktemp("conformance")
    all_procs = [p for d in deployments.values() for p in d.procs]
    waves = plan_waves({n: len(d.procs) for n, d in deployments.items()})
    ready: dict[str, int] = {}
    ready_checks: dict[str, Any] = {}
    runs: dict[str, RunState] = {}
    try:
        for i, wave in enumerate(waves):
            wave_procs = [p for n in wave for p in deployments[n].procs]
            need = wave_connections([len(deployments[n].procs) for n in wave])
            try:
                check_headroom(
                    db_url, need, f"split conformance wave {i + 1}/{len(waves)}"
                )
            except ConnectionBudgetError as exc:
                pytest.fail(str(exc), pytrace=False)
            for name in wave:
                dep = deployments[name]
                for proc in dep.procs:
                    proc.start(_proc_env(base, dep, proc, db_url), log_dir)
            _wait_ready(wave_procs)
            for proc in wave_procs:
                key = f"{proc.deployment}/{proc.label}"
                response = httpx.get(
                    f"{proc.url}/ready",
                    headers={"X-Internal-Token": INTERNAL_TOKEN},
                    timeout=10,
                )
                ready[key] = response.status_code
                ready_checks[key] = response.json()

            async def _run_wave(wave: list[str] = wave) -> dict[str, RunState]:
                out: dict[str, RunState] = {}
                if "monolith" in wave:
                    out["monolith"] = await run_script(
                        deployments["monolith"], db_url, None
                    )
                mono = out.get("monolith") or runs["monolith"]
                others = [
                    deployments[n]
                    for n in wave
                    if n not in ("monolith", FAULT_DEPLOYMENT)
                ]
                states = await asyncio.gather(
                    *(run_script(d, db_url, mono) for d in others)
                )
                return {**out, **{s.deployment.name: s for s in states}}

            runs.update(asyncio.run(_run_wave()))
            if i < len(waves) - 1:  # the last wave stays up for live tests
                for proc in wave_procs:
                    proc.stop()
        dump = os.environ.get("SANDLOOM_CONFORMANCE_DUMP")
        if dump:  # debugging aid: every deployment's normalised responses
            Path(dump).write_text(
                json.dumps(
                    {
                        name: [
                            [r.step, r.target, r.status, r.body] for r in state.results
                        ]
                        for name, state in runs.items()
                    },
                    indent=1,
                    default=str,
                )
            )
        yield Matrix(deployments, runs, ready, ready_checks)
    finally:
        for proc in all_procs:
            proc.stop()
        _cleanup_streams(cache_url, f"conf-{RUN}-")
        asyncio.run(_drop_databases(db_url, [*db_names, template]))


# ---------------------------------------------------------------------------
# assertions
# ---------------------------------------------------------------------------

SUBSETS = [*_proper_subsets(), ALL]


def _subset_id(subset: tuple[str, ...]) -> str:
    return "+".join(SHORT[m] for m in subset)


def _deployment_for(
    subset: tuple[str, ...], variant: str, transport: str = DEFAULT_TRANSPORT
) -> str:
    if subset == ALL:
        return "monolith"
    if variant == "bound":
        x = subset[0] if len(subset) == 1 else next(m for m in ALL if m not in subset)
        suffix = "" if transport == DEFAULT_TRANSPORT else f"-{transport}"
        return f"split-{SHORT[x]}{suffix}"
    return f"alone-{_subset_id(subset)}"


def _by_step(state: RunState) -> dict[str, list[Result]]:
    out: dict[str, list[Result]] = {}
    for r in state.results:
        out.setdefault(r.step, []).append(r)
    return out


def test_matrix_waves_stay_within_the_connection_budget() -> None:
    """The matrix never holds more than CONNECTION_BUDGET (40) connections of
    the shared server: every wave's pools at their maximum fit (it used to
    boot all 21 processes at once and peak at ~67 of 100)."""
    counts = {name: len(groups) for name, groups in _deployment_plan().items()}
    waves = plan_waves(counts)
    assert sorted(n for wave in waves for n in wave) == sorted(counts)
    assert "monolith" in waves[0]
    assert set(LIVE_DEPLOYMENTS) <= set(waves[-1])
    for wave in waves:
        assert wave_connections([counts[n] for n in wave]) <= CONNECTION_BUDGET <= 40
    assert PER_PROCESS_CONNECTIONS == 4


def test_monolith_script_matches_expectations(matrix: Matrix) -> None:
    """The script itself is meaningful: the monolith answers as specified."""
    mono = _by_step(matrix.runs["monolith"])
    wrong = [
        (step.name, mono[step.name][0].status, mono[step.name][0].body)
        for step in _script()
        if step.expect is not None and mono[step.name][0].status != step.expect
    ]
    assert not wrong, f"{wrong}\n{matrix.logs('monolith')}"


def test_every_process_is_ready(matrix: Matrix) -> None:
    """No deployment of the matrix is held not-ready by a peer or a preflight."""
    not_ready = {k: v for k, v in matrix.ready.items() if v != 200}
    assert not not_ready, {
        "statuses": not_ready,
        "checks": {key: matrix.ready_checks.get(key) for key in not_ready},
    }


def test_identity_issues_asymmetric_tokens(matrix: Matrix) -> None:
    """Every identity-minted token is EdDSA under the issuer's kid — and is
    verified by processes that hold only the public key (the script's
    cross-service steps above)."""
    issued = [
        token
        for state in matrix.runs.values()
        for token in state.tokens.values()
        if token and token.count(".") == 2  # access JWTs (refresh is opaque)
    ]
    assert issued
    for token in issued:
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "EdDSA" and header["kid"] == JWT_KID, header


def test_internal_rpc_requires_an_allowed_pair_mac(matrix: Matrix) -> None:
    """Between real processes: the ops token alone opens no contract, a
    caller is held to its own SERVICE_BINDINGS, a captured request is refused
    when replayed or presented to another audience, a caller id cannot be
    claimed with another service's key, and an Ed25519-signed request without
    the pair MAC is refused — no downgrade."""
    from types import SimpleNamespace

    from sandloom.core import rpc_identity, rpc_pair_mac
    from sandloom.core.rpc import context as rpc_context
    from sandloom.core.signing_keys import load_private_key

    name = "split-billing"
    dep = matrix.deployments[name]
    caller, provider = dep.host(B), dep.host(T)
    assert caller is not None and provider is not None and caller is not provider
    billing_id = f"{name}-{caller.label}"
    tenancy_id = f"{name}-{provider.label}"
    published = {
        f"{name}-{p.label}": {"d1": _dh_key(f"{name}-{p.label}")[1]} for p in dep.procs
    }

    def cfg(service_id: str, pem_of: str) -> Any:
        return SimpleNamespace(
            rpc_service_id=service_id,
            rpc_dh_private_key=_dh_key(pem_of)[0],
            rpc_dh_key_id="d1",
            rpc_service_dh_keys=published,
        )

    def headers_for(
        contract: str, who: Any, audience: str = tenancy_id
    ) -> dict[str, str]:
        key = rpc_pair_mac.caller_key(who, audience)
        headers, signer = rpc_outbound._get_auth(contract, who, key, audience)
        nonce = headers[rpc_wire._NONCE_HEADER] = rpc_binding.new_nonce()
        signer.apply(headers, nonce)
        return headers

    def get(contract: str, headers: dict[str, str]) -> httpx.Response:
        url = f"{provider.url}/_sandloom/rpc/{contract}"
        return httpx.get(url, headers=headers, timeout=10)

    def code(response: httpx.Response) -> str:
        return str(rpc_wire._unpack(response.content)["error"]["code"])

    billing = cfg(billing_id, billing_id)
    allowed = headers_for("tenancy.membership.v1", billing)
    assert get("tenancy.membership.v1", allowed).status_code == 200  # bound
    replay = get("tenancy.membership.v1", allowed)  # the same bytes again
    assert replay.status_code == 401 and code(replay) == "rpc_auth_replayed"
    denied = get("identity.claims.v1", headers_for("identity.claims.v1", billing))
    assert denied.status_code == 403  # not bound -> denied
    token = {"X-Internal-Token": INTERNAL_TOKEN}
    assert get("tenancy.membership.v1", token).status_code == 404
    # addressed to another audience (billing itself), presented to tenancy
    other = get(
        "tenancy.membership.v1",
        headers_for("tenancy.membership.v1", billing, audience=billing_id),
    )
    assert other.status_code == 401 and code(other) == "rpc_auth_invalid"
    # billing's key, claiming to be the provider's own service id
    forged = get(
        "tenancy.membership.v1",
        headers_for("tenancy.membership.v1", cfg(tenancy_id, billing_id)),
    )
    assert forged.status_code == 401 and code(forged) == "rpc_auth_invalid"
    # an Ed25519 signature without the pair MAC: refused
    identity = rpc_identity.CallerIdentity(
        billing_id, "k1", load_private_key(_rpc_key(billing_id)[0], "EdDSA")
    )
    context = rpc_context._encode_context(
        rpc_context._context_payload(identity, tenancy_id)
    )
    ed = get(
        "tenancy.membership.v1",
        {
            rpc_wire._CTX_HEADER: context,
            rpc_wire._NONCE_HEADER: rpc_binding.new_nonce(),
            rpc_identity.SVC_SIG_HEADER: rpc_identity.sign(
                identity, "GET", "tenancy.membership.v1", b"", context
            ),
        },
    )
    assert ed.status_code == 401 and code(ed) == "rpc_auth_required"


@pytest.mark.parametrize(
    "variant", ["bound", *(f"bound-{t}" for t in OTHER_TRANSPORTS), "unbound"]
)
@pytest.mark.parametrize("subset", SUBSETS, ids=_subset_id)
def test_subset_conforms_to_monolith(
    matrix: Matrix, subset: tuple[str, ...], variant: str
) -> None:
    variant, _, transport = variant.partition("-")
    name = _deployment_for(subset, variant, transport or DEFAULT_TRANSPORT)
    mono = _by_step(matrix.runs["monolith"])
    run = _by_step(matrix.runs[name])
    problems: list[str] = []
    for step in _script():
        if step.action is not None:
            continue
        expected = mono[step.name][0]
        hosted = {m for p in matrix.deployments[name].procs for m in p.modules}
        for got in run.get(step.name, []):
            if got.status >= 500 and got.status not in EXPLICIT_DEGRADATION:
                problems.append(f"{step.name}@{got.target}: {got.status} {got.body}")
                continue
            if variant == "unbound" and not step.needs <= hosted:
                continue  # its precondition could not happen here
            if got.key == expected.key:
                continue
            if variant == "unbound" and got.status in EXPLICIT_DEGRADATION:
                continue
            if (
                variant == "unbound"
                and got.invented - expected.invented
                and got.status in INPUT_DIVERGED_DENIALS
            ):
                # The request names an entity this subset could not create
                # (its module is absent): a denial is the honest answer — the
                # monolith denies the same input identically.
                continue
            problems.append(
                f"{step.name}@{got.target}: got {got.status} {got.body!r}, "
                f"monolith {expected.status} {expected.body!r}"
            )
    assert not problems, "\n".join(problems) + "\n" + matrix.logs(name)[-6000:]


# ---------------------------------------------------------------------------
# revocation across services, with a failing mark write in tenancy
# ---------------------------------------------------------------------------


async def _fault_setup(
    client: httpx.AsyncClient, dep: Deployment, base_db_url: str
) -> dict[str, str]:
    """carol owns an org (tenancy process) that dave joined; tokens minted by
    the identity process. Returns ids + dave's access token."""
    ident, ten = dep.target(I), dep.target(T)
    users = {u: f"{u}-{RUN}@fault.example" for u in ("carol", "dave")}
    for name, email in users.items():
        resp = await client.post(
            f"{ident.url}/auth/register",
            json={"email": email, "full_name": name, "password": PASSWORD},
        )
        assert resp.status_code == 202, resp.text
    await _sql(
        _db_url(base_db_url, ident.db_name), "UPDATE users SET is_verified = true"
    )

    async def _login(user: str) -> str:
        resp = await client.post(
            f"{ident.url}/auth/login",
            json={"email": users[user], "password": PASSWORD},
        )
        assert resp.status_code == 200, resp.text
        return str(resp.json()["access_token"])

    carol = {"Authorization": f"Bearer {await _login('carol')}"}
    dave_token = await _login("dave")
    dave = {"Authorization": f"Bearer {dave_token}"}
    org = await client.post(
        f"{ten.url}/api/v1/orgs",
        json={"name": "Fault Org", "slug": f"fault-{RUN}"},
        headers=carol,
    )
    assert org.status_code == 201, org.text
    org_id = org.json()["id"]
    invite = await client.post(
        f"{ten.url}/api/v1/orgs/{org_id}/invitations",
        json={"email": users["dave"], "role": "member"},
        headers=carol,
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        f"{ten.url}/api/v1/invitations/accept",
        json={"token": invite.json()["token"]},
        headers=dave,
    )
    assert accept.status_code == 200, accept.text
    dave_id = jwt.decode(dave_token, options={"verify_signature": False})["sub"]
    return {
        "org": org_id,
        "dave": dave_id,
        "carol_auth": carol["Authorization"],
        "dave_auth": dave["Authorization"],
    }


async def _statuses(
    client: httpx.AsyncClient, dep: Deployment, org: str, auth: str
) -> dict[str, int]:
    """dave's old token, sent to EVERY service of the deployment."""
    headers = {"Authorization": auth}
    out: dict[str, int] = {}
    for proc in dep.procs:
        out[f"{proc.label}:probe"] = (
            await client.get(f"{proc.url}/_conf/member/{org}", headers=headers)
        ).status_code
    out["identity:/auth/me"] = (
        await client.get(f"{dep.target(I).url}/auth/me", headers=headers)
    ).status_code
    return out


def test_revocation_across_services_survives_a_failing_mark_write(
    matrix: Matrix,
) -> None:
    """Split mode (identity | tenancy, own databases, Streams bus):

    1. tenancy cannot write the mark (cache outage in that process): the
       removal is REFUSED (503) — dave stays a member, nothing half-applied;
    2. tenancy's write is acknowledged but LOST (failover): the removal
       commits; the durable ``core.auth.tokens_revoked.v1`` event, consumed
       by identity in the OTHER process, writes the mark — dave's old token
       is then rejected by every service. The measured lag is reported.
    """
    dep = matrix.deployments[FAULT_DEPLOYMENT]
    ten = dep.target(T)
    db_url = _test_env()["DATABASE_URL"]

    async def _run() -> tuple[dict[str, int], dict[str, int], float]:
        async with httpx.AsyncClient(timeout=30) as client:
            ids = await _fault_setup(client, dep, db_url)
            org, carol = ids["org"], {"Authorization": ids["carol_auth"]}
            remove = f"{ten.url}/api/v1/orgs/{org}/members/{ids['dave']}"

            fault = await client.post(f"{ten.url}/_conf/fault/revocation/outage")
            assert fault.status_code == 200, fault.text
            refused = await client.delete(remove, headers=carol)
            assert refused.status_code == 503, refused.text
            during_outage = await _statuses(client, dep, org, ids["dave_auth"])

            await client.post(f"{ten.url}/_conf/fault/revocation/lost")
            committed = await client.delete(remove, headers=carol)
            assert committed.status_code == 204, committed.text
            started = time.monotonic()
            after: dict[str, int] = {}
            while time.monotonic() - started < 20:
                after = await _statuses(client, dep, org, ids["dave_auth"])
                if all(code == 401 for code in after.values()):
                    break
                await asyncio.sleep(0.05)
            lag = time.monotonic() - started
            await client.post(f"{ten.url}/_conf/fault/revocation/off")
            return during_outage, after, lag

    during_outage, after, lag = asyncio.run(_run())
    logs = matrix.logs(FAULT_DEPLOYMENT)[-6000:]
    # 1. refused removal: still a member everywhere, token still valid.
    assert set(during_outage.values()) == {200}, f"{during_outage}\n{logs}"
    # 2. applied by identity's durable consumer in the other process.
    assert set(after.values()) == {401}, f"{after} after {lag:.2f}s\n{logs}"
    assert lag < 10, lag
    print(f"\n[fault-revocation] lost mark write -> rejected everywhere in {lag:.3f}s")
