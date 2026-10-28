"""E2E: the configurable monolith split into two real processes.

Boots TWO uvicorn subprocesses from the same codebase, differing only in
configuration:

- **A** — ``SANDLOOM_MODULES=[identity, tenancy, e2e_probe]`` on database ``<db>_a``;
- **B** — ``SANDLOOM_MODULES=[billing, e2e_probe]`` on database ``<db>_b``;

with ``SERVICE_BINDINGS`` pointing at each other (pair-MAC authenticated,
each process with its own keys) and one shared Redis Streams event bus. It
proves:

1. register/login on A mints a token whose claims come from tenancy (local)
   AND billing (collected over RPC from B: ``billing.claims.v1``), with
   authoritative provenance;
2. a billing endpoint on B gated on organization membership resolves tenancy
   membership over RPC to A (and so do the live ``OrgRole`` /
   ``SubscriptionActive`` atoms);
3. an event published on A (``tenancy.organization_created.v1``) is consumed
   on B;
3b. billing's access facts (``Entitled(...)`` / ``Subscribed(of=Account.user())``) decide the
   same on A (billing remote, via ``billing.facts.v1``) as on B (local);
4. every contract call (read and command) answers identically whether the
   provider is local or remote.

Skips cleanly when PostgreSQL / Redis are unavailable — unless
``SANDLOOM_E2E_REQUIRED=1`` (set in CI): then missing infrastructure FAILS the run
instead of silently skipping the only proof that the split deployment works.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import httpx
import jwt
import pytest

from sandloom.core.contract_testing import rpc_env
from sandloom.core.testing import worker_env_values
from sandloom.core.testing.served import (
    ReservedPort,
    SpawnedServerError,
    probe,
    spawn_served,
)

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

BACKEND = Path(__file__).resolve().parents[2]
E2E_DIR = Path(__file__).resolve().parent
INTERNAL_TOKEN = "e2e-internal-token-" + uuid.uuid4().hex
STARTUP_TIMEOUT = 60.0


def _test_env() -> dict[str, str]:
    # Worker-aware under pytest-xdist (own database / Redis DBs per worker).
    values = worker_env_values(BACKEND / ".env.test")
    for key in ("DATABASE_URL", "CACHE_URL", "BROKER_URL", "JWT_SECRET"):
        if os.environ.get(f"E2E_{key}"):
            values[key] = os.environ[f"E2E_{key}"]
    return values


def _e2e_required() -> bool:
    return os.environ.get("SANDLOOM_E2E_REQUIRED", "").strip() == "1"


def _unavailable(reason: str) -> NoReturn:
    """Skip — or FAIL when ``SANDLOOM_E2E_REQUIRED=1`` (CI must not skip silently)."""
    if _e2e_required():
        pytest.fail(f"e2e infrastructure required but unavailable: {reason}")
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
    parsed = urlparse(base)
    return parsed._replace(path=f"/{name}").geturl()


async def _recreate_databases(base_url: str, names: list[str]) -> None:
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
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()
    for name in names:
        engine = create_async_engine(_db_url(base_url, name))
        try:
            async with engine.begin() as conn:
                await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "citext"'))
                await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
        finally:
            await engine.dispose()


async def _sql(url: str, statement: str, **params: Any) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


def _process_env(base: dict[str, str], **overrides: str) -> dict[str, str]:
    env = {**os.environ, **base}
    env.update(
        {
            "ENVIRONMENT": "test",
            "EVENT_BUS_BACKEND": "streams",
            "OUTBOX_RELAY_ENABLED": "on",
            "INTERNAL_API_TOKEN": INTERNAL_TOKEN,
            "RATE_LIMIT_ENABLED": "false",
            "NOTIFIER_BACKEND": "log",
            "SERVICE_RPC_TIMEOUT_SECONDS": "5",
            "PYTHONPATH": os.pathsep.join(
                [str(E2E_DIR), str(BACKEND / "src"), os.environ.get("PYTHONPATH", "")]
            ),
        }
    )
    env.update(overrides)
    return env


def _migrate(env: dict[str, str]) -> None:
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


class _Proc:
    def __init__(
        self, name: str, port: ReservedPort, env: dict[str, str], log: Path
    ) -> None:
        self.name = name
        self.url = port.url
        self._port = port
        self._log_path = log
        with log.open("wb") as out:
            # Served on the harness-held socket (no bind race) and identified
            # by a per-spawn nonce on readiness.
            self.served = spawn_served(
                port,
                "sandloom.main:app",
                env=env,
                label=name,
                cwd=BACKEND,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        self.proc = self.served.proc

    def logs(self) -> str:
        return self._log_path.read_text(errors="replace")[-6000:]

    def wait_ready(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            try:
                if probe(self.served):
                    return
            except SpawnedServerError as exc:
                raise AssertionError(f"{exc}\n{self.logs()}") from None
            time.sleep(0.3)
        raise AssertionError(f"{self.name} did not start:\n{self.logs()}")

    def stop(self) -> None:
        self.served.stop()
        self._port.close()


@pytest.fixture(scope="module")
def split(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    base = _test_env()
    db_url = base.get("DATABASE_URL", "")
    cache_url = base.get("CACHE_URL", "")
    if not db_url or not cache_url:
        _unavailable("DATABASE_URL / CACHE_URL not configured for e2e")
    if not _reachable(db_url, 5432):
        _unavailable(f"PostgreSQL unavailable at {urlparse(db_url).netloc}")
    if not _reachable(cache_url, 6379):
        _unavailable(f"Redis unavailable at {urlparse(cache_url).netloc}")

    base_name = urlparse(db_url).path.lstrip("/") or "sandloom_test"
    db_a, db_b = f"{base_name}_a", f"{base_name}_b"
    try:
        asyncio.run(_recreate_databases(db_url, [db_a, db_b]))
    except Exception as exc:  # noqa: BLE001 - no CREATEDB privilege etc.
        _unavailable(f"cannot create e2e databases: {exc}")

    port_a, port_b = ReservedPort(), ReservedPort()
    url_a, url_b = port_a.url, port_b.url
    to_b = {"base_url": url_b, "service_id": "e2e-b"}
    to_a = {"base_url": url_a, "service_id": "e2e-a"}
    # B serves billing, A every other default module. A binds every billing
    # contract; B binds the tenancy contracts it calls plus anything billing
    # REQUIRES (derived, so a module that gains a required contract needs no
    # edit here).
    from sandloom.core.descriptors.module_names import installed_modules
    from sandloom.core.testing.boot import module_topology, provided_contracts

    DEFAULT_MODULES = [m.path for m in installed_modules()]

    modules_a = [m for m in DEFAULT_MODULES if m != "sandloom_billing"]
    bindings_a: dict[str, Any] = dict.fromkeys(
        sorted(provided_contracts(["sandloom_billing"])), to_b
    )
    bindings_a["billing.claims.v1"] = {**to_b, "critical": False}
    wanted_b = {"tenancy.membership.v1", "tenancy.claims.v1"}
    wanted_b |= module_topology("sandloom_billing").requires
    bindings_b = dict.fromkeys(sorted(wanted_b), to_a)
    allowed = {"e2e-a": sorted(bindings_a), "e2e-b": sorted(bindings_b)}
    env_a = _process_env(
        base,
        DATABASE_URL=_db_url(db_url, db_a),
        SANDLOOM_MODULES=json.dumps([*modules_a, "e2e_probe"]),
        SERVICE_BINDINGS=json.dumps(bindings_a),
        **rpc_env("e2e-a", peers=["e2e-b"], allowed_calls=allowed),
    )
    env_b = _process_env(
        base,
        DATABASE_URL=_db_url(db_url, db_b),
        SANDLOOM_MODULES=json.dumps(["sandloom_billing", "e2e_probe"]),
        E2E_PROBE_CONSUME="1",
        SERVICE_BINDINGS=json.dumps(bindings_b),
        **rpc_env("e2e-b", peers=["e2e-a"], allowed_calls=allowed),
    )
    _migrate(env_a)
    _migrate(env_b)

    logs = tmp_path_factory.mktemp("e2e")
    proc_b = _Proc("B(billing)", port_b, env_b, logs / "b.log")
    proc_a = _Proc("A(identity+tenancy)", port_a, env_a, logs / "a.log")
    try:
        proc_b.wait_ready()
        proc_a.wait_ready()
        yield {
            "a": proc_a,
            "b": proc_b,
            "db_a": _db_url(db_url, db_a),
            "jwt_secret": base.get("JWT_SECRET", ""),
        }
    finally:
        proc_a.stop()
        proc_b.stop()
        # Never leave the per-run copies behind: unmarked, they pile up on a
        # shared test server (tools/checks/test_resources.py).
        from sqlalchemy.exc import SQLAlchemyError

        with contextlib.suppress(SQLAlchemyError, OSError):  # cleanup only
            asyncio.run(_drop_databases(db_url, [db_a, db_b]))


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


def _login(a: _Proc, email: str, password: str) -> tuple[str, dict[str, Any]]:
    resp = httpx.post(
        f"{a.url}/auth/login", json={"email": email, "password": password}, timeout=20
    )
    assert resp.status_code == 200, resp.text + a.logs()
    token = resp.json()["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    return token, claims


def test_split_deployment_end_to_end(split: dict[str, Any]) -> None:
    a: _Proc = split["a"]
    b: _Proc = split["b"]
    email = f"e2e-{uuid.uuid4().hex[:10]}@example.com"
    password = "E2e-Passw0rd!x"

    # -- register + verify + login on A ---------------------------------
    resp = httpx.post(
        f"{a.url}/auth/register",
        json={"email": email, "full_name": "E2E User", "password": password},
        timeout=20,
    )
    assert resp.status_code == 202, resp.text + a.logs()
    asyncio.run(
        _sql(
            split["db_a"],
            "UPDATE users SET is_verified = true WHERE email = :email",
            email=email,
        )
    )
    _token, claims = _login(a, email, password)
    user_id = claims["sub"]
    # Billing's claims were collected over RPC from B (billing.claims.v1);
    # tenancy's locally. Provenance is authoritative (failed list present).
    assert set(claims["claims_sources"]) >= {"identity", "tenancy", "billing"}, claims
    assert claims["claims_sources_failed"] == [], claims
    assert claims["tenant_ids"] == []

    # -- create an organization on A (publishes the event) --------------
    token1, _ = _login(a, email, password)
    resp = httpx.post(
        f"{a.url}/api/v1/orgs",
        json={"name": "E2E Org", "slug": f"e2e-{uuid.uuid4().hex[:8]}"},
        headers={"Authorization": f"Bearer {token1}"},
        timeout=20,
    )
    assert resp.status_code == 201, resp.text + a.logs()
    org_id = resp.json()["id"]

    # Fresh token: tenant_ids from tenancy now include the org.
    token, claims = _login(a, email, password)
    assert claims["tenant_ids"] == [org_id], claims
    auth = {"Authorization": f"Bearer {token}"}

    # -- billing endpoint on B resolves membership via RPC to A ---------
    resp = httpx.get(
        f"{b.url}/api/v1/billing/subscription",
        params={"org_id": org_id},
        headers=auth,
        timeout=20,
    )
    assert resp.status_code == 200, resp.text + b.logs()
    resp = httpx.get(
        f"{b.url}/api/v1/billing/subscription",
        params={"org_id": str(uuid.uuid4())},
        headers=auth,
        timeout=20,
    )
    assert resp.status_code == 403, resp.text  # not a member (asked A)

    # Live authz atoms on B: OrgRole via RPC to A; SubscriptionActive local.
    resp = httpx.get(f"{b.url}/_e2e/orgrole/{org_id}", headers=auth, timeout=20)
    assert resp.status_code == 200, resp.text + b.logs()
    resp = httpx.get(f"{b.url}/_e2e/subscribed/{org_id}", headers=auth, timeout=20)
    assert resp.status_code == 402, resp.text + b.logs()

    # -- billing's access facts decide identically on A (billing remote:
    # resolved through billing.facts.v1) and on B (billing local) ----------
    # No subscription anywhere: 402 (personal / member org); 403 for an
    # organization the user is not a member of — never a 500.
    for path, expected_status in (
        ("/_e2e/entitled/personal", 402),
        ("/_e2e/subscribed-claim", 402),
        (f"/_e2e/entitled/org/{org_id}", 402),
        (f"/_e2e/entitled/org/{uuid.uuid4()}", 403),
    ):
        on_a = httpx.get(f"{a.url}{path}", headers=auth, timeout=20)
        on_b = httpx.get(f"{b.url}{path}", headers=auth, timeout=20)
        assert on_b.status_code == expected_status, (path, on_b.text, b.logs())
        assert on_a.status_code == on_b.status_code, (path, on_a.text, a.logs())

    # -- event published on A is consumed on B --------------------------
    deadline = time.monotonic() + 20
    received: list[str] = []
    while time.monotonic() < deadline:
        received = httpx.get(f"{b.url}/_e2e/received", timeout=5).json()[
            "organization_ids"
        ]
        if org_id in received:
            break
        time.sleep(0.3)
    assert org_id in received, f"B never consumed the org event:\n{b.logs()}"

    # -- contracts answer identically locally and remotely --------------
    params = {"org_id": org_id, "user_id": user_id}
    on_a = httpx.get(f"{a.url}/_e2e/contracts", params=params, timeout=20)
    on_b = httpx.get(f"{b.url}/_e2e/contracts", params=params, timeout=20)
    assert on_a.status_code == 200, on_a.text + a.logs()
    assert on_b.status_code == 200, on_b.text + b.logs()
    expected = {
        "role": "owner",
        "member": True,
        "orgs": [org_id],
        "active": False,
        "balance": 0,
        "spend_status": 402,  # command contract: same domain error both ways
    }
    assert on_a.json() == expected
    assert on_b.json() == expected


async def _scalar(url: str, statement: str, **params: Any) -> Any:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(statement), params)).scalar()
    finally:
        await engine.dispose()


def test_emergency_block_across_the_split(split: dict[str, Any]) -> None:
    """block_user on B (no identity) stops A's issuers and ends A's sessions."""
    a: _Proc = split["a"]
    b: _Proc = split["b"]
    email = f"e2e-block-{uuid.uuid4().hex[:10]}@example.com"
    password = "E2e-Passw0rd!x"
    resp = httpx.post(
        f"{a.url}/auth/register",
        json={"email": email, "full_name": "E2E Block", "password": password},
        timeout=20,
    )
    assert resp.status_code == 202, resp.text + a.logs()
    asyncio.run(
        _sql(
            split["db_a"],
            "UPDATE users SET is_verified = true WHERE email = :email",
            email=email,
        )
    )
    first = httpx.post(
        f"{a.url}/auth/login", json={"email": email, "password": password}, timeout=20
    )
    assert first.status_code == 200, first.text
    refresh = first.json()["refresh_token"]
    auth = {"Authorization": f"Bearer {first.json()['access_token']}"}
    user_id = jwt.decode(
        first.json()["access_token"], options={"verify_signature": False}
    )["sub"]
    time.sleep(1.05)  # tokens minted in the revocation's own second stay valid

    resp = httpx.post(f"{b.url}/_e2e/block/{user_id}", timeout=20)
    assert resp.status_code == 200, resp.text + b.logs()

    # A (identity) refuses every issuance; both services refuse the token.
    for resp in (
        httpx.post(
            f"{a.url}/auth/login",
            json={"email": email, "password": password},
            timeout=20,
        ),
        httpx.post(
            f"{a.url}/auth/refresh", json={"refresh_token": refresh}, timeout=20
        ),
        httpx.get(f"{b.url}/_e2e/entitled/personal", headers=auth, timeout=20),
    ):
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == "Account blocked", resp.text
    # identity's own user loader masks every rejection as "Invalid token".
    assert httpx.get(f"{a.url}/auth/me", headers=auth, timeout=20).status_code == 401

    # B's outbox -> Streams -> identity's consumer on A revokes refresh tokens.
    deadline = time.monotonic() + 20
    live = None
    while time.monotonic() < deadline:
        live = asyncio.run(
            _scalar(
                split["db_a"],
                "SELECT count(*) FROM refresh_tokens "
                "WHERE user_id = :u AND is_revoked = false",
                u=uuid.UUID(user_id),
            )
        )
        if live == 0:
            break
        time.sleep(0.3)
    assert live == 0, f"refresh tokens not revoked on A:\n{a.logs()}"

    resp = httpx.post(f"{b.url}/_e2e/unblock/{user_id}", timeout=20)
    assert resp.status_code == 200, resp.text
    # Lifting the block does not revive the old session.
    resp = httpx.post(
        f"{a.url}/auth/refresh", json={"refresh_token": refresh}, timeout=20
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] != "Account blocked"
    assert httpx.get(f"{a.url}/auth/me", headers=auth, timeout=20).status_code == 401
    fresh = httpx.post(
        f"{a.url}/auth/login", json={"email": email, "password": password}, timeout=20
    )
    assert fresh.status_code == 200, fresh.text


def test_required_mode_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SANDLOOM_E2E_REQUIRED=1`` turns a missing-infra skip into a failure."""
    monkeypatch.setenv("SANDLOOM_E2E_REQUIRED", "1")
    with pytest.raises(pytest.fail.Exception, match="required but unavailable"):
        _unavailable("Redis unavailable")
    monkeypatch.setenv("SANDLOOM_E2E_REQUIRED", "0")
    with pytest.raises(pytest.skip.Exception):
        _unavailable("Redis unavailable")
