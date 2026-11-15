"""Drivers read options, not settings; the kernel fills them (ADR-0016).

The ``redis`` cache, the ``redis`` / ``streams`` event buses and the
``celery`` task queue read no kernel setting: :func:`fill_driver_defaults`
puts what they used to read (``CACHE_URL``, ``BROKER_URL``, the ``CACHE_*``
client bounds) into their instances' options, so behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sandloom_redis.cache as redis_cache
from sandloom_celery import taskqueue as celery_taskqueue
from sandloom_redis.cache import RedisCacheOptions

from sandloom.core.config.settings import settings
from sandloom.core.eventing.volatile_provider import VolatileBusOptions
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InstanceConfig,
    fill_driver_defaults,
    load_infra_config,
)

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


def test_the_default_cache_gets_cache_url_and_every_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cache_max_connections", 7)
    monkeypatch.setattr(settings, "cache_socket_timeout_seconds", 0.25)
    options = load_infra_config(settings)["cache"][DEFAULT_INSTANCE].options
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
        "taskqueue": {
            "jobs": _cfg("taskqueue", "jobs", "celery", broker="redis://b/2"),
        },
    }
    fill_driver_defaults(config, settings)
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
    jobs = config["taskqueue"]["jobs"].options
    assert jobs == {
        "broker": "redis://b/2",
        "backend": settings.cache_url,
        "health_timeout_seconds": settings.health_check_timeout_seconds,
    }
    celery_taskqueue.CeleryTaskQueueOptions.model_validate(jobs)


def test_the_default_taskqueue_gets_broker_backend_and_the_probe_bound() -> None:
    options = load_infra_config(settings)["taskqueue"][DEFAULT_INSTANCE].options
    assert options == {
        "broker": settings.broker_url,
        "backend": settings.cache_url,
        "health_timeout_seconds": settings.health_check_timeout_seconds,
    }


def test_the_probe_bound_follows_the_setting_and_a_lower_stated_one_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 5.0)
    config = {
        "taskqueue": {
            "a": _cfg("taskqueue", "a", "celery"),
            "b": _cfg("taskqueue", "b", "celery", health_timeout_seconds=1.0),
        }
    }
    fill_driver_defaults(config, settings)
    assert config["taskqueue"]["a"].options["health_timeout_seconds"] == 5.0
    assert config["taskqueue"]["b"].options["health_timeout_seconds"] == 1.0


def test_a_stated_probe_bound_never_exceeds_the_readiness_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/ready`` stops waiting at ``HEALTH_CHECK_TIMEOUT_SECONDS``: a connect
    allowed 300 s would only outlive its answered probe (option 300, setting 5:
    the probe is bounded by 5)."""
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 5.0)
    config = {
        "taskqueue": {
            "b": _cfg("taskqueue", "b", "celery", health_timeout_seconds=300),
        }
    }
    fill_driver_defaults(config, settings)
    assert config["taskqueue"]["b"].options["health_timeout_seconds"] == 5.0
    options = config["taskqueue"]["b"].options
    provider = celery_taskqueue.create_celery_taskqueue(
        _cfg("taskqueue", "b", "celery", **{**options, "broker": "redis://b/2"})
    )
    assert provider._typed.health_timeout_seconds == 5.0


@pytest.mark.parametrize("raw", ["0", "-1", "301", "soon"])
def test_a_bad_health_check_timeout_is_refused_naming_the_variable(raw: str) -> None:
    from pydantic import ValidationError

    from sandloom.core.config.settings import Settings

    with pytest.raises(ValidationError, match="HEALTH_CHECK_TIMEOUT_SECONDS"):
        Settings(_env_file=None, HEALTH_CHECK_TIMEOUT_SECONDS=raw)


@pytest.mark.parametrize("bound", [0, -1, 301, "soon"])
def test_a_bad_probe_bound_is_refused_at_build(bound: object) -> None:
    with pytest.raises(ValueError, match="health_timeout_seconds"):
        celery_taskqueue.create_celery_taskqueue(
            _cfg(
                "taskqueue",
                "jobs",
                "celery",
                broker="redis://b/2",
                health_timeout_seconds=bound,
            )
        )


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


def test_a_celery_instance_without_a_broker_is_refused() -> None:
    with pytest.raises(ValueError, match="'broker'"):
        celery_taskqueue.create_celery_taskqueue(_cfg("taskqueue", "jobs", "celery"))
