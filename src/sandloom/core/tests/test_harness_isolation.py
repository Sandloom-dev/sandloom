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

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sandloom_redis.cache as redis_cache
import sandloom_redis.eventbus.driver as event_bus_driver
from sandloom_celery.taskqueue import CeleryTaskQueueProvider

from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.testing import (
    ownership,
    redis_isolation,
    redis_urls,
    session_checks,
    workers,
)
from sandloom.driverkit import testing_isolation as isolation

pytestmark = pytest.mark.subprocess_probe


@pytest.fixture(autouse=True)
def _default_layout() -> Iterator[None]:
    """The layout is process state the harness sets: each test starts and ends
    on the default one."""
    isolation.set_worker_layout()
    yield
    isolation.set_worker_layout()


REDIS_DRIVERS: tuple[tuple[type[InfraProvider], str], ...] = (
    (redis_cache.RedisCacheProvider, "url"),
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
        f"redis://cache.internal:6379/{3 + isolation.DEFAULT_STRIDE * (number + 1)}"
    )
    assert isolated["pool"] == 5
    assert options[key] == "redis://cache.internal:6379/3"  # the input is not mutated


@pytest.mark.parametrize(("driver", "key"), REDIS_DRIVERS)
def test_redis_driver_hook_matches_the_harness_layout(
    driver: type[InfraProvider], key: str
) -> None:
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
    isolated = isolation.isolated_redis_options(
        options, "gw1", keys=("broker", "backend")
    )
    assert isolated == {
        "broker": "redis://h/33",
        "backend": "redis://h/32",
        "other": "x",
    }


def test_celery_taskqueue_hook_moves_broker_and_backend() -> None:
    options = {"broker": "redis://h:6380/1", "backend": "redis://h:6380/0", "x": 1}
    isolated = CeleryTaskQueueProvider.test_isolation(options, "gw2")
    stride = isolation.DEFAULT_STRIDE * 3
    assert isolated == {
        "broker": f"redis://h:6380/{1 + stride}",
        "backend": f"redis://h:6380/{stride}",
        "x": 1,
    }
    assert options["broker"] == "redis://h:6380/1"  # the input is not mutated


def test_celery_taskqueue_hook_leaves_memory_and_other_workers_alone() -> None:
    memory = {"broker": "memory://", "backend": None}
    assert CeleryTaskQueueProvider.test_isolation(memory, "gw1") == memory
    options = {"broker": "redis://h/1"}
    assert CeleryTaskQueueProvider.test_isolation(options, "master") is options


@pytest.mark.parametrize("worker", ["gw0", "gw3"])
def test_the_harness_moves_broker_url_through_the_celery_hook(worker: str) -> None:
    base = "redis://127.0.0.1:6380/1"
    number = int(worker[2:])
    env = {workers.WORKER_ENV: worker}
    out = workers.rewrite_for_worker({"BROKER_URL": base}, env)
    expected = f"redis://127.0.0.1:6380/{1 + isolation.DEFAULT_STRIDE * (number + 1)}"
    assert out == {"BROKER_URL": expected}
    hooked = CeleryTaskQueueProvider.test_isolation({"broker": base}, worker)
    assert hooked["broker"] == expected


def test_the_broker_url_of_a_declared_taskqueue_driver_goes_through_its_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness asks the driver the instance names, not a hard-wired one."""
    asked: list[tuple[str, str, str]] = []

    def fake(capability: str, name: str, driver: str) -> type[InfraProvider] | None:
        asked.append((capability, name, driver))

        class Moved(InfraProvider):
            capability = "taskqueue"

            @classmethod
            def test_isolation(cls, options: Any, worker_id: str) -> dict[str, Any]:
                return {**options, "broker": "redis://moved/9"}

        return Moved

    monkeypatch.setattr(redis_isolation, "_driver_class", fake)
    infra = json.dumps({"taskqueue": {"default": {"driver": "other"}}})
    out = workers.rewrite_for_worker(
        {"BROKER_URL": "redis://h/1", "INFRA": infra}, {workers.WORKER_ENV: "gw1"}
    )
    assert out["BROKER_URL"] == "redis://moved/9"
    assert asked == [("taskqueue", "default", "other")]


def test_memory_defaults_keep_their_options() -> None:
    """A driver that does not override the hook returns the options as given."""

    class Plain(InfraProvider):
        capability = "cache"

    options = {"url": "memory://"}
    assert Plain.test_isolation(options, "gw4") is options


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


# ---- the harness calls the hook (one layout, named instances) ------------

INFRA = json.dumps(
    {
        "cache": {"sessions": {"driver": "redis", "url": "redis://h:6380/3"}},
        "eventbus": {"fanout": {"driver": "streams", "url": "redis://h:6380/5"}},
        "objectstore": {"exports": {"driver": "local", "path": "./exports"}},
    }
)


def test_a_named_instance_url_is_moved_by_the_harness() -> None:
    values = {"CACHE_URL": "redis://h:6380/0", "INFRA": INFRA}
    env = {workers.WORKER_ENV: "gw1"}
    out = workers.rewrite_for_worker(values, env)
    assert out["CACHE_URL"] == "redis://h:6380/32"
    infra = json.loads(out["INFRA"])
    assert infra["cache"]["sessions"]["url"] == "redis://h:6380/35"
    assert infra["eventbus"]["fanout"]["url"] == "redis://h:6380/37"
    assert infra["objectstore"] == {"exports": {"driver": "local", "path": "./exports"}}


def test_a_named_instance_url_is_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    claimed: list[str] = []
    monkeypatch.setattr(ownership, "claim_redis", claimed.append)
    values = {"CACHE_URL": "redis://h:6380/0", "INFRA": INFRA}
    out = workers.rewrite_for_worker(values, {workers.WORKER_ENV: "gw2"})
    ownership.claim_test_resources({**values, **out})
    assert "redis://h:6380/48" in claimed  # CACHE_URL
    assert "redis://h:6380/51" in claimed  # cache instance "sessions"
    assert "redis://h:6380/53" in claimed  # eventbus instance "fanout"


def test_apply_worker_env_moves_infra_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Applying twice never shifts twice (gw0: 1 -> 17, not 33)."""
    env = {
        workers.WORKER_ENV: "gw0",
        "CACHE_URL": "redis://h/0",
        "INFRA": json.dumps(
            {"cache": {"s": {"driver": "redis", "url": "redis://h/1"}}}
        ),
    }
    monkeypatch.setattr(workers, "_applied", {})
    workers.apply_worker_env(env, create_database=False)
    workers.apply_worker_env(env, create_database=False)
    assert json.loads(env["INFRA"])["cache"]["s"]["url"] == "redis://h/17"
    assert env["CACHE_URL"] == "redis://h/16"


@pytest.mark.parametrize(("driver", "key"), REDIS_DRIVERS)
@pytest.mark.parametrize("stride", ["", "32", "7"])
def test_hook_is_the_harness_under_any_stride(
    driver: type[InfraProvider], key: str, stride: str
) -> None:
    base = "redis://127.0.0.1:6380/1"
    for number in (0, 3):
        env = {workers.WORKER_ENV: f"gw{number}"}
        if stride:
            env[redis_isolation.REDIS_STRIDE_ENV] = stride
        harness = workers.rewrite_for_worker({"CACHE_URL": base}, env)["CACHE_URL"]
        # the harness handed its layout to the drivers before this call
        assert driver.test_isolation({key: base}, f"gw{number}")[key] == harness
    if stride:
        assert harness == f"redis://127.0.0.1:6380/{1 + int(stride) * 4}"


@pytest.mark.parametrize(("driver", "key"), REDIS_DRIVERS)
def test_hook_is_the_harness_under_auto(driver: type[InfraProvider], key: str) -> None:
    base = "redis://127.0.0.1:6380/1"
    for number in (0, 2):
        env = {
            workers.WORKER_ENV: f"gw{number}",
            redis_isolation.REDIS_STRIDE_ENV: "auto",
            redis_isolation.REDIS_MAP_ENV: "1=70,71,72",
        }
        harness = workers.rewrite_for_worker({"CACHE_URL": base}, env)["CACHE_URL"]
        assert harness == f"redis://127.0.0.1:6380/{70 + number}"
        assert driver.test_isolation({key: base}, f"gw{number}")[key] == harness


def _no_redis_client(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.util.find_spec

    def find_spec(name: str, *args: Any) -> Any:
        return None if name.partition(".")[0] == "redis" else real(name, *args)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)


def test_auto_without_a_client_skips_redis_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_redis_client(monkeypatch)
    env = {
        workers.WORKER_ENV: "gw1",
        redis_isolation.REDIS_STRIDE_ENV: "auto",  # no mapping: nothing allocated it
    }
    values = {
        "DATABASE_URL": "sqlite+aiosqlite:///./x.db",
        "CACHE_URL": "redis://h/0",
        "BROKER_URL": "redis://h/1",
        "INFRA": INFRA,
    }
    out = workers.rewrite_for_worker(values, env)
    assert set(out) == {"DATABASE_URL"}
    assert redis_urls.redis_urls(values) == []
    assert session_checks.worker_targets(values, 2)[1]["CACHE_URL"] == "redis://h/0"
    session_checks.allocate_worker_resources(
        values, 2
    )  # no client: nothing to allocate


def test_auto_without_a_redis_url_needs_no_mapping() -> None:
    env = {workers.WORKER_ENV: "gw0", redis_isolation.REDIS_STRIDE_ENV: "auto"}
    out = workers.rewrite_for_worker(
        {"DATABASE_URL": "sqlite+aiosqlite:///./x.db"}, env
    )
    assert set(out) == {"DATABASE_URL"}


# ---- no redis importable -------------------------------------------------

_BLOCKER = '''\
"""Imported by every Python child (PYTHONPATH): the redis client is not installed."""
import importlib.abc
import sys


sys.redis_import_attempts = []


class _NoRedis(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.partition(".")[0] != "redis":
            return None
        frame = sys._getframe(1)
        probe = False  # importlib.util.find_spec("redis") asks, it does not import
        importer = None
        while frame is not None:
            code = frame.f_code
            probe = probe or (
                code.co_name == "find_spec" and "util" in code.co_filename
            )
            if importer is None and "importlib" not in code.co_filename and "frozen" not in code.co_filename:
                importer = f"{code.co_filename}:{frame.f_lineno}"
            frame = frame.f_back
        if not probe:
            sys.redis_import_attempts.append(f"{name} from {importer}")
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
    # no client: this process reaches no Redis, so no URL is rewritten or claimed
    assert os.environ["CACHE_URL"] == "redis://127.0.0.1:1/0"
    assert os.environ["BROKER_URL"] == "redis://127.0.0.1:1/1"


def test_the_second_test_runs_too():
    assert os.environ["CACHE_URL"].startswith("redis://127.0.0.1:1/")


def test_redis_client_never_loaded():
    import importlib
    import sys

    assert "redis" not in sys.modules
    # the modules a memory-only app's run touches, imported with the client
    # refused: none of them may try to import it at module level
    for name in (
        "sandloom.core.testing.resources",
        "sandloom.core.testing.ownership",
        "sandloom.core.testing.slots",
        "sandloom.core.testing.session_checks",
        "sandloom.core.cli.dev.services",
    ):
        importlib.import_module(name)
    assert sys.redis_import_attempts == []


def test_no_redis_module_of_the_harness_loaded():
    import sys

    for name in (
        "sandloom.core.testing.redis_isolation",
        "sandloom_redis.cache",
        "sandloom.core.drivers.bootstrap",
    ):
        assert name not in sys.modules, name
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
    assert "4 passed" in out, out[-1500:]
