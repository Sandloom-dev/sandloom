"""The shutdown closes the process-wide default client through the drivers.

The kernel no longer looks the Redis cache module up by name
(``sys.modules.get("sandloom_redis.cache")``): the registry asks each loaded
driver class what it shares (``InfraProvider.aclose_shared``), and the cache and
the Streams bus (which runs on the default client when it has no url of its own)
each close it.
"""

from __future__ import annotations

import pytest

from sandloom.core.infra.registry import InfraRegistry
from sandloom_redis import cache as redis_cache
from sandloom_redis.cache import RedisCacheProvider
from sandloom_redis.eventbus.driver import StreamsEventBusProvider


class _Client:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
def default_client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    """An injected default client (the seam the module documents)."""
    client = _Client()
    monkeypatch.setattr(redis_cache, "_default_client", client)
    monkeypatch.setattr(redis_cache, "_default_client_origin", None)
    return client


@pytest.mark.parametrize(
    ("capability", "driver", "provider"),
    [
        ("cache", "redis", RedisCacheProvider),
        ("eventbus", "streams", StreamsEventBusProvider),
    ],
    ids=["cache", "streams-bus-alone"],
)
async def test_the_registry_closes_the_default_client_for_each_driver_alone(
    default_client: _Client, capability: str, driver: str, provider: type
) -> None:
    registry = InfraRegistry()
    registry.register_driver(capability, driver, provider)
    await registry.aclose_shared()
    assert default_client.closed == 1
    assert not redis_cache.has_default_client()


async def test_both_drivers_closing_it_close_it_once(default_client: _Client) -> None:
    registry = InfraRegistry()
    registry.register_driver("cache", "redis", RedisCacheProvider)
    registry.register_driver("eventbus", "streams", StreamsEventBusProvider)
    await registry.aclose_shared()
    assert default_client.closed == 1


async def test_a_registry_that_loaded_no_redis_driver_leaves_the_client_alone(
    default_client: _Client,
) -> None:
    await InfraRegistry().aclose_shared()
    assert default_client.closed == 0
