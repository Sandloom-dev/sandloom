"""Multi-database migration helpers (sandloom-migrate).

Proves the migration tooling can address any configured database by logical
name and bootstrap the per-database transactional outbox.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.migrations.migrations import (
    MigrationDependencyError,
    ensure_core_infra_tables,
    list_database_instances,
    resolve_database_sync_url,
)
from sandloom.core.migrations.urls import to_sync_url
from sandloom.core.testing import reload_test_env


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    backend_dir = Path(__file__).resolve().parents[4]
    env_test = backend_dir / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def test_to_sync_url_strips_async_driver() -> None:
    assert (
        to_sync_url("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql+psycopg2://u:p@h:5432/db"
    )


def test_to_sync_url_is_explicit_and_idempotent() -> None:
    # A bare postgresql:// is psycopg2 on SQLAlchemy 2.0 but psycopg 3 on 2.1:
    # migrations always name the driver.
    assert to_sync_url("postgresql://u:p@h/db") == "postgresql+psycopg2://u:p@h/db"
    once = to_sync_url("postgresql+asyncpg://u:p@h/db")
    assert to_sync_url(once) == once
    # Other explicit drivers are the operator's choice.
    assert to_sync_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
    assert to_sync_url("sqlite:///x.db") == "sqlite:///x.db"


def test_to_sync_url_translates_asyncpg_ssl_to_sslmode() -> None:
    # psycopg2 rejects asyncpg's ``ssl=`` parameter.
    assert (
        to_sync_url("postgresql+asyncpg://u@h/db?ssl=require&application_name=x")
        == "postgresql+psycopg2://u@h/db?sslmode=require&application_name=x"
    )
    assert to_sync_url("postgresql+asyncpg://u@h/db?ssl=false").endswith(
        "?sslmode=disable"
    )


def test_lists_default_database() -> None:
    instances = list_database_instances()
    assert "default" in instances
    assert instances["default"].startswith("postgresql+psycopg2://")


def test_lists_named_databases_from_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {
            "database": {
                "analytics": {
                    "driver": "sqlalchemy",
                    "url": "postgresql+asyncpg://u:p@h:5432/analytics",
                }
            }
        },
        raising=False,
    )
    instances = list_database_instances()
    assert set(instances) == {"default", "analytics"}
    assert instances["analytics"] == "postgresql+psycopg2://u:p@h:5432/analytics"


def test_resolve_unknown_database_raises() -> None:
    with pytest.raises(MigrationDependencyError):
        resolve_database_sync_url("nope")


def test_ensure_outbox_dry_run_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    # Dry-run must not touch the database; just resolve + log.
    url = resolve_database_sync_url("default")
    ensure_core_infra_tables(url, dry_run=True)  # must not raise


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not configured")
def test_ensure_outbox_creates_table_idempotently() -> None:
    from sqlalchemy import create_engine, inspect

    url = resolve_database_sync_url("default")
    ensure_core_infra_tables(url, dry_run=False)
    ensure_core_infra_tables(url, dry_run=False)  # idempotent: must not raise

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        # All THREE shared core infra tables must be ensured, not just outbox —
        # a database that publishes events also needs audit + consumer dedupe.
        assert inspector.has_table("outbox_messages")
        assert inspector.has_table("audit_log")
        assert inspector.has_table("consumer_inbox")
    finally:
        engine.dispose()
