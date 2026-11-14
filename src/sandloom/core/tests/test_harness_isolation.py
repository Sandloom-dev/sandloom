"""The test harness has no Redis in it: isolation goes through the driver hook.

* ``InfraProvider.test_isolation(options, worker_id)`` of the Redis drivers moves
  the database index of the instance's URL; the harness's own rewrite of
  ``CACHE_URL`` / ``BROKER_URL`` (``sandloom.core.testing.redis_isolation``, the
  half that moves with the Redis drivers) gives the same answer;
* an application on memory drivers runs its tests, parallel included, with the
  redis client not importable: a real ``pytest -n 2`` child whose import
  system refuses ``redis`` passes, its workers each on their own URLs, and the
  client was never loaded. (Settings still require ``redis://`` URLs for
  ``CACHE_URL`` / ``BROKER_URL``; the probe's name no server and nothing
  connects to them.)
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.drivers import event_bus_driver, redis_cache
from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.testing import redis_isolation, workers

pytestmark = pytest.mark.subprocess_probe

REDIS_DRIVERS: tuple[tuple[type[InfraProvider], str], ...] = (
    (redis_cache.RedisCacheProvider, "url"),
    (event_bus_driver.RedisEventBusProvider, "url"),
    (event_bus_driver.StreamsEventBusProvider, "url"),
)


@pytest.mark.parametrize(("driver", "key"), REDIS_DRIVERS)
@pytest.mark.parametrize("worker", ["gw0", "gw1", "gw7"])
def test_redis_driver_hook_moves_the_database_index(
    driver: type[InfraProvider], key: str, worker: str
) -> None:
    options = {key: "redis://cache.internal:6379/3", "pool": 5}
    isolated = driver.test_isolation(options, worker)
    number = int(worker[2:])
    assert isolated[key] == (
        f"redis://cache.internal:6379/{3 + redis_isolation.DEFAULT_REDIS_STRIDE * (number + 1)}"
    )
    assert isolated["pool"] == 5
    assert options[key] == "redis://cache.internal:6379/3"  # the input is not mutated


@pytest.mark.parametrize(("driver", "key"), REDIS_DRIVERS)
def test_redis_driver_hook_matches_the_harness_layout(
    driver: type[InfraProvider], key: str
) -> None:
    assert redis_cache.TEST_WORKER_DB_STRIDE == redis_isolation.DEFAULT_REDIS_STRIDE
    base = "redis://127.0.0.1:6380/1"
    for number in (0, 3, 9):
        env = {workers.WORKER_ENV: f"gw{number}"}
        harness = workers.rewrite_for_worker({"CACHE_URL": base}, env)["CACHE_URL"]
        assert driver.test_isolation({key: base}, f"gw{number}")[key] == harness


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"url": None},
        {"url": "memory://"},
        {"url": "redis:///var/run/redis.sock"},  # not a plain index
        {"url": 3},
    ],
)
def test_redis_driver_hook_leaves_other_options_alone(
    options: dict[str, Any],
) -> None:
    assert redis_cache.RedisCacheProvider.test_isolation(options, "gw2") == options


def test_redis_driver_hook_ignores_a_worker_id_that_is_not_gw_n() -> None:
    options = {"url": "redis://h/0"}
    assert redis_cache.RedisCacheProvider.test_isolation(options, "master") is options


def test_hook_keys_name_the_options_holding_a_url() -> None:
    options = {"broker": "redis://h/1", "backend": "redis://h/0", "other": "x"}
    isolated = redis_cache.isolated_redis_options(
        options, "gw1", keys=("broker", "backend")
    )
    assert isolated == {
        "broker": "redis://h/33",
        "backend": "redis://h/32",
        "other": "x",
    }


def test_memory_defaults_keep_their_options() -> None:
    """A driver that does not override the hook returns the options as given."""

    class Plain(InfraProvider):
        capability = "cache"

    options = {"url": "memory://"}
    assert Plain.test_isolation(options, "gw4") is options


def test_driverkit_exposes_the_helper_to_driver_packages() -> None:
    from sandloom.driverkit import testing_isolation

    assert (
        testing_isolation.isolated_redis_options is redis_cache.isolated_redis_options
    )


def test_auto_layout_is_the_harness_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under ``auto`` the controller's mapping decides; the harness rewrites
    ``CACHE_URL`` from it, the hook keeps its fixed layout."""
    env = {
        workers.WORKER_ENV: "gw1",
        redis_isolation.REDIS_STRIDE_ENV: "auto",
        redis_isolation.REDIS_MAP_ENV: "0=40,41,42",
    }
    rewritten = workers.rewrite_for_worker({"CACHE_URL": "redis://h/0"}, env)
    assert rewritten == {"CACHE_URL": "redis://h/41"}


# ---- no redis importable -------------------------------------------------

_BLOCKER = '''\
"""Imported by every Python child (PYTHONPATH): the redis client is not installed."""
import importlib.abc
import sys


class _NoRedis(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.partition(".")[0] != "redis":
            return None
        raise ModuleNotFoundError(f"No module named {name!r} (blocked)", name=name)


sys.meta_path.insert(0, _NoRedis())
'''

_PYPROJECT = """\
[tool.pytest.ini_options]
pythonpath = ["."]
addopts = ["-p", "sandloom.core.testing.plugin", "-p", "no:cacheprovider"]
asyncio_mode = "auto"
sandloom_composition_root = "fakeroot"
"""

#: An application that composes no module: nothing in it needs a cache driver.
_ROOT = """\
class _Core:
    loaded_modules = []

    def get_service(self, name):
        return None


class _State:
    core = _Core()


class _App:
    state = _State()
    dependency_overrides = {}


app = _App()
"""

_ENV_TEST = """\
ENVIRONMENT=test
JWT_SECRET=harness_probe_secret_value_long_enough_0123
DATABASE_URL=sqlite+aiosqlite:///{db}
CACHE_URL=redis://127.0.0.1:1/0
BROKER_URL=redis://127.0.0.1:1/1
"""

_TESTS = """\
import os


def _worker():
    return os.environ.get("PYTEST_XDIST_WORKER", "")


def test_env_is_isolated_per_worker_without_redis():
    worker = _worker()
    assert worker.startswith("gw")
    assert os.environ["DATABASE_URL"].endswith(f"_{worker}.db")
    offset = 16 * (int(worker[2:]) + 1)
    assert os.environ["CACHE_URL"] == f"redis://127.0.0.1:1/{offset}"
    assert os.environ["BROKER_URL"] == f"redis://127.0.0.1:1/{1 + offset}"


def test_the_second_test_runs_too():
    assert os.environ["CACHE_URL"].startswith("redis://127.0.0.1:1/")


def test_redis_client_never_loaded():
    import sys

    assert "redis" not in sys.modules
"""


def test_an_app_with_no_redis_runs_its_tests_in_parallel(tmp_path: Path) -> None:
    app = tmp_path / "app"
    site = tmp_path / "site"
    app.mkdir()
    site.mkdir()
    (site / "sitecustomize.py").write_text(_BLOCKER)
    (app / "pyproject.toml").write_text(_PYPROJECT)
    (app / "fakeroot.py").write_text(_ROOT)
    (app / ".env.test").write_text(_ENV_TEST.format(db=app / "run_test.db"))
    (app / "test_app.py").write_text(textwrap.dedent(_TESTS))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SANDLOOM_", "PYTEST_", "JWT_"))
        and k not in {"ENVIRONMENT", "DATABASE_URL", "CACHE_URL", "BROKER_URL"}
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [str(site), *filter(None, [env.get("PYTHONPATH")])]
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n", "2"],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-3000:]
    assert "3 passed" in out, out[-1500:]
