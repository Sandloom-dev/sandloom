"""The kernel's rows for the Redis drivers of ``sandloom-redis`` and the entry
points that package publishes are one set of drivers."""

from __future__ import annotations

from importlib import metadata

import pytest
from sandloom_redis.atomic_store import RedisAtomicStoreProvider
from sandloom_redis.broadcast import RedisBroadcastProvider
from sandloom_redis.lease import RedisLeaseProvider

import sandloom.core.infra.registry as registry_module
from sandloom.core.drivers.bootstrap import (
    BUILTIN_DRIVER_ROWS,
    BUILTIN_DRIVER_TIERS,
    build_infra_registry,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import (
    DriverConflictError,
    DriverLoadError,
    DriverRegistration,
    DriverUnavailableError,
    InfraRegistry,
    InfraStartupError,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry

_CLASSES = {
    "atomic_store": RedisAtomicStoreProvider,
    "lease": RedisLeaseProvider,
    "broadcast": RedisBroadcastProvider,
}


def _rows() -> dict[str, tuple[str, tuple[str, ...]]]:
    return {
        capability: (target, requires)
        for capability, driver, target, requires, _install, _by in BUILTIN_DRIVER_ROWS
        if driver == "redis" and capability in _CLASSES
    }


def test_each_row_targets_the_entry_point_of_the_package() -> None:
    entries = {
        entry.name: entry.value
        for entry in metadata.entry_points(group="sandloom.drivers")
        if entry.name.endswith(".redis") and entry.dist is not None
        if entry.dist.name == "sandloom-redis"
    }
    assert set(entries) == {f"{capability}.redis" for capability in _CLASSES}
    for capability, (target, requires) in _rows().items():
        assert entries[f"{capability}.redis"] == target
        assert "sandloom_redis" in requires  # preflight names the missing package


def test_each_row_resolves_to_the_package_class() -> None:
    registry = build_infra_registry(InMemorySchemaRegistry(), load_plugins=False)
    for capability, provider_class in _CLASSES.items():
        assert registry.driver_class(capability, "redis") is provider_class
        assert registry.driver_tier(capability, "redis") == provider_class.tier
        # The tier the row is registered with is the class's own declaration.
        assert provider_class.tier == BUILTIN_DRIVER_TIERS[(capability, "redis")]


def test_the_entry_point_of_a_row_is_the_same_driver_not_a_conflict() -> None:
    config = {
        "lease": {
            "beat": InstanceConfig("lease", "beat", "redis", {"url": "redis://h"})
        }
    }
    registry = build_infra_registry(InMemorySchemaRegistry(), config=config)
    assert registry.driver_class("lease", "redis") is RedisLeaseProvider
    assert registry.driver_target("lease", "redis") == _rows()["lease"][0]


def test_another_package_cannot_publish_the_name_of_a_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Entry:
        name = "lease.redis"
        value = "other_pkg.lease:Provider"
        dist = type("_Dist", (), {"name": "other-pkg"})()

        @staticmethod
        def load() -> type:
            return RedisLeaseProvider

    class _Entries:
        def select(self, *, group: str, name: str | None = None) -> list[object]:
            if group == "sandloom.drivers" and name in (None, "lease.redis"):
                return [_Entry()]
            return []

    monkeypatch.setattr(
        "sandloom.core.infra.registry.metadata.entry_points", lambda: _Entries()
    )
    config = {
        "lease": {
            "beat": InstanceConfig("lease", "beat", "redis", {"url": "redis://h"})
        }
    }
    with pytest.raises(DriverConflictError, match="built in"):
        build_infra_registry(InMemorySchemaRegistry(), config=config)


def _without_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    original = InfraRegistry._is_installed

    def is_installed(self: InfraRegistry, name: str) -> bool:
        return False if name == "sandloom_redis" else original(self, name)

    monkeypatch.setattr(InfraRegistry, "_is_installed", is_installed)


def test_a_kernel_without_the_package_boots_on_its_default_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_the_package(monkeypatch)
    registry = build_infra_registry(InMemorySchemaRegistry(), load_plugins=False)
    assert registry.missing_packages("lease", "beat") == ("sandloom_redis",)


def test_an_instance_infra_names_is_refused_without_the_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_the_package(monkeypatch)
    config = {
        "atomic_store": {
            "security": InstanceConfig(
                "atomic_store", "security", "redis", {"url": "redis://h"}
            )
        }
    }
    with pytest.raises(DriverUnavailableError, match="sandloom_redis") as caught:
        build_infra_registry(
            InMemorySchemaRegistry(), config=config, load_plugins=False
        )
    assert "sandloom[redis]" in str(caught.value.install)


def _implicit_lease_registry() -> InfraRegistry:
    config = {
        "lease": {
            "beat": InstanceConfig(
                "lease", "beat", "redis", {"url": "redis://h"}, implicit=True
            )
        }
    }
    registry = build_infra_registry(
        InMemorySchemaRegistry(), config=config, load_plugins=False
    )
    registry.use_kernel_instance("lease", "beat")
    return registry


async def test_an_implicit_default_without_the_package_is_b037_at_strict_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_the_package(monkeypatch)
    registry = _implicit_lease_registry()
    with pytest.raises(InfraStartupError, match="sandloom_redis") as caught:
        await registry.aopen_all(strict=True)
    assert caught.value.sandloom_code == "SANDLOOM-B037"
    assert "sandloom[redis]" in str(caught.value)


def test_first_use_of_a_lazy_row_without_the_package_names_the_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_the_package(monkeypatch)
    real_import = registry_module._import_target

    def import_target(
        capability: str, driver: str, registration: DriverRegistration
    ) -> object:
        if (registration.target or "").startswith("sandloom_redis"):
            raise DriverLoadError("sandloom_redis is not importable", fix="-")
        return real_import(capability, driver, registration)

    monkeypatch.setattr(registry_module, "_import_target", import_target)
    registry = _implicit_lease_registry()
    with pytest.raises(DriverUnavailableError) as caught:
        registry.provider("lease", "beat")
    assert caught.value.sandloom_code == "SANDLOOM-B037"
    assert caught.value.missing == ("sandloom_redis",)
    assert "sandloom_redis" in str(caught.value)
    assert "pip install 'sandloom[redis]'" in str(caught.value)
