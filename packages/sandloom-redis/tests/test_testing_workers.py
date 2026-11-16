"""The Redis half of the pytest-xdist worker isolation (``sandloom_redis.testing``).

Each xdist worker gets its own Redis database indexes beside its own database:
``index + stride * (N + 1)``, or the controller's allocation with
``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``. The urls are the ones ``INFRA`` states
(``cache.default.url``, ``taskqueue.default.broker``): there is no flat url
variable. The database half is tested with no family in
``src/sandloom/core/tests/test_testing_workers.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from sandloom_redis.testing.isolation import (
    REDIS_MAP_ENV,
    REDIS_STRIDE_ENV,
    format_redis_map,
    parse_redis_map,
)

from sandloom.core.testing import families, workers
from sandloom.core.testing.families import HarnessFamily

BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5433/sandloom_test",
    "INFRA": json.dumps(
        {
            "cache": {
                "default": {"driver": "redis", "url": "redis://localhost:6380/15"}
            },
            "taskqueue": {
                "default": {"driver": "celery", "broker": "redis://localhost:6380/14"}
            },
        }
    ),
}


def _cache(values: Mapping[str, str]) -> str:
    return str(json.loads(values["INFRA"])["cache"]["default"]["url"])


def _broker(values: Mapping[str, str]) -> str:
    return str(json.loads(values["INFRA"])["taskqueue"]["default"]["broker"])


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
    monkeypatch.delenv(REDIS_STRIDE_ENV, raising=False)
    monkeypatch.delenv(REDIS_MAP_ENV, raising=False)
    monkeypatch.setattr(workers, "_applied", {})
    monkeypatch.setattr(workers, "ensure_database", created.append)
    yield created
    os.environ.clear()
    os.environ.update(saved)


def test_the_real_redis_family_is_the_one_the_workers_ask() -> None:
    assert "redis" in {f.name for f in families.harness_families()}
    assert all(isinstance(f, HarnessFamily) for f in families.harness_families())


def test_worker_gets_its_own_database_and_redis_dbs(_fresh_state: list[str]) -> None:
    env = {**BASE, "PYTEST_XDIST_WORKER": "gw2"}
    workers.apply_worker_env(env)
    assert env["DATABASE_URL"].endswith("/sandloom_test_gw2")
    assert env["DATABASE_URL"].startswith("postgresql+asyncpg://u:p@localhost:5433/")
    # index + 16 * (N + 1): distinct URLs stay distinct, workers never overlap
    assert _cache(env) == "redis://localhost:6380/63"
    assert _broker(env) == "redis://localhost:6380/62"
    assert _fresh_state == [env["DATABASE_URL"]]
    other = {**BASE, "PYTEST_XDIST_WORKER": "gw0"}
    assert _cache(workers.rewrite_for_worker(other, other)) == (
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
    env = {**BASE, "PYTEST_XDIST_WORKER": "gw0", REDIS_STRIDE_ENV: "2"}
    assert _broker(workers.rewrite_for_worker(env, env)).endswith("/16")
    env[REDIS_STRIDE_ENV] = "junk"
    assert _broker(workers.rewrite_for_worker(env, env)).endswith("/30")


def test_reload_test_env_reapplies_the_worker_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env.test"
    env_file.write_text("".join(f"{k}='{v}'\n" for k, v in BASE.items()))
    for key, value in BASE.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw4")
    # The expected index below assumes the default stride (16); the pre-push
    # hook sets a smaller one for its own run.
    monkeypatch.delenv(REDIS_STRIDE_ENV, raising=False)
    workers.reload_test_env(env_file)
    workers.reload_test_env(env_file)  # a second module re-loading the file
    assert _cache(os.environ) == "redis://localhost:6380/95"
    values = workers.worker_env_values(env_file)
    assert _cache(values) == _cache(os.environ)
    assert _broker(values) == _broker(os.environ)


def test_auto_stride_uses_the_controllers_allocation() -> None:
    """``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``: each worker takes its allocated
    index per base index (equal bases stay equal, distinct ones distinct)."""
    mapping = {14: [40, 41], 15: [7, 99]}
    raw = format_redis_map(mapping)
    assert parse_redis_map(raw) == mapping
    env = {
        **BASE,
        "PYTEST_XDIST_WORKER": "gw1",
        REDIS_STRIDE_ENV: "auto",
        REDIS_MAP_ENV: raw,
    }
    out = workers.rewrite_for_worker(env, env)
    assert _cache(out) == "redis://localhost:6380/99"
    assert _broker(out) == "redis://localhost:6380/41"
    assert out["DATABASE_URL"].endswith("/sandloom_test_gw1")


def test_auto_stride_without_an_allocation_fails_loudly() -> None:
    env = {
        **BASE,
        "PYTEST_XDIST_WORKER": "gw0",
        REDIS_STRIDE_ENV: "auto",
    }
    with pytest.raises(RuntimeError, match="SANDLOOM_TEST_REDIS_WORKER_DBS"):
        workers.rewrite_for_worker(env, env)
    env[REDIS_MAP_ENV] = format_redis_map({15: [7]})
    with pytest.raises(RuntimeError, match="base index 14"):
        workers.rewrite_for_worker(env, env)  # the broker's /14 has none
