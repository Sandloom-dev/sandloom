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
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest
from pydantic import BaseModel, Field

from sandloom.core.cli._operator import add_operator_arguments, operator_intent
from sandloom.core.config.settings import settings
from sandloom.core.migrations.backup import (
    CORE_TABLES,
    CORE_VERSION_TABLE,
    LEASE_FENCE_OFFSET,
    build_backup_plan,
    command_restorer,
    dump_format,
    fence_leases,
    format_plan,
    verify_dump,
)
from sandloom.core.migrations.migrations import core_chain_dir
from sandloom.core.testing import reload_test_env
from sandloom.core.testing.migrations import (
    MigrationChain,
    migrate_module,
    run_sql,
    tables_created_by,
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


class _KeyedModule(BaseModel):
    """A module's settings declaration with a secret to back up."""

    shop_signing_key: str | None = Field(None, alias="SHOP_SIGNING_KEY")
    backup: ClassVar[tuple[tuple[str, str], ...]] = (
        ("SHOP_SIGNING_KEY", "losing SHOP_SIGNING_KEY: orders cannot be verified"),
    )


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
    items = build_backup_plan(settings, (), [_KeyedModule])
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
    # the kernel's keys, and each loaded module's declared secret and loss note
    assert "JWT_SECRET" in text and "STRIPE" not in text
    assert "SHOP_SIGNING_KEY" in text
    assert "orders cannot be verified" in text


def test_plan_names_no_module_secret_without_a_declaration() -> None:
    text = format_plan(build_backup_plan(settings))
    assert "TWOFA_ENCRYPTION_KEY" not in text and "STRIPE_" not in text


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _fake_dump(tmp_path: Path) -> Path:
    dump = tmp_path / "default.dump"
    dump.write_bytes(b"PGDMP fake custom-format header")
    return dump


def test_verify_fails_when_the_restore_fails(tmp_path: Path) -> None:
    def broken(dump: Path, url: str) -> None:
        raise subprocess.CalledProcessError(1, ["pg_restore"])

    report = verify_dump(
        _fake_dump(tmp_path), server_url=_server_url(), chains=CHAINS, restorer=broken
    )
    assert not report.ok
    assert [c.name for c in report.checks] == ["restore"]


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


# --------------------------------------------------------------------------- #
# core tables, lease fences
# --------------------------------------------------------------------------- #
def test_core_tables_equal_what_the_core_chain_creates() -> None:
    chain = MigrationChain(
        key="core",
        module=None,
        script_location=core_chain_dir(),
        version_table=CORE_VERSION_TABLE,
    )
    assert set(CORE_TABLES) == tables_created_by(chain)
    assert len(set(CORE_TABLES)) == len(CORE_TABLES)


def test_fence_leases_lift_fences_and_release_leases() -> None:
    with temporary_database(_server_url()) as url:
        upgrade_core_chain(url)
        run_sql(
            url,
            "INSERT INTO infra_lease (name, owner, token, expires_at, fence) VALUES "
            "('held', 'a', 't1', now() + interval '1 hour', 5), "
            "('free', 'b', 't2', now() - interval '1 hour', 9)",
        )
        dry = fence_leases(url, apply=False)
        assert [(x.name, x.fence_before, x.held) for x in dry] == [
            ("free", 9, False),
            ("held", 5, True),
        ]
        assert [x.fence_before for x in fence_leases(url, apply=False)] == [9, 5]
        applied = fence_leases(url, apply=True)
        assert [x.fence_after for x in applied] == [
            9 + LEASE_FENCE_OFFSET,
            5 + LEASE_FENCE_OFFSET,
        ]
        after = fence_leases(url, apply=False)
        assert [(x.fence_before, x.held) for x in after] == [
            (9 + LEASE_FENCE_OFFSET, False),
            (5 + LEASE_FENCE_OFFSET, False),
        ]


def test_fence_leases_without_the_table_is_empty() -> None:
    with temporary_database(_server_url()) as url:
        assert fence_leases(url, apply=True) == []


def test_fence_leases_refuses_a_non_positive_offset() -> None:
    with pytest.raises(ValueError, match="positive"):
        fence_leases("postgresql://x/y", apply=True, offset=0)


def test_fence_leases_command_is_dry_run_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sandloom.core.migrations.migrations as migrations
    from sandloom.core.cli.backup import main

    with temporary_database(_server_url()) as url:
        upgrade_core_chain(url)
        run_sql(
            url,
            "INSERT INTO infra_lease (name, owner, token, expires_at, fence) "
            "VALUES ('x', 'a', 't', now() + interval '1 hour', 3)",
        )
        monkeypatch.setattr(
            migrations, "list_database_instances", lambda: {"default": url}
        )
        assert main(["fence-leases", "--json"]) == 0
        assert '"fence_before": 3' in capsys.readouterr().out
        assert fence_leases(url, apply=False)[0].fence_before == 3
        assert main(["fence-leases", "--database", "nope"]) == 2
        with pytest.raises(SystemExit, match="--reason"):
            main(["fence-leases", "--apply"])
        assert fence_leases(url, apply=False)[0].fence_before == 3
