"""The beat singleton lease: the kernel's holder (``FencedLease``) and the Celery
side (``sandloom.core.beat_lease``), over a ``lease`` store (spec 5; review-map H-10).

Two beats on one broker must not both schedule: the second stands by, takes
over when the first releases its lease or dies (TTL expiry), and a holder
that loses the lease stops at once.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest
from celery import Celery, signals
from celery.beat import Service

from sandloom.core import beat_lease
from sandloom.core.beat_lease import (
    BeatLease,
    LeasedScheduler,
    install_beat_lease,
)
from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers.memory_lease import MemoryLeaseProvider, _Held
from sandloom.core.infra import fenced_lease
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import InfraError, PermissionDenied, Unavailable
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


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def _pair(ttl: int = 30) -> tuple[_Clock, _Store, BeatLease, BeatLease]:
    clock = _Clock()
    store = _Store(clock)
    first = BeatLease(
        store, _NAME, ttl_seconds=ttl, runner=_run, clock=clock, identity="a:1"
    )
    second = BeatLease(
        store, _NAME, ttl_seconds=ttl, runner=_run, clock=clock, identity="b:2"
    )
    return clock, store, first, second


def test_second_beat_stands_by_and_takes_over_after_release() -> None:
    clock, _store, first, second = _pair()

    assert first.ensure() is True
    assert second.ensure() is False
    assert first.fence == 1 and second.token is None

    first.release()
    clock.now += second.renew_interval
    assert second.ensure() is True
    assert second.fence == 2  # fencing numbers only grow


def test_standby_takes_over_after_the_holder_dies() -> None:
    clock, store, first, second = _pair(ttl=30)
    assert first.ensure() is True
    assert second.ensure() is False

    # The holder crashes: no renewals, the lease's TTL runs out.
    clock.now += 31
    assert second.ensure() is True
    assert second.fence == 2
    assert _holder(store) == ("b:2", 2)


def _holder(store: _Store) -> tuple[str, int]:
    held = store._held[_NAME]
    return held.owner, held.fence


def test_holder_renews_and_keeps_the_lease() -> None:
    clock, store, first, second = _pair(ttl=30)
    assert first.ensure() is True
    for _ in range(10):  # 10 renew periods > 3 TTLs
        clock.now += first.renew_interval
        assert first.ensure() is True
        assert second.ensure() is False
    assert store._held[_NAME].token == first.token


def test_a_holder_that_lost_its_token_stops_scheduling_at_once() -> None:
    clock, store, first, _second = _pair(ttl=30)
    assert first.ensure() is True
    # taken over: another grant of the name
    store._held[_NAME] = _Held("other:1", "other-token", 99, clock.now + 30)

    clock.now += first.renew_interval
    assert first.ensure() is False
    assert first.held is False


def test_cache_outage_keeps_scheduling_only_until_the_local_deadline() -> None:
    clock, store, first, _second = _pair(ttl=30)
    assert first.ensure() is True
    store.broken = True

    clock.now += first.renew_interval
    assert first.ensure() is True  # still inside the lease the store granted
    clock.now = 1000.0 + 30  # the local deadline (from before the write)
    assert first.ensure() is False
    assert first.held is False
    # A beat that never held the lease does not schedule through an outage.
    clock.now += 100
    assert first.ensure() is False


def test_leased_scheduler_ticks_only_while_holding() -> None:
    clock, _store, first, second = _pair()

    class _Inner:
        ticks = 0
        closed = False
        max_interval = 300

        def tick(self) -> float:
            self.ticks += 1
            return 120.0

        def close(self) -> None:
            self.closed = True

    held_inner, standby_inner = _Inner(), _Inner()
    held = LeasedScheduler(held_inner, first)
    standby = LeasedScheduler(standby_inner, second)

    assert held.tick() == pytest.approx(first.renew_interval)  # capped to renew
    assert standby.tick() == pytest.approx(second.renew_interval)
    assert held_inner.ticks == 1 and standby_inner.ticks == 0
    assert standby.max_interval == 300  # everything else is the inner's

    held.close()
    assert held_inner.closed and first.held is False
    clock.now += second.renew_interval
    standby.tick()
    assert standby_inner.ticks == 1  # took over after the clean shutdown


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
        self.infra = SimpleNamespace(provider=self._provider)
        self.store = store
        self.asked: list[tuple[str, str]] = []
        self.loaded_modules = ["sandloom_billing"]

    def _provider(self, capability: str, name: str = "default") -> LeaseProvider:
        self.asked.append((capability, name))
        return self.store


def test_beat_init_wraps_the_scheduler_of_this_app_only(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    core = _Core(_Store(clock))
    app = Celery("lease-test", broker="memory://")
    other = Celery("other", broker="memory://")
    monkeypatch.setattr(beat_lease, "_default_runner", _run)
    install_beat_lease(app, core, instance="default")
    install_beat_lease(app, core, instance="default")  # idempotent

    service = Service(app, schedule_filename=str(tmp_path / "schedule"))
    signals.beat_init.send(sender=service)
    assert isinstance(service.scheduler, LeasedScheduler)
    assert service.scheduler.lease.name.startswith(fenced_lease.LEASE_KEY_PREFIX)
    assert core.asked == [("lease", "beat")]  # the instance INFRA.lease.beat

    foreign = Service(other, schedule_filename=str(tmp_path / "other"))
    signals.beat_init.send(sender=foreign)
    assert not isinstance(foreign.scheduler, LeasedScheduler)

    second = Service(app, schedule_filename=str(tmp_path / "second"))
    signals.beat_init.send(sender=second)
    assert service.scheduler.tick() > 0
    assert service.scheduler.lease.held
    second.scheduler.tick()
    assert not second.scheduler.lease.held  # the second beat stands by
    service.scheduler.close()
    second.scheduler.close()


def test_beat_refuses_to_start_on_a_lease_that_is_not_shared_in_production(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod = SimpleNamespace(is_prod=True, beat_lease_name="", beat_lease_ttl_seconds=30)
    monkeypatch.setattr(beat_lease, "settings", prod)
    core = _Core(_Store(_Clock()))
    with pytest.raises(LeaseNotSharedError, match="not shared"):
        beat_lease.build_lease(Celery("x", broker="memory://"), core, instance="d")

    app = Celery("lease-notshared", broker="memory://")
    install_beat_lease(app, core, instance="default")
    service = Service(app, schedule_filename=str(tmp_path / "schedule"))
    with pytest.raises(SystemExit) as exc:
        signals.beat_init.send(sender=service)
    assert exc.value.code == 1
    assert service._is_shutdown.is_set()


def test_the_beat_lease_wraps_the_kernel_holder_without_subclassing_it() -> None:
    _clock, _store, first, _second = _pair()
    assert not issubclass(BeatLease, FencedLease)
    assert isinstance(first._holder, FencedLease)
    assert first._holder.label == "[beat]"


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
    monkeypatch.setattr(beat_lease, "settings", prod)
    lite = DatabaseLeaseProvider(
        name="beat",
        driver="database",
        options={"url": f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}"},
    )
    with pytest.raises(LeaseNotSharedError, match="not shared"):
        beat_lease.build_lease(
            Celery("x", broker="memory://"), _Core(lite), instance="d"
        )
    shared = DatabaseLeaseProvider(
        name="beat",
        driver="database",
        options={"url": "postgresql+asyncpg://u:p@127.0.0.1:1/db"},
    )
    built = beat_lease.build_lease(
        Celery("x", broker="memory://"), _Core(shared), instance="d"
    )
    assert built.name.startswith(fenced_lease.LEASE_KEY_PREFIX)


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


def test_beat_refuses_to_start_on_a_backend_that_refuses_with_the_real_cause(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(beat_lease, "_default_runner", _run)
    core = _Core(_Refusing(name="beat", driver="memory", options={}))
    app = Celery("lease-refused", broker="memory://")
    install_beat_lease(app, core, instance="default")
    service = Service(app, schedule_filename=str(tmp_path / "schedule"))
    with caplog.at_level("CRITICAL", logger="sandloom.core.beat_lease"):
        with pytest.raises(SystemExit) as exc:
            signals.beat_init.send(sender=service)
    assert exc.value.code == 1
    assert service._is_shutdown.is_set()
    assert "allkeys-lru" in caplog.text  # the policy, not "cannot reach"


def test_beat_starts_on_a_backend_that_is_only_unreachable(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Down(_Refusing):
        error = Unavailable

    monkeypatch.setattr(beat_lease, "_default_runner", _run)
    core = _Core(_Down(name="beat", driver="memory", options={}))
    app = Celery("lease-down", broker="memory://")
    install_beat_lease(app, core, instance="default")
    service = Service(app, schedule_filename=str(tmp_path / "schedule"))
    signals.beat_init.send(sender=service)  # stands by and retries, no exit
    assert isinstance(service.scheduler, LeasedScheduler)
    service.scheduler.close()


async def test_a_refusing_backend_is_logged_as_a_refusal_not_as_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _Refusing(name="beat", driver="memory", options={})
    holder = FencedLease(store, _NAME, ttl_seconds=30, label="[saga-sweep]")
    with caplog.at_level("WARNING", logger="sandloom.core.infra.fenced_lease"):
        assert await holder.ensure_async() is False
    assert "allkeys-lru" in caplog.text
    assert "cannot reach" not in caplog.text


async def test_two_leases_on_real_redis() -> None:
    """Acquire / standby / release / takeover against the real redis lease driver."""
    from sandloom.core.drivers.redis_lease import RedisLeaseProvider

    url = os.getenv("SANDLOOM_TEST_CACHE_URL")
    if not url:
        pytest.skip("SANDLOOM_TEST_CACHE_URL not set")
    store = RedisLeaseProvider(name="beat", driver="redis", options={"url": url})
    await store.aopen()
    loop = asyncio.get_running_loop()

    def runner(coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=10)

    name = f"sandloom:beat:lease:redis-test-{os.getpid()}-{id(store)}"
    first = BeatLease(store, name, ttl_seconds=3, runner=runner, identity="a:1")
    second = BeatLease(store, name, ttl_seconds=3, runner=runner, identity="b:2")
    try:
        assert await asyncio.to_thread(first.ensure) is True
        assert await asyncio.to_thread(second.ensure) is False
        holder = await store.holder(name)
        assert holder is not None and holder.owner == "a:1"

        await asyncio.to_thread(first.release)
        assert await store.holder(name) is None
        second._holder._retry_at = 0.0  # skip the standby back-off
        assert await asyncio.to_thread(second.ensure) is True
        assert second.fence is not None and first.fence is not None
        assert second.fence > first.fence

        # The holder "dies": its lease expires on the server after the TTL
        # (polled until gone, not a fixed sleep).
        for _ in range(300):
            if await store.holder(name) is None:
                break
            await asyncio.sleep(0.02)
        assert await store.holder(name) is None
        first._holder._retry_at = 0.0
        assert await asyncio.to_thread(first.ensure) is True
        second._holder._renew_at = 0.0
        assert await asyncio.to_thread(second.ensure) is False  # lost, stops
    finally:
        await asyncio.to_thread(first.release)
        await store.aclose()
