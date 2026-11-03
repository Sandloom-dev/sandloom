"""Test-run slots (``sandloom.core.testing.slots``) and ``.env.test.local``.

Two test runs in one checkout never share a database or Redis indexes: the
second takes slot 2. Real PostgreSQL / Redis: the test server of this run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit, urlunsplit

import pytest

from sandloom.core.cli import run_tests
from sandloom.core.testing import env as test_env
from sandloom.core.testing import ownership, slots, workers

BACKEND = Path(__file__).resolve().parents[5]


def _test_url(var: str) -> str:
    values = workers.worker_env_values(BACKEND / ".env.test")
    return os.environ.get(var) or values.get(var) or ""


def _plain(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="postgresql", path=f"/{name}", query=""))


@pytest.fixture
def checkout(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """A checkout whose ``.env.test.local`` names this server's test database
    ``sandloom_test_slots_<random>`` (its slot copies dropped afterwards)."""
    psycopg2 = pytest.importorskip("psycopg2")
    base = _test_url("DATABASE_URL")
    cache = _test_url("CACHE_URL")
    if not base.startswith("postgresql") or not cache.startswith("redis"):
        pytest.skip("PostgreSQL / Redis test URLs not configured")
    name = f"sandloom_test_slots_{uuid.uuid4().hex[:8]}"
    root = tmp_path / "app"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".env.test").write_text("ENVIRONMENT=test\n")
    redis_server = urlunsplit(urlsplit(cache)._replace(path=""))
    database = urlunsplit(urlsplit(base)._replace(path="/" + name))
    (root / ".env.test.local").write_text(
        f"SANDLOOM_TEST_DATABASE_URL={database}\n"
        f"SANDLOOM_TEST_CACHE_URL={redis_server}/15\n"
        f"SANDLOOM_TEST_BROKER_URL={redis_server}/14\n"
    )
    yield root, name
    admin = psycopg2.connect(_plain(base, "postgres"), connect_timeout=5)
    admin.autocommit = True
    with admin.cursor() as cur:
        for suffix in ("", "_2", "_3"):
            cur.execute(f'DROP DATABASE IF EXISTS "{name}{suffix}" WITH (FORCE)')
    admin.close()


def _clean_env(home: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SANDLOOM_TEST_") and k not in {"CI", "PYTEST_XDIST_WORKER"}
    }
    env["SANDLOOM_HOME"] = str(home)
    return env


_HOLDER = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from sandloom.core.testing import slots
    slot = slots.acquire_test_slot([Path(sys.argv[1])], cwd=Path(sys.argv[1]))
    print(json.dumps({"number": slot.number, "db": slot.database_url,
                      "owner": slot.owner, "redis": slot.redis_urls}), flush=True)
    time.sleep(60)
    """
)


def test_a_second_run_in_one_checkout_gets_slot_two(
    checkout: tuple[Path, str], tmp_path: Path
) -> None:
    root, name = checkout
    env = _clean_env(tmp_path / "home")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root)],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        first = json.loads(holder.stdout.readline())
        assert first["number"] == 1
        assert first["db"].endswith(f"/{name}")
        mine = dict(env)
        slot = slots.acquire_test_slot([root], mine, cwd=root, announce=False)
        assert slot is not None
        try:
            assert slot.number == 2
            assert mine["SANDLOOM_TEST_DATABASE_URL"].endswith(f"/{name}_2")
            assert mine["SANDLOOM_TEST_OWNER"].endswith("#2")
            assert mine["SANDLOOM_TEST_OWNER"] != first["owner"]
            assert mine[slots.SLOT_HELD_ENV] == str(os.getpid())
            assert mine["SANDLOOM_TEST_REDIS_DB_STRIDE"] == "auto"
            for var in ("SANDLOOM_TEST_CACHE_URL", "SANDLOOM_TEST_BROKER_URL"):
                assert mine[var] != first["redis"][var], var
            # Every Redis index used is distinct across the two runs.
            indexes = {
                urlsplit(u).path
                for u in (*first["redis"].values(), *slot.redis_urls.values())
            }
            assert len(indexes) == 4
        finally:
            slot.release()
    finally:
        holder.kill()
        holder.wait()
    # The holder is gone: its slot is free again.
    again = dict(env)
    slot = slots.acquire_test_slot([root], again, cwd=root, announce=False)
    assert slot is not None and slot.number == 1
    slot.release()


@pytest.mark.parametrize(
    "extra",
    [
        {"CI": "true"},
        {"SANDLOOM_TEST_DATABASE_URL": "postgresql+asyncpg://u:p@h/sandloom_test_x"},
        {"SANDLOOM_TEST_OWNER": "me"},
        {"PYTEST_XDIST_WORKER": "gw0"},
        {slots.SLOT_HELD_ENV: str(os.getpid())},
    ],
    ids=["ci", "explicit-url", "explicit-owner", "xdist-worker", "held-by-parent"],
)
def test_no_slot_for_ci_explicit_overrides_or_a_held_slot(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    root = tmp_path / "app"
    root.mkdir()
    (root / ".env.test").write_text("ENVIRONMENT=test\n")
    (root / ".env.test.local").write_text(
        "SANDLOOM_TEST_DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:1/x\n"
    )
    env = {**_clean_env(tmp_path / "home"), **extra}
    before = dict(env)
    assert slots.acquire_test_slot([root], env, announce=False) is None
    assert env == before


def test_no_slot_without_env_test_local(tmp_path: Path) -> None:
    (tmp_path / ".env.test").write_text("ENVIRONMENT=test\n")
    env = _clean_env(tmp_path / "home")
    assert slots.acquire_test_slot([tmp_path], env, announce=False) is None


def test_slot_names() -> None:
    base = "postgresql+asyncpg://u:p@127.0.0.1:5/sandloom_test_abc"
    assert slots.slot_database_url(base, 1) == base
    assert slots.slot_database_url(base, 3).endswith("/sandloom_test_abc_3")
    assert slots.slot_owner("m1", Path("/w/app"), 2) == "m1:/w/app#2"
    key = slots.slot_key("m1", Path("/w/app"))
    assert -(2**31) <= key < 2**31 and key == slots.slot_key("m1", Path("/w/app"))
    assert key != slots.slot_key("m2", Path("/w/app"))


# --------------------------------------------------------------------------- #
# .env.test.local loading
# --------------------------------------------------------------------------- #


def _load(start: Path, **exported: str) -> dict[str, str]:
    with mock.patch.dict(os.environ, clear=False):
        for key in list(os.environ):
            if key.startswith("SANDLOOM_TEST_") or key == "PYTEST_XDIST_WORKER":
                del os.environ[key]
        os.environ.update(exported)
        test_env.load_test_env([start], create_database=False)
        return dict(os.environ)


def test_env_test_local_is_loaded_beside_env_test_without_overriding(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.test").write_text("ENVIRONMENT=test\n")
    (tmp_path / ".env.test.local").write_text(
        "SANDLOOM_TEST_CACHE_URL=redis://127.0.0.1:24004/15\n"
    )
    loaded = _load(tmp_path)
    assert loaded["SANDLOOM_TEST_CACHE_URL"] == "redis://127.0.0.1:24004/15"
    assert loaded["CACHE_URL"] == "redis://127.0.0.1:24004/15"
    exported = _load(tmp_path, SANDLOOM_TEST_CACHE_URL="redis://127.0.0.1:1/3")
    assert exported["SANDLOOM_TEST_CACHE_URL"] == "redis://127.0.0.1:1/3"


def test_a_nested_worktree_never_reads_the_outer_env_test_local(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.test").write_text("ENVIRONMENT=test\n")
    (tmp_path / ".env.test.local").write_text(
        "SANDLOOM_TEST_CACHE_URL=redis://127.0.0.1:24004/15\n"
    )
    inner = tmp_path / ".claude" / "worktrees" / "wt"
    inner.mkdir(parents=True)
    (inner / ".env.test").write_text("ENVIRONMENT=test\n")
    assert "SANDLOOM_TEST_CACHE_URL" not in _load(inner)


def test_sandloom_test_takes_the_slot_before_loading_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env.test").write_text("ENVIRONMENT=test\n")
    calls: list[str] = []
    monkeypatch.setattr(
        run_tests, "acquire_test_slot", lambda starts: calls.append("slot")
    )
    monkeypatch.setattr(
        run_tests, "load_test_env", lambda starts: calls.append("env") or tmp_path
    )
    run_tests.load_project_test_env(tmp_path)
    assert calls == ["slot", "env"]


def test_the_default_owner_carries_the_machine_id(tmp_path: Path) -> None:
    env = {"SANDLOOM_HOME": str(tmp_path / "home")}
    (tmp_path / "wt" / ".git").mkdir(parents=True)
    owner = ownership.current_owner(env, cwd=tmp_path / "wt")
    machine, _, path = owner.partition(":")
    assert len(machine) == 12 and path == str((tmp_path / "wt").resolve())
