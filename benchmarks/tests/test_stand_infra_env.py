"""Every stand keeps the infrastructure instances it selected on main.

The flat ``CACHE_URL`` / ``BROKER_URL`` settings only reached the instances the
stand's ROOT selected: the standard root (``sandloom_standard.main``) selects a
cache, a streams bus, a security store, a broadcast, a lease and a task queue; a
lean root (``create_app()`` with no composition selection, e.g.
``benchsuite.apps.sandloom_app``) selects none. A stand that names an instance in
``INFRA`` SELECTS it, so a lean-root stand names only what it named on main and a
standard-root stand gives each of the standard instances its OWN url. Each test
pins one stand's selection (a change of it breaks the recorded baselines).
Pure functions: no Redis, no database.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest
import yaml

from benchsuite import common, mix, platform, rpcstand, s5, writes

REDIS = "redis://redis-a:6379/3"
BROKER = "redis://redis-b:6379/4"
FLAT = ("CACHE_URL", "BROKER_URL")
QUALIFICATION = Path(__file__).resolve().parents[1] / "qualification"
#: What the standard root selects (``sandloom_standard.DEFAULT_INFRA`` less the
#: database), by ``capability.instance``.
STANDARD = {
    "cache.default",
    "eventbus.default",
    "atomic_store.security",
    "broadcast.default",
    "lease.beat",
    "taskqueue.default",
}


@pytest.fixture(autouse=True)
def _stand_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCH_REDIS_URL", REDIS)
    monkeypatch.setenv("BENCH_CELERY_BROKER", BROKER)
    monkeypatch.setenv("BENCH_SECURITY_REDIS_URL", "redis://redis-c:6379/5")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")


def _urls(infra: dict[str, dict[str, dict[str, object]]]) -> dict[str, str]:
    """``capability.instance`` -> the url (the broker for the task queue)."""
    out: dict[str, str] = {}
    for capability, named in infra.items():
        for name, options in named.items():
            url = options.get("url") or options.get("broker")
            if url:
                out[f"{capability}.{name}"] = str(url)
    return out


def test_the_standard_set_gives_every_instance_its_own_url() -> None:
    infra = json.loads(common.infra_env(instances=common.STANDARD_REDIS))
    assert _urls(infra) == {
        "cache.default": REDIS,
        "eventbus.default": REDIS,
        "atomic_store.security": REDIS,
        "broadcast.default": REDIS,
        "lease.beat": REDIS,
        "taskqueue.default": BROKER,
    }
    assert infra["taskqueue"]["default"] == {"driver": "celery", "broker": BROKER}
    assert "url" not in infra["taskqueue"]["default"]


def test_the_broker_defaults_to_the_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BENCH_CELERY_BROKER")
    assert common.celery_broker() == REDIS
    monkeypatch.setenv("BENCH_CELERY_BROKER", "")
    assert common.celery_broker() == REDIS


def test_an_unknown_instance_is_refused() -> None:
    with pytest.raises(KeyError):
        common.redis_infra(("cache", "nonsense"))


@pytest.mark.parametrize(
    ("name", "env"),
    [
        ("platform", platform.server_env),
        ("mix", mix.server_env),
        ("mix-full", lambda: mix.server_env(full=True)),
        ("rpcstand", rpcstand.server_env),
    ],
)
def test_a_standard_root_stand_selects_the_standard_instances_as_on_main(
    name: str, env: object
) -> None:
    built = env()  # type: ignore[operator]
    assert not [k for k in FLAT if k in built], name
    infra = json.loads(built["INFRA"])
    assert set(_urls(infra)) == STANDARD, name
    assert infra["taskqueue"]["default"]["broker"] == BROKER, name
    assert infra["cache"]["default"]["url"] == REDIS, name


def test_the_rpc_stand_runs_the_standard_root_and_selects_its_instances() -> None:
    """The provider (``ladder/rpc_stand.py``), the in-process rungs and the
    smoke all import ``sandloom_standard.main``: a lean selection here would
    leave the root's cache, bus and task queue without a url."""
    root = Path(rpcstand.__file__).resolve().parents[1] / "ladder" / "rpc_stand.py"
    assert "sandloom_standard.main:app" in root.read_text()
    assert set(_urls(json.loads(rpcstand.server_env()["INFRA"]))) == STANDARD


def test_the_s5_stand_selects_no_redis_instance_as_on_main() -> None:
    """``benchsuite.apps.sandloom_app`` is a lean root: only the database
    option, no cache, bus or security store (INFRA there would add them)."""
    built = s5.server_env("uow")
    assert not [k for k in FLAT if k in built]
    assert json.loads(built["INFRA"]) == {"database": {"default": {"tenant_rls": True}}}


@pytest.mark.parametrize("stage", ["d0", "d1", "d2"])
def test_the_s5_owner_dsn_diag_stages_select_nothing_as_on_main(stage: str) -> None:
    built = s5.diag_env(stage)
    assert json.loads(built["INFRA"]) == {}
    assert not [k for k in FLAT if k in built]


def test_the_s5_rls_diag_stages_keep_the_s5_selection() -> None:
    assert json.loads(s5.diag_env("d3")["INFRA"]) == {
        "database": {"default": {"tenant_rls": True}}
    }


def test_the_write_stand_selects_no_redis_instance_as_on_main() -> None:
    """``writes.server_env`` is the s5 env (the lean root) plus its own keys."""
    built = writes.server_env()
    assert json.loads(built["INFRA"]) == {"database": {"default": {"tenant_rls": True}}}


def test_the_server_profile_stand_selects_only_the_security_store_as_on_main() -> None:
    from ladder import server_profile

    built = server_profile.server_env("uow")
    assert not [k for k in FLAT if k in built]
    assert json.loads(built["INFRA"]) == {
        "database": {"default": {"tenant_rls": True}},
        "atomic_store": {
            "security": {"driver": "redis", "url": "redis://redis-c:6379/5"}
        },
    }


def test_the_soak_stand_selects_only_its_bus_option_as_on_main() -> None:
    from ladder import soak

    built = soak.server_env("on", pool=5, overflow=5, relay="off")
    assert not [k for k in FLAT if k in built]
    assert json.loads(built["INFRA"]) == {
        "eventbus": {"default": {"max_length": 20000}}
    }


def test_the_write_tail_stand_selects_its_security_and_bus_only_as_on_main() -> None:
    from ladder import write_tail

    infra = json.loads(write_tail._env(2, 5, 5, "pg")["INFRA"])
    assert set(_urls(infra)) == {"atomic_store.security", "eventbus.default"}
    assert infra["database"] == {"default": {"tenant_rls": True}}


def test_the_login_concurrency_stand_selects_the_standard_instances_as_on_main() -> (
    None
):
    from ladder import login_concurrency

    built = login_concurrency._container_env("2", 1)
    assert not [k for k in FLAT if k in built]
    assert set(_urls(json.loads(built["INFRA"]))) == STANDARD


def test_the_saga_stand_selects_the_standard_instances_less_the_memory_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ladder import saga_rung

    # ``_setup_env`` writes os.environ directly: work on a copy
    env = dict(os.environ)
    for key in ("INFRA", *FLAT):
        env.pop(key, None)
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(saga_rung, "REDIS", REDIS)
    saga_rung._setup_env()
    assert set(_urls(json.loads(env["INFRA"]))) == STANDARD - {"eventbus.default"}
    assert not [k for k in FLAT if k in env]


def test_the_boot_measurement_gives_each_root_what_it_selected_on_main() -> None:
    """The candidate (standard root) names the standard instances' urls, the
    base (an older tree, its own root) keeps the flat settings."""
    from boot import m_boot

    base = m_boot._env("base A", Path("/nonexistent"), m_boot.BASE_ROOT)
    assert "INFRA" not in base
    assert base["CACHE_URL"] and base["BROKER_URL"]
    candidate = m_boot._env("candidate", Path("/nonexistent"), m_boot.BASE_ROOT)
    assert not [k for k in FLAT if k in candidate]
    assert set(_urls(json.loads(candidate["INFRA"]))) == STANDARD - {"eventbus.default"}


def test_the_prod_stand_selects_no_redis_instance_as_on_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``benchsuite.run``'s lean-root prod stand selected nothing from Redis
    before the flat settings went (its CACHE_URL reached no instance): INFRA
    here would add a cache, a bus and a security store and break comparability
    with the recorded baselines."""
    # the runner imports psutil (process-tree CPU), a benchmarks-only dependency
    # the root environment lacks; the env builder does not use it
    monkeypatch.setitem(sys.modules, "psutil", types.ModuleType("psutil"))
    monkeypatch.delitem(sys.modules, "benchsuite.run", raising=False)
    from benchsuite import run

    built = run._sandloom_env()
    assert "INFRA" not in built
    assert not [k for k in FLAT if k in built]


def _compose(name: str) -> dict[str, object]:
    return yaml.safe_load((QUALIFICATION / name).read_text())


def test_the_qualification_compose_selects_its_security_store_only_as_on_main() -> None:
    """The container runs ``benchsuite.apps.sandloom_app`` (a lean root): the
    flat CACHE_URL / BROKER_URL it carried reached no instance."""
    compose = _compose("compose.yml")
    env = compose["x-env"]  # type: ignore[index]
    assert not [k for k in FLAT if k in env]
    infra = json.loads(env["INFRA"])
    assert set(infra) == {"database", "atomic_store"}
    assert set(infra["atomic_store"]) == {"security"}
    services = compose["services"]  # type: ignore[index]
    assert services["setup"]["environment"]["INFRA"] == "{}"
    restored = services["restored-app"]["environment"]
    assert not [k for k in FLAT if k in restored]
    assert restored["INFRA"] == env["INFRA"]  # the app's selection, unchanged


def test_the_production_compose_selects_its_bus_and_cache_names_as_on_main() -> None:
    compose = _compose("compose.production.yml")
    env = compose["services"]["app"]["environment"]  # type: ignore[index]
    assert not [k for k in FLAT if k in env]
    infra = json.loads(env["INFRA"])
    assert set(infra) == {"database", "cache", "eventbus"}
    assert set(infra["cache"]) == {"security"}
    assert set(infra["eventbus"]) == {"default"}
