"""The test harness has no Redis in it: isolation goes through the driver hook.

* ``InfraProvider.test_isolation(options, worker_id)`` of the Redis drivers moves
  the database index of the instance's URL; the harness's own rewrite of the
  urls ``INFRA`` states (``sandloom_redis.testing.isolation``, the Redis half
  of the harness, registered by this package) gives the same answer;
* an application on memory drivers runs its tests, parallel included, with the
  redis client not importable: a real ``pytest -n 2`` child whose import
  system refuses ``redis`` passes, its workers each on their own URLs, and the
  client was never loaded. (Its ``INFRA`` states ``redis://`` urls; the probe's
  name no server and nothing connects to them.)
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
import sandloom_redis.atomic_store as redis_atomic_store
import sandloom_redis.cache as redis_cache
import sandloom_redis.eventbus.driver as event_bus_driver
from sandloom_celery.taskqueue import CeleryTaskQueueProvider
from sandloom_redis.testing import family as redis_family
from sandloom_redis.testing import isolation as redis_isolation
from sandloom_redis.testing import urls as redis_urls

from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.testing import (
    family_seams,
    ownership,
    session_checks,
    workers,
)
from sandloom.driverkit import testing_isolation as isolation

pytestmark = pytest.mark.subprocess_probe


@pytest.fixture(autouse=True)
def _default_layout() -> Iterator[None]:
    """The layout is process state the harness sets: each test starts and ends
    on the default one."""
    redis_isolation.use_layout()
    yield
    isolation.set_url_mover(None)


REDIS_DRIVERS: tuple[tuple[type[InfraProvider], str], ...] = (
    (redis_cache.RedisCacheProvider, "url"),
    (event_bus_driver.StreamsEventBusProvider, "url"),
    # the security store: revocation marks of one worker never reach another's
    (redis_atomic_store.RedisAtomicStoreProvider, "url"),
)


def _harness_url(
    base: str,
    env: dict[str, str],
    *,
    capability: str = "cache",
    driver: str = "redis",
    option: str = "url",
) -> str:
    """What the harness makes of the one url ``INFRA`` states for the default
    instance of ``capability`` (the only place a url is stated)."""
    infra = {capability: {"default": {"driver": driver, option: base}}}
    out = workers.rewrite_for_worker({"INFRA": json.dumps(infra)}, env)
    return str(json.loads(out["INFRA"])[capability]["default"][option])


@pytest.mark.parametrize(("driver", "key"), REDIS_DRIVERS)
@pytest.mark.parametrize("worker", ["gw0", "gw1", "gw7"])
def test_redis_driver_hook_moves_the_database_index(
    driver: type[InfraProvider], key: str, worker: str
) -> None:
    options = {key: "redis://cache.internal:6379/3", "pool": 5}
    isolated = driver.test_isolation(options, worker)
    number = int(worker[2:])
    assert isolated[key] == (
        f"redis://cache.internal:6379/{3 + redis_isolation.DEFAULT_STRIDE * (number + 1)}"
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
        harness = _harness_url(base, env)
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


def test_the_layout_keeps_everything_but_the_index() -> None:
    """Moved from the kernel's ``test_testing_workers.py`` with the URL layout."""
    stride = redis_isolation.DEFAULT_STRIDE
    assert (
        redis_isolation.worker_redis_url("redis://:pw@h:1", 0, stride)
        == "redis://:pw@h:1/16"
    )
    assert (
        redis_isolation.worker_redis_url("rediss://h:1/3?x=1", 1, stride)
        == "rediss://h:1/35?x=1"
    )
    # a socket path is no plain index: it is left as it is
    sock = "redis+unix:///var/run/redis.sock"
    assert redis_isolation.worker_redis_url(sock, 0, stride) == sock


def test_replacement_workers_get_the_spares_then_a_shared_slot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Moved from the kernel's ``test_testing_worker_spares.py``: a worker that
    replaces a crashed one gets an allocated spare, else a shared slot with a
    warning, never a conftest import failure."""
    mapped = redis_isolation.mapped_redis_url
    mapping = {14: [0, 1, 2, 3, 8, 9]}  # 4 workers + 2 spares
    assert mapped("redis://r:6379/14", 2, mapping) == "redis://r:6379/2"
    assert mapped("redis://r:6379/14", 4, mapping) == "redis://r:6379/8"
    assert mapped("redis://r:6379/14", 5, mapping) == "redis://r:6379/9"
    assert mapped("redis://r:6379/14", 7, mapping) == "redis://r:6379/1"
    assert "sharing gw1's index 1" in capsys.readouterr().err
    with pytest.raises(RuntimeError, match="no Redis index allocated"):
        mapped("redis://r:6379/15", 0, mapping)


def test_the_redis_mover_leaves_the_urls_of_other_backends_alone() -> None:
    """A task queue's ``broker`` may be another backend's URL: the mover the
    family installs moves Redis ones only."""
    options = {"broker": "amqp://h/1", "backend": "redis://h/0"}
    isolated = isolation.isolated_url_options(
        options, "gw0", keys=("broker", "backend")
    )
    assert isolated == {"broker": "amqp://h/1", "backend": "redis://h/16"}


def test_hook_keys_name_the_options_holding_a_url() -> None:
    options = {"broker": "redis://h/1", "backend": "redis://h/0", "other": "x"}
    isolated = isolation.isolated_url_options(
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
    stride = redis_isolation.DEFAULT_STRIDE * 3
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
def test_the_harness_moves_the_broker_through_the_celery_hook(worker: str) -> None:
    base = "redis://127.0.0.1:6380/1"
    number = int(worker[2:])
    env = {workers.WORKER_ENV: worker}
    expected = (
        f"redis://127.0.0.1:6380/{1 + redis_isolation.DEFAULT_STRIDE * (number + 1)}"
    )
    moved = _harness_url(
        base, env, capability="taskqueue", driver="celery", option="broker"
    )
    assert moved == expected
    hooked = CeleryTaskQueueProvider.test_isolation({"broker": base}, worker)
    assert hooked["broker"] == expected


def test_the_broker_of_a_declared_taskqueue_driver_goes_through_its_hook(
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

    monkeypatch.setattr(family_seams, "_driver_class", fake)
    infra = json.dumps(
        {"taskqueue": {"default": {"driver": "other", "broker": "redis://h/1"}}}
    )
    out = workers.rewrite_for_worker({"INFRA": infra}, {workers.WORKER_ENV: "gw1"})
    assert json.loads(out["INFRA"])["taskqueue"]["default"]["broker"] == (
        "redis://moved/9"
    )
    assert asked == [("taskqueue", "default", "other")]


def test_memory_defaults_keep_their_options() -> None:
    """A driver that does not override the hook returns the options as given."""

    class Plain(InfraProvider):
        capability = "cache"

    options = {"url": "memory://"}
    assert Plain.test_isolation(options, "gw4") is options


def test_auto_layout_is_the_harness_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under ``auto`` the controller's mapping decides; the harness rewrites
    the cache url from it, the hook keeps its fixed layout."""
    env = {
        workers.WORKER_ENV: "gw1",
        redis_isolation.REDIS_STRIDE_ENV: "auto",
        redis_isolation.REDIS_MAP_ENV: "0=40,41,42",
    }
    assert _harness_url("redis://h/0", env) == "redis://h/41"


# ---- the harness calls the hook (one layout, named instances) ------------

INFRA = json.dumps(
    {
        "cache": {
            "default": {"driver": "redis", "url": "redis://h:6380/0"},
            "sessions": {"driver": "redis", "url": "redis://h:6380/3"},
        },
        "eventbus": {"fanout": {"driver": "streams", "url": "redis://h:6380/5"}},
        "objectstore": {"exports": {"driver": "local", "path": "./exports"}},
    }
)


def test_a_named_instance_url_is_moved_by_the_harness() -> None:
    values = {"INFRA": INFRA}
    env = {workers.WORKER_ENV: "gw1"}
    out = workers.rewrite_for_worker(values, env)
    assert set(out) == {"INFRA"}
    infra = json.loads(out["INFRA"])
    assert infra["cache"]["default"]["url"] == "redis://h:6380/32"
    assert infra["cache"]["sessions"]["url"] == "redis://h:6380/35"
    assert infra["eventbus"]["fanout"]["url"] == "redis://h:6380/37"
    assert infra["objectstore"] == {"exports": {"driver": "local", "path": "./exports"}}


def test_a_named_instance_url_is_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    claimed: list[str] = []
    monkeypatch.setattr(redis_family, "claim_redis", claimed.append)
    values = {"INFRA": INFRA}
    out = workers.rewrite_for_worker(values, {workers.WORKER_ENV: "gw2"})
    ownership.claim_test_resources({**values, **out})
    assert "redis://h:6380/48" in claimed  # cache instance "default"
    assert "redis://h:6380/51" in claimed  # cache instance "sessions"
    assert "redis://h:6380/53" in claimed  # eventbus instance "fanout"


def test_apply_worker_env_moves_infra_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Applying twice never shifts twice (gw0: 1 -> 17, not 33)."""
    env = {
        workers.WORKER_ENV: "gw0",
        "INFRA": json.dumps(
            {
                "cache": {
                    "default": {"driver": "redis", "url": "redis://h/0"},
                    "s": {"driver": "redis", "url": "redis://h/1"},
                }
            }
        ),
    }
    monkeypatch.setattr(workers, "_applied", {})
    workers.apply_worker_env(env, create_database=False)
    workers.apply_worker_env(env, create_database=False)
    cache = json.loads(env["INFRA"])["cache"]
    assert cache["s"]["url"] == "redis://h/17"
    assert cache["default"]["url"] == "redis://h/16"


#: ``INFRA`` of a ``.env.test``: every instance carries its own url, the task
#: queue its ``broker`` and ``backend``.
FLAT_FREE_INFRA = json.dumps(
    {
        "cache": {"default": {"driver": "redis", "url": "redis://h:6380/15"}},
        "lease": {"beat": {"driver": "redis", "url": "redis://h:6380/15"}},
        "taskqueue": {
            "default": {
                "driver": "celery",
                "broker": "redis://h:6380/14",
                "backend": "redis://h:6380/13",
            }
        },
    }
)


def test_infra_alone_moves_every_instance_and_the_celery_broker() -> None:
    """The worker gets its own index everywhere INFRA states a url."""
    out = workers.rewrite_for_worker(
        {"INFRA": FLAT_FREE_INFRA}, {workers.WORKER_ENV: "gw1"}
    )
    assert set(out) == {"INFRA"}  # no flat variable invented
    infra = json.loads(out["INFRA"])
    assert infra["cache"]["default"]["url"] == "redis://h:6380/47"
    assert infra["lease"]["beat"]["url"] == "redis://h:6380/47"
    assert infra["taskqueue"]["default"] == {
        "driver": "celery",
        "broker": "redis://h:6380/46",
        "backend": "redis://h:6380/45",
    }


def test_the_celery_broker_of_infra_is_a_redis_url_the_run_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sorted(redis_urls.infra_redis_urls(FLAT_FREE_INFRA)) == [
        "redis://h:6380/13",
        "redis://h:6380/14",
        "redis://h:6380/15",
        "redis://h:6380/15",
    ]
    claimed: list[str] = []
    monkeypatch.setattr(redis_family, "claim_redis", claimed.append)
    ownership.claim_test_resources({"INFRA": FLAT_FREE_INFRA})
    assert "redis://h:6380/14" in claimed  # taskqueue.default.broker
    # an instance on another scheme is not a Redis resource
    memory = json.dumps(
        {"taskqueue": {"q": {"driver": "celery", "broker": "memory://"}}}
    )
    assert redis_urls.infra_redis_urls(memory) == []


def test_the_override_replaces_the_url_infra_states_and_creates_none() -> None:
    infra = json.dumps(
        {
            "cache": {"default": {"driver": "redis", "url": "redis://h/1"}},
            "taskqueue": {"default": {"driver": "celery", "broker": "redis://h/2"}},
            "eventbus": {"default": {"driver": "streams"}},
        }
    )
    merged = json.loads(
        workers.infra_with_overrides(
            infra, {"CACHE_URL": "redis://h/11", "BROKER_URL": "redis://h/12"}
        )
        or ""
    )
    assert merged["cache"]["default"] == {"driver": "redis", "url": "redis://h/11"}
    assert merged["taskqueue"]["default"] == {
        "driver": "celery",
        "broker": "redis://h/12",
    }
    assert merged["eventbus"]["default"] == {"driver": "streams"}  # no url of its own
    # an instance INFRA only selects has no url to replace: refused, not created
    only_selected = json.dumps({"cache": {"default": {"driver": "redis"}}})
    with pytest.raises(workers.UnmatchedOverrideError):
        workers.infra_with_overrides(only_selected, {"CACHE_URL": "redis://h/11"})


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
        harness = _harness_url(base, env)
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
        harness = _harness_url(base, env)
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
        "INFRA": INFRA,
    }
    out = workers.rewrite_for_worker(values, env)
    assert set(out) == {"DATABASE_URL"}
    assert redis_urls.redis_urls(values) == []
    # no client: the urls INFRA states stay as written, on every worker
    assert session_checks.worker_targets(values, 2)[1]["INFRA"] == INFRA
    session_checks.allocate_worker_resources(
        values, 2
    )  # no client: nothing to allocate


def test_the_driver_of_an_instance_is_infra_then_the_settings_then_the_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        redis_isolation, "default_driver", lambda capability, name: "from-composition"
    )
    infra = {"cache": {"default": {"driver": "declared"}}}
    of = redis_isolation._driver_of
    assert of(infra, "cache", "default", chosen="chosen") == "declared"
    assert of({}, "eventbus", "default", chosen="memory") == "memory"
    assert of({}, "eventbus", "default") == "from-composition"
    monkeypatch.setattr(redis_isolation, "default_driver", lambda *_: None)
    assert of({}, "cache", "default", fallback="redis") == "redis"
    assert of({}, "taskqueue", "default") is None  # nothing selects one: no driver


def test_the_settings_choose_only_the_default_event_bus() -> None:
    chosen = redis_isolation._chosen_driver
    assert chosen({"EVENT_BUS_BACKEND": "Memory "}, "eventbus", "default") == "memory"
    assert chosen({"EVENT_BUS_BACKEND": "auto"}, "eventbus", "default") is None
    assert chosen({}, "eventbus", "default") is None
    assert chosen({"EVENT_BUS_BACKEND": ""}, "eventbus", "default") is None
    assert chosen({"EVENT_BUS_BACKEND": "memory"}, "eventbus", "other") is None
    assert chosen({"EVENT_BUS_BACKEND": "memory"}, "cache", "default") is None


@pytest.mark.parametrize("worker", ["gw0", "gw1", "gw3"])
def test_a_broker_with_no_task_queue_driver_is_not_moved(
    monkeypatch: pytest.MonkeyPatch, worker: str
) -> None:
    """No composition selects a task queue and INFRA names no driver for it: the
    harness cannot ask a driver for the hook, so it moves nothing (the registry
    refuses the instance when the settings load) instead of guessing Celery. A
    non-redis broker is not touched either."""
    monkeypatch.setattr(redis_isolation, "default_driver", lambda *_: None)
    env = {workers.WORKER_ENV: worker}
    declared = json.dumps({"taskqueue": {"default": {"broker": "redis://h/1"}}})
    out = workers.rewrite_for_worker({"INFRA": declared}, env)
    assert json.loads(out["INFRA"]) == json.loads(declared)
    memory = json.dumps(
        {"taskqueue": {"default": {"driver": "celery", "broker": "memory://"}}}
    )
    assert workers.rewrite_for_worker({"INFRA": memory}, env) == {}


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
INFRA='{infra}'
"""

#: A Redis url INFRA states, which a process that cannot import the client
#: never rewrites or claims.
_PROBE_INFRA = json.dumps(
    {"lease": {"beat": {"driver": "redis", "url": "redis://127.0.0.1:1/0"}}}
)

_TESTS = """\
import os


def _worker():
    return os.environ.get("PYTEST_XDIST_WORKER", "")


def test_env_is_isolated_per_worker_without_redis():
    worker = _worker()
    assert worker.startswith("gw")
    assert os.environ["DATABASE_URL"].endswith(f"_{worker}.db")
    # no client: this process reaches no Redis, so no URL is rewritten or claimed
    assert "127.0.0.1:1/0" in os.environ["INFRA"]


def test_the_second_test_runs_too():
    assert "redis://127.0.0.1:1/0" in os.environ["INFRA"]


def test_redis_client_never_loaded():
    import importlib
    import sys

    assert "redis" not in sys.modules
    # the modules a memory-only app's run touches, imported with the client
    # refused: none of them may try to import it at module level
    for name in (
        "sandloom.core.testing.resources",
        "sandloom.core.testing.ownership",
        "sandloom_redis.testing.family",
        "sandloom_redis.testing.resources",
        "sandloom_redis.testing.ownership",
        "sandloom.core.testing.slots",
        "sandloom.core.testing.session_checks",
        "sandloom.core.cli.dev.services",
    ):
        importlib.import_module(name)
    assert sys.redis_import_attempts == []


def test_no_redis_module_of_the_harness_loaded():
    import sys

    for name in (
        "sandloom_redis.testing.isolation",
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
    (app / ".env.test").write_text(
        _ENV_TEST.format(db=app / "run_test.db", infra=_PROBE_INFRA)
    )
    (app / "test_app.py").write_text(textwrap.dedent(_TESTS))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SANDLOOM_", "PYTEST_", "JWT_"))
        and k not in {"ENVIRONMENT", "DATABASE_URL", "INFRA"}
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
