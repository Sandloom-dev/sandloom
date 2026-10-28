"""Platform-endpoint stand: the identity/tenancy/billing GETs of ``sandloom.main:app``.

Measures the per-request cost of the platform's own hot reads (``/auth/me``,
``/api/v1/orgs…``, ``/api/v1/billing/…``) in-process (ladder rungs ``P*``)
or over a socket (``server_bench.py``). It generates NO load by itself.

    # DATABASE_URL = an owner DSN of a dedicated database (created if absent)
    python -m benchsuite.platform seed    # migrations + one user, one org, membership
    python -m benchsuite.platform smoke   # every endpoint answers 200 (in-process)
    python -m benchsuite.platform env     # server env (export lines)

The seeded user is the owner of exactly one organization, so the billing GETs
resolve that organization (one ``tenancy.membership.v1`` contract call).
"""

from __future__ import annotations

import asyncio
import os
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

#: rung -> (description, path) — the platform GETs.
ENDPOINTS: dict[str, tuple[str, str]] = {
    "Pme": ("GET /auth/me", "/auth/me"),
    "Porg": ("GET /api/v1/orgs/{id} (guard + org)", f"/api/v1/orgs/{ORG}"),
    "Porgs": ("GET /api/v1/orgs (my orgs)", "/api/v1/orgs"),
    "Pmem": ("GET /api/v1/orgs/{id}/members", f"/api/v1/orgs/{ORG}/members"),
    "Psub": ("GET /api/v1/billing/subscription", "/api/v1/billing/subscription"),
    "Pcred": ("GET /api/v1/billing/credits", "/api/v1/billing/credits"),
}


def server_env() -> dict[str, str]:
    return {
        "ENVIRONMENT": "dev",
        "SANDLOOM_MODULES": '["sandloom_identity", "sandloom_tenancy", "sandloom_billing"]',
        "LOG_LEVEL": "WARNING",
        "RATE_LIMIT_ENABLED": "false",
        "DOCS_ENABLED": "false",
        "BILLING_PROVIDER": "fake",
        "DATABASE_URL": common.database_url(),
        "CACHE_URL": common.cache_url(),
        "BROKER_URL": common.broker_url(),
        "JWT_SECRET": common.jwt_secret(),
    }


def mint(hours: int = 6) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(USER),
        "token_type": "access",
        "iat": now,
        "nbf": now,
        "exp": now + hours * 3600,
        "jti": uuid.uuid4().hex,
        "email": "platform@example.com",
        "email_verified": True,
        "tenant_ids": [str(ORG)],
        "claims_sources": ["identity", "tenancy", "billing"],
    }
    return jwt.encode(claims, common.jwt_secret(), algorithm="HS256")


async def _ensure_database() -> None:
    parts = urlsplit(common.asyncpg_dsn())
    name = parts.path.lstrip("/").split("?")[0]
    conn = await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


def _migrate() -> None:
    exe = shutil.which("sandloom-migrate", path=os.path.dirname(sys.executable))
    cmd = [exe] if exe else [sys.executable, "-m", "sandloom.core.cli.migrate"]
    env = {**os.environ, **server_env(), "DATABASE_URL": common.raw_database_url()}
    subprocess.run([*cmd, "upgrade-all", "--apply"], env=env, check=True)


async def seed() -> None:
    await _ensure_database()
    _migrate()
    conn = await asyncpg.connect(common.asyncpg_dsn())
    try:
        await conn.execute(
            """INSERT INTO users (id, email, full_name, is_active, is_superuser,
                   is_verified, created_at, updated_at)
               VALUES ($1, 'platform@example.com', 'Platform User', true, false,
                       true, now(), now())
               ON CONFLICT (id) DO NOTHING""",
            USER,
        )
        await conn.execute(
            """INSERT INTO tenancy_organizations
                   (id, name, slug, owner_id, is_active, created_at, updated_at)
               VALUES ($1, 'Platform Org', 'platform-org', $2, true, now(), now())
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
    finally:
        await conn.close()
    print(f"platform database ready: {urlsplit(common.database_url()).path}")


async def smoke() -> None:
    os.environ.update(server_env())
    import httpx

    from sandloom.main import app

    headers = {"Authorization": f"Bearer {mint()}"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            bad = []
            for name, path in ENDPOINTS.values():
                r = await c.get(path, headers=headers)
                print(f"{r.status_code} {name} {r.text[:300]}")
                if r.status_code != 200:
                    bad.append((name, r.status_code))
    if bad:
        raise SystemExit(f"platform smoke failed: {bad}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        asyncio.run(seed())
    elif cmd == "smoke":
        asyncio.run(smoke())
    elif cmd == "env":
        for k, v in server_env().items():
            print(f"export {k}={shlex.quote(v)}")
    elif cmd == "token":
        print(mint())
    else:
        raise SystemExit("usage: python -m benchsuite.platform seed|smoke|env|token")


if __name__ == "__main__":
    main()
