"""``sandloom migrate upgrade-all --create-databases`` provisions a missing database.

A service added to a running microservices stack gets a database name in
``POSTGRES_MULTIPLE_DATABASES``, but ``init-databases.sh`` runs only on an
EMPTY Postgres volume. Its ``migrate-<service>`` one-shot therefore creates
its own database when missing (idempotent), so following the "add a module
later" guide on an existing volume works.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, make_url, pool, text

import sandloom.core.cli.migrate as migrate_cli
import sandloom.core.migrations.migrations as migrations
from sandloom.core.migrations.migrations import ensure_database
from sandloom.core.migrations.urls import to_sync_url
from sandloom.core.testing import reload_test_env


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _server_url() -> str:
    raw = os.environ.get("SANDLOOM_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw or not raw.startswith("postgresql"):
        pytest.skip("needs the test PostgreSQL server")
    return to_sync_url(raw)


def _admin(url: str) -> object:
    return create_engine(
        make_url(url).set(database="postgres"),
        poolclass=pool.NullPool,
        isolation_level="AUTOCOMMIT",
    )


def test_a_missing_database_is_created_once_with_its_extensions() -> None:
    server = _server_url()
    name = f"sandloom_ensure_{secrets.token_hex(4)}"
    target = make_url(server).set(database=name).render_as_string(hide_password=False)
    admin = _admin(server)
    try:
        assert ensure_database(target) is True
        assert ensure_database(target) is False  # idempotent: exists now
        engine = create_engine(target, poolclass=pool.NullPool)
        try:
            with engine.connect() as conn:
                extensions = set(
                    conn.execute(text("SELECT extname FROM pg_extension")).scalars()
                )
        finally:
            engine.dispose()
        assert {"citext", "pg_trgm"} <= extensions
    finally:
        with admin.connect() as conn:  # type: ignore[attr-defined]
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()  # type: ignore[attr-defined]


def _extensions(url: str) -> set[str]:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            return set(conn.execute(text("SELECT extname FROM pg_extension")).scalars())
    finally:
        engine.dispose()


def test_a_concurrent_caller_never_sees_an_uninitialized_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 (C-DEPLOY-09): creator A pauses after CREATE DATABASE, before
    the extensions. B's ensure_database must not return (and B start the
    tenancy migration that needs CITEXT) until the database is complete:
    provisioning is serialized per target name, and whoever returns has the
    extensions, whether it created the database or not."""
    server = _server_url()
    name = f"sandloom_ensure_{secrets.token_hex(4)}"
    target = make_url(server).set(database=name).render_as_string(hide_password=False)
    admin = _admin(server)
    paused, resume = threading.Event(), threading.Event()
    real_info = migrations.logger.info

    def info(message: str, *args: object, **kwargs: object) -> None:
        # the creator logs this between CREATE DATABASE and the extensions
        real_info(message, *args, **kwargs)
        if message.startswith("[migrations] created database") and not paused.is_set():
            paused.set()
            resume.wait(10)

    monkeypatch.setattr(migrations.logger, "info", info)
    seen: dict[str, tuple[bool, set[str]]] = {}

    def ensure(label: str) -> None:
        created = ensure_database(target)
        seen[label] = (created, _extensions(target))

    a = threading.Thread(target=ensure, args=("a",))
    b = threading.Thread(target=ensure, args=("b",))
    try:
        a.start()
        assert paused.wait(10), "creator A never reached CREATE DATABASE"
        b.start()
        for _ in range(200):
            if "b" in seen or _waits_on_advisory(admin):
                break
            time.sleep(0.02)
        resume.set()
        a.join(10)
        b.join(10)
        assert seen["a"][0] is True and seen["b"][0] is False
        for created, extensions in seen.values():
            assert {"citext", "pg_trgm"} <= extensions, (created, extensions)
    finally:
        resume.set()
        with admin.connect() as conn:  # type: ignore[attr-defined]
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()  # type: ignore[attr-defined]


def _waits_on_advisory(admin: Any) -> bool:
    with admin.connect() as conn:
        waiting = conn.execute(
            text(
                "SELECT count(*) FROM pg_locks l JOIN pg_database d "
                "ON d.oid = l.database WHERE l.locktype = 'advisory' "
                "AND NOT l.granted AND d.datname = 'postgres'"
            )
        )
        return int(waiting.scalar_one()) > 0


def test_an_existing_database_and_a_non_postgres_url_are_left_alone() -> None:
    assert ensure_database(_server_url()) is False
    assert ensure_database("sqlite:///nowhere.db") is False


def test_upgrade_all_takes_the_create_databases_flag() -> None:
    args = migrate_cli._build_parser().parse_args(
        ["upgrade-all", "--apply", "--create-databases"]
    )
    assert args.create_databases is True
    plain = migrate_cli._build_parser().parse_args(["upgrade-all", "--apply"])
    assert plain.create_databases is False
