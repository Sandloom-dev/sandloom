"""Run owner-console browser acceptance with a new, disposable test database.

Requires the contributor environment, admin npm dependencies and an isolated
SANDLOOM_TEST_DATABASE_URL. Creates/drops only its own uniquely named database;
never uses a development/production database as its application database.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import asyncpg
from argon2 import PasswordHasher
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]


async def provision(base: str, database: str) -> dict[str, str]:
    url = make_url(base)
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database="postgres",
    )
    try:
        await connection.execute(
            f'CREATE DATABASE "{database}"'
        )  # generated identifier only
    finally:
        await connection.close()
    return {
        "DATABASE_URL": url.set(database=database).render_as_string(hide_password=False)
    }


async def seed(database_url: str, password: str) -> dict[str, str]:
    url = make_url(database_url)
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database,
    )
    try:
        ids = [uuid4() for _ in range(7)]
        owner, target, member, admin_role, editor_role, permission, customer = ids
        hashed = PasswordHasher().hash(password)
        async with connection.transaction():
            for uid, email, name in [
                (owner, "owner@acceptance.example.com", "Acceptance owner"),
                (target, "target@acceptance.example.com", "Acceptance target"),
                (member, "member@acceptance.example.com", "Acceptance member"),
            ]:
                await connection.execute(
                    "INSERT INTO users "
                    "(id,email,full_name,hashed_password,is_active,is_superuser,"
                    "is_verified,password_reset_required,created_at,updated_at,"
                    "email_verified_at) "
                    "VALUES ($1,$2,$3,$4,true,false,true,false,now(),now(),now())",
                    uid,
                    email,
                    name,
                    hashed,
                )
            for rid, name, code, system in [
                (admin_role, "Acceptance admin", "admin", True),
                (editor_role, "Acceptance editor", "acceptance_editor", False),
            ]:
                await connection.execute(
                    "INSERT INTO roles "
                    "(id,name,code,is_system,created_at,updated_at) VALUES "
                    "($1,$2,$3,$4,now(),now())",
                    rid,
                    name,
                    code,
                    system,
                )
            await connection.execute(
                "INSERT INTO user_roles (id,user_id,role_id,assigned_at) VALUES "
                "($1,$2,$3,now())",
                uuid4(),
                owner,
                admin_role,
            )
            await connection.execute(
                "INSERT INTO permissions (id,name,code,is_system,created_at) VALUES "
                "($1,$2,$3,false,now())",
                permission,
                "Acceptance read",
                "acceptance.read",
            )
            await connection.execute(
                "INSERT INTO billing_customers (id,user_id) VALUES ($1,$2)",
                customer,
                target,
            )
            product = await connection.fetchval(
                "INSERT INTO billing_products (name,type,is_active) "
                "VALUES ('Acceptance product','one_time',true) RETURNING id"
            )
        return {
            "SANDLOOM_ACCEPTANCE_EMAIL": "owner@acceptance.example.com",
            "SANDLOOM_ACCEPTANCE_TARGET": str(target),
            "SANDLOOM_ACCEPTANCE_CUSTOMER": str(customer),
            "SANDLOOM_ACCEPTANCE_PRODUCT": str(product),
        }
    finally:
        await connection.close()


async def cleanup(base: str, database: str) -> None:
    url = make_url(base)
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database="postgres",
    )
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1 "
            "AND pid<>pg_backend_pid()",
            database,
        )
        await connection.execute(
            f'DROP DATABASE IF EXISTS "{database}"'
        )  # generated identifier only
    finally:
        await connection.close()


def main() -> int:
    base = os.environ.get("SANDLOOM_TEST_DATABASE_URL", "")
    if not base or "test" not in (make_url(base).database or ""):
        raise SystemExit(
            "Set SANDLOOM_TEST_DATABASE_URL to your isolated test service first"
        )
    database = "sandloom_admin_acceptance_test_" + uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    env = {**os.environ, **asyncio.run(provision(base, database))}
    env.update(
        {
            "ENVIRONMENT": "dev",
            "JWT_SECRET": secrets.token_urlsafe(48),
            "SANDLOOM_MODULES": (
                '["sandloom_identity","sandloom_tenancy","sandloom_billing"]'
            ),
            "SANDLOOM_COMPOSITION_ROOT": "sandloom.main",
            "INTERNAL_API_TOKEN": secrets.token_urlsafe(32),
            "SERVICE_BINDINGS": "{}",
            "EVENT_BUS_BACKEND": "memory",
            "BILLING_PROVIDER": "fake",
            "NOTIFIER_BACKEND": "log",
            "CACHE_URL": os.environ.get(
                "SANDLOOM_TEST_CACHE_URL", "redis://127.0.0.1:6380/0"
            ),
            "BROKER_URL": os.environ.get(
                "SANDLOOM_TEST_BROKER_URL", "redis://127.0.0.1:6380/1"
            ),
            "SANDLOOM_ADMIN_API_TARGET": "http://127.0.0.1:2289",
            "SANDLOOM_ACCEPTANCE_PASSWORD": password,
        }
    )
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="sandloom-admin-live-") as temp:
            env["STORAGE_PATH"] = temp
            log_path = ROOT / "admin/.cache/live-acceptance/backend.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("w+") as log:
                log_path.chmod(0o600)
                subprocess.run(  # noqa: S603 - fixed repository migration CLI
                    [
                        str(ROOT / ".venv/bin/sandloom"),
                        "migrate",
                        "upgrade-all",
                        "--apply",
                    ],
                    cwd=ROOT,
                    env=env,
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                env.update(asyncio.run(seed(env["DATABASE_URL"], password)))
                process = subprocess.Popen(  # noqa: S603 - fixed local uvicorn entrypoint
                    [
                        str(ROOT / ".venv/bin/python"),
                        "-m",
                        "uvicorn",
                        "sandloom.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "2289",
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError(
                            "Isolated backend exited; review the local acceptance log"
                        )
                    try:
                        with urlopen(  # noqa: S310 - fixed loopback HTTP health endpoint
                            env["SANDLOOM_ADMIN_API_TARGET"] + "/health", timeout=1
                        ) as response:
                            if response.status == 200:
                                break
                    except (URLError, TimeoutError):
                        time.sleep(0.2)
                else:
                    raise RuntimeError("Isolated backend did not become healthy")
                result = subprocess.run(  # noqa: S603 - fixed local Playwright command
                    [
                        shutil.which("npx") or "/usr/bin/false",
                        "playwright",
                        "test",
                        "--config",
                        "playwright.live.config.ts",
                    ],
                    cwd=ROOT / "admin",
                    env=env,
                    check=False,
                )
                # Browser error contexts can include input values, even for password
                # controls. Redact our ephemeral credential before retaining artifacts.
                for artifact in (ROOT / "admin/test-results").rglob("*.md"):
                    artifact.write_text(
                        artifact.read_text().replace(password, "<REDACTED>")
                    )
                return result.returncode
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        asyncio.run(cleanup(base, database))


if __name__ == "__main__":
    raise SystemExit(main())
