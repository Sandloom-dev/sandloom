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
    BeatLeaseUnavailableError,
    LeasedScheduler,
    install_beat_lease,
)
from sandloom.core.drivers.memory_lease import MemoryLeaseProvider, _Held
from sandloom.core.infra import fenced_lease
from sandloom.core.infra.ports.lease import Lease

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
    def __init__(self, store: MemoryLeaseProvider) -> None:
        self.infra = SimpleNamespace(provider=self._provider)
        self.store = store
        self.asked: list[tuple[str, str]] = []
        self.loaded_modules = ["sandloom_billing"]

    def _provider(self, capability: str, name: str = "default") -> MemoryLeaseProvider:
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
    with pytest.raises(BeatLeaseUnavailableError, match="not shared"):
        beat_lease.build_lease(Celery("x", broker="memory://"), core, instance="d")

    app = Celery("lease-notshared", broker="memory://")
    install_beat_lease(app, core, instance="default")
    service = Service(app, schedule_filename=str(tmp_path / "schedule"))
    with pytest.raises(SystemExit) as exc:
        signals.beat_init.send(sender=service)
    assert exc.value.code == 1
    assert service._is_shutdown.is_set()


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
        second._retry_at = 0.0  # skip the standby back-off
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
        first._retry_at = 0.0
        assert await asyncio.to_thread(first.ensure) is True
        second._renew_at = 0.0
        assert await asyncio.to_thread(second.ensure) is False  # lost, stops
    finally:
        await asyncio.to_thread(first.release)
        await store.aclose()
