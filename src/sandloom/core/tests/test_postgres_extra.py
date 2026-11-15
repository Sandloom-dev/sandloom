"""asyncpg is the optional ``postgres`` extra of the kernel.

A kernel installed without it serves a SQLite database and refuses a
PostgreSQL URL at startup, naming the extra (SANDLOOM-B037). The app boot
imports neither asyncpg nor psycopg2 (the sync driver of the ops tools).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sandloom.core.app_factory import configuration_problems, dbapi_config_problem
from sandloom.core.config.settings import settings

#: Hides asyncpg from the child as a missing extra is: ``find_spec`` answers
#: ``None`` and an import raises ``ModuleNotFoundError``.
_BLOCK_ASYNCPG = """
import sys

sys.modules["asyncpg"] = None
"""

_BOOT = (
    _BLOCK_ASYNCPG
    + """
import asyncio
import httpx
from sandloom.core.app_factory import create_app

async def main():
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            health = (await c.get("/health")).status_code
    loaded = sorted(
        m for m, mod in sys.modules.items()
        if mod is not None and m.split(".")[0] in ("asyncpg", "psycopg2")
    )
    print("RESULT " + repr((health, loaded)))

asyncio.run(main())
"""
)

_SELECTION = {
    "database": {"default": {"driver": "sqlalchemy"}},
    "cache": {"default": {"driver": "memory"}},
    "eventbus": {"default": {"driver": "memory"}},
    "atomic_store": {"security": {"driver": "memory"}},
}


def _child_env(database_url: str) -> dict[str, str]:
    return {
        "JWT_SECRET": "postgres-extra-test-secret-0123456789abcdef",
        "DATABASE_URL": database_url,
        "CACHE_URL": settings.cache_url,
        "BROKER_URL": settings.broker_url,
        "SANDLOOM_MODULES": "[]",
        "SANDLOOM_SERVICE_NAME": "postgres-extra-probe",
        "INFRA": json.dumps(_SELECTION),
    }


def _run(
    code: str, tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "child.py"
    script.write_text(code)
    return subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.subprocess_probe
def test_the_app_boots_on_sqlite_without_asyncpg_and_imports_no_driver(
    tmp_path: Path,
) -> None:
    done = _run(_BOOT, tmp_path, _child_env(f"sqlite+aiosqlite:///{tmp_path}/app.db"))
    assert done.returncode == 0, done.stderr[-3000:]
    assert "RESULT (200, [])" in done.stdout, (done.stdout[-500:], done.stderr[-1500:])


_OPS_TOOLS = """
import importlib, sys

tools = [
    "sandloom.core.migrations.backup",
    "sandloom.core.cli.extract",
    "sandloom.core.cli.dev.services",
    "sandloom.core.testing.ownership",
    "sandloom.core.testing.resources",
    "sandloom.core.testing.slots",
    "sandloom.core.testing.session_checks",
    "sandloom.core.testing.migrations",
]
for name in tools:
    importlib.import_module(name)
assert "psycopg2" not in sys.modules, "an ops tool imported psycopg2 at import"
from sandloom.core.migrations.pg_sync import errors, psycopg2, sql
assert "psycopg2" not in sys.modules
assert issubclass(psycopg2.Error, Exception)  # the first read loads it
assert "psycopg2" in sys.modules
assert issubclass(errors.DuplicateDatabase, psycopg2.Error)
assert sql.SQL("select {}").format(sql.Identifier("t"))
print("OPS-OK")
"""


@pytest.mark.subprocess_probe
def test_the_ops_tools_import_psycopg2_when_they_first_use_it(tmp_path: Path) -> None:
    done = _run(
        _OPS_TOOLS, tmp_path, _child_env(f"sqlite+aiosqlite:///{tmp_path}/x.db")
    )
    assert done.returncode == 0, done.stderr[-3000:]
    assert "OPS-OK" in done.stdout


@pytest.mark.subprocess_probe
def test_a_postgresql_url_without_asyncpg_refuses_the_boot_naming_the_extra(
    tmp_path: Path,
) -> None:
    done = _run(
        _BLOCK_ASYNCPG
        + "from sandloom.core.app_factory import enforce_configuration\n"
        + "enforce_configuration()\n",
        tmp_path,
        _child_env("postgresql+asyncpg://u:p@127.0.0.1:1/db"),
    )
    assert done.returncode != 0
    assert "pip install 'sandloom[postgres]'" in done.stderr, done.stderr[-1500:]
    assert "SANDLOOM-B037" in done.stderr


def test_the_adapter_names_the_extra_only_when_asyncpg_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = importlib.util.find_spec
    installed = real("importlib")
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@h/db")

    def present(name: str, *args: object) -> object:
        return installed if name == "asyncpg" else real(name, *args)  # type: ignore[arg-type]

    def absent(name: str, *args: object) -> object:
        return None if name == "asyncpg" else real(name, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", present)
    assert dbapi_config_problem(settings) is None

    monkeypatch.setattr(importlib.util, "find_spec", absent)
    problem = dbapi_config_problem(settings) or ""
    assert "pip install 'sandloom[postgres]'" in problem
    assert "[SANDLOOM-B037]" in problem
    assert any("sandloom[postgres]" in p for p in configuration_problems(settings))
    # Another DBAPI is none of its business: SQLite keeps its own refusal.
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:////tmp/x.db")
    assert "asyncpg" not in (dbapi_config_problem(settings) or "")
