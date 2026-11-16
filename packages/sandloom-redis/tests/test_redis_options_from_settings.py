"""The Redis drivers read options, not settings; the kernel fills them (ADR-0016).

The ``redis`` cache, broadcast, atomic store and lease, and the ``redis`` /
``streams`` event buses read no kernel setting: what they used to read
(``CACHE_URL``, the ``CACHE_*`` client bounds) is put into their instances'
options by :func:`fill_driver_defaults`, as each driver's class declares it
(``InfraProvider.settings_fill``), so behaviour is unchanged. The engine, which
names no driver, is ``sandloom.core.tests.test_driver_options_fill`` (it runs
where nothing is installed).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import sandloom_redis.cache as redis_cache
from sandloom_redis.cache import RedisCacheOptions

from sandloom.core.config.settings import Settings, settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.eventing.volatile_provider import VolatileBusOptions
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    FILLABLE_SETTINGS,
    InfraConfig,
    InstanceConfig,
    load_infra_config,
)
from sandloom.core.infra.registry import (
    configured_stream_retention,
    configured_stream_retention_seconds,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tests._driver_fill import fill, registry_for

_BOUNDS = {
    "socket_timeout_seconds": "cache_socket_timeout_seconds",
    "socket_connect_timeout_seconds": "cache_socket_connect_timeout_seconds",
    "health_check_interval_seconds": "cache_health_check_interval_seconds",
    "max_connections": "cache_max_connections",
    "pool_timeout_seconds": "cache_pool_timeout_seconds",
    "retry_attempts": "cache_retry_attempts",
}


def _cfg(capability: str, name: str, driver: str, **options: Any) -> InstanceConfig:
    return InstanceConfig(
        capability=capability, name=name, driver=driver, options=options
    )


def _loaded() -> InfraConfig:
    return fill(load_infra_config(settings))


def test_the_default_cache_gets_cache_url_and_every_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cache_max_connections", 7)
    monkeypatch.setattr(settings, "cache_socket_timeout_seconds", 0.25)
    options = _loaded()["cache"][DEFAULT_INSTANCE].options
    assert options["url"] == settings.cache_url
    for option, setting in _BOUNDS.items():
        assert options[option] == getattr(settings, setting), option
    parsed = RedisCacheOptions.model_validate(options)  # the schema accepts it
    assert parsed.max_connections == 7
    assert parsed.socket_timeout_seconds == 0.25


def test_a_named_instance_gets_what_it_leaves_out_and_keeps_what_it_says() -> None:
    config = {
        "cache": {
            "sessions": _cfg("cache", "sessions", "redis", max_connections=3),
            "other": _cfg("cache", "other", "memory"),
        },
        "eventbus": {
            DEFAULT_INSTANCE: _cfg("eventbus", DEFAULT_INSTANCE, "streams"),
            "side": _cfg("eventbus", "side", "redis"),
            "mem": _cfg("eventbus", "mem", "memory"),
        },
    }
    fill(config)
    sessions = config["cache"]["sessions"].options
    assert sessions["url"] == settings.cache_url
    assert sessions["max_connections"] == 3  # stated: kept
    assert config["cache"]["other"].options == {}  # not a redis cache
    default_bus = config["eventbus"][DEFAULT_INSTANCE].options
    assert default_bus["url"] == settings.cache_url
    side = config["eventbus"]["side"].options
    assert "url" not in side  # a named bus without one shares the default
    assert side["health_check_interval_seconds"] == (
        settings.cache_health_check_interval_seconds
    )
    VolatileBusOptions.model_validate(side)
    assert config["eventbus"]["mem"].options == {}


def test_a_redis_cache_built_from_options_ignores_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the driver connects to is the option, whatever CACHE_URL says."""
    built: list[RedisCacheOptions] = []

    class _Client:
        async def aclose(self) -> None:
            return None

    def _build(options: RedisCacheOptions) -> Any:
        built.append(options)
        return _Client()

    monkeypatch.setattr(redis_cache, "_build_client", _build)
    monkeypatch.setattr(settings, "cache_url", "redis://settings-host:1/0")
    provider = redis_cache.create_redis_cache(
        _cfg(
            "cache",
            "sessions",
            "redis",
            url="redis://option-host:2/3",
            max_connections=5,
        )
    )
    asyncio.run(provider._client_obj())
    assert built[0].url == "redis://option-host:2/3"
    assert built[0].max_connections == 5


def test_the_default_client_follows_the_default_instance_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default cache instance sets the default client's options; the
    default event bus never replaces them, and a change rebuilds the client."""
    built: list[RedisCacheOptions] = []

    class _Client:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    def _build(options: RedisCacheOptions) -> Any:
        built.append(options)
        return _Client()

    monkeypatch.setattr(redis_cache, "_build_client", _build)
    monkeypatch.setattr(redis_cache, "_default_client", None)
    monkeypatch.setattr(redis_cache, "_default_client_origin", None)
    monkeypatch.setattr(redis_cache, "_default_options", None)
    monkeypatch.setattr(redis_cache, "_default_built_from", None)

    async def scenario() -> None:
        redis_cache.create_redis_cache(
            _cfg("cache", DEFAULT_INSTANCE, "redis", url="redis://a/0")
        )
        first = await redis_cache.get_default_client()
        redis_cache.configure_default_client(
            RedisCacheOptions(url="redis://bus/0"), replace=False
        )
        assert await redis_cache.get_default_client() is first
        redis_cache.create_redis_cache(
            _cfg("cache", DEFAULT_INSTANCE, "redis", url="redis://b/0")
        )
        second = await redis_cache.get_default_client()
        assert second is not first and first.closed
        await redis_cache.close_default_client()

    asyncio.run(scenario())
    assert [o.url for o in built] == ["redis://a/0", "redis://b/0"]


def test_an_unconfigured_default_client_says_why() -> None:
    async def build() -> None:
        await redis_cache.get_default_client()

    original = (redis_cache._default_client, redis_cache._default_options)
    redis_cache._default_client, redis_cache._default_options = None, None
    try:
        with pytest.raises(RuntimeError, match="configure_default_client"):
            asyncio.run(build())
    finally:
        redis_cache._default_client, redis_cache._default_options = original


def test_an_explicit_config_is_filled_alike_for_every_driver_incl_the_lease() -> None:
    """An explicit config asking for the fill gets the lease url and the cache
    url alike: one rule for every driver, not a kernel special case."""
    config: InfraConfig = {
        "lease": {DEFAULT_INSTANCE: _cfg("lease", DEFAULT_INSTANCE, "redis")},
        "cache": {DEFAULT_INSTANCE: _cfg("cache", DEFAULT_INSTANCE, "redis")},
    }
    build_infra_registry(
        InMemorySchemaRegistry(),
        settings=settings,
        config=config,
        fill_from_settings=True,
    )
    assert config["lease"][DEFAULT_INSTANCE].options["url"] == settings.cache_url
    assert config["cache"][DEFAULT_INSTANCE].options["url"] == settings.cache_url


def test_an_option_that_follows_reads_the_resolved_default_of_its_capability() -> None:
    def broadcast_url(cache: InstanceConfig | None) -> str | None:
        config: InfraConfig = {"broadcast": {"b": _cfg("broadcast", "b", "redis")}}
        if cache is not None:
            config["cache"] = {DEFAULT_INSTANCE: cache}
        fill(config)
        return config["broadcast"]["b"].options["url"]

    own = _cfg("cache", DEFAULT_INSTANCE, "redis", url="redis://own-host/9")
    assert broadcast_url(own) == "redis://own-host/9"  # INFRA's, not CACHE_URL
    assert broadcast_url(_cfg("cache", DEFAULT_INSTANCE, "redis")) == settings.cache_url
    # a cache of another driver has no url the broadcast could share
    assert (
        broadcast_url(_cfg("cache", DEFAULT_INSTANCE, "memory")) == settings.cache_url
    )
    assert broadcast_url(None) == settings.cache_url


def test_the_atomic_store_url_follows_the_cache_on_a_synthesized_instance_only() -> (
    None
):
    synthesized = _cfg("atomic_store", "security", "redis")
    synthesized.implicit = True
    declared = _cfg("atomic_store", "security2", "redis", max_connections=4)
    config = {
        "cache": {
            DEFAULT_INSTANCE: _cfg(
                "cache", DEFAULT_INSTANCE, "redis", url="redis://c/1"
            )
        },
        "atomic_store": {"security": synthesized, "security2": declared},
    }
    fill(config)
    assert synthesized.options["url"] == "redis://c/1"
    assert "url" not in declared.options  # its url is its own, never the cache's
    assert declared.options["max_connections"] == 4
    assert synthesized.options["max_connections"] == settings.cache_max_connections


def test_a_streams_bus_gets_the_url_on_the_default_instance_only() -> None:
    config = {
        "eventbus": {
            DEFAULT_INSTANCE: _cfg("eventbus", DEFAULT_INSTANCE, "streams"),
            "side": _cfg("eventbus", "side", "streams"),
        }
    }
    fill(config)
    assert config["eventbus"][DEFAULT_INSTANCE].options["url"] == settings.cache_url
    assert "url" not in config["eventbus"]["side"].options
    assert "socket_connect_timeout_seconds" in config["eventbus"]["side"].options


_DECLARING_DRIVERS = [
    ("cache", "redis"),
    ("broadcast", "redis"),
    ("atomic_store", "redis"),
    ("lease", "redis"),
    ("eventbus", "streams"),
    ("eventbus", "redis"),
]


@pytest.mark.parametrize(("capability", "driver"), _DECLARING_DRIVERS)
def test_every_declared_fill_names_settings_that_exist(
    capability: str, driver: str
) -> None:
    config: InfraConfig = {
        capability: {DEFAULT_INSTANCE: _cfg(capability, DEFAULT_INSTANCE, driver)}
    }
    declared = registry_for(config).settings_fill(capability, driver)
    assert declared is not None, (capability, driver)
    named = {
        *declared.urls.values(),
        *declared.bounds.values(),
        *declared.caps.values(),
    }
    assert named, (capability, driver)
    assert named <= set(Settings.model_fields), named - set(Settings.model_fields)
    assert named <= FILLABLE_SETTINGS


def _redis_cache_config() -> InfraConfig:
    return {
        "cache": {
            DEFAULT_INSTANCE: _cfg(
                "cache", DEFAULT_INSTANCE, "redis", url="redis://x/0"
            )
        }
    }


def test_an_explicit_config_is_built_as_given_unless_the_caller_asks() -> None:
    given = build_infra_registry(InMemorySchemaRegistry(), config=_redis_cache_config())
    options = given.instance_config("cache", DEFAULT_INSTANCE)
    assert options is not None and "max_connections" not in options.options
    asked = build_infra_registry(
        InMemorySchemaRegistry(), config=_redis_cache_config(), fill_from_settings=True
    )
    options = asked.instance_config("cache", DEFAULT_INSTANCE)
    assert options is not None
    assert options.options["url"] == "redis://x/0"  # stated: kept
    assert options.options["max_connections"] == settings.cache_max_connections


# -- the retention a deployment's bus is configured with -------------------------


def _bus_settings(backend: str, **options: Any) -> Any:
    infra = {"eventbus": {DEFAULT_INSTANCE: {"driver": backend, **options}}}
    return SimpleNamespace(event_bus_backend=None, infra=infra)


def test_the_retention_is_the_stated_option_else_the_drivers_schema_default() -> None:
    from sandloom.core.config.eventing import DEFAULT_EVENT_STREAM_RETENTION_SECONDS

    assert configured_stream_retention_seconds(_bus_settings("streams")) == (
        DEFAULT_EVENT_STREAM_RETENTION_SECONDS
    )
    assert (
        configured_stream_retention_seconds(
            _bus_settings("streams", retention_seconds=60)
        )
        == 60
    )
    assert (
        configured_stream_retention_seconds(
            _bus_settings("streams", retention_seconds=0)
        )
        == 0
    )


def test_the_redis_bus_has_no_retention_option_so_none_is_configured() -> None:
    assert configured_stream_retention_seconds(_bus_settings("redis")) is None
    assert (
        configured_stream_retention_seconds(_bus_settings("redis", retention_seconds=9))
        == 9  # only what the configuration states: no option refutes it
    )


def test_an_installed_streams_bus_gives_no_reason_to_refuse_a_purge() -> None:
    assert configured_stream_retention(_bus_settings("streams")).reason is None
