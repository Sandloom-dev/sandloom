"""Identity replaced by an external OIDC issuer: tenancy + billing, no identity.

The kernel-boundary proof with a real replacement (tm
``kb-proto-replace-identity``): an application composes the standard
``sandloom_tenancy`` and ``sandloom_billing`` batteries WITHOUT
``sandloom_identity``. Users sign in at an external OpenID provider
(:class:`tests._oidc_issuer.ExternalIssuer`, a local issuer with discovery, JWKS
and RS256 keys only it holds); the app's own third-party module
``acme_idp`` (``oidc_identity/modules/acme_idp``) verifies the id_token, maps the
subject to a local account and mints the access token. The composition root
(``oidc_identity/asgi.py``) names acme_idp the issuer and the authority of
``roles`` / ``permissions`` (``email`` / ``email_verified`` follow the
issuer), tenancy of ``tenant_ids``, billing of its subscription keys. Nothing
in ``sandloom.core`` / ``sdk`` / ``shared`` or in the batteries changed for it.

Each topology proves, against real processes, PostgreSQL and Redis:

- sign-in: an id_token the provider signed gives a token whose ``roles`` /
  ``permissions`` / ``email`` come from acme_idp, ``tenant_ids`` from tenancy
  and the subscription claims from billing; a forged, expired, foreign or
  mis-addressed id_token is refused; a rotated provider key is fetched;
- tenancy: an organization created with the token, its ``tenant_ids`` grant
  and ``TenantScope``; an invitation accepted on the acme_idp-minted
  ``email`` / ``email_verified`` (a wrong or unverified address is refused);
- billing: ``Subscribed`` (claim) 402 then 200 once a subscription is
  active, ``OrgRole`` and ``SubscriptionActive`` (live) and billing's own
  organization-scoped read;
- revocation: removing a member revokes their tokens (tenancy), the
  provider's back-channel logout revokes a subject's tokens (acme_idp); a
  new sign-in works again;
- ``mono`` (one process) and ``split`` (P1 = acme_idp + storefront, P2 =
  tenancy + billing, over the signed internal RPC with EdDSA tokens only P1
  can mint).

Skips without PostgreSQL / Redis unless ``SANDLOOM_E2E_REQUIRED=1``.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from sandloom.sdk.security import (  # sandloom: advanced RPC keys of the split deployment
    generate_private_key,
    generate_x25519_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)
from sandloom.sdk.testing import (  # sandloom: advanced served processes
    ReservedPort,
    SpawnedServer,
    SpawnedServerError,
    probe,
    spawn_served,
    worker_env_values,
)
from tests._oidc_issuer import ExternalIssuer

pytestmark = [pytest.mark.slow]

HERE = Path(__file__).resolve().parents[1]
RUN = uuid.uuid4().hex[:8]
ROOT = "oidc_identity.asgi:app"
CLIENT_ID = "storefront-web"
IDP = "oidc_identity.modules.acme_idp"
STOREFRONT = "oidc_identity.modules.storefront"
BATTERIES = ["sandloom_tenancy", "sandloom_billing"]
ALL = [IDP, *BATTERIES, STOREFRONT]
P1_MODULES = [IDP, STOREFRONT]
P2_MODULES = BATTERIES

#: Interpreter settings a child needs from this environment; every Sandloom
#: setting is given explicitly (nothing of the test run's own .env leaks in).
_PASS_THROUGH = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "SYSTEMROOT",
    "VIRTUAL_ENV",
)


# ---------------------------------------------------------------------------
# infrastructure
# ---------------------------------------------------------------------------


def _unavailable(reason: str) -> NoReturn:
    """Skip, or fail where CI requires the multi-process suites to run."""
    if os.environ.get("SANDLOOM_E2E_REQUIRED", "").strip() == "1":
        pytest.fail(f"example infrastructure required but unavailable: {reason}")
    pytest.skip(reason)


def _reachable(url: str, default_port: int) -> bool:
    """Whether a TCP connection to ``url``'s host and port opens."""
    parsed = urlparse(url.replace("+asyncpg", ""))
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or default_port), timeout=1.5
        ):
            return True
    except OSError:
        return False


def _with_path(url: str, name: str) -> str:
    """``url`` naming the database ``name``."""
    return urlparse(url)._replace(path=f"/{name}").geturl()


async def _admin_sql(url: str, *statements: str) -> None:
    """Run ``statements`` outside a transaction (CREATE / DROP DATABASE)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


def _child_base_env() -> dict[str, str]:
    """The interpreter environment of a child, without any Sandloom setting."""
    env = {k: os.environ[k] for k in _PASS_THROUGH if k in os.environ}
    env["PYTHONPATH"] = str(HERE)
    if os.environ.get("SANDLOOM_TEST_OWNER"):
        env["SANDLOOM_TEST_OWNER"] = os.environ["SANDLOOM_TEST_OWNER"]
    return env


@dataclass(frozen=True)
class Infra:
    """The database (every module's chain at head), Redis and the provider."""

    database_url: str
    cache_url: str
    broker_url: str
    logs: Path
    idp: ExternalIssuer


def _migrate_env(
    url: str, cache: str, broker: str, idp: ExternalIssuer
) -> dict[str, str]:
    return {
        **_child_base_env(),
        "ENVIRONMENT": "test",
        "DATABASE_URL": url,
        "CACHE_URL": cache,
        "BROKER_URL": broker,
        "JWT_SECRET": f"oidc-migrate-{uuid.uuid4().hex}",
        "ACME_IDP_ISSUER": idp.url,
        "ACME_IDP_CLIENT_ID": CLIENT_ID,
        "SANDLOOM_COMPOSITION_ROOT": "oidc_identity.asgi",
        "SANDLOOM_MODULES": json.dumps(ALL),
    }


@pytest.fixture(scope="module")
def infra(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Infra]:
    """A fresh database migrated through the example's own root."""
    values = worker_env_values(HERE / ".env.test")
    base = values.get("DATABASE_URL", "")
    cache = values.get("CACHE_URL", "")
    broker = values.get("BROKER_URL", "")
    if not base.startswith("postgresql") or not cache.startswith("redis"):
        _unavailable("needs a PostgreSQL DATABASE_URL and a Redis CACHE_URL")
    if not _reachable(base, 5432) or not _reachable(cache, 6379):
        _unavailable("PostgreSQL or Redis is not reachable")
    name = f"sandloom_oidc_{RUN}"
    maintenance = _with_path(base, "postgres")
    asyncio.run(
        _admin_sql(
            maintenance,
            f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
            f'CREATE DATABASE "{name}"',
        )
    )
    url = _with_path(base, name)
    try:
        with ExternalIssuer(CLIENT_ID) as idp:
            migrate = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "sandloom.core.cli.migrate",
                    "upgrade-all",
                    "--apply",
                ],
                cwd=HERE,
                env=_migrate_env(url, cache, broker, idp),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            assert migrate.returncode == 0, (
                migrate.stdout[-3000:] + migrate.stderr[-3000:]
            )
            yield Infra(
                database_url=url,
                cache_url=cache,
                broker_url=broker,
                logs=tmp_path_factory.mktemp("oidc-logs"),
                idp=idp,
            )
    finally:
        asyncio.run(
            _admin_sql(maintenance, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        )


async def _sql(url: str, statement: str, **params: Any) -> Any:
    """One statement in its own transaction; the first column of its row."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return result.scalar() if result.returns_rows else None
    finally:
        await engine.dispose()


def seed_active_subscription(infra: Infra, user_id: str) -> None:
    """An active catalog subscription for ``user_id``'s personal tenant.

    Written as the PSP webhook would leave it (billing's tables, an
    intent-less ``psp`` row, which ``BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS``
    lets grant): how the subscription came about is billing's business, not
    the issuer's; what is under test is that acme_idp's next sign-in carries
    billing's claim.
    """
    url = infra.database_url

    async def _seed() -> None:
        product = await _sql(
            url,
            "INSERT INTO billing_products (name, type, is_active) "
            "VALUES (:n, 'subscription', true) RETURNING id",
            n=f"Pro {uuid.uuid4().hex[:6]}",
        )
        price = await _sql(
            url,
            "INSERT INTO billing_prices (product_id, amount, currency, "
            "recurring_interval, stripe_price_id) VALUES (:p, 1000, 'usd', "
            "'month', :s) RETURNING id",
            p=product,
            s=f"price_{uuid.uuid4().hex[:10]}",
        )
        customer = await _sql(
            url,
            "INSERT INTO billing_customers (id, user_id, stripe_customer_id) "
            "VALUES (gen_random_uuid(), :u, :c) RETURNING id",
            u=user_id,
            c=f"cus_{uuid.uuid4().hex[:10]}",
        )
        await _sql(
            url,
            "INSERT INTO billing_subscriptions (customer_id, price_id, "
            "stripe_subscription_id, status, current_period_end, "
            "cancel_at_period_end, origin) VALUES (:c, :p, :s, 'active', :e, "
            "false, 'psp')",
            c=customer,
            p=price,
            s=f"sub_{uuid.uuid4().hex[:10]}",
            e=datetime.now(UTC) + timedelta(days=30),
        )

    asyncio.run(_seed())


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------


@dataclass
class Proc:
    """One process of a topology, serving the example's root."""

    label: str
    modules: list[str]
    env: dict[str, str]
    port: ReservedPort = field(default_factory=ReservedPort)
    server: SpawnedServer | None = None
    log: Path | None = None

    @property
    def url(self) -> str:
        return self.port.url

    def start(self, infra: Infra) -> None:
        self.log = infra.logs / f"{self.label}-{uuid.uuid4().hex[:6]}.log"
        env = {**self.env, "SANDLOOM_MODULES": json.dumps(self.modules)}
        with self.log.open("wb") as fh:
            self.server = spawn_served(
                self.port,
                ROOT,
                env=env,
                label=self.label,
                cwd=str(HERE),
                stdout=fh,
                stderr=subprocess.STDOUT,
            )

    def log_text(self) -> str:
        return self.log.read_text(errors="replace") if self.log else ""

    def wait_ready(self, timeout: float = 90.0) -> None:
        assert self.server is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if probe(self.server):
                    status = httpx.get(self.url + "/ready", timeout=2).status_code
                    if status == 200:
                        return
            except httpx.HTTPError:
                pass
            except SpawnedServerError as exc:
                raise SpawnedServerError(f"{exc}\n{self.log_text()[-4000:]}") from exc
            time.sleep(0.2)
        raise SpawnedServerError(f"{self.label} not ready:\n{self.log_text()[-4000:]}")

    def close(self) -> None:
        if self.server is not None:
            self.server.stop()
        self.port.close()


@dataclass
class Topology:
    """The processes; who serves the issuer and who the batteries."""

    procs: list[Proc]

    @property
    def issuer(self) -> Proc:
        return self.procs[0]

    @property
    def batteries(self) -> Proc:
        return self.procs[-1]


def _base_env(infra: Infra) -> dict[str, str]:
    return {
        **_child_base_env(),
        "DATABASE_URL": infra.database_url,
        "CACHE_URL": infra.cache_url,
        "BROKER_URL": infra.broker_url,
        "EVENT_BUS_BACKEND": "memory",
        "ENVIRONMENT": "dev",
        "OUTBOX_RELAY_ENABLED": "off",
        "INTERNAL_API_TOKEN": f"oidc-internal-{uuid.uuid4().hex}",
        "RATE_LIMIT_ENABLED": "false",
        "LOG_FORMAT": "text",
        "NOTIFIER_BACKEND": "log",
        "ACME_IDP_ISSUER": infra.idp.url,
        "ACME_IDP_CLIENT_ID": CLIENT_ID,
        "BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS": "true",
    }


def mono(infra: Infra) -> Topology:
    env = _base_env(infra)
    env["JWT_SECRET"] = f"oidc-jwt-{uuid.uuid4().hex}"
    return Topology([Proc("mono", ALL, env)])


# Every contract tenancy and billing provide (P1 calls them over RPC), listed
# as an app author writes SERVICE_BINDINGS.
BATTERY_CONTRACTS = [
    "billing.claims.v1",
    "billing.credits.v1",
    "billing.entitlements.v1",
    "billing.facts.v1",
    "tenancy.claims.v1",
    "tenancy.membership.v1",
]


def split(infra: Infra) -> Topology:
    """P1 (issuer + consumer) and P2 (tenancy + billing) over the real RPC.

    Per-pair X25519 MACs, Ed25519 command signatures, ``service_id`` pins and
    the allowlist; EdDSA access tokens whose private key only P1 holds, so
    P2 verifies what acme_idp minted with the public key alone.
    """
    jwt_key = generate_private_key("EdDSA")
    rpc = {s: generate_private_key("EdDSA") for s in ("p1", "p2")}
    dh = {s: generate_x25519_key() for s in ("p1", "p2")}
    p1 = Proc("p1", P1_MODULES, {})
    p2 = Proc("p2", P2_MODULES, {})
    target = {"base_url": p2.url, "service_id": "p2"}
    bound: dict[str, dict[str, Any]] = dict.fromkeys(BATTERY_CONTRACTS, target)
    # Billing's keys are fail-safe (ADR-0014 decision 7): a billing outage
    # still signs a user in, the subscription atoms escalate or deny.
    bound["billing.claims.v1"] = {**target, "critical": False}
    common = {
        **_base_env(infra),
        "JWT_ALGORITHM": "EdDSA",
        "JWT_KID": "oidc-1",
        "JWT_PUBLIC_KEYS": json.dumps({"oidc-1": public_pem(jwt_key)}),
        "JWT_PREVIOUS_SECRETS": "[]",
        "JWT_SECRET": "",
        "RPC_COMMANDS_REQUIRE_SIGNATURE": "on",
        "RPC_KEY_ID": "k1",
        "RPC_SERVICE_KEYS": json.dumps(
            {s: {"k1": public_pem(k)} for s, k in rpc.items()}
        ),
        "RPC_ALLOWED_CALLS": json.dumps({"p1": sorted(bound)}),
        "RPC_DH_KEY_ID": "d1",
        "RPC_SERVICE_DH_KEYS": json.dumps(
            {s: {"d1": x25519_public_pem(k)} for s, k in dh.items()}
        ),
        "SERVICE_RPC_TIMEOUT_SECONDS": "5",
    }
    for proc, sid, private in ((p1, "p1", private_pem(jwt_key)), (p2, "p2", "")):
        proc.env = {
            **common,
            "RPC_SERVICE_ID": sid,
            "SANDLOOM_SERVICE_NAME": sid,
            "RPC_PRIVATE_KEY": private_pem(rpc[sid]),
            "RPC_DH_PRIVATE_KEY": x25519_private_pem(dh[sid]),
            "JWT_PRIVATE_KEY": private,
            "SERVICE_BINDINGS": json.dumps(bound if sid == "p1" else {}),
        }
    return Topology([p1, p2])


@pytest.fixture(params=["mono", "split"])
def topology(request: pytest.FixtureRequest, infra: Infra) -> Iterator[Topology]:
    """The running topology (every process ready), stopped after the test."""
    topo = mono(infra) if request.param == "mono" else split(infra)
    with ExitStack() as stack:
        for proc in topo.procs:
            stack.callback(proc.close)
        for proc in topo.procs:
            proc.start(infra)
        for proc in topo.procs:
            proc.wait_ready()
        yield topo


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """A response's status and decoded JSON body (or text)."""

    status: int
    body: Any


def _answer(response: httpx.Response) -> Answer:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return Answer(response.status_code, body)


def call(
    proc: Proc,
    method: str,
    path: str,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    tenant: str | None = None,
    params: dict[str, str] | None = None,
) -> Answer:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if tenant:
        headers["X-Tenant-Id"] = tenant
    response = httpx.request(
        method, proc.url + path, json=body, headers=headers, params=params, timeout=20
    )
    return _answer(response)


@dataclass
class Person:
    """Someone the external provider knows (``sub``, e-mail, groups)."""

    name: str
    groups: list[str] = field(default_factory=list)
    verified: bool = True
    subject: str = field(default_factory=lambda: f"ext|{uuid.uuid4().hex}")

    @property
    def email(self) -> str:
        return f"{self.name}-{self.subject[-8:]}@example.com"


@dataclass
class Session:
    token: str
    user_id: str
    claims: dict[str, Any]


def sign_in(topo: Topology, idp: ExternalIssuer, person: Person) -> Session:
    """The provider signs an id_token; acme_idp exchanges it."""
    id_token = idp.id_token(
        person.subject,
        email=person.email,
        email_verified=person.verified,
        groups=person.groups,
    )
    answer = call(
        topo.issuer, "POST", "/api/v1/acme_idp/login", body={"id_token": id_token}
    )
    assert answer.status == 200, (answer, topo.issuer.log_text()[-3000:])
    return Session(
        answer.body["access_token"], answer.body["user_id"], answer.body["claims"]
    )


def _wait_past(valid_after: int) -> None:
    """Until a token minted now is issued after the revocation mark."""
    while time.time() <= valid_after:
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# the proof
# ---------------------------------------------------------------------------


def _assert_sign_in(topo: Topology, idp: ExternalIssuer) -> tuple[Session, Session]:
    """Claims from every authority; a bad id_token is refused."""
    admin = sign_in(topo, idp, Person("alice", groups=["platform-admins"]))
    claims = admin.claims
    assert claims["roles"] == ["admin"]
    assert claims["permissions"] == ["acme.platform.manage"]
    assert claims["email_verified"] is True
    assert claims["email"].startswith("alice-")
    assert claims["tenant_ids"] == []
    assert set(claims["claims_sources"]) == {"acme_idp", "tenancy", "billing"}, claims
    assert claims["claims_sources_failed"] == []
    me = call(topo.issuer, "GET", "/api/v1/storefront/me", admin.token)
    assert me.status == 200, me
    assert me.body == {
        "user_id": admin.user_id,
        "roles": ["admin"],
        "permissions": ["acme.platform.manage"],
    }

    user = sign_in(topo, idp, Person("bob"))
    assert user.claims["roles"] == [] and user.claims["permissions"] == []
    for path, admin_status in (("/platform", 200), ("/manage", 200)):
        url = f"/api/v1/storefront{path}"
        assert call(topo.issuer, "GET", url, admin.token).status == admin_status
        assert call(topo.issuer, "GET", url, user.token).status == 403

    # What the provider did not sign for this app is refused.
    login = "/api/v1/acme_idp/login"
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for forged in (
        idp.id_token("ext|x", email="x@example.com", key=stranger),
        idp.id_token("ext|x", email="x@example.com", audience="another-app"),
        idp.id_token("ext|x", email="x@example.com", issuer="https://evil.example"),
        idp.id_token("ext|x", email="x@example.com", lifetime=-60),
        "not-a-jwt",
    ):
        answer = call(topo.issuer, "POST", login, body={"id_token": forged})
        assert answer.status == 401, answer
    # The provider rotates its key: the new kid is fetched from its JWKS.
    old = idp.rotate()
    idp.retire(old)
    rotated = sign_in(topo, idp, Person("rotated"))
    assert rotated.claims["roles"] == []
    return admin, user


def _assert_tenancy_and_billing(
    topo: Topology, infra: Infra, owner: Session, outsider: Session
) -> tuple[str, Session]:
    """An org, its grant, an accepted invitation, billing's gates."""
    idp = infra.idp
    bat = topo.batteries
    slug = f"acme-{uuid.uuid4().hex[:8]}"
    created = call(
        bat, "POST", "/api/v1/orgs", owner.token, {"name": "Acme", "slug": slug}
    )
    assert created.status == 201, (created, bat.log_text()[-3000:])
    org = created.body["id"]

    owner = sign_in(
        topo, idp, Person("alice", ["platform-admins"], subject=_subject(infra, owner))
    )
    assert owner.claims["tenant_ids"] == [org]
    tenant = "/api/v1/storefront/tenant"
    assert call(topo.issuer, "GET", tenant, owner.token, tenant=org).body == {
        "tenant": org
    }
    assert call(topo.issuer, "GET", tenant, outsider.token, tenant=org).status == 403

    # OrgRole and billing's org read: live over tenancy.membership.v1.
    admin_gate = f"/api/v1/storefront/orgs/{org}/admin"
    assert call(topo.issuer, "GET", admin_gate, owner.token).status == 200
    assert call(topo.issuer, "GET", admin_gate, outsider.token).status == 403
    sub = call(
        bat, "GET", "/api/v1/billing/subscription", owner.token, params={"org_id": org}
    )
    assert sub.status == 200 and sub.body["status"] == "none", sub
    sub = call(
        bat,
        "GET",
        "/api/v1/billing/subscription",
        outsider.token,
        params={"org_id": org},
    )
    assert sub.status == 403, sub
    org_sub = f"/api/v1/storefront/orgs/{org}/subscribed"
    assert call(topo.issuer, "GET", org_sub, owner.token).status == 402

    # Subscribed (claim tier): 402 until billing has an active subscription
    # for the personal tenant, then 200 on the next acme_idp sign-in.
    gate = "/api/v1/storefront/subscribed"
    assert call(topo.issuer, "GET", gate, owner.token).status == 402
    seed_active_subscription(infra, owner.user_id)
    owner = sign_in(
        topo, idp, Person("alice", ["platform-admins"], subject=_subject(infra, owner))
    )
    assert owner.claims["subscription_status"] == "active", owner.claims
    assert call(topo.issuer, "GET", gate, owner.token).status == 200

    # Invitation accept: the address acme_idp minted must match and be
    # verified by the provider.
    carol_person = Person("carol")
    invite = call(
        bat,
        "POST",
        f"/api/v1/orgs/{org}/invitations",
        owner.token,
        {"email": carol_person.email, "role": "member"},
    )
    assert invite.status == 201, (invite, bat.log_text()[-3000:])
    accept = {"token": invite.body["token"]}
    # The invited address, but the provider did not verify it: refused.
    unverified = _with_email(topo, idp, carol_person, verified=False)
    refused = call(bat, "POST", "/api/v1/invitations/accept", unverified, accept)
    assert refused.status == 403, refused
    wrong = call(bat, "POST", "/api/v1/invitations/accept", outsider.token, accept)
    assert wrong.status == 403, wrong
    carol = sign_in(topo, idp, carol_person)
    joined = call(bat, "POST", "/api/v1/invitations/accept", carol.token, accept)
    assert joined.status == 200, (joined, bat.log_text()[-3000:])
    assert joined.body["role"] == "member"
    carol = sign_in(topo, idp, carol_person)
    assert carol.claims["tenant_ids"] == [org]
    assert call(topo.issuer, "GET", tenant, carol.token, tenant=org).status == 200
    return org, carol


def _with_email(
    topo: Topology, idp: ExternalIssuer, person: Person, *, verified: bool
) -> str:
    """A token for a second subject carrying ``person``'s address."""
    other = Person(
        person.name, verified=verified, subject=f"ext|alt-{uuid.uuid4().hex}"
    )
    id_token = idp.id_token(other.subject, email=person.email, email_verified=verified)
    answer = call(
        topo.issuer, "POST", "/api/v1/acme_idp/login", body={"id_token": id_token}
    )
    assert answer.status == 200, answer
    assert answer.body["claims"]["email_verified"] is verified
    return str(answer.body["access_token"])


def _subject(infra: Infra, session: Session) -> str:
    """The external subject of ``session``'s account (acme_idp's table)."""
    return str(
        asyncio.run(
            _sql(
                infra.database_url,
                "SELECT subject FROM acme_idp_accounts WHERE id = :i",
                i=session.user_id,
            )
        )
    )


def _assert_revocation(
    topo: Topology,
    infra: Infra,
    owner: Session,
    org: str,
    member: Session,
    user: Session,
) -> None:
    """Tenancy's member removal and the provider's logout revoke tokens."""
    idp = infra.idp
    bat = topo.batteries
    tenant = "/api/v1/storefront/tenant"
    removed = call(
        bat, "DELETE", f"/api/v1/orgs/{org}/members/{member.user_id}", owner.token
    )
    assert removed.status == 204, (removed, bat.log_text()[-3000:])
    # The member's token is revoked everywhere (the mark is in the shared
    # cache), not only where tenancy runs.
    assert call(topo.issuer, "GET", tenant, member.token, tenant=org).status == 401
    assert call(bat, "GET", "/api/v1/orgs", member.token).status == 401

    # OIDC back-channel logout from the provider: every token of the
    # subject is revoked; a forged logout token is refused.
    logout = "/api/v1/acme_idp/backchannel-logout"
    subject = _subject(infra, user)
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = idp.id_token(subject, email="x@example.com", key=stranger)
    assert (
        call(topo.issuer, "POST", logout, body={"logout_token": forged}).status == 400
    )
    id_as_logout = idp.id_token(subject, email="x@example.com")
    assert (
        call(topo.issuer, "POST", logout, body={"logout_token": id_as_logout}).status
        == 400
    )
    assert call(topo.issuer, "GET", "/api/v1/storefront/me", user.token).status == 200
    done = call(
        topo.issuer, "POST", logout, body={"logout_token": idp.logout_token(subject)}
    )
    assert done.status == 200 and done.body["revoked"] is True, done
    assert call(topo.issuer, "GET", "/api/v1/storefront/me", user.token).status == 401
    assert call(bat, "GET", "/api/v1/orgs", user.token).status == 401
    # Signing in again (after the mark) works.
    _wait_past(int(done.body["valid_after"]))
    again = sign_in(topo, idp, Person("bob", subject=subject))
    assert call(topo.issuer, "GET", "/api/v1/storefront/me", again.token).status == 200

    # The kernel's emergency block through the stateless issuer: the token
    # is refused in every process, and so is the next sign-in.
    block = f"/api/v1/acme_idp/accounts/{again.user_id}/block"
    assert call(topo.issuer, "POST", block, again.token).status == 403
    blocked = call(topo.issuer, "POST", block, owner.token)
    assert blocked.status == 200, (blocked, topo.issuer.log_text()[-3000:])
    assert call(topo.issuer, "GET", "/api/v1/storefront/me", again.token).status == 401
    assert call(bat, "GET", "/api/v1/orgs", again.token).status == 401
    id_token = idp.id_token(subject, email="bob@example.com")
    refused = call(
        topo.issuer, "POST", "/api/v1/acme_idp/login", body={"id_token": id_token}
    )
    assert refused.status == 401, refused


def test_identity_replaced_by_an_external_oidc_issuer(
    topology: Topology, infra: Infra
) -> None:
    """Sign-in, tenancy, billing and revocation with acme_idp as the issuer."""
    owner, outsider = _assert_sign_in(topology, infra.idp)
    org, member = _assert_tenancy_and_billing(topology, infra, owner, outsider)
    owner = sign_in(
        topology,
        infra.idp,
        Person("alice", ["platform-admins"], subject=_subject(infra, owner)),
    )
    _assert_revocation(topology, infra, owner, org, member, outsider)
