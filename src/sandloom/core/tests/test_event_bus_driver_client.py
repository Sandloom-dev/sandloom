"""The event bus's Redis client is bounded (R1).

The default eventbus instance used to get a synthesized ``url=CACHE_URL``,
which made the driver build its own ``redis.from_url`` client: no connect
timeout, no keepalive, no read timeout. After a Redis failover a
``XREADGROUP BLOCK`` on a half-open socket then hung forever while ``/ready``
stayed green. The default instance now shares the driver-owned default
transport client (on the ``url`` the kernel fills in, since the driver reads
no setting), and every bus client is bounded: connect timeout, TCP
keepalive, health checks and — for the Streams bus — a read timeout longer
than its ``BLOCK`` window.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers import redis_cache
from sandloom.core.drivers.bootstrap import volatile_event_bus_allowed
from sandloom.core.drivers.event_bus_driver import make_streams_driver
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.eventing.volatile_provider import (
    VolatileBusOptions,
    make_volatile_bus_drivers,
)
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InstanceConfig,
    load_infra_config,
)
from sandloom.core.infra.errors import Unsupported
from sandloom.core.schema.registry import InMemorySchemaRegistry

_URL = "redis://127.0.0.1:6399/0"  # never connected: clients connect lazily


def test_default_streams_bus_gets_cache_url_and_the_client_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kernel fills the options the driver reads (it reads no setting);
    the default instance still shares the default transport client (below)."""
    monkeypatch.setattr(settings, "event_bus_backend", "streams")
    options = load_infra_config(settings)["eventbus"][DEFAULT_INSTANCE].options
    assert options == {
        "url": settings.cache_url,
        "socket_connect_timeout_seconds": settings.cache_socket_connect_timeout_seconds,
        "health_check_interval_seconds": settings.cache_health_check_interval_seconds,
    }


@pytest.fixture
def fresh_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redis_cache, "_default_client", None)
    monkeypatch.setattr(redis_cache, "_default_client_origin", None)
    monkeypatch.setattr(redis_cache, "_default_transport", {})
    monkeypatch.setattr(redis_cache, "_default_built_from", None)
    monkeypatch.setattr(
        redis_cache, "_default_options", redis_cache.RedisCacheOptions(url=_URL)
    )


async def _streams_client(cfg: InstanceConfig) -> tuple[Any, Any]:
    provider = make_streams_driver()(cfg)
    return provider, await provider._client()  # type: ignore[attr-defined]


def _assert_bounded(client: Any, *, read_timeout: bool) -> None:
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs.get("socket_connect_timeout")
    assert kwargs.get("socket_keepalive") is True
    assert kwargs.get("health_check_interval")
    if read_timeout:
        block_s = StreamsEventBus._BLOCK_MS / 1000
        assert kwargs.get("socket_timeout") is not None
        assert kwargs["socket_timeout"] > block_s


@pytest.mark.usefixtures("fresh_default")
async def test_default_streams_bus_uses_the_bounded_default_transport() -> None:
    cfg = load_infra_config(settings)["eventbus"][DEFAULT_INSTANCE]
    cfg = InstanceConfig(
        capability="eventbus",
        name=DEFAULT_INSTANCE,
        driver="streams",
        options={**cfg.options, "url": _URL},
    )
    try:
        provider, client = await _streams_client(cfg)
        _assert_bounded(client, read_timeout=True)
        main = await redis_cache.get_default_client()
        assert client is not main  # never the cache client's short timeout
        # Shared, not private: the provider owns no client of its own.
        assert provider._owned_client is None
        await provider.aclose()  # shared: owned by the cache lifecycle
    finally:
        await redis_cache.close_default_client()


async def test_named_streams_instance_client_is_bounded() -> None:
    cfg = InstanceConfig(
        capability="eventbus", name="side", driver="streams", options={"url": _URL}
    )
    provider, client = await _streams_client(cfg)
    try:
        _assert_bounded(client, read_timeout=True)
    finally:
        await provider.aclose()


def _redis_bus(options: dict[str, Any], name: str = "side") -> Any:
    cfg = InstanceConfig(
        capability="eventbus", name=name, driver="redis", options=options
    )
    return make_volatile_bus_drivers(
        InMemorySchemaRegistry(), volatile_allowed=volatile_event_bus_allowed
    )["redis"](cfg)


def test_the_default_redis_bus_gets_no_url_it_uses_the_default_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The volatile bus has no Redis client of its own: it runs over a
    ``broadcast`` instance (option ``broadcast``, default ``default``)."""
    monkeypatch.setattr(settings, "event_bus_backend", "redis")
    monkeypatch.setattr(settings, "infra", {})
    options = load_infra_config(settings)["eventbus"][DEFAULT_INSTANCE].options
    assert "url" not in options
    assert VolatileBusOptions.model_validate(options).broadcast == "default"


def test_a_redis_bus_with_a_url_is_refused_and_says_where_the_url_goes() -> None:
    """A ``url`` here used to be dropped by the bus, so its events went to the
    cache Redis outside the operator's TLS/ACL/network separation."""
    with pytest.raises(RuntimeError, match="broadcast"):
        _redis_bus({"url": _URL})


async def test_a_redis_bus_refuses_a_broadcast_without_pattern_subscribe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoPatterns:
        def features(self) -> frozenset[str]:
            return frozenset({"shared"})

    monkeypatch.setattr(
        "sandloom.core.eventing.pubsub_bus.access.broadcast",
        lambda name="default": _NoPatterns(),
    )
    provider = _redis_bus({"broadcast": "plain"})
    with pytest.raises(Unsupported, match="pattern_subscribe"):
        await provider.aopen()


async def test_a_redis_bus_health_is_its_broadcast_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Down:
        def features(self) -> frozenset[str]:
            return frozenset({"pattern_subscribe"})

        async def health(self) -> HealthResult:
            return HealthResult(status="fail", details={"error": "Unavailable"})

    monkeypatch.setattr(
        "sandloom.core.eventing.pubsub_bus.access.broadcast",
        lambda name="default": _Down(),
    )
    provider = _redis_bus({"broadcast": "down"})
    await provider.aopen()
    health = await provider.health()
    assert health.status == "fail"
    assert health.details == {"error": "Unavailable"}
