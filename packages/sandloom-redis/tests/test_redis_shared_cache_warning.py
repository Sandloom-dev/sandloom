"""A persistent Redis instance on the database of ``cache.default`` warns at startup.

No instance inherits another's url any more (``SettingsFill.follows`` is gone),
so an ``atomic_store``, a ``lease`` or a ``streams`` bus on the cache's Redis
database is a choice or a copied url. The drivers register their endpoints in
``sandloom.driverkit.shared_endpoints`` (the rendezvous itself is tested in
``sandloom/core/tests/test_driverkit_shared_endpoints.py``); the log line names
both instances and the endpoint, never a credential, and nothing is refused. An
instance the kernel synthesized (``implicit``) is logged at INFO, one declared
in ``INFRA`` at WARNING.

Lane: ``redis-drivers`` (a package test; the provider rows run on the test Redis
``SANDLOOM_TEST_CACHE_URL``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import pytest
import sandloom_redis.cache as redis_cache
from _redis_required import require_redis

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InfraConfig, InstanceConfig
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit.shared_endpoints import register, reset

_LOGGER = "sandloom.driverkit.shared_endpoints"


@pytest.fixture(autouse=True)
def _clean_rendezvous(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset()
    monkeypatch.setattr(redis_cache, "_default_options", None)
    yield
    reset()


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == level
    ]


def _with_db(url: str, db: int) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{db}"))


def test_only_the_default_cache_instance_registers_its_endpoint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default event bus's ``configure_default_client`` and a named cache
    never do: only a ``cache.default`` built from the redis driver."""
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        register("lease", "default", "c-host:6379/5", "persistent")
        redis_cache.configure_default_client(
            redis_cache.RedisCacheOptions(url="redis://c-host:6379/5"), replace=False
        )
        redis_cache.create_redis_cache(
            InstanceConfig("cache", "named", "redis", {"url": "redis://c-host:6379/5"})
        )
        assert _records(caplog, logging.WARNING) == []
        redis_cache.create_redis_cache(
            InstanceConfig(
                "cache", "default", "redis", {"url": "redis://c-host:6379/5"}
            )
        )
    assert len(_records(caplog, logging.WARNING)) == 1


# -- through the drivers' aopen, as an application starts them --------------------


def _registry(shared_db: int, other_db: int) -> InfraRegistry:
    """``cache.default`` on ``shared_db``; instances of each persistent
    capability on it (``same``) and on ``other_db`` (``apart``); ``dev``, a
    synthesized ``atomic_store`` on the shared database."""
    base = settings.cache_url
    config: InfraConfig = {
        "cache": {
            "default": InstanceConfig(
                "cache", "default", "redis", {"url": _with_db(base, shared_db)}
            )
        },
        "atomic_store": {
            "same": InstanceConfig(
                "atomic_store",
                "same",
                "redis",
                {"url": _with_db(base, shared_db), "never_evicts_check": "assume"},
            ),
            "apart": InstanceConfig(
                "atomic_store",
                "apart",
                "redis",
                {"url": _with_db(base, other_db), "never_evicts_check": "assume"},
            ),
            "dev": InstanceConfig(
                "atomic_store",
                "dev",
                "redis",
                {"url": _with_db(base, shared_db), "never_evicts_check": "assume"},
                implicit=True,
            ),
        },
        "lease": {
            "same": InstanceConfig(
                "lease", "same", "redis", {"url": _with_db(base, shared_db)}
            ),
        },
        "eventbus": {
            "default": InstanceConfig(
                "eventbus", "default", "streams", {"url": _with_db(base, shared_db)}
            ),
            "apart": InstanceConfig(
                "eventbus", "apart", "streams", {"url": _with_db(base, other_db)}
            ),
        },
    }
    return build_infra_registry(
        InMemorySchemaRegistry(), settings=settings, config=config
    )


_OPENED = [
    ("atomic_store", "same"),
    ("atomic_store", "apart"),
    ("atomic_store", "dev"),
    ("lease", "same"),
    ("eventbus", "default"),
    ("eventbus", "apart"),
]


def _named(messages: list[str]) -> list[tuple[str, str]]:
    return [
        (capability, name)
        for capability, name in _OPENED
        if any(m.startswith(f"{capability} instance '{name}'") for m in messages)
    ]


async def test_the_redis_drivers_warn_on_the_cache_database_and_only_there(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await require_redis(settings.cache_url)
    registry = _registry(shared_db=201, other_db=202)
    try:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            for capability, name in _OPENED:
                await registry.provider(capability, name).aopen()
            # the cache is built after them here, as a configuration may order it
            registry.provider("cache", "default")
    finally:
        await registry.aclose_all()
    warnings = _records(caplog, logging.WARNING)
    assert _named(warnings) == [
        ("atomic_store", "same"),
        ("lease", "same"),
        ("eventbus", "default"),
    ]
    assert len(warnings) == 3  # once per instance
    # the instance the kernel synthesized shares the database too, at INFO only
    assert _named(_records(caplog, logging.INFO)) == [("atomic_store", "dev")]


async def test_a_second_app_in_the_process_does_not_hear_of_the_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await require_redis(settings.cache_url)
    first = _registry(shared_db=203, other_db=204)
    try:
        for capability, name in _OPENED:
            await first.provider(capability, name).aopen()
    finally:
        await first.aclose_all()
    second = _registry(shared_db=203, other_db=204)
    try:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            second.provider("cache", "default")  # no persistent instance opened
    finally:
        await second.aclose_all()
    assert _records(caplog, logging.WARNING) == []
    assert _records(caplog, logging.INFO) == []
