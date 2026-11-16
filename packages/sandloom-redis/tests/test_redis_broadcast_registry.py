"""The registry's default broadcast instance when the composition selects ``redis``."""

from __future__ import annotations

import pytest
from sandloom_redis._url import MissingUrlError

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.schema.registry import InMemorySchemaRegistry


async def test_the_default_broadcast_instance_is_redis_on_the_url_infra_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {"broadcast": {"default": {"url": "redis://fanout-host:6391/5"}}},
    )
    registry = build_infra_registry(InMemorySchemaRegistry())
    config = registry._config["broadcast"]["default"]
    assert config.driver == "redis"
    assert config.options["url"] == "redis://fanout-host:6391/5"
    provider = registry.provider("broadcast")
    assert type(provider).__name__ == "RedisBroadcastProvider"
    assert provider.features() == {"pattern_subscribe", "shared"}
    assert registry.in_use("broadcast")  # an instance INFRA names is selected


async def test_a_broadcast_instance_without_a_url_is_refused_not_pointed_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composition selects ``redis`` but supplies no url: nothing is
    inherited from the cache, so opening the provider names ``INFRA``."""
    monkeypatch.setattr(
        settings,
        "infra",
        {"cache": {"default": {"url": "redis://cache-host:6390/4"}}},
    )
    registry = build_infra_registry(InMemorySchemaRegistry())
    config = registry._config["broadcast"]["default"]
    assert config.driver == "redis"
    assert "url" not in config.options
    with pytest.raises(MissingUrlError, match=r"broadcast\.default\.url"):
        await registry.provider("broadcast").aopen()
