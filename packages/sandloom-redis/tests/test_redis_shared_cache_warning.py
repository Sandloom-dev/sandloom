"""A persistent Redis instance on the database of ``cache.default`` warns at startup.

No instance inherits another's url any more (``SettingsFill.follows`` is gone),
so an ``atomic_store``, a ``lease`` or a ``streams`` bus on the cache's Redis
database is a choice or a copied url: ``sandloom_redis.shared_cache`` logs one
WARNING naming both and the endpoint, never a credential, and refuses nothing.

Lane: ``redis-drivers`` (a package test; the provider rows run on the test Redis
``SANDLOOM_TEST_CACHE_URL``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import pytest
import sandloom_redis.cache as redis_cache
import sandloom_redis.shared_cache as shared_cache
from _redis_required import require_redis
from sandloom_redis.shared_cache import (
    endpoint,
    register_default_cache,
    warn_if_shares_default_cache,
)

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InfraConfig, InstanceConfig
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.schema.registry import InMemorySchemaRegistry

_LOGGER = "sandloom_redis.shared_cache"
_SECRET = "s3cret-pw"


@pytest.fixture(autouse=True)
def _clean_rendezvous(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(shared_cache, "_default_cache", None)
    monkeypatch.setattr(shared_cache, "_persistent", {})
    monkeypatch.setattr(redis_cache, "_default_options", None)
    yield


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _LOGGER and record.levelno == logging.WARNING
    ]


# -- the endpoint comparison -----------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://Host:6380/3", "host:6380/3"),
        ("redis://host", "host:6379/0"),
        ("redis://host:6379/", "host:6379/0"),
        ("redis://host/?db=4", "host:6379/4"),
        ("redis://:pw@host:6379/1", "host:6379/1"),
        ("rediss://user:pw@host:6379/1?ssl_cert_reqs=none", "host:6379/1"),
        ("memory://", None),
        ("amqp://host:5672//", None),
        ("redis:///0", None),
        ("redis://host:notaport/0", None),
        ("", None),
        (None, None),
    ],
)
def test_the_endpoint_is_host_port_db_without_credentials(
    url: str | None, expected: str | None
) -> None:
    assert endpoint(url) == expected


# -- the warning -----------------------------------------------------------------


def test_the_same_database_warns_once_naming_both_without_the_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        register_default_cache(f"redis://:{_SECRET}@Cache-Host:6379/2")
        warn_if_shares_default_cache(
            "atomic_store", "security", f"redis://:other-{_SECRET}@cache-host/2"
        )
    [message] = _warnings(caplog)
    assert "atomic_store" in message
    assert "'security'" in message
    assert "cache.default" in message
    assert "cache-host:6379/2" in message
    assert _SECRET not in message
    assert _SECRET not in caplog.text


@pytest.mark.parametrize(
    ("default_url", "url"),
    [
        ("redis://cache-host:6379/2", "redis://cache-host:6379/3"),  # other db
        ("redis://cache-host:6379/2", "redis://cache-host:6380/2"),  # other port
        ("redis://cache-host:6379/2", "redis://other-host:6379/2"),  # other host
        (None, "redis://cache-host:6379/2"),  # the default cache is not a Redis one
        ("redis://cache-host:6379/2", "memory://"),
        ("redis://cache-host:6379/2", None),
    ],
)
def test_another_endpoint_or_no_redis_default_cache_does_not_warn(
    caplog: pytest.LogCaptureFixture, default_url: str | None, url: str | None
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        register_default_cache(default_url)
        warn_if_shares_default_cache("lease", "default", url)
    assert _warnings(caplog) == []


@pytest.mark.parametrize("cache_first", [True, False])
def test_the_warning_does_not_depend_on_which_instance_comes_first(
    caplog: pytest.LogCaptureFixture, cache_first: bool
) -> None:
    """Instances start in the order of the configuration, not a fixed one: the
    second to arrive finds the first and warns, exactly once."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        if cache_first:
            register_default_cache("redis://cache-host:6379/2")
        warn_if_shares_default_cache("atomic_store", "security", "redis://cache-host/2")
        if not cache_first:
            assert _warnings(caplog) == []
            register_default_cache("redis://cache-host:6379/2")
    assert len(_warnings(caplog)) == 1


def test_a_default_cache_built_again_on_the_same_url_does_not_warn_twice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        warn_if_shares_default_cache("lease", "default", "redis://cache-host/2")
        register_default_cache("redis://cache-host/2")
        register_default_cache("redis://cache-host/2")
    assert len(_warnings(caplog)) == 1


def test_only_the_default_cache_instance_sets_the_url_the_warning_compares(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default event bus's ``configure_default_client`` and a named cache
    never do: only a ``cache.default`` built from the redis driver."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        warn_if_shares_default_cache("lease", "default", "redis://c-host:6379/5")
        redis_cache.configure_default_client(
            redis_cache.RedisCacheOptions(url="redis://c-host:6379/5"), replace=False
        )
        redis_cache.create_redis_cache(
            InstanceConfig("cache", "named", "redis", {"url": "redis://c-host:6379/5"})
        )
        assert _warnings(caplog) == []
        redis_cache.create_redis_cache(
            InstanceConfig(
                "cache", "default", "redis", {"url": "redis://c-host:6379/5"}
            )
        )
    assert len(_warnings(caplog)) == 1


# -- through the drivers' aopen, as an application starts them --------------------


def _with_db(url: str, db: int) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{db}"))


def _registry(shared_db: int, other_db: int) -> InfraRegistry:
    """``cache.default`` on ``shared_db``; instances of each persistent
    capability on it (``same``) and on ``other_db`` (``apart``)."""
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
    ("lease", "same"),
    ("eventbus", "default"),
    ("eventbus", "apart"),
]


async def test_the_redis_drivers_warn_on_the_cache_database_and_only_there(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await require_redis(settings.cache_url)
    registry = _registry(shared_db=201, other_db=202)
    try:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            for capability, name in _OPENED:
                await registry.provider(capability, name).aopen()
            # the cache is built after them here, as a configuration may order it
            registry.provider("cache", "default")
    finally:
        await registry.aclose_all()
    warned = [
        (capability, name)
        for capability, name in _OPENED
        if any(f"{capability} instance '{name}'" in m for m in _warnings(caplog))
    ]
    assert warned == [
        ("atomic_store", "same"),
        ("lease", "same"),
        ("eventbus", "default"),
    ]
    assert len(_warnings(caplog)) == 3  # once per instance
