"""A Redis or Celery instance with no url refuses startup and names its INFRA option.

``CACHE_URL`` and ``BROKER_URL`` are optional settings (flat-urls stage 0a): an
instance that ends with no ``url`` / ``broker`` (neither INFRA nor a flat
setting gave one) is a configuration mistake, so startup aborts in every
environment, and the message says which INFRA option to set, never that the
kernel fills it from a flat setting.
"""

from __future__ import annotations

import pytest
from sandloom_celery.taskqueue import CeleryTaskQueueProvider, MissingBrokerError
from sandloom_redis.cache import MissingUrlError, RedisCacheProvider

from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InfraConfig, InstanceConfig
from sandloom.core.infra.registry import InfraStartupError
from sandloom.core.schema.registry import InMemorySchemaRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["default", "sessions"])
async def test_a_redis_cache_without_a_url_names_the_infra_option(name: str) -> None:
    cfg = InstanceConfig("cache", name, "redis", {})
    provider = RedisCacheProvider.from_config(cfg)  # building opens nothing
    with pytest.raises(MissingUrlError) as caught:
        await provider.aopen()
    message = str(caught.value)
    assert f"cache.{name}.url" in message
    assert "INFRA=" in message
    assert "CACHE_URL" not in message
    assert caught.value.fatal_at_startup is True


@pytest.mark.asyncio
async def test_a_redis_cache_with_a_url_opens_without_connecting() -> None:
    provider = RedisCacheProvider.from_config(
        InstanceConfig("cache", "sessions", "redis", {"url": "redis://x:6379/0"})
    )
    await provider.aopen()  # no connection: the host above does not exist
    assert provider.name == "sessions"


@pytest.mark.parametrize("name", ["default", "jobs"])
def test_a_celery_taskqueue_without_a_broker_names_the_infra_option(name: str) -> None:
    cfg = InstanceConfig("taskqueue", name, "celery", {})
    with pytest.raises(MissingBrokerError) as caught:
        CeleryTaskQueueProvider.from_config(cfg)
    message = str(caught.value)
    assert f"taskqueue.{name}.broker" in message
    assert "INFRA=" in message
    assert "BROKER_URL" not in message
    assert caught.value.fatal_at_startup is True


@pytest.mark.asyncio
async def test_startup_aborts_on_a_redis_cache_with_no_url_even_outside_production() -> (
    None
):
    config: InfraConfig = {
        "cache": {"default": InstanceConfig("cache", "default", "redis", {})}
    }
    registry = build_infra_registry(
        InMemorySchemaRegistry(), config=config, load_plugins=True
    )
    # not strict (the non-production default): a plain failure would only warn
    with pytest.raises(InfraStartupError, match=r"cache\.default\.url"):
        await registry.aopen_all(strict=False)
