"""InfraRegistry: driver registration, resolution, lifecycle, plugins.

These tests use a fully custom in-memory driver — no SQLAlchemy, Redis, or any
concrete backend — proving the kernel depends only on the abstract provider
contracts and that arbitrary infrastructure can be plugged in by configuration.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import CacheProvider
from sandloom.core.infra.registry import (
    InfraError,
    InfraRegistry,
)


class _FakeCache(CacheProvider):
    """An entirely in-process cache provider (no Redis)."""

    closed = False

    def __init__(self, *, name: str, driver: str, options: dict[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._store: dict[str, str] = {}

    # The five portable operations — no Redis, no client object. A backend is
    # plug-compatible the moment it implements exactly these.
    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        self._store[key] = value

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        if key in self._store:
            return False
        self._store[key] = value
        return True

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        new = int(self._store.get(key, "0")) + 1
        self._store[key] = str(new)
        return new

    async def health(self) -> HealthResult:
        return HealthResult(status="ok", details={"size": len(self._store)})

    async def aclose(self) -> None:
        self.closed = True


def _fake_cache_factory(cfg: InstanceConfig) -> _FakeCache:
    return _FakeCache(name=cfg.name, driver=cfg.driver, options=dict(cfg.options))


def _registry_with_fake() -> InfraRegistry:
    reg = InfraRegistry(
        config={
            "cache": {
                "default": InstanceConfig(
                    capability="cache", name="default", driver="fake", options={}
                ),
                "sessions": InstanceConfig(
                    capability="cache",
                    name="sessions",
                    driver="fake",
                    options={"db": 3},
                ),
            }
        }
    )
    reg.register_driver("cache", "fake", _fake_cache_factory)
    return reg


def test_resolves_and_caches_instances() -> None:
    reg = _registry_with_fake()
    p1 = reg.provider("cache", "default")
    p2 = reg.provider("cache", "default")
    assert p1 is p2  # cached
    sessions = reg.provider("cache", "sessions")
    assert sessions is not p1  # distinct named instance
    assert sessions.options == {"db": 3}
    assert reg.instances("cache") == ["default", "sessions"]


def test_unknown_instance_raises() -> None:
    reg = _registry_with_fake()
    with pytest.raises(InfraError):
        reg.provider("cache", "does-not-exist")


def test_unknown_driver_raises() -> None:
    reg = InfraRegistry(
        config={
            "cache": {
                "default": InstanceConfig(
                    capability="cache", name="default", driver="ghost", options={}
                )
            }
        }
    )
    with pytest.raises(InfraError):
        reg.provider("cache", "default")


@pytest.mark.asyncio
async def test_health_all_and_aclose_all() -> None:
    reg = _registry_with_fake()
    health = await reg.health_all()
    assert health["cache:default"].status == "ok"
    assert health["cache:sessions"].status == "ok"

    built = reg.all_built()
    assert built and all(isinstance(p, _FakeCache) for p in built)
    await reg.aclose_all()
    assert all(p.closed for p in built)
    # cache cleared after close
    assert reg.all_built() == []


def test_describe_snapshot() -> None:
    reg = _registry_with_fake()
    reg.provider("cache", "default")  # build one
    desc = reg.describe()
    assert "cache/fake" in desc["drivers"]
    assert desc["instances"]["cache"]["default"]["built"] is True
    assert desc["instances"]["cache"]["sessions"]["built"] is False


def test_configure_instance_overrides_and_invalidates_cache() -> None:
    reg = _registry_with_fake()
    first = reg.provider("cache", "default")
    reg.configure_instance(
        InstanceConfig(
            capability="cache", name="default", driver="fake", options={"x": 1}
        )
    )
    second = reg.provider("cache", "default")
    assert second is not first
    assert second.options == {"x": 1}


def test_snapshot_and_restore_an_instance() -> None:
    """Reconfigure an instance temporarily and put back config AND provider."""
    reg = _registry_with_fake()
    original = reg.provider("cache", "default")
    assert reg.instance_config("cache").driver == "fake"  # type: ignore[union-attr]
    snapshot = reg.snapshot_instance("cache")

    reg.configure_instance(
        InstanceConfig(
            capability="cache", name="default", driver="fake", options={"x": 1}
        )
    )
    temporary = reg.provider("cache", "default")
    assert temporary is not original
    assert reg.restore_instance(snapshot) is temporary  # displaced; caller closes
    assert reg.provider("cache", "default") is original
    assert reg.instance_config("cache").options == {}  # type: ignore[union-attr]

    # An instance that did not exist is removed again on restore.
    absent = reg.snapshot_instance("cache", "scratch")
    assert absent.config is None and absent.provider is None
    reg.configure_instance(
        InstanceConfig(capability="cache", name="scratch", driver="fake", options={})
    )
    assert reg.restore_instance(absent) is None  # never built
    assert reg.instance_config("cache", "scratch") is None
    assert "scratch" not in reg.instances("cache")
