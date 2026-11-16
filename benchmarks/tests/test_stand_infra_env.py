"""The stands give Sandloom its Redis through ``INFRA``, one url per instance.

No stand sets the flat ``CACHE_URL`` / ``BROKER_URL`` settings: each Redis
instance its composition selects carries its OWN url (nothing follows another's)
and the Celery task queue its own broker. Pure functions: no Redis, no database.
"""

from __future__ import annotations

import json

import pytest

from benchsuite import common, mix, platform, rpcstand, s5

REDIS = "redis://redis-a:6379/3"
BROKER = "redis://redis-b:6379/4"
FLAT = ("CACHE_URL", "BROKER_URL")


@pytest.fixture(autouse=True)
def _stand_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCH_REDIS_URL", REDIS)
    monkeypatch.setenv("BENCH_CELERY_BROKER", BROKER)
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


def test_the_lean_set_selects_no_task_queue_broadcast_or_lease() -> None:
    infra = json.loads(common.infra_env())
    assert set(_urls(infra)) == {
        "cache.default",
        "eventbus.default",
        "atomic_store.security",
    }


def test_extra_options_merge_over_an_instance_without_dropping_its_url() -> None:
    infra = json.loads(
        common.infra_env(
            {
                "database": {"default": {"tenant_rls": True}},
                "atomic_store": {"security": {"url": "redis://elsewhere:6379/9"}},
            }
        )
    )
    assert infra["database"]["default"] == {"tenant_rls": True}
    assert infra["atomic_store"]["security"] == {
        "driver": "redis",
        "url": "redis://elsewhere:6379/9",
    }
    assert infra["cache"]["default"]["url"] == REDIS


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
        ("rpcstand", rpcstand.server_env),
        ("s5", lambda: s5.server_env("uow")),
        ("s5-diag-d0", lambda: s5.server_env("d0")),
    ],
)
def test_no_stand_names_the_flat_redis_settings(name: str, env: object) -> None:
    built = env()  # type: ignore[operator]
    assert not [k for k in FLAT if k in built], name
    urls = _urls(json.loads(built["INFRA"]))
    assert urls["cache.default"] == REDIS, name
    assert urls["atomic_store.security"] == REDIS, name


def test_the_rls_stand_keeps_tenant_rls_beside_its_urls() -> None:
    infra = json.loads(s5.server_env("uow")["INFRA"])
    assert infra["database"]["default"] == {"tenant_rls": True}
    assert infra["cache"]["default"]["url"] == REDIS
    # the diagnostic stages without RLS carry the urls and no tenant_rls
    assert "database" not in json.loads(s5.server_env("d0")["INFRA"])
