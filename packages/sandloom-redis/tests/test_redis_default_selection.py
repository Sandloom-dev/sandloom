"""A composition that selects the Redis drivers, as the registry builds them.

The kernel names no driver (``src/sandloom/core/tests/test_infra_default_selection.py``
runs the rules on its ``memory`` drivers); the Redis drivers come from this
package's entry points, and the kernel hands the ``atomic_store`` and ``lease``
drivers its eviction policy. Nothing connects: the providers are built, not
opened.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any

import pytest

from sandloom.core.config.common import Environment
from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import (
    KNOWN_DRIVER_PACKAGES,
    build_infra_registry,
    unverified_noeviction_allowed,
)
from sandloom.core.infra.config import (
    KERNEL_INSTANCES,
    InstanceConfig,
    use_default_infra,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry

_STANDARD = {
    "database": {"default": {"driver": "sqlalchemy"}},
    "cache": {"default": {"driver": "redis"}},
    "eventbus": {"default": {"driver": "streams"}},
    "atomic_store": {"security": {"driver": "redis"}},
}


@pytest.fixture(autouse=True)
def _standard_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "event_bus_backend", None)
    monkeypatch.setattr(settings, "infra", {})
    use_default_infra(_STANDARD)


def test_a_selection_for_every_required_instance_starts() -> None:
    registry = build_infra_registry(InMemorySchemaRegistry())
    for capability, name in KERNEL_INSTANCES:
        assert registry.in_use(capability, name)
    # The package's entry points registered the drivers the composition named.
    assert registry.driver_class("atomic_store", "redis").__name__ == (
        "RedisAtomicStoreProvider"
    )


def test_entry_point_atomic_store_and_lease_get_the_eviction_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {
        "atomic_store": {
            "security": InstanceConfig(
                "atomic_store", "security", "redis", {"url": "redis://h:1/0"}
            )
        },
        "lease": {
            "beat": InstanceConfig("lease", "beat", "redis", {"url": "redis://h:1/0"})
        },
    }
    registry = build_infra_registry(InMemorySchemaRegistry(), config=config)
    for capability, name in (("atomic_store", "security"), ("lease", "beat")):
        built: Any = registry.provider(capability, name)
        assert built._assume_allowed is not None, capability
        # dev: the policy allows ``assume``; prod: only the accepted risk does.
        assert built._assume_allowed() is unverified_noeviction_allowed(settings)
        assert built._assume_allowed() is True
    monkeypatch.setattr(settings, "environment", Environment.prod)
    assert built._assume_allowed() is False


def test_the_known_packages_are_the_entry_points_this_package_publishes() -> None:
    published = {
        entry.name
        for entry in metadata.entry_points(group="sandloom.drivers")
        if entry.dist is not None and entry.dist.name == "sandloom-redis"
    }
    known = {
        f"{capability}.{driver}"
        for (capability, driver), (package, install) in KNOWN_DRIVER_PACKAGES.items()
        if package == "sandloom_redis" and install == "pip install sandloom-redis"
    }
    assert known
    assert known == published
