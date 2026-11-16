"""The beat singleton lease, the kernel's side: the holder (``FencedLease``) and the
kernel's resolution of the lease (``sandloom.core.worker.run_scheduler``), over a
``lease`` store (spec 5; review-map H-10).

The Celery side (``BeatLease``, the scheduler wrapper, the host's refusal) is
``packages/sandloom-celery/tests/test_celery_beat_lease.py``, which borrows the
clock and the stores below.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core import worker
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers.memory_lease import MemoryLeaseProvider
from sandloom.core.infra import fenced_lease
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import InfraError, PermissionDenied
from sandloom.core.infra.fenced_lease import FencedLease, LeaseNotSharedError
from sandloom.core.infra.ports.lease import (
    LEASE_CAPABILITY,
    Lease,
    LeaseProvider,
)
from sandloom.core.infra.registry import InfraRegistry, InfraStartupError

_NAME = "sandloom:beat:lease:test"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Store(MemoryLeaseProvider):
    """The memory lease on a fake clock, with an outage switch."""

    def __init__(self, clock: _Clock) -> None:
        super().__init__(name="beat", driver="memory", options={}, clock=clock)
        self.broken = False

    def _check(self) -> None:
        if self.broken:
            raise ConnectionError("lease backend down")

    async def acquire(self, name: str, *, ttl_seconds: int, owner: str) -> Lease | None:
        self._check()
        return await super().acquire(name, ttl_seconds=ttl_seconds, owner=owner)

    async def renew(self, lease: Lease) -> bool:
        self._check()
        return await super().renew(lease)

    async def release(self, lease: Lease) -> bool:
        self._check()
        return await super().release(lease)


def test_lease_scope_separates_services_and_shares_replicas() -> None:
    a = fenced_lease.lease_scope(
        "redis://b/0", "default", ["sandloom_identity", "sandloom_billing"]
    )
    replica = fenced_lease.lease_scope(
        "redis://b/0", "default", ["sandloom_billing", "sandloom_identity"]
    )
    other = fenced_lease.lease_scope("redis://b/0", "default", ["sandloom_billing"])
    assert a == replica
    assert a != other


class _Core:
    def __init__(self, store: LeaseProvider) -> None:
        self.infra = SimpleNamespace(
            provider=self._provider, instance_config=self._instance_config
        )
        self.store = store
        self.asked: list[tuple[str, str]] = []
        self.loaded_modules = ["sandloom_billing"]

    def _instance_config(self, capability: str, name: str) -> InstanceConfig:
        return InstanceConfig(
            capability=capability,
            name=name,
            driver="fake",
            options={"broker": "redis://broker.invalid:6379/1"},
        )

    def _provider(self, capability: str, name: str = "default") -> LeaseProvider:
        self.asked.append((capability, name))
        return self.store


def test_fenced_lease_exposes_read_only_name_fence_interval_and_store() -> None:
    """What ``BeatLease`` (the Celery host) reads is a pinned, read-only surface."""
    store = _Store(_Clock())
    lease = FencedLease(store, _NAME, ttl_seconds=30)
    assert lease.name == _NAME
    assert lease.store is store
    assert lease.renew_interval == 10.0
    assert lease.fence is None
    for attribute in ("name", "store", "renew_interval", "fence"):
        assert isinstance(getattr(FencedLease, attribute), property)
        with pytest.raises(AttributeError):
            setattr(lease, attribute, object())


class _Provider:
    """A taskqueue instance whose host records the scheduler call."""

    driver = "fake"

    def __init__(self) -> None:
        self.options = {"broker": "redis://broker.invalid:6379/1"}
        self.scheduled: list[dict[str, Any]] = []

    def worker_host(self) -> Any:
        provider = self

        class _Host:
            def run_scheduler(self, boot: Any, **kwargs: Any) -> int:
                provider.scheduled.append(kwargs)
                return 0

        return _Host()

    def features(self) -> frozenset[str]:
        return frozenset({"periodic"})


class _SchedulerCore(_Core):
    def __init__(self, store: LeaseProvider) -> None:
        super().__init__(store)
        self.provider = _Provider()

    def taskqueue(self, name: str = "default") -> _Provider:
        return self.provider


def test_the_scheduler_holds_the_lease_the_kernel_resolved() -> None:
    store = _Store(_Clock())
    core = _SchedulerCore(store)
    assert worker.run_scheduler(core=core, instance="jobs") == 0
    assert core.asked == [("lease", "beat")]  # the instance INFRA.lease.beat
    (call,) = core.provider.scheduled
    assert call["lease"] is store
    # scoped by the instance's own broker (the one ``broker_url_of`` reads)
    assert call[
        "lease_name"
    ] == fenced_lease.LEASE_KEY_PREFIX + fenced_lease.beat_scope(
        "redis://broker.invalid:6379/1", "jobs", core
    )
    assert call["lease_ttl_seconds"] == settings.beat_lease_ttl_seconds


def test_beat_refuses_to_start_on_a_lease_that_is_not_shared_in_production(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    prod = SimpleNamespace(is_prod=True, beat_lease_name="", beat_lease_ttl_seconds=30)
    monkeypatch.setattr(worker, "settings", prod)
    monkeypatch.setattr(fenced_lease, "settings", prod)
    core = _SchedulerCore(_Store(_Clock()))
    with caplog.at_level("CRITICAL", logger="sandloom.core.worker"):
        assert worker.run_scheduler(core=core, instance="default") == 1
    assert "not shared" in caplog.text
    assert core.provider.scheduled == []  # nothing was scheduled


def test_the_kernel_resolves_the_store_the_scope_and_the_shared_check() -> None:
    store = _Store(_Clock())
    core = _Core(store)
    resolved, name = fenced_lease.resolve_beat_lease(
        core, broker_url="memory://", instance="d", shared_required=False
    )
    assert resolved is store
    assert name == fenced_lease.LEASE_KEY_PREFIX + fenced_lease.beat_scope(
        "memory://", "d", core
    )
    with pytest.raises(LeaseNotSharedError, match="not shared"):
        fenced_lease.resolve_beat_lease(
            core, broker_url="memory://", instance="d", shared_required=True
        )


def test_a_sqlite_lease_is_refused_for_a_beat_in_production(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.drivers.database_lease import DatabaseLeaseProvider

    prod = SimpleNamespace(is_prod=True, beat_lease_name="", beat_lease_ttl_seconds=30)
    monkeypatch.setattr(worker, "settings", prod)
    monkeypatch.setattr(fenced_lease, "settings", prod)
    lite = DatabaseLeaseProvider(
        name="beat",
        driver="database",
        options={"url": f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}"},
    )
    refused = _SchedulerCore(lite)
    assert worker.run_scheduler(core=refused, instance="d") == 1
    assert refused.provider.scheduled == []
    shared = DatabaseLeaseProvider(
        name="beat",
        driver="database",
        options={"url": "postgresql+asyncpg://u:p@127.0.0.1:1/db"},
    )
    accepted = _SchedulerCore(shared)
    assert worker.run_scheduler(core=accepted, instance="d") == 0
    (call,) = accepted.provider.scheduled
    assert call["lease_name"].startswith(fenced_lease.LEASE_KEY_PREFIX)


# ------------------------------------------- a refusing lease backend (L1)
class _Refusing(MemoryLeaseProvider):
    """A lease driver whose server refuses to serve (the eviction policy)."""

    error: type[InfraError] = PermissionDenied

    async def aopen(self) -> None:
        raise self.error(
            "the Redis server evicts keys (maxmemory-policy is 'allkeys-lru')",
            capability="lease",
            instance=self.name,
        )

    async def health(self) -> HealthResult:
        return HealthResult(status="fail", details={"reason": "eviction_check_refused"})

    async def acquire(self, name: str, *, ttl_seconds: int, owner: str) -> Lease | None:
        await self.aopen()
        return None


def _registry_with_refusing_beat(driver: type[_Refusing]) -> InfraRegistry:
    registry = InfraRegistry()
    registry.register_capability(LEASE_CAPABILITY)
    registry.register_driver("lease", "refusing", driver)
    implicit = InstanceConfig("lease", "beat", "refusing", {})
    implicit.implicit = True
    registry.set_config({"lease": {"beat": implicit}})
    return registry


async def test_a_process_that_hosts_sagas_opens_and_probes_the_beat_lease() -> None:
    registry = _registry_with_refusing_beat(_Refusing)
    # Nothing requires the synthesized instance: it is neither opened nor probed.
    await registry.aopen_all(strict=True)
    assert "lease:beat" not in await registry.health_all()

    from sandloom.core.sagas.sweep import SagaSweeper

    runtime = SimpleNamespace(
        core=SimpleNamespace(infra=registry),
        registry=[object()],
        settings=SimpleNamespace(saga_sweep_interval_seconds=1, saga_sweep_batch=1),
    )
    SagaSweeper(runtime)  # type: ignore[arg-type]
    assert registry.in_use("lease", "beat")
    with pytest.raises(InfraStartupError, match="lease/beat.*allkeys-lru"):
        await registry.aopen_all(strict=True)
    health = (await registry.health_all())["lease:beat"]
    assert health.status == "fail"


def test_a_process_without_sagas_leaves_the_beat_lease_unused() -> None:
    from sandloom.core.sagas.sweep import SagaSweeper

    registry = _registry_with_refusing_beat(_Refusing)
    runtime = SimpleNamespace(
        core=SimpleNamespace(infra=registry),
        registry=[],
        settings=SimpleNamespace(saga_sweep_interval_seconds=1, saga_sweep_batch=1),
    )
    SagaSweeper(runtime)  # type: ignore[arg-type]
    assert not registry.in_use("lease", "beat")


async def test_a_refusing_backend_is_logged_as_a_refusal_not_as_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Refusing(name="beat", driver="memory", options={})
    holder = FencedLease(store, _NAME, ttl_seconds=30, label="[saga-sweep]")
    with caplog.at_level("WARNING", logger="sandloom.core.infra.fenced_lease"):
        assert await holder.ensure_async() is False
    assert "allkeys-lru" in caplog.text
    assert "cannot reach" not in caplog.text
