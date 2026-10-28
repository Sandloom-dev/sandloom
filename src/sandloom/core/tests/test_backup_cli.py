"""``sandloom backup plan|verify`` and the operator guard rails (H-20).

* the plan names every database instance and Redis endpoint, flags the
  security cache as critical and never prints a password;
* ``verify`` restores into a throw-away database, checks every hosted
  migration chain is at its head plus basic integrity, and drops it;
* a restore that fails, or a database behind its chain head, fails the
  verification;
* with ``SANDLOOM_TEST_PG_CLIENT`` (e.g. ``docker exec -i sandloom_postgres_test``) and
  ``SANDLOOM_TEST_PG_CLIENT_URL`` (the server as that client reaches it) the real
  ``pg_dump`` → ``pg_restore`` round trip runs too.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from sandloom.core.cli._operator import add_operator_arguments, operator_intent
from sandloom.core.config.settings import settings
from sandloom.core.migrations.backup import (
    CORE_VERSION_TABLE,
    build_backup_plan,
    command_restorer,
    dump_format,
    format_plan,
    verify_dump,
)
from sandloom.core.migrations.migrations import core_chain_dir
from sandloom.core.testing import reload_test_env
from sandloom.core.testing.migrations import (
    migrate_module,
    run_sql,
    temporary_database,
    upgrade_core_chain,
)

IDENTITY_CHAIN = (
    "sandloom_identity",
    str(Path(__file__).resolve().parents[3] / "sandloom_identity" / "migrations"),
    "alembic_version_identity",
)
ENV_TEST = Path(__file__).resolve().parents[4] / ".env.test"


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    if ENV_TEST.exists():
        reload_test_env(ENV_TEST)


CHAINS = [("core", str(core_chain_dir()), CORE_VERSION_TABLE), IDENTITY_CHAIN]


def _server_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured")
    return url


def _migrate(url: str, identity_revision: str = "head") -> None:
    upgrade_core_chain(url)
    migrate_module(url, "sandloom_identity", identity_revision)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_plan_covers_databases_and_security_redis_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {
            "database": {
                "analytics": {
                    "driver": "sqlalchemy",
                    "url": "postgresql+asyncpg://u:s3cret@db2:5432/analytics",
                }
            },
            "cache": {"security": {"driver": "redis", "url": "redis://:pw@sec:6379/0"}},
        },
    )
    monkeypatch.setattr(settings, "security_cache_instance", "security")
    items = build_backup_plan(settings)
    text = format_plan(items)
    names = {(i.kind, i.name) for i in items}
    assert ("postgres", "default") in names
    assert ("postgres", "analytics") in names
    security = [i for i in items if i.kind == "redis" and "cache:security" in i.name]
    assert len(security) == 1
    assert security[0].criticality == "critical"
    assert any("reconcile-security" in step for step in security[0].restore)
    assert any("noeviction" in step for step in security[0].backup)
    assert "s3cret" not in text and ":pw@" not in text
    assert "pg_dump --format=custom" in text
    assert "TWOFA_ENCRYPTION_KEY" in text


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _fake_dump(tmp_path: Path) -> Path:
    dump = tmp_path / "default.dump"
    dump.write_bytes(b"PGDMP fake custom-format header")
    return dump


def test_verify_passes_for_a_current_restore_and_drops_the_scratch_db(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    def restore(dump: Path, url: str) -> None:
        seen.append(url)
        _migrate(url)

    report = verify_dump(
        _fake_dump(tmp_path), server_url=_server_url(), chains=CHAINS, restorer=restore
    )
    assert report.ok, report.as_dict()
    names = {c.name for c in report.checks}
    assert {
        "restore",
        "chain core",
        "chain sandloom_identity",
        "foreign keys hold",
    } <= names
    assert {"table audit_log", "table outbox_messages"} <= names
    scratch = urlsplit(seen[0]).path.lstrip("/")
    assert scratch == report.scratch
    left = run_sql(
        _sync(_server_url(), "postgres"),
        "SELECT count(*) FROM pg_database WHERE datname = %s",
        (scratch,),
    )
    assert left[0][0] == 0


def test_verify_fails_when_a_chain_is_behind_its_head(tmp_path: Path) -> None:
    report = verify_dump(
        _fake_dump(tmp_path),
        server_url=_server_url(),
        chains=CHAINS,
        restorer=lambda _d, url: _migrate(url, "0007_identity"),
    )
    assert not report.ok
    failed = {c.name: c.detail for c in report.checks if not c.ok}
    assert list(failed) == ["chain sandloom_identity"]
    assert "0007_identity" in failed["chain sandloom_identity"]


def test_verify_fails_when_the_restore_fails(tmp_path: Path) -> None:
    def broken(dump: Path, url: str) -> None:
        raise subprocess.CalledProcessError(1, ["pg_restore"])

    report = verify_dump(
        _fake_dump(tmp_path), server_url=_server_url(), chains=CHAINS, restorer=broken
    )
    assert not report.ok
    assert [c.name for c in report.checks] == ["restore"]


def test_verify_flags_orphan_rows_of_a_broken_restore(tmp_path: Path) -> None:
    def partial(dump: Path, url: str) -> None:
        _migrate(url)
        run_sql(
            url.replace("postgresql+psycopg2://", "postgresql://"),
            "SET session_replication_role = replica; "
            "INSERT INTO user_roles (id, user_id, role_id, assigned_at) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), now())",
        )

    report = verify_dump(
        _fake_dump(tmp_path), server_url=_server_url(), chains=CHAINS, restorer=partial
    )
    failed = [c for c in report.checks if not c.ok]
    assert [c.name for c in failed] == ["foreign keys hold"], report.as_dict()


def test_dump_format_detection(tmp_path: Path) -> None:
    custom = tmp_path / "a.dump"
    custom.write_bytes(b"PGDMP\x01\x0e")
    plain = tmp_path / "a.sql"
    plain.write_text("-- PostgreSQL database dump\n")
    tar = tmp_path / "a.tar"
    tar.write_bytes(b"\0" * 257 + b"ustar" + b"\0" * 250)
    assert dump_format(custom) == "custom"
    assert dump_format(plain) == "plain"
    assert dump_format(tar) == "tar"
    assert dump_format(tmp_path) == "directory"


def _sync(url: str, db: str) -> str:
    from sandloom.core.testing.migrations import _sync as sync

    return sync(url, db)


@pytest.mark.slow
def test_real_pg_dump_round_trip(tmp_path: Path) -> None:
    client = os.getenv("SANDLOOM_TEST_PG_CLIENT")
    client_url = os.getenv("SANDLOOM_TEST_PG_CLIENT_URL")
    if not client or not client_url:
        pytest.skip("SANDLOOM_TEST_PG_CLIENT / SANDLOOM_TEST_PG_CLIENT_URL not set")
    with temporary_database(_server_url()) as source:
        _migrate(source)
        run_sql(
            source,
            "INSERT INTO users (id, email, full_name, is_active, is_superuser, "
            "is_verified) VALUES (gen_random_uuid(), 'dump@example.com', 'D', "
            "true, false, true)",
        )
        name = urlsplit(source).path.lstrip("/")
        parts = urlsplit(client_url)
        dsn = parts._replace(path=f"/{name}").geturl()
        dump = tmp_path / "source.dump"
        with dump.open("wb") as out:
            subprocess.run(
                [*shlex.split(client.replace(" -i", "")), "pg_dump", "-Fc", dsn],
                stdout=out,
                check=True,
            )
    report = verify_dump(
        dump,
        server_url=_server_url(),
        chains=CHAINS,
        restorer=command_restorer(
            pg_restore=f"{client} pg_restore", restore_url=client_url
        ),
    )
    assert report.ok, report.as_dict()


# --------------------------------------------------------------------------- #
# operator guard rails
# --------------------------------------------------------------------------- #
def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_operator_arguments(parser)
    return parser.parse_args(list(argv))


def test_operator_guard_rails(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.testing import set_environment

    dry = operator_intent(_args())
    assert dry.apply is False
    with pytest.raises(SystemExit, match="--reason"):
        operator_intent(_args("--apply"))
    ok: Any = operator_intent(_args("--apply", "--reason", "leak", "--operator", "al"))
    assert (ok.apply, ok.operator, ok.reason) == (True, "al", "leak")

    set_environment(monkeypatch, "prod")
    with pytest.raises(SystemExit, match="--confirm-production"):
        operator_intent(_args("--apply", "--reason", "leak"))
    confirmed = operator_intent(
        _args("--apply", "--confirm-production", "--reason", "leak")
    )
    assert confirmed.apply is True
    assert operator_intent(_args()).apply is False  # a dry-run is always allowed
