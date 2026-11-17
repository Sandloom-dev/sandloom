"""The ``streams`` bus opens after the cache whatever order ``INFRA`` lists them in.

A bus with no ``url`` runs on the default cache client, which ``cache.default``
configures when it opens. The kernel reads ``opens_after`` from the driver
class; without the declaration a bus listed before the cache is refused for a
url the cache has (the bug of the first streams round).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sandloom_redis import cache as redis_cache
from sandloom_redis.eventbus.driver import StreamsEventBusProvider

from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InfraConfig, InstanceConfig
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit.shared_endpoints import reset


@pytest.fixture
def _no_default_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No process-wide default client, and no endpoint left for the next test."""
    monkeypatch.setattr(redis_cache, "_default_options", None)
    monkeypatch.setattr(redis_cache, "_default_client", None)
    reset()
    yield
    reset()


def test_the_streams_bus_declares_that_it_opens_after_the_cache() -> None:
    assert StreamsEventBusProvider.opens_after == ("cache",)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_default_client")
async def test_a_url_less_bus_listed_before_the_cache_opens_on_the_cache_url() -> None:
    """Real drivers, the url on the cache only: opening takes no connection, so
    the url is all it reads."""
    config: InfraConfig = {
        "eventbus": {"default": InstanceConfig("eventbus", "default", "streams", {})},
        "cache": {
            "default": InstanceConfig(
                "cache", "default", "redis", {"url": "redis://127.0.0.1:1/0"}
            )
        },
    }
    registry = build_infra_registry(
        InMemorySchemaRegistry(), config=config, load_plugins=True
    )
    try:
        await registry.aopen_all(strict=True)
        assert redis_cache.default_client_url() == "redis://127.0.0.1:1/0"
    finally:
        await registry.aclose_all()
