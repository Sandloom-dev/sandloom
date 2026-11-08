"""Replaceability: identity, tenancy and billing replaced by fixture modules.

ADR-0012 (fitness tests) and goals.md "Replaceability decides the scope": each
standard battery is replaced by a test implementation outside the kernel, and
what the kernel guarantees must still hold — mono and split. The fixtures
(``pocapp/modules``) are the ones of the replaceability PoC
(``design/kernel-boundary/validation.md`` §1, tm ``kb-poc-replace``):

- ``test_identity`` — a token issuer that is not ``sandloom_identity`` and not
  named ``identity``; ``roles``/``permissions`` from a critical contributor;
- ``forged`` — a non-critical contributor whose answer the test sets;
- ``grants`` — an independent tenant-grant provider (critical contributor
  writing ``tenant_ids``, grant/revoke routes, a down switch);
- ``notes`` — a ``TenantScoped`` table under PostgreSQL row security;
- ``inventory`` — the access fact ``inventory.stock.available`` and its contract;
- ``shop`` — the consumer: ``Role``, ``Perm``, ``require_platform_admin``,
  ``TenantScope``, ``Entitled``;
- ``identity`` — the ``test_identity`` issuer under the short name ``identity``.

Each topology boots a fixture composition root (``pocapp/roots``): the claim
authorities are the root's code (ADR-0014). The standard one, ``trust``,
assigns ``roles``/``permissions`` and the issuer to ``test_identity`` and
``tenant_ids`` to ``grants``; a contributor writing a protected claim it is
not assigned is a violator and loses its whole result (KB-10). The trust rows
T-1..T-8 themselves are in ``test_trust_rows.py``.

Each test boots real ``pocapp.roots.<map>:app`` processes (one for the monolith; P1 =
issuer and consumers, P2 = providers for the split, over the signed internal
RPC with EdDSA tokens only P1 can mint). Fixture state (forged answers, grants,
fault switches) lives in process memory, so every topology starts clean; each
gets its own user ids, so a revocation mark in the shared Redis never reaches
another topology.

The open gaps of validation §1.2 are strict ``xfail`` tests asserting the
guarantee, each naming the task that closes it: when it is fixed the xpass
fails the run, and that task turns the test into an ordinary one.

Skips without PostgreSQL / Redis — unless ``SANDLOOM_E2E_REQUIRED=1`` (CI). The
``kernel-without-batteries`` CI job runs it with no module package importable.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import httpx
import pytest

from sandloom.core.signing_keys import (
    generate_private_key,
    generate_x25519_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)
from sandloom.core.testing import worker_env_values
from sandloom.core.testing.served import (
    ReservedPort,
    SpawnedServer,
    SpawnedServerError,
    probe,
    spawn_served,
)

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.slow]

BACKEND = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
RUN = uuid.uuid4().hex[:8]

T_A = "0a000000-0000-4000-8000-00000000000a"
T_B = "0b000000-0000-4000-8000-00000000000b"
T_FOREIGN = "0f000000-0000-4000-8000-00000000000f"

M = "pocapp.modules."
#: The fixture composition roots (one claim-authority map each).
TRUST_ROOT = "pocapp.roots.trust:app"
NO_GRANTS_ROOT = "pocapp.roots.no_grants:app"
IDENTITY_ROOT = "pocapp.roots.identity:app"
ALL = [
    f"{M}test_identity",
    f"{M}forged",
    f"{M}grants",
    f"{M}notes",
    f"{M}inventory",
    f"{M}shop",
]
P1_MODULES = [f"{M}test_identity", f"{M}notes", f"{M}shop"]
P2_MODULES = [f"{M}grants", f"{M}inventory", f"{M}forged"]
SPLIT_BINDINGS: dict[str, dict[str, Any]] = {
    "grants.claims.v1": {},
    "forged.claims.v1": {"critical": False},
    "inventory.facts.v1": {},
}

#: The installed identity battery claims the module name ``identity`` (B020).
IDENTITY_BATTERY = importlib.util.find_spec("sandloom_identity") is not None

UNAVAILABLE_FORGED = "Token claims from 'forged' are unavailable; try again later"
UNAVAILABLE_ISSUER = (
    "Token claims from 'test_identity' are unavailable; try again later"
)
UNAVAILABLE_GRANTS = "Token claims from 'grants' are unavailable; try again later"
NOT_AUTHORIZED = "tenant is not authorized by the access token"
PLATFORM_REQUIRED = "Platform administrator role required"
ENTITLEMENT = "requires entitlement 'inventory.stock.available'"

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
        pytest.fail(f"replaceability infrastructure required but unavailable: {reason}")
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


def _with_credentials(url: str, user: str, password: str) -> str:
    """``url`` connecting as ``user``."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    netloc = f"{user}:{password}@{host}" + (f":{parsed.port}" if parsed.port else "")
    return parsed._replace(netloc=netloc).geturl()


async def _admin_sql(url: str, *statements: str) -> None:
    """Run ``statements`` outside a transaction (CREATE/DROP DATABASE, roles)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _seed_notes(url: str, role: str) -> None:
    """``notes_items`` with the kernel's own RLS DDL, one note per tenant.

    The app role is neither superuser, BYPASSRLS nor the table owner, so
    PostgreSQL row security applies to every connection the app makes.
    """
    from sqlalchemy import text
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.ext.asyncio import create_async_engine

    from sandloom.core.sql_alchemy.rls import enable_tenant_rls_statements

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE notes_items (id uuid PRIMARY KEY, tenant_id uuid "
                    "NOT NULL, body varchar(200) NOT NULL)"
                )
            )
            for sql in enable_tenant_rls_statements(
                "notes_items", dialect=postgresql.dialect()
            ):
                await conn.execute(text(sql))
            await conn.execute(
                text(
                    "INSERT INTO notes_items VALUES (gen_random_uuid(), :a, "
                    "'note of A'), (gen_random_uuid(), :b, 'note of B')"
                ),
                {"a": T_A, "b": T_B},
            )
            await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            await conn.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                    f'public TO "{role}"'
                )
            )
            await conn.execute(
                text(
                    f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"'
                )
            )
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class Infra:
    """The database (as the RLS-bound app role) and Redis the topologies use."""

    database_url: str
    cache_url: str
    broker_url: str
    logs: Path


@pytest.fixture(scope="module")
def infra(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Infra]:
    """A fresh database with the core schema and an RLS-bound app role.

    The kernel's core chain is applied by ``sandloom.core.cli.migrate`` as the
    test user; the app connects as a role created for this run; both are
    dropped at the end.
    """
    values = worker_env_values(BACKEND / ".env.test")
    base = values.get("DATABASE_URL", "")
    cache = values.get("CACHE_URL", "")
    broker = values.get("BROKER_URL", "")
    if not base.startswith("postgresql") or not cache.startswith("redis"):
        _unavailable("needs a PostgreSQL DATABASE_URL and a Redis CACHE_URL")
    if not _reachable(base, 5432) or not _reachable(cache, 6379):
        _unavailable("PostgreSQL or Redis is not reachable")
    name = f"sandloom_replace_{RUN}"
    role = f"sandloom_replace_app_{RUN}"
    password = uuid.uuid4().hex
    maintenance = _with_path(base, "postgres")
    asyncio.run(
        _admin_sql(
            maintenance,
            f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
            f'CREATE DATABASE "{name}"',
            f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS",
        )
    )
    owner_url = _with_path(base, name)
    try:
        migrate = subprocess.run(
            [
                sys.executable,
                "-m",
                "sandloom.core.cli.migrate",
                "upgrade-core",
                "--apply",
            ],
            cwd=HERE,
            env={
                **_child_base_env(),
                "ENVIRONMENT": "test",
                "DATABASE_URL": owner_url,
                "CACHE_URL": cache,
                "BROKER_URL": broker,
                "JWT_SECRET": f"replace-migrate-{uuid.uuid4().hex}",
                "SANDLOOM_MODULES": "[]",
            },
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert migrate.returncode == 0, migrate.stdout[-3000:] + migrate.stderr[-3000:]
        asyncio.run(_seed_notes(owner_url, role))
        yield Infra(
            database_url=_with_credentials(owner_url, role, password),
            cache_url=cache,
            broker_url=broker,
            logs=tmp_path_factory.mktemp("replaceability-logs"),
        )
    finally:
        asyncio.run(
            _admin_sql(
                maintenance,
                f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
                f'DROP ROLE IF EXISTS "{role}"',
            )
        )


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------


def _child_base_env() -> dict[str, str]:
    """The interpreter environment of a child, without any Sandloom setting."""
    env = {k: os.environ[k] for k in _PASS_THROUGH if k in os.environ}
    env["PYTHONPATH"] = str(HERE)
    if os.environ.get("SANDLOOM_TEST_OWNER"):
        env["SANDLOOM_TEST_OWNER"] = os.environ["SANDLOOM_TEST_OWNER"]
    return env


@dataclass
class Users:
    """This topology's ``ADMIN`` (in ``POC_ADMINS``) and ``USER`` ids."""

    admin: str = field(default_factory=lambda: str(uuid.uuid4()))
    user: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class Proc:
    """One process of a topology, serving the composition root ``root``."""

    label: str
    modules: list[str]
    env: dict[str, str]
    port: ReservedPort = field(default_factory=ReservedPort)
    server: SpawnedServer | None = None
    log: Path | None = None
    root: str = TRUST_ROOT

    @property
    def url(self) -> str:
        """The base URL the process serves on (reserved before it starts)."""
        return self.port.url

    def start(self, infra: Infra) -> None:
        """Spawn the process on its reserved port, logging to a file."""
        self.log = infra.logs / f"{self.label}-{uuid.uuid4().hex[:6]}.log"
        env = {**self.env, "SANDLOOM_MODULES": json.dumps(self.modules)}
        with self.log.open("wb") as fh:
            self.server = spawn_served(
                self.port,
                self.root,
                env=env,
                label=self.label,
                cwd=str(HERE),
                stdout=fh,
                stderr=subprocess.STDOUT,
            )

    def log_text(self) -> str:
        """The process log so far (empty before it started)."""
        return self.log.read_text(errors="replace") if self.log else ""

    def wait_ready(self, timeout: float = 90.0) -> None:
        """Until this spawn answers its nonce and ``/ready`` is 200."""
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

    def stop(self) -> None:
        """Stop the process; the port stays reserved until :meth:`close`."""
        if self.server is not None:
            self.server.stop()

    def close(self) -> None:
        """Stop the process and release its port."""
        self.stop()
        self.port.close()


def _base_env(infra: Infra, users: Users) -> dict[str, str]:
    """Settings every process of a topology shares."""
    return {
        **_child_base_env(),
        "DATABASE_URL": infra.database_url,
        "CACHE_URL": infra.cache_url,
        "BROKER_URL": infra.broker_url,
        "EVENT_BUS_BACKEND": "memory",
        "ENVIRONMENT": "dev",
        "OUTBOX_RELAY_ENABLED": "off",
        "INTERNAL_API_TOKEN": f"replace-internal-{uuid.uuid4().hex}",
        "RATE_LIMIT_ENABLED": "false",
        "LOG_FORMAT": "text",
        "POC_ADMINS": users.admin,
        # PostgreSQL row security on the default database.
        "INFRA": json.dumps({"database": {"default": {"tenant_rls": True}}}),
    }


def mono(
    infra: Infra,
    users: Users,
    modules: list[str],
    *,
    root: str = TRUST_ROOT,
    **extra: str,
) -> list[Proc]:
    """One process with ``modules`` on the composition root ``root``, HS256."""
    env = _base_env(infra, users)
    env["JWT_SECRET"] = f"replace-jwt-{uuid.uuid4().hex}"
    env.update(extra)
    return [Proc("mono", modules, env, root=root)]


def split(
    infra: Infra,
    users: Users,
    bindings: dict[str, dict[str, Any]],
    **extra: str,
) -> list[Proc]:
    """P1 (issuer + consumers) and P2 (providers) over the real internal RPC.

    Per-pair X25519 MACs, Ed25519 command signatures, ``service_id`` pins and
    the allowlist; EdDSA access tokens, whose private key only P1 holds.
    """
    jwt_key = generate_private_key("EdDSA")
    rpc = {s: generate_private_key("EdDSA") for s in ("p1", "p2")}
    dh = {s: generate_x25519_key() for s in ("p1", "p2")}
    p1 = Proc("p1", P1_MODULES, {})
    p2 = Proc("p2", P2_MODULES, {})
    bound = {
        name: {"base_url": p2.url, "service_id": "p2", **extra}
        for name, extra in bindings.items()
    }
    common = {
        **_base_env(infra, users),
        "JWT_ALGORITHM": "EdDSA",
        "JWT_KID": "replace-1",
        "JWT_PUBLIC_KEYS": json.dumps({"replace-1": public_pem(jwt_key)}),
        "JWT_PREVIOUS_SECRETS": "[]",
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
        **extra,
    }
    p1.env = {
        **common,
        "RPC_SERVICE_ID": "p1",
        "SANDLOOM_SERVICE_NAME": "p1",
        "RPC_PRIVATE_KEY": private_pem(rpc["p1"]),
        "RPC_DH_PRIVATE_KEY": x25519_private_pem(dh["p1"]),
        "JWT_PRIVATE_KEY": private_pem(jwt_key),
        "JWT_SECRET": "",
        "SERVICE_BINDINGS": json.dumps(bound),
    }
    p2.env = {
        **common,
        "RPC_SERVICE_ID": "p2",
        "SANDLOOM_SERVICE_NAME": "p2",
        "RPC_PRIVATE_KEY": private_pem(rpc["p2"]),
        "RPC_DH_PRIVATE_KEY": x25519_private_pem(dh["p2"]),
        "JWT_PRIVATE_KEY": "",
        "JWT_SECRET": "",
        "SERVICE_BINDINGS": "{}",
    }
    return [p1, p2]


@contextmanager
def running(infra: Infra, procs: list[Proc]) -> Iterator[list[Proc]]:
    """Start every process, wait until all are ready, stop them all after."""
    with ExitStack() as stack:
        for proc in procs:
            stack.callback(proc.close)
        for proc in procs:
            proc.start(infra)
        for proc in procs:
            proc.wait_ready()
        yield procs


def boot_refusal(infra: Infra, procs: list[Proc]) -> str:
    """Start the one process of ``procs``; it must exit before ready: its log."""
    (proc,) = procs
    try:
        proc.start(infra)
        assert proc.server is not None
        try:
            proc.server.proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{proc.label} started although the boot must be refused")
        assert proc.server.proc.returncode != 0
        return proc.log_text()
    finally:
        proc.close()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """A response's status and decoded JSON body (or text)."""

    status: int
    body: Any

    @property
    def detail(self) -> Any:
        """The error ``detail`` of a JSON body, else the body."""
        return self.body.get("detail") if isinstance(self.body, dict) else self.body


def _answer(response: httpx.Response) -> Answer:
    """``response`` as an :class:`Answer`."""
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return Answer(response.status_code, body)


def post(proc: Proc, path: str, body: dict[str, Any]) -> Answer:
    """``POST`` JSON to ``proc``."""
    return _answer(httpx.post(proc.url + path, json=body, timeout=20))


def get(
    proc: Proc, path: str, token: str | None = None, tenant: str | None = None
) -> Answer:
    """``GET`` from ``proc`` with an optional bearer token and ``X-Tenant-Id``."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if tenant:
        headers["X-Tenant-Id"] = tenant
    return _answer(httpx.get(proc.url + path, headers=headers, timeout=20))


def mint(issuer: Proc, user: str, module: str = "test_identity") -> Answer:
    """The issuer's half of a login: claims collected, a token minted."""
    return post(issuer, f"/api/v1/{module}/mint", {"user_id": user})


def token_of(answer: Answer) -> str:
    """The access token of a successful mint."""
    assert answer.status == 200, answer
    return str(answer.body["access_token"])


def forge(provider: Proc, payload: dict[str, Any]) -> None:
    """Set what the forged contributor answers."""
    assert post(provider, "/api/v1/forged/set", {"payload": payload}).status == 200


def gate_statuses(proc: Proc, token: str | None) -> dict[str, int]:
    """Status of each rights gate of ``shop`` and the kernel's platform API."""
    return {
        path: get(proc, path, token).status
        for path in (
            "/api/v1/shop/role-admin",
            "/api/v1/shop/perm-write",
            "/api/v1/shop/platform",
            "/api/v1/platform/shop/services",
        )
    }


def topology_of(name: str, infra: Infra, users: Users) -> list[Proc]:
    """The standard topology ``name``: ``mono`` (all six) or ``split``."""
    if name == "mono":
        return mono(infra, users, ALL)
    return split(infra, users, SPLIT_BINDINGS)


def _provider(procs: list[Proc]) -> Proc:
    """The process hosting the providers (P2 of a split, else the monolith)."""
    return procs[-1]


# ---------------------------------------------------------------------------
# A: identity replaced by trusted rights through the claims mechanism
# ---------------------------------------------------------------------------


def _assert_rights_from_the_issuer(issuer: Proc, users: Users) -> None:
    """A1: rights from the replacement issuer; the platform gates stay closed."""
    admin = mint(issuer, users.admin)
    assert admin.status == 200, admin
    claims = admin.body["claims"]
    assert claims["roles"] == ["admin"]
    assert claims["permissions"] == ["perm.poc.write"]
    assert claims["claims_sources_failed"] == []
    assert gate_statuses(issuer, token_of(admin)) == {
        "/api/v1/shop/role-admin": 200,
        "/api/v1/shop/perm-write": 200,
        # the platform gates do not trust a non-identity issuer (T1)
        "/api/v1/shop/platform": 403,
        "/api/v1/platform/shop/services": 403,
    }
    user = mint(issuer, users.user)
    assert user.status == 200, user
    assert user.body["claims"]["roles"] == []
    assert user.body["claims"]["permissions"] == []
    assert set(gate_statuses(issuer, token_of(user)).values()) == {403}


FORGED_PAYLOADS = (
    {"roles": ["admin"], "permissions": ["perm.poc.write"]},
    {"permissions": ["perm.poc.write"]},
    {"claims_sources": ["identity"]},
    {"provenance_authoritative": False},
    {"user_id": "ADMIN"},
    {"sub": "ADMIN"},
)


def _assert_forged_dropped(issuer: Proc, users: Users, payload: dict[str, Any]) -> None:
    """The login succeeds with the authorities' values; ``forged`` is dropped."""
    answer = mint(issuer, users.user)
    assert answer.status == 200, (payload, answer)
    claims = answer.body["claims"]
    assert claims["roles"] == [], payload
    assert claims["permissions"] == [], payload
    assert claims["claims_sources_failed"] == ["forged"], payload
    assert "forged" not in claims["claims_sources"], payload
    assert set(gate_statuses(issuer, token_of(answer)).values()) == {403}, payload


def _forged_payload(payload: dict[str, Any], users: Users) -> dict[str, Any]:
    """``payload`` with the placeholder ``"ADMIN"`` replaced by the admin's id."""
    return {k: users.admin if v == "ADMIN" else v for k, v in payload.items()}


@pytest.mark.parametrize("topology", ["mono", "split"])
def test_rights_come_from_a_replacement_issuer_and_a_forged_contributor_cannot_raise_them(
    infra: Infra, topology: str
) -> None:
    """A1, A2 (validation §1.3): ``Role``/``Perm`` work on a replacement issuer.

    A non-critical contributor (local, or remote over ``forged.claims.v1``)
    writing rights, provenance or the subject loses its whole result: the
    login still succeeds, with the authorities' values only (KB-10).
    """
    users = Users()
    with running(infra, topology_of(topology, infra, users)) as procs:
        issuer, provider = procs[0], _provider(procs)
        forge(provider, {})
        _assert_rights_from_the_issuer(issuer, users)
        for payload in FORGED_PAYLOADS:
            forge(provider, _forged_payload(payload, users))
            _assert_forged_dropped(issuer, users, payload)


def test_a_forged_contributor_registered_before_the_issuer_cannot_raise_rights(
    infra: Infra,
) -> None:
    """A2 with ``forged`` loaded first: still no token with forged rights, and
    the violator is ``forged`` whatever the order (never the issuer)."""
    users = Users()
    order = [f"{M}forged", f"{M}test_identity", *ALL[2:]]
    with running(infra, mono(infra, users, order)) as (proc,):
        forge(proc, {})
        _assert_rights_from_the_issuer(proc, users)
        for payload in FORGED_PAYLOADS:
            forge(proc, _forged_payload(payload, users))
            _assert_forged_dropped(proc, users, payload)


def test_a_critical_issuer_that_fails_refuses_the_login(infra: Infra) -> None:
    """A4: the critical rights contributor down → no token at all (503)."""
    users = Users()
    with running(infra, mono(infra, users, ALL)) as (proc,):
        assert post(proc, "/api/v1/test_identity/down", {"on": True}).status == 200
        answer = mint(proc, users.admin)
        assert answer.status == 503
        assert answer.detail == UNAVAILABLE_ISSUER


def test_a_noncritical_rights_authority_refuses_the_boot(infra: Infra) -> None:
    """A3 (validation §1.2), closed by KB-10: a non-critical authority of
    ``roles`` / ``permissions`` is refused at startup (ADR-0014 decision 7), so
    its outage can never let another contributor's rights through; and a
    ``forged`` contributor writing rights is a violator anyway."""
    users = Users()
    log = boot_refusal(infra, mono(infra, users, ALL, POC_TEST_IDENTITY_CRITICAL="0"))
    assert "SANDLOOM-B035" in log
    assert (
        "claim 'roles' is assigned to 'pocapp.modules.test_identity', whose claims "
        "contributor is registered critical=False" in log
    )


@pytest.mark.xfail(
    not IDENTITY_BATTERY,
    strict=True,
    reason="gap A6 (validation §1.2): the platform gates trust the module NAME "
    "'identity'; closed by KB-11 (one platform admission)",
)
def test_a_module_named_identity_is_not_trusted_as_the_platform_authority(
    infra: Infra,
) -> None:
    """A6: the replacement issuer under the short name ``identity``.

    With the identity battery installed, the local module is refused at boot
    (SANDLOOM-B020); without it, the name must not open the platform gates.
    """
    users = Users()
    procs = mono(infra, users, [f"{M}identity", f"{M}shop"], root=IDENTITY_ROOT)
    if IDENTITY_BATTERY:
        assert "SANDLOOM-B020" in boot_refusal(infra, procs)
        return
    with running(infra, procs) as (proc,):
        admin = mint(proc, users.admin, module="identity")
        assert admin.status == 200, admin
        token = token_of(admin)
        assert get(proc, "/api/v1/shop/platform", token).status == 403
        assert get(proc, "/api/v1/platform/shop/services", token).status == 403


# ---------------------------------------------------------------------------
# B: tenancy replaced by an independent tenant-grant provider
# ---------------------------------------------------------------------------


def _tenant_statuses(proc: Proc, token: str | None) -> dict[str, Answer]:
    """``TenantScope`` and the RLS-bound notes for each tenant header."""
    out = {"none": get(proc, "/api/v1/shop/tenant", token)}
    for name, tid in (("T_A", T_A), ("T_B", T_B), ("T_FOREIGN", T_FOREIGN)):
        out[name] = get(proc, "/api/v1/shop/tenant", token, tenant=tid)
    out["notes T_A"] = get(proc, "/api/v1/notes/items", token, tenant=T_A)
    out["notes T_B"] = get(proc, "/api/v1/notes/items", token, tenant=T_B)
    return out


def grant(provider: Proc, user: str, tenant: str, op: str = "grant") -> Answer:
    """Grant (or revoke) ``tenant`` to ``user`` at the grant provider."""
    return post(
        provider, f"/api/v1/grants/{op}", {"user_id": user, "tenant_id": tenant}
    )


@pytest.mark.parametrize("topology", ["mono", "split"])
def test_tenant_grants_from_an_independent_provider_drive_scope_and_row_security(
    infra: Infra, topology: str
) -> None:
    """B1-B5 (validation §1.4): the grant drives ``TenantScope`` and RLS.

    A header without a grant is 403, a forged contributor cannot grant
    tenants, revocation reaches every process, the provider down fails the
    login closed (in the split also with its process stopped), and a token
    minted before the fault keeps the tenant it was granted.
    """
    users = Users()
    with running(infra, topology_of(topology, infra, users)) as procs:
        issuer, provider = procs[0], _provider(procs)
        forge(provider, {})
        assert grant(provider, users.user, T_A).body == {"tenant_ids": [T_A]}

        # B1: the grant binds T_A for TenantScope, the ORM guard and RLS
        user = mint(issuer, users.user)
        assert user.body["claims"]["tenant_ids"] == [T_A]
        tok = token_of(user)
        seen = _tenant_statuses(issuer, tok)
        assert (seen["none"].status, seen["none"].body) == (200, {"tenant": T_A})
        assert (seen["T_A"].status, seen["T_A"].body) == (200, {"tenant": T_A})
        for key in ("T_B", "T_FOREIGN", "notes T_B"):
            assert (seen[key].status, seen[key].detail) == (403, NOT_AUTHORIZED), key
        assert seen["notes T_A"].status == 200
        assert seen["notes T_A"].body == {
            "rls_tenant_setting": T_A,
            "bodies": ["note of A"],
        }

        # B2: no token, or a token with no grant: never a tenant
        assert get(issuer, "/api/v1/shop/tenant", None, tenant=T_A).status == 401
        admin = mint(issuer, users.admin)
        assert admin.body["claims"]["tenant_ids"] == []
        seen = _tenant_statuses(issuer, token_of(admin))
        assert (seen["none"].status, seen["none"].detail) == (
            400,
            "tenant context required",
        )
        for key in ("T_A", "T_B", "T_FOREIGN", "notes T_A", "notes T_B"):
            assert (seen[key].status, seen[key].detail) == (403, NOT_AUTHORIZED), key

        # B3: a forged contributor writing tenants is dropped whole (KB-10):
        # the token carries the grant provider's tenants only
        for payload in (
            {"tenant_id": T_FOREIGN},
            {"org_id": T_FOREIGN},
            {"tenant_ids": [T_FOREIGN]},
        ):
            forge(provider, payload)
            answer = mint(issuer, users.user)
            assert answer.status == 200, (payload, answer)
            assert answer.body["claims"]["tenant_ids"] == [T_A], payload
            assert answer.body["claims"]["claims_sources_failed"] == ["forged"]
            assert "tenant_id" not in answer.body["claims"], payload
            assert "org_id" not in answer.body["claims"], payload
            seen = get(
                issuer, "/api/v1/shop/tenant", token_of(answer), tenant=T_FOREIGN
            )
            assert (seen.status, seen.detail) == (403, NOT_AUTHORIZED), payload
        forge(provider, {})

        # B5: revoking the grant revokes the user's tokens in every process
        assert grant(provider, users.user, T_A, op="revoke").body["tenant_ids"] == []
        old = get(issuer, "/api/v1/shop/tenant", tok, tenant=T_A)
        assert (old.status, old.detail) == (401, "Token has been revoked")
        old = get(issuer, "/api/v1/notes/items", tok, tenant=T_A)
        assert (old.status, old.detail) == (401, "Token has been revoked")
        time.sleep(2.5)  # past AUTH_REVOCATION_CLOCK_SKEW_SECONDS (2.0)
        again = mint(issuer, users.user)
        assert again.body["claims"]["tenant_ids"] == []
        assert (
            get(issuer, "/api/v1/shop/tenant", token_of(again), tenant=T_A).status
            == 403
        )

        # B4: the provider down fails the login closed
        assert grant(provider, users.user, T_B).body == {"tenant_ids": [T_B]}
        before = token_of(mint(issuer, users.user))
        assert post(provider, "/api/v1/grants/down", {"on": True}).status == 200
        down = mint(issuer, users.user)
        assert (down.status, down.detail) == (503, UNAVAILABLE_GRANTS)
        kept = get(issuer, "/api/v1/shop/tenant", before, tenant=T_B)
        assert (kept.status, kept.body) == (200, {"tenant": T_B})
        forge(provider, {"tenant_ids": [T_FOREIGN]})
        down = mint(issuer, users.user)
        assert (down.status, down.detail) == (503, UNAVAILABLE_GRANTS)
        forge(provider, {})
        assert post(provider, "/api/v1/grants/down", {"on": False}).status == 200
        if topology == "split":
            provider.close()  # the port too: the peer refuses connections
            down = mint(issuer, users.user)
            assert (down.status, down.detail) == (503, UNAVAILABLE_GRANTS)
            kept = get(issuer, "/api/v1/shop/tenant", before, tenant=T_B)
            assert (kept.status, kept.body) == (200, {"tenant": T_B})


NO_GRANTS = [f"{M}test_identity", f"{M}forged", f"{M}notes", f"{M}shop"]


def test_without_a_grant_provider_no_tenant_is_bound(infra: Infra) -> None:
    """B6, first half: no tenant-grant provider deployed → no tenant at all."""
    users = Users()
    with running(infra, mono(infra, users, NO_GRANTS, root=NO_GRANTS_ROOT)) as (proc,):
        forge(proc, {})
        answer = mint(proc, users.user)
        assert "tenant_ids" not in answer.body["claims"]
        seen = _tenant_statuses(proc, token_of(answer))
        assert (seen["none"].status, seen["none"].detail) == (
            400,
            "tenant context required",
        )
        for key in ("T_A", "T_B", "T_FOREIGN", "notes T_A", "notes T_B"):
            assert (seen[key].status, seen[key].detail) == (403, NOT_AUTHORIZED), key


def test_without_a_grant_provider_a_forged_contributor_binds_no_tenant(
    infra: Infra,
) -> None:
    """B6, second half: ``forged`` writes ``tenant_ids`` with no grant provider.

    Closed at the mint by KB-10: ``tenant_ids`` has no authority in this
    composition, so ``forged`` is a violator and its tenants never reach a
    token (the read side of B6, a token minted elsewhere, is KB-11).
    """
    users = Users()
    with running(infra, mono(infra, users, NO_GRANTS, root=NO_GRANTS_ROOT)) as (proc,):
        forge(proc, {"tenant_ids": [T_FOREIGN, T_B]})
        answer = mint(proc, users.user)
        assert answer.status == 200, answer
        assert "tenant_ids" not in answer.body["claims"]
        assert answer.body["claims"]["claims_sources_failed"] == ["forged"]
        seen = _tenant_statuses(proc, token_of(answer))
        assert seen["T_B"].status != 200
        assert seen["T_FOREIGN"].status != 200
        assert seen["notes T_B"].status != 200


# ---------------------------------------------------------------------------
# C: billing replaced by an unrelated fact, inventory.stock.available
# ---------------------------------------------------------------------------

BUY = {
    "/api/v1/shop/items/sku-in/buy": 200,
    "/api/v1/shop/items/sku-out/buy": 403,
    "/api/v1/shop/items/sku-in/buy?qty=5": 200,
    "/api/v1/shop/items/sku-in/buy?qty=6": 403,
    "/api/v1/shop/items/sku-none/buy": 403,
}


@pytest.mark.parametrize("topology", ["mono", "split"])
def test_an_unrelated_fact_gates_a_route_locally_and_over_rpc(
    infra: Infra, topology: str
) -> None:
    """C1, C2 (validation §1.5): ``Entitled`` on the owner's fact, no kernel change.

    In the split the fact is answered over ``inventory.facts.v1``; the owner's
    process stopped → 503, never an allow.
    """
    users = Users()
    bindings = {"grants.claims.v1": {}, "inventory.facts.v1": {}}
    procs = (
        mono(infra, users, ALL) if topology == "mono" else split(infra, users, bindings)
    )
    with running(infra, procs) as procs:
        issuer, provider = procs[0], _provider(procs)
        tok = token_of(mint(issuer, users.user))
        for path, status in BUY.items():
            answer = get(issuer, path, tok)
            assert answer.status == status, (path, answer)
            if status == 403:
                assert answer.detail == ENTITLEMENT, (path, answer)
        assert get(issuer, "/api/v1/shop/items/sku-in/buy").status == 401
        if topology == "split":
            provider.close()  # the port too: the peer refuses connections
            for path in BUY:
                answer = get(issuer, path, tok)
                assert answer.status == 503, (path, answer)
                assert answer.body["code"] == "remote_service_unavailable"


def test_a_fact_with_no_owner_loaded_or_bound_is_501(infra: Infra) -> None:
    """C1 with ``inventory`` neither loaded nor bound: 501, never an allow."""
    users = Users()
    procs = mono(infra, users, [f"{M}test_identity", f"{M}shop"], root=NO_GRANTS_ROOT)
    with running(infra, procs) as (proc,):
        tok = token_of(mint(proc, users.user))
        for path in BUY:
            answer = get(proc, path, tok)
            assert (answer.status, answer.body["code"]) == (501, "fact_unregistered")
        assert get(proc, "/api/v1/shop/items/sku-in/buy").status == 401


@pytest.mark.parametrize(
    "with_owner", [False, True], ids=["owner-absent", "owner-loaded"]
)
def test_a_fact_registered_under_another_module_name_refuses_the_boot(
    infra: Infra, with_owner: bool
) -> None:
    """C-squat, C-duplicate: ``forged`` registers ``inventory``'s fact (B034)."""
    users = Users()
    owner = [f"{M}inventory"] if with_owner else []
    modules = [f"{M}test_identity", *owner, f"{M}forged", f"{M}shop"]
    log = boot_refusal(
        infra, mono(infra, users, modules, root=NO_GRANTS_ROOT, POC_FORGED_FACT="1")
    )
    assert "SANDLOOM-B034" in log
    assert (
        "registers access fact 'inventory.stock.available' under module 'inventory'"
        in log
    )
