"""S5 (secure tenant read) helper: setup, server env, token, smoke check.

    # 1. as the DB owner (DATABASE_URL = owner/superuser DSN): core chain, the
    #    S3/S4 tables AND the S5 table + RLS + bench_s5_get + app role
    python -m benchsuite.setup_db

    # 2. environment of the Sandloom server for one variant (uow|ro|fast|fn|plan|planopt);
    #    DATABASE_URL becomes the NON-superuser role S5_APP_ROLE
    eval "$(python -m benchsuite.s5 env ro)"

    # 3. an access token for tenant A (subscription claim included)
    python -m benchsuite.s5 token

    # 4. ≤ a few seconds, in-process, one variant per process: the S5 contract
    #    (200 own / 404 cross-tenant / 401 / 402 / 403) — or `smoke all`
    python -m benchsuite.s5 smoke all

The load generator then hits ``common.S5_ROUTES[variant] + <id>`` with ids in
``1..S5_ROWS_PER_TENANT`` (all owned by tenant A) and the token.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from typing import Any

import jwt

from benchsuite import common

VARIANTS = tuple(common.S5_ROUTES)


def diag_env(stage: str) -> dict[str, str]:
    """Server env of a cost-attribution stage d0..d6 (DIAGNOSTIC ONLY)."""
    _route, rls, revocation = common.S5_DIAG_STAGES[stage]
    env = server_env("uow")
    env["BENCH_S5_DIAG"] = "1"
    if not rls:  # owner DSN (RLS not enforced for it), no tenant_rls hook
        env["DATABASE_URL"] = common.database_url()
        env["INFRA"] = "{}"
    if not revocation:
        env["BENCH_DIAG_NO_REVOCATION"] = "1"
    return env


def server_env(variant: str) -> dict[str, str]:
    """Sandloom production env for ``variant`` (on top of the runner's _sandloom_env)."""
    if variant in common.S5_DIAG_STAGES:
        return diag_env(variant)
    if variant not in VARIANTS:
        raise SystemExit(f"unknown S5 variant {variant!r}; one of {VARIANTS}")
    return {
        "ENVIRONMENT": "prod",
        "SANDLOOM_MODULES": '["benchsuite.bench"]',
        "MODULE_REQUIRED": '["benchsuite.bench"]',
        "LOG_LEVEL": "WARNING",
        "RATE_LIMIT_ENABLED": "false",
        "BENCH_S5": "1",  # mount the S5 routes (and its TenantScoped model)
        "DATABASE_URL": common.s5_app_database_url(),
        "CACHE_URL": common.cache_url(),
        "BROKER_URL": common.broker_url(),
        "JWT_SECRET": common.jwt_secret(),
        # A production process without the token issuer verifying the shared
        # HS256 secret (the stand mints its own tokens): accepted explicitly.
        "SANDLOOM_ACCEPTED_RISKS": "shared_hs_secret_split,relay_disabled,local_storage_in_prod",
        "INFRA": json.dumps({"database": {"default": {"tenant_rls": True}}}),
        "DB_READ_ONLY_SESSIONS": "true" if variant == "ro" else "false",
        "PLANNER_OPTIMIZED_EXECUTOR": (
            "true" if variant in ("planopt", "planopt2") else "false"
        ),
    }


def mint(
    tenant: str = common.S5_TENANTS[0],
    *,
    subscription: str | None = "active",
    hours: int = 6,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "token_type": "access",
        "iat": now,
        "nbf": now,
        "exp": now + hours * 3600,
        "jti": uuid.uuid4().hex,
        "tenant_id": tenant,
        "tenant_ids": [tenant],
    }
    if subscription is not None:
        claims["subscription_status"] = subscription
    return jwt.encode(claims, common.jwt_secret(), algorithm="HS256")


async def _smoke(variant: str) -> None:
    import httpx

    from benchsuite.apps.sandloom_app import app

    route = (
        common.S5_DIAG_STAGES[variant][0]
        if variant in common.S5_DIAG_STAGES
        else common.S5_ROUTES[variant]
    )
    a, b = common.S5_TENANTS
    own, cross = 1, common.S5_ROWS_PER_TENANT + 1
    good = {"Authorization": f"Bearer {mint(a)}"}
    checks = [
        (f"{route}{own}", good, 200),
        (f"{route}{cross}", good, 404),
        (f"{route}{own}", {}, 401),
        (
            f"{route}{own}",
            {"Authorization": f"Bearer {mint(a, subscription=None)}"},
            402,
        ),
        (f"{route}{own}", {**good, "X-Tenant-Id": b}, 403),
    ]
    if variant in common.S5_DIAG_STAGES:  # each stage: only its layers apply
        n = int(variant[1])
        nosub = {"Authorization": f"Bearer {mint(a, subscription=None)}"}
        checks = [
            (f"{route}{own}", good, 200),
            (f"{route}{cross}", good, 404 if n >= 2 else 200),
            (f"{route}{own}", {}, 401 if n >= 1 else 200),
            (f"{route}{own}", nosub, 402 if n >= 5 else 200),
        ]
        if n >= 2:
            checks.append((f"{route}{own}", {**good, "X-Tenant-Id": b}, 403))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://s5"
        ) as http,
    ):
        for path, headers, status in checks:
            r = await http.get(path, headers=headers)
            if r.status_code != status:
                raise SystemExit(
                    f"[{variant}] {path}: {r.status_code} != {status}: {r.text}"
                )
        body = (await http.get(f"{route}{own}", headers=good)).json()
        if body != {"id": own, "name": f"titem-{own}", "value": 1}:
            raise SystemExit(f"[{variant}] wrong body {body}")
    print(f"S5 {variant}: contract OK ({len(checks) + 1} requests)")


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "help"
    if cmd == "env":
        for k, v in server_env(argv[1]).items():
            print(f"export {k}={shlex.quote(v)}")
    elif cmd == "token":
        print(mint(argv[1] if len(argv) > 1 else common.S5_TENANTS[0]))
    elif cmd == "smoke":
        variant = argv[1] if len(argv) > 1 else "all"
        if variant == "diag":
            for v in common.S5_DIAG_STAGES:
                subprocess.run(
                    [sys.executable, "-m", "benchsuite.s5", "smoke", v], check=True
                )
            return
        if variant == "all":
            for v in VARIANTS:  # one process per variant: settings read at import
                subprocess.run(
                    [sys.executable, "-m", "benchsuite.s5", "smoke", v], check=True
                )
            return
        os.environ.update(server_env(variant))
        asyncio.run(_smoke(variant))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
