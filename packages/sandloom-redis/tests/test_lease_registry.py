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

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InstanceConfig, load_infra_config
from sandloom.core.infra.errors import PermissionDenied
from sandloom.core.schema.registry import InMemorySchemaRegistry


def test_the_redis_lease_driver_is_registered_and_delivers() -> None:
    registry = build_infra_registry(InMemorySchemaRegistry())
    registration = registry._registration("lease", "redis")
    assert registration is not None
    assert not registration.delivered_by


def test_the_composition_supplies_the_beat_lease_instance_infra_overrides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``sandloom-redis`` installed the beat lease defaults to ``redis``
    (without it the kernel selects ``memory``, so this is not a kernel test)."""
    monkeypatch.setattr(settings, "infra", {})
    beat = load_infra_config(settings)["lease"]["beat"]
    assert (beat.driver, beat.implicit) == ("redis", True)
    monkeypatch.setattr(
        settings, "infra", {"lease": {"beat": {"driver": "database", "url": "x"}}}
    )
    named = load_infra_config(settings)["lease"]["beat"]
    assert (named.driver, named.implicit) == ("database", False)


def test_the_default_registry_builds_the_beat_lease_on_the_url_infra_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lease takes its url from its own ``INFRA`` entry, never from the
    cache's; an entry without one is refused when the lease is built."""
    monkeypatch.setattr(
        settings, "infra", {"lease": {"beat": {"url": "redis://lease-host:6390/3"}}}
    )
    registry = build_infra_registry(InMemorySchemaRegistry())
    store = registry.provider("lease", "beat")  # builds, opens nothing
    assert isinstance(store, RedisLeaseProvider)
    assert store.options["url"] == "redis://lease-host:6390/3"
    assert store.features() == {"shared"}
    assert registry.provider("lease", "beat") is store


async def test_a_beat_lease_without_a_url_is_refused_not_pointed_at_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom_redis._url import MissingUrlError

    monkeypatch.setattr(
        settings,
        "infra",
        {"cache": {"default": {"driver": "redis", "url": "redis://cache-host:6390/4"}}},
    )
    registry = build_infra_registry(InMemorySchemaRegistry())
    with pytest.raises(MissingUrlError, match=r"lease\.beat\.url"):
        await registry.provider("lease", "beat").aopen()


def _prod(*risks: str) -> Any:
    return SimpleNamespace(
        is_prod=True,
        accepts_risk=lambda risk: risk in risks,
        infra={},
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
