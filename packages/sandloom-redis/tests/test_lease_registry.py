"""The ``lease`` registry builds the ``redis`` beat lease the kernel selects by
default (spec 5). The kit runs on the drivers in
``src/sandloom/core/tests/test_lease.py`` (``memory``, ``database``) and
``test_redis_lease.py`` (``redis``); the registration of ``memory`` and
``database``, the beat instance the composition supplies and the
``unverified_noeviction`` risk rule are kernel tests in ``test_lease.py``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sandloom_redis.lease import RedisLeaseProvider

from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import PermissionDenied
from sandloom.core.schema.registry import InMemorySchemaRegistry


def test_the_redis_lease_driver_is_registered_and_delivers() -> None:
    registry = build_infra_registry(InMemorySchemaRegistry())
    registration = registry._registration("lease", "redis")
    assert registration is not None
    assert not registration.delivered_by


def test_the_default_registry_builds_the_beat_lease_on_the_cache_url() -> None:
    from sandloom.core.config.settings import settings

    registry = build_infra_registry(InMemorySchemaRegistry())
    store = registry.provider("lease", "beat")  # builds, opens nothing
    assert isinstance(store, RedisLeaseProvider)
    assert store.options["url"] == settings.cache_url
    assert store.features() == {"shared"}
    assert registry.provider("lease", "beat") is store


def _prod(*risks: str) -> Any:
    return SimpleNamespace(
        is_prod=True,
        accepts_risk=lambda risk: risk in risks,
        infra={},
        cache_url="redis://h/0",
    )


def _beat_redis(settings: Any, **options: Any) -> Any:
    config = {
        "lease": {"beat": InstanceConfig("lease", "beat", "redis", dict(options))}
    }
    registry = build_infra_registry(
        InMemorySchemaRegistry(), settings=settings, config=config
    )
    return registry.provider("lease", "beat")


def test_verify_in_production_needs_no_risk() -> None:
    assert isinstance(_beat_redis(_prod(), url="redis://h/0"), RedisLeaseProvider)


async def test_the_production_policy_reaches_the_driver_through_the_build() -> None:
    options = {"url": "redis://h/0", "never_evicts_check": "assume"}
    refused = _beat_redis(_prod(), **options)
    with pytest.raises(PermissionDenied, match="unverified_noeviction"):
        await refused.aopen()
    accepted = _beat_redis(_prod("unverified_noeviction"), **options)
    assert accepted._assume_allowed is not None and accepted._assume_allowed()
