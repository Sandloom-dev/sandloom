"""Hard ownership of test resources (``sandloom.core.testing.ownership``).

A run never resets a database / Redis index another run marked as its own:
the per-test reset refuses it, and the session refuses to start on it (no test
runs, nothing is truncated). The pre-push hook refuses the shared default
database unless ``SANDLOOM_TEST_DATABASE_URL`` names one explicitly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from sandloom.core.testing import ownership, workers

BACKEND = Path(__file__).resolve().parents[4]


def _test_url(var: str) -> str:
    values = workers.worker_env_values(BACKEND / ".env.test")
    return os.environ.get(var) or values.get(var) or ""


def _plain(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="postgresql", path=f"/{name}", query=""))


@pytest.fixture
def scratch_db() -> Iterator[str]:
    """A fresh database next to the test database (dropped afterwards)."""
    psycopg2 = pytest.importorskip("psycopg2")
    base = _test_url("DATABASE_URL")
    if not base.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    base_name = urlsplit(base).path.lstrip("/")
    name = f"{base_name}_own_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg2.connect(_plain(base, "postgres"), connect_timeout=5)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    try:
        yield urlunsplit(urlsplit(base)._replace(path=f"/{name}"))
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _marker(url: str) -> str | None:
    import psycopg2  # type: ignore[import-untyped]

    conn = psycopg2.connect(_plain(url, urlsplit(url).path.lstrip("/")))
    try:
        with conn.cursor() as cur:
            return ownership.database_owner(cur)
    finally:
        conn.close()


def test_owner_is_the_explicit_id_else_the_checkout_root(tmp_path: Path) -> None:
    assert ownership.current_owner({"SANDLOOM_TEST_OWNER": "agent-7"}) == "agent-7"
    checkout = tmp_path / "wt"
    (checkout / "src").mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: elsewhere\n")  # a worktree
    assert ownership.current_owner({}, cwd=checkout / "src") == str(checkout.resolve())


def test_an_unmarked_database_is_claimed_then_refused_to_others(
    scratch_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    ownership.claim_database(scratch_db, owner="run-a")
    assert _marker(scratch_db) == "run-a"
    ownership.claim_database(scratch_db, owner="run-a")  # its owner: fine
    with pytest.raises(ownership.TestResourceOwnershipError) as refused:
        ownership.claim_database(scratch_db, owner="run-b")
    message = str(refused.value)
    assert "run-a" in message and "SANDLOOM_TEST_DATABASE_URL" in message
    assert ownership.TAKEOVER_ENV in message
    assert _marker(scratch_db) == "run-a"


def test_takeover_is_explicit(scratch_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    ownership.claim_database(scratch_db, owner="run-a")
    monkeypatch.setenv(ownership.TAKEOVER_ENV, "1")
    with pytest.warns(UserWarning, match="taking over"):
        ownership.claim_database(scratch_db, owner="run-b")
    assert _marker(scratch_db) == "run-b"


def test_a_database_the_harness_creates_is_stamped_on_creation(
    scratch_db: str,
) -> None:
    import psycopg2

    name = f"{urlsplit(scratch_db).path.lstrip('/')}_new"
    url = urlunsplit(urlsplit(scratch_db)._replace(path=f"/{name}"))
    try:
        workers.ensure_database(url)
        assert _marker(url) == ownership.current_owner()
    finally:
        admin = psycopg2.connect(_plain(scratch_db, "postgres"))
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


async def test_the_per_test_reset_refuses_a_foreign_database(
    scratch_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.testing.database import open_test_engine

    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    monkeypatch.delenv("SANDLOOM_TEST_DATABASE_URL", raising=False)
    ownership.claim_database(scratch_db, owner="someone-else")
    with pytest.raises(ownership.TestResourceOwnershipError):
        async with open_test_engine(scratch_db):
            pass  # pragma: no cover - refused before the reset


# --------------------------------------------------------------------------- #
# Redis
# --------------------------------------------------------------------------- #


@pytest.fixture
async def own_redis() -> AsyncIterator[Any]:
    redis = pytest.importorskip("redis.asyncio")
    url = _test_url("CACHE_URL")
    if not url.startswith("redis"):
        pytest.skip("Redis CACHE_URL not configured")
    client = redis.from_url(url)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip(f"Redis unreachable: {exc}")
    previous = await client.get(ownership.REDIS_OWNER_KEY)
    try:
        yield client
    finally:
        if previous is None:
            await client.delete(ownership.REDIS_OWNER_KEY)
        else:
            await client.set(ownership.REDIS_OWNER_KEY, previous)
        await client.aclose()


async def test_flush_refuses_another_runs_redis_and_keeps_its_data(
    own_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = own_redis
    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    await client.set(ownership.REDIS_OWNER_KEY, "run-a")
    await client.set("sandloom:test:probe", "kept")
    with pytest.raises(ownership.TestResourceOwnershipError, match="run-a"):
        await ownership.flush_owned_redis(client, owner="run-b")
    assert await client.get("sandloom:test:probe") == b"kept"
    await ownership.flush_owned_redis(client, owner="run-a")
    assert await client.get("sandloom:test:probe") is None
    # The marker survives its owner's flush.
    assert await client.get(ownership.REDIS_OWNER_KEY) == b"run-a"


def test_claim_redis_refuses_another_owner(
    own_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    url = _test_url("CACHE_URL")
    ownership._claimed.clear()
    ownership.claim_redis(url, owner=ownership.current_owner())
    with pytest.raises(
        ownership.TestResourceOwnershipError, match="SANDLOOM_TEST_CACHE"
    ):
        ownership.claim_redis(url, owner="run-b")


# --------------------------------------------------------------------------- #
# The whole session / the pre-push hook
# --------------------------------------------------------------------------- #


def test_a_run_on_a_foreign_database_aborts_before_any_test(
    scratch_db: str, tmp_path: Path
) -> None:
    """End to end: an app suite (the plugin) pointed at another run's
    database stops at session start — no test runs, nothing is truncated."""
    import psycopg2

    plain = _plain(scratch_db, urlsplit(scratch_db).path.lstrip("/"))
    conn = psycopg2.connect(plain)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE precious (v int)")
        cur.execute("INSERT INTO precious VALUES (42)")
    conn.close()
    ownership.claim_database(scratch_db, owner="the-other-agent")

    (tmp_path / ".env.test").write_text(
        f"ENVIRONMENT=test\nDATABASE_URL={scratch_db}\n"
        f"CACHE_URL={_test_url('CACHE_URL')}\nBROKER_URL={_test_url('BROKER_URL')}\n"
        "JWT_SECRET=test_secret_for_the_ownership_probe_only\n"
    )
    (tmp_path / "test_probe.py").write_text(
        textwrap.dedent(
            """
            def test_never_runs():
                raise AssertionError("ran on a foreign database")
            """
        )
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SANDLOOM_TEST_", "PYTEST_XDIST", "PYTEST_"))
    }
    env["SANDLOOM_TEST_OWNER"] = "this-run"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "sandloom.core.testing.plugin",
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-cov",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
    assert "belongs to another test run" in output and "the-other-agent" in output
    assert "ran on a foreign database" not in output
    conn = psycopg2.connect(plain)
    with conn.cursor() as cur:
        cur.execute("SELECT v FROM precious")
        assert cur.fetchall() == [(42,)]
    conn.close()


def test_prepush_hook_refuses_the_shared_default_database() -> None:
    env = {k: v for k, v in os.environ.items() if k != "SANDLOOM_TEST_DATABASE_URL"}
    result = subprocess.run(
        ["bash", str(BACKEND / "tools" / "checks" / "prepush_full_tests.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "SANDLOOM_TEST_DATABASE_URL is not set" in result.stderr
    assert "export SANDLOOM_TEST_DATABASE_URL=" in result.stderr
