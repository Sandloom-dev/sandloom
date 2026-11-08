"""An application boots on SQLite (design/infra-agnostic/spec.md, slice 8b).

The settings accept any ``<dialect>+<dbapi>://`` URL and startup asks the
drivers (SANDLOOM-B024); a SQLite file takes its schema from the models at
startup (``create_all``), never altered: a file whose tables differ from the
models refuses to start, or is recreated in development (SANDLOOM-B028);
``sandloom migrate`` refuses it (SANDLOOM-B028); a production process refuses
it without its accepted risk, and always under the strict profile
(SANDLOOM-B029); ``DATABASE_SYSTEM_URL`` on it is refused (SANDLOOM-B026).

The boot itself runs the platform root (identity, tenancy, billing, admin)
in a child process on a file in ``tmp_path``: ``/health``, a public route
(registration) and authenticated ones (create and list an organization).

Moved from ``sandloom/core/tests/test_sqlite_boot.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.config.settings import settings


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


_BOOT = """
import asyncio, json
import httpx
from sandloom.main import app
from sandloom.sdk.testing import as_user

async def main():
    out = {}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            out["health"] = (await c.get("/health")).status_code
            r = await c.post("/auth/register", json={
                "email": "lite@example.com", "password": "Lite-pass-0123456789!",
                "full_name": "Lite"})
            out["register"] = r.status_code
            _, headers = as_user(tenant=False)
            r = await c.post("/api/v1/orgs", json={"name": "Acme", "slug": "acme"},
                             headers=headers)
            out["create_org"] = r.status_code
            r = await c.get("/api/v1/orgs", headers=headers)
            out["orgs"] = [o["slug"] for o in r.json()]
    print("RESULT " + json.dumps(out))

asyncio.run(main())
"""


def _boot(
    tmp_path: Path, database: Path, **extra: str
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "boot.py"
    script.write_text(_BOOT)
    env = {
        "JWT_SECRET": "sqlite-boot-test-secret-0123456789abcdef0123",
        "DATABASE_URL": _url(database),
        "CACHE_URL": settings.cache_url,
        "BROKER_URL": settings.broker_url,
        **extra,
    }
    return subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def _result(run: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert run.returncode == 0, run.stderr[-4000:]
    line = next(x for x in run.stdout.splitlines() if x.startswith("RESULT "))
    return dict(json.loads(line.removeprefix("RESULT ")))


@pytest.mark.subprocess_probe
def test_the_platform_boots_on_sqlite_and_refuses_a_drifted_file(
    tmp_path: Path,
) -> None:
    database = tmp_path / "platform.db"
    first = _result(_boot(tmp_path, database, ENVIRONMENT="test"))
    assert first == {
        "health": 200,
        "register": 202,
        "create_org": 201,
        "orgs": ["acme"],
    }

    # A model change the file does not have: create_all never alters it, and
    # ENVIRONMENT unset (dev by default) is no signal to touch the file.
    with sqlite3.connect(database) as conn:
        conn.execute("ALTER TABLE outbox_messages ADD COLUMN stray TEXT")
    refused = _boot(tmp_path, database)
    assert refused.returncode != 0
    assert "SANDLOOM-B028" in refused.stderr
    assert "outbox_messages.stray (not in the models)" in refused.stderr
    assert not [p for p in tmp_path.iterdir() if ".stale-" in p.name]
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT count(*) FROM tenancy_organizations"
        ).fetchone() == (1,)

    # The signal `sandloom dev run` sets: the file is set aside, a fresh one
    # built (the same slug is free again), and the log names where it went.
    recreated = _boot(tmp_path, database, DB_RECREATE_ON_MODEL_CHANGE="true")
    out = _result(recreated)
    assert out["create_org"] == 201 and out["orgs"] == ["acme"]
    stale = [p for p in tmp_path.iterdir() if p.name.startswith("platform.db.stale-")]
    assert stale and any(str(p) in recreated.stderr for p in stale)
    with sqlite3.connect(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox_messages)")}
        assert "stray" not in columns
