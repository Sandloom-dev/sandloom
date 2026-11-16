"""The Redis drivers read options, not settings; the kernel fills them (ADR-0016).

The ``redis`` cache, broadcast, atomic store and lease, and the ``redis`` /
``streams`` event buses read no kernel setting, and the kernel fills none of
their options: every url and client bound is an option of the instance in
``INFRA`` (``CACHE_URL`` and the ``CACHE_*`` bounds are removed settings, see
``sandloom.core.tests.test_config_tiers``). The engine, which names no driver,
is ``sandloom.core.tests.test_driver_options_fill`` (it runs where nothing is
installed).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import sandloom_redis.cache as redis_cache
from sandloom_redis.cache import RedisCacheOptions

from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InfraConfig,
    InstanceConfig,
)
from sandloom.core.infra.registry import (
    configured_stream_retention,
    configured_stream_retention_seconds,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tests._driver_fill import fill, registry_for


def _cfg(capability: str, name: str, driver: str, **options: Any) -> InstanceConfig:
    return InstanceConfig(
        capability=capability, name=name, driver=driver, options=options
    )


_DECLARING_DRIVERS = [
    ("cache", "redis"),
    ("broadcast", "redis"),
    ("atomic_store", "redis"),
    ("lease", "redis"),
    ("eventbus", "streams"),
    ("eventbus", "redis"),
]


@pytest.mark.parametrize(("capability", "driver"), _DECLARING_DRIVERS)
def test_a_redis_driver_declares_no_settings_fill(capability: str, driver: str) -> None:
    """None of them takes an option from a flat setting: no ``settings_fill``."""
    config: InfraConfig = {
        capability: {DEFAULT_INSTANCE: _cfg(capability, DEFAULT_INSTANCE, driver)}
    }
    assert registry_for(config).settings_fill(capability, driver) is None


@pytest.mark.parametrize(("capability", "driver"), _DECLARING_DRIVERS)
def test_the_kernel_fills_no_option_of_a_redis_instance(
    capability: str, driver: str
) -> None:
    """A url or a bound an instance leaves out stays out (its own refusal, not a
    value from another instance or a setting); what it states stays as stated."""
    bare = _cfg(capability, DEFAULT_INSTANCE, driver)
    stated = _cfg(
        capability, "side", driver, url="redis://own-host/9", max_connections=4
    )
    config: InfraConfig = {capability: {DEFAULT_INSTANCE: bare, "side": stated}}
    fill(config)
    assert bare.options == {}
    assert stated.options == {"url": "redis://own-host/9", "max_connections": 4}


def test_a_redis_instance_without_a_url_refuses_naming_its_infra_option() -> None:
    from sandloom_redis._settings_fill import MissingUrlError, require_url

    with pytest.raises(MissingUrlError, match=r"cache\.sessions\.url") as raised:
        require_url("cache", "sessions", None)
    assert raised.value.fatal_at_startup is True


def test_a_redis_cache_built_from_options_ignores_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the driver connects to is the option, whatever the settings say."""
    built: list[RedisCacheOptions] = []

    class _Client:
        async def aclose(self) -> None:
            return None

    def _build(options: RedisCacheOptions) -> Any:
        built.append(options)
        return _Client()

    monkeypatch.setattr(redis_cache, "_build_client", _build)
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


def test_an_explicit_config_is_built_as_given_with_or_without_the_fill() -> None:
    """The registry fills nothing into a Redis instance, asked or not: the options
    it was built with are the ones it keeps."""
    for ask in (False, True):
        config: InfraConfig = {
            "cache": {
                DEFAULT_INSTANCE: _cfg(
                    "cache", DEFAULT_INSTANCE, "redis", url="redis://x/0"
                )
            }
        }
        built = build_infra_registry(
            InMemorySchemaRegistry(), config=config, fill_from_settings=ask
        )
        options = built.instance_config("cache", DEFAULT_INSTANCE)
        assert options is not None
        assert options.options == {"url": "redis://x/0"}, ask


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
