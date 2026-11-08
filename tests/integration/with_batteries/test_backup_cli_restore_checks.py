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

Moved from ``sandloom/core/tests/test_backup_cli.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from sandloom.core.migrations.backup import (
    CORE_VERSION_TABLE,
    verify_dump,
)
from sandloom.core.migrations.migrations import core_chain_dir
from sandloom.core.testing.migrations import (
    migrate_module,
    run_sql,
    upgrade_core_chain,
)

IDENTITY_CHAIN = (
    "sandloom_identity",
    str(
        Path(__file__).resolve().parents[3] / "src" / "sandloom_identity" / "migrations"
    ),
    "alembic_version_identity",
)

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


@pytest.mark.parametrize("orphan", [False, True])
def test_restore_check_quotes_catalog_column_identifiers(
    tmp_path: Path, orphan: bool
) -> None:
    def restore(_dump: Path, url: str) -> None:
        _migrate(url)
        url = url.replace("postgresql+psycopg2://", "postgresql://")
        run_sql(
            url,
            'CREATE TABLE "parent odd" ("key"";--" integer PRIMARY KEY); '
            'CREATE TABLE "child odd" ("ref"";--" integer REFERENCES '
            '"parent odd" ("key"";--")); '
            'INSERT INTO "parent odd" VALUES (1); '
            'INSERT INTO "child odd" VALUES (1)',
        )
        if orphan:
            run_sql(
                url,
                "SET session_replication_role = replica; "
                'INSERT INTO "child odd" VALUES (2)',
            )

    report = verify_dump(
        _fake_dump(tmp_path), server_url=_server_url(), chains=CHAINS, restorer=restore
    )
    assert all(c.ok for c in report.checks if c.name != "foreign keys hold"), (
        report.as_dict()
    )
    foreign_keys = [c for c in report.checks if c.name == "foreign keys hold"]
    assert len(foreign_keys) == 1, report.as_dict()
    assert foreign_keys[0].ok is not orphan


def _sync(url: str, db: str) -> str:
    from sandloom.core.testing.migrations import _sync as sync

    return sync(url, db)
