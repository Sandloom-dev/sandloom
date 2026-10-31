"""pytest-xdist isolation helpers (sandloom.core.testing.workers)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandloom.core.testing import workers

_REAL_ENSURE_DATABASE = workers.ensure_database
BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5433/sandloom_test",
    "CACHE_URL": "redis://localhost:6380/15",
    "BROKER_URL": "redis://localhost:6380/14",
}


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    created: list[str] = []
    # reload_test_env() writes os.environ directly: restore it afterwards, or
    # a fake DATABASE_URL leaks into every later test of this process.
    saved = dict(os.environ)
    for var in workers.OVERRIDABLE_VARS:  # the run's own overrides stay out
        monkeypatch.delenv(workers.OVERRIDE_PREFIX + var, raising=False)
    # Nor the run's own worker-index layout (the pre-push hook runs with
    # SANDLOOM_TEST_REDIS_DB_STRIDE=auto): these tests use fake base indexes.
    monkeypatch.delenv(workers.REDIS_STRIDE_ENV, raising=False)
    monkeypatch.delenv(workers.REDIS_MAP_ENV, raising=False)
    monkeypatch.setattr(workers, "_applied", {})
    monkeypatch.setattr(workers, "ensure_database", created.append)
    yield created
    os.environ.clear()
    os.environ.update(saved)


def test_no_worker_means_no_rewrite() -> None:
    env = dict(BASE)
    assert workers.apply_worker_env(env) == {}
    assert env == BASE
    assert workers.worker_id({}) is None
    assert workers.worker_id({"PYTEST_XDIST_WORKER": "master"}) is None


def test_worker_gets_its_own_database_and_redis_dbs(_fresh_state: list[str]) -> None:
    env = {**BASE, "PYTEST_XDIST_WORKER": "gw2"}
    workers.apply_worker_env(env)
    assert env["DATABASE_URL"].endswith("/sandloom_test_gw2")
    assert env["DATABASE_URL"].startswith("postgresql+asyncpg://u:p@localhost:5433/")
    # index + 16 * (N + 1): distinct URLs stay distinct, workers never overlap
    assert env["CACHE_URL"] == "redis://localhost:6380/63"
    assert env["BROKER_URL"] == "redis://localhost:6380/62"
    assert _fresh_state == [env["DATABASE_URL"]]
    other = {**BASE, "PYTEST_XDIST_WORKER": "gw0"}
    workers.rewrite_for_worker(other, other)
    assert workers.rewrite_for_worker(other, other)["CACHE_URL"] == (
        "redis://localhost:6380/31"
    )


def test_apply_is_idempotent_and_survives_reloads() -> None:
    env = {**BASE, "PYTEST_XDIST_WORKER": "gw1"}
    workers.apply_worker_env(env)
    first = dict(env)
    assert workers.apply_worker_env(env) == {}  # already applied: untouched
    assert env == first
    env.update(BASE)  # a `.env.test` reload puts the base values back
    workers.apply_worker_env(env)
    assert env == first


def test_stride_is_configurable() -> None:
    env = {**BASE, "PYTEST_XDIST_WORKER": "gw0", "SANDLOOM_TEST_REDIS_DB_STRIDE": "2"}
    assert workers.rewrite_for_worker(env, env)["BROKER_URL"].endswith("/16")
    env["SANDLOOM_TEST_REDIS_DB_STRIDE"] = "junk"
    assert workers.rewrite_for_worker(env, env)["BROKER_URL"].endswith("/30")


def test_url_helpers_keep_everything_but_the_name_or_index() -> None:
    url = "postgresql+asyncpg://u:p%40ss@db:5432/app_test?ssl=disable"
    assert workers.worker_database_url(url, "gw3") == (
        "postgresql+asyncpg://u:p%40ss@db:5432/app_test_gw3?ssl=disable"
    )
    assert workers.worker_database_url(
        workers.worker_database_url(url, "gw3"), "gw3"
    ) == workers.worker_database_url(url, "gw3")
    assert workers.worker_redis_url("redis://:pw@h:1", 0, 16) == "redis://:pw@h:1/16"
    assert workers.worker_redis_url("rediss://h:1/3?x=1", 1, 16) == (
        "rediss://h:1/35?x=1"
    )


def test_reload_test_env_reapplies_the_worker_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env.test"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in BASE.items()))
    for key, value in BASE.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw4")
    # The expected index below assumes the default stride (16); the pre-push
    # hook sets a smaller one for its own run.
    monkeypatch.delenv(workers.REDIS_STRIDE_ENV, raising=False)
    workers.reload_test_env(env_file)
    assert os.environ["DATABASE_URL"].endswith("/sandloom_test_gw4")
    workers.reload_test_env(env_file)  # a second module re-loading the file
    assert os.environ["DATABASE_URL"].endswith("/sandloom_test_gw4")
    assert os.environ["CACHE_URL"] == "redis://localhost:6380/95"
    values = workers.worker_env_values(env_file)
    assert values["DATABASE_URL"] == os.environ["DATABASE_URL"]
    assert values["BROKER_URL"] == os.environ["BROKER_URL"]


def test_explicit_overrides_win_over_env_test_on_every_load_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SANDLOOM_TEST_<VAR> beats the file; a bare shell DATABASE_URL still does not."""
    from sandloom.core.testing.fixtures import load_test_env

    env_file = tmp_path / ".env.test"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in BASE.items()))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    own_db = "postgresql+asyncpg://u:p@localhost:5433/sandloom_test_mine"
    own_cache = "redis://localhost:6380/11"
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://dev/dev_db")
    monkeypatch.setenv("CACHE_URL", "redis://dev/0")
    monkeypatch.setenv("BROKER_URL", "redis://dev/1")
    monkeypatch.setenv("SANDLOOM_TEST_DATABASE_URL", own_db)
    monkeypatch.setenv("SANDLOOM_TEST_CACHE_URL", own_cache)

    load_test_env([env_file])
    assert os.environ["DATABASE_URL"] == own_db
    assert os.environ["CACHE_URL"] == own_cache
    # No override for BROKER_URL: the file wins over the stray shell value.
    assert os.environ["BROKER_URL"] == BASE["BROKER_URL"]

    os.environ["DATABASE_URL"] = "postgresql+asyncpg://dev/dev_db"
    workers.reload_test_env(env_file)
    assert os.environ["DATABASE_URL"] == own_db

    values = workers.worker_env_values(env_file)
    assert values["DATABASE_URL"] == own_db
    assert values["CACHE_URL"] == own_cache
    assert values["BROKER_URL"] == BASE["BROKER_URL"]


def test_overrides_are_rewritten_per_xdist_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env.test"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in BASE.items()))
    monkeypatch.setenv(
        "SANDLOOM_TEST_DATABASE_URL", "postgresql+asyncpg://u:p@h:1/mine"
    )
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw2")
    workers.reload_test_env(env_file)
    assert os.environ["DATABASE_URL"] == "postgresql+asyncpg://u:p@h:1/mine_gw2"
    values = workers.worker_env_values(env_file)
    assert values["DATABASE_URL"] == "postgresql+asyncpg://u:p@h:1/mine_gw2"


def test_ensure_database_creates_the_worker_database() -> None:
    """Real PostgreSQL: the worker database exists afterwards, with citext."""
    from sandloom.core.testing.workers import worker_env_values

    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    url = os.environ.get("DATABASE_URL") or str(
        worker_env_values(env_test).get("DATABASE_URL") or ""
    )
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    try:
        import psycopg2  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover
        pytest.skip("psycopg2 unavailable")
    target = workers.worker_database_url(url, "wtest")
    plain = url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        _REAL_ENSURE_DATABASE(target)
    except psycopg2.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    name = target.rsplit("/", 1)[1].split("?", 1)[0]
    try:
        conn = psycopg2.connect(plain.rsplit("/", 1)[0] + f"/{name}")
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'citext'")
            assert cur.fetchone() == (1,)
        conn.close()
        _REAL_ENSURE_DATABASE(target)  # second call: no-op
    finally:
        admin = psycopg2.connect(plain.rsplit("/", 1)[0] + "/postgres")
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


def test_auto_stride_uses_the_controllers_allocation() -> None:
    """``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``: each worker takes its allocated
    index per base index (equal bases stay equal, distinct ones distinct)."""
    mapping = {14: [40, 41], 15: [7, 99]}
    raw = workers.format_redis_map(mapping)
    assert workers.parse_redis_map(raw) == mapping
    env = {
        **BASE,
        "PYTEST_XDIST_WORKER": "gw1",
        workers.REDIS_STRIDE_ENV: "auto",
        workers.REDIS_MAP_ENV: raw,
    }
    out = workers.rewrite_for_worker(env, env)
    assert out["CACHE_URL"] == "redis://localhost:6380/99"
    assert out["BROKER_URL"] == "redis://localhost:6380/41"
    assert out["DATABASE_URL"].endswith("/sandloom_test_gw1")


def test_auto_stride_without_an_allocation_fails_loudly() -> None:
    env = {**BASE, "PYTEST_XDIST_WORKER": "gw0", workers.REDIS_STRIDE_ENV: "auto"}
    with pytest.raises(RuntimeError, match="SANDLOOM_TEST_REDIS_WORKER_DBS"):
        workers.rewrite_for_worker(env, env)
    env[workers.REDIS_MAP_ENV] = workers.format_redis_map({15: [7]})
    with pytest.raises(RuntimeError, match="base index 14"):
        workers.rewrite_for_worker(env, env)  # BROKER_URL's /14 has none


def test_generate_opens_no_database_for_an_explicit_test_url(tmp_path: Path) -> None:
    """A taken-port setup names its test database before the services are up."""
    (tmp_path / ".env.test").write_text("ENVIRONMENT=test\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST")}
    env["SANDLOOM_TEST_DATABASE_URL"] = (
        "postgresql+asyncpg://user:secret@127.0.0.1:1/nothing_listens_here"
    )
    done = subprocess.run(
        [sys.executable, "-c", "import sandloom; sandloom.main()", "generate"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "psycopg2" not in done.stderr, done.stderr
    assert "Connection refused" not in done.stderr, done.stderr
