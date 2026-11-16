"""The registry's default broadcast instance when the composition selects ``redis``."""

from __future__ import annotations

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.schema.registry import InMemorySchemaRegistry


async def test_the_default_broadcast_instance_is_redis_on_the_cache_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "infra", {})
    registry = build_infra_registry(InMemorySchemaRegistry())
    config = registry._config["broadcast"]["default"]
    assert config.driver == "redis"
    assert config.implicit is True
    assert config.options["url"] == settings.cache_url
    provider = registry.provider("broadcast")
    assert type(provider).__name__ == "RedisBroadcastProvider"
    assert provider.features() == {"pattern_subscribe", "shared"}
    assert not registry.in_use("broadcast")  # opened only when a user needs it
