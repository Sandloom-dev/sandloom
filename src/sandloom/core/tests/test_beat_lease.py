"""Celery beat singleton lease (``sandloom.core.beat_lease``, review-map H-10).

Two beats on one broker must not both schedule: the second stands by, takes
over when the first releases its lease or dies (TTL expiry), and a holder
that loses the lease stops at once.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from celery import Celery, signals
from celery.beat import Service

from sandloom.core import beat_lease
from sandloom.core.beat_lease import BeatLease, LeasedScheduler, install_beat_lease
from sandloom.core.infra.contracts import CacheProvider


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _TTLCache(CacheProvider):
    """Dict cache with real (fake-clock) TTL expiry and ``cas``."""

    capability = "cache"

    def __init__(self, clock: _Clock) -> None:
        super().__init__(name="default", driver="fake", options={})
        self.clock = clock
        self.data: dict[str, tuple[str, float | None]] = {}
        self.broken = False

    def features(self) -> frozenset[str]:
        return frozenset({"cas"})

    def _live(self, key: str) -> str | None:
        if self.broken:
            raise ConnectionError("cache down")
        item = self.data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and self.clock() >= expires:
            del self.data[key]
            return None
        return value

    def _exp(self, ttl: int | None) -> float | None:
        return None if ttl is None else self.clock() + ttl

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        self._live(key)
        self.data[key] = (value, self._exp(ttl_seconds))

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        if self._live(key) is not None:
            return False
        self.data[key] = (value, self._exp(ttl_seconds))
        return True

    async def delete(self, key: str) -> None:
        self._live(key)
        self.data.pop(key, None)

    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        value = int(self._live(key) or 0) + 1
        self.data[key] = (str(value), None)
        return value

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        if self._live(key) != expected:
            return False
        self.data[key] = (value, self._exp(ttl_seconds))
        return True

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        if self._live(key) != expected:
            return False
        del self.data[key]
        return True


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def _pair(ttl: int = 30) -> tuple[_Clock, _TTLCache, BeatLease, BeatLease]:
    clock = _Clock()
    cache = _TTLCache(clock)
    key = "sandloom:beat:lease:test"
    first = BeatLease(
        cache, key, ttl_seconds=ttl, runner=_run, clock=clock, identity="a:1"
    )
    second = BeatLease(
        cache, key, ttl_seconds=ttl, runner=_run, clock=clock, identity="b:2"
    )
    return clock, cache, first, second


def test_second_beat_stands_by_and_takes_over_after_release() -> None:
    clock, _cache, first, second = _pair()

    assert first.ensure() is True
    assert second.ensure() is False
    assert first.fence == 1 and second.token is None

    first.release()
    clock.now += second.renew_interval
    assert second.ensure() is True
    assert second.fence == 2  # fencing numbers only grow


def test_standby_takes_over_after_the_holder_dies() -> None:
    clock, _cache, first, second = _pair(ttl=30)
    assert first.ensure() is True
    assert second.ensure() is False

    # The holder crashes: no renewals, the server-side TTL runs out.
    clock.now += 31
    assert second.ensure() is True
    assert second.token is not None and second.token.startswith("2:b:2:")


def test_holder_renews_and_keeps_the_lease() -> None:
    clock, cache, first, second = _pair(ttl=30)
    assert first.ensure() is True
    for _ in range(10):  # 10 renew periods > 3 TTLs
        clock.now += first.renew_interval
        assert first.ensure() is True
        assert second.ensure() is False
    assert cache.data["sandloom:beat:lease:test"][0] == first.token


def test_a_holder_that_lost_its_token_stops_scheduling_at_once() -> None:
    clock, cache, first, _second = _pair(ttl=30)
    assert first.ensure() is True
    cache.data["sandloom:beat:lease:test"] = ("99:other:1:x", None)  # taken over

    clock.now += first.renew_interval
    assert first.ensure() is False
    assert first.held is False


def test_cache_outage_keeps_scheduling_only_until_the_local_deadline() -> None:
    clock, cache, first, _second = _pair(ttl=30)
    assert first.ensure() is True
    cache.broken = True

    clock.now += first.renew_interval
    assert first.ensure() is True  # still inside the lease the server granted
    clock.now = 1000.0 + 30  # the local deadline (from before the write)
    assert first.ensure() is False
    assert first.held is False
    # A beat that never held the lease does not schedule through an outage.
    assert _pair()[3].ensure() is True  # sanity: a healthy cache grants it
    clock.now += 100
    assert first.ensure() is False


def test_leased_scheduler_ticks_only_while_holding() -> None:
    clock, _cache, first, second = _pair()

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
    a = beat_lease.lease_scope(
        "redis://b/0", "default", ["sandloom_identity", "sandloom_billing"]
    )
    replica = beat_lease.lease_scope(
        "redis://b/0", "default", ["sandloom_billing", "sandloom_identity"]
    )
    other = beat_lease.lease_scope("redis://b/0", "default", ["sandloom_billing"])
    assert a == replica
    assert a != other


class _Core:
    def __init__(self, cache: CacheProvider) -> None:
        self._cache = cache
        self.loaded_modules = ["sandloom_billing"]

    def cache(self, name: str = "default") -> CacheProvider:
        return self._cache


def test_beat_init_wraps_the_scheduler_of_this_app_only(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    cache = _TTLCache(clock)
    app = Celery("lease-test", broker="memory://")
    other = Celery("other", broker="memory://")
    monkeypatch.setattr(beat_lease, "_default_runner", _run)
    install_beat_lease(app, _Core(cache), instance="default")
    install_beat_lease(app, _Core(cache), instance="default")  # idempotent

    service = Service(app, schedule_filename=str(tmp_path / "schedule"))
    signals.beat_init.send(sender=service)
    assert isinstance(service.scheduler, LeasedScheduler)
    assert service.scheduler.lease.key.startswith(beat_lease.LEASE_KEY_PREFIX)

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


def test_beat_refuses_to_start_without_a_cas_cache(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _NoCas(_TTLCache):
        def features(self) -> frozenset[str]:
            return frozenset()

    app = Celery("lease-nocas", broker="memory://")
    install_beat_lease(app, _Core(_NoCas(_Clock())), instance="default")
    service = Service(app, schedule_filename=str(tmp_path / "schedule"))
    with pytest.raises(SystemExit) as exc:
        signals.beat_init.send(sender=service)
    assert exc.value.code == 1
    assert service._is_shutdown.is_set()


@pytest.fixture
async def redis_cache() -> Any:
    from sandloom.core.drivers.redis_cache import RedisCacheProvider
    from sandloom.core.testing import reload_test_env

    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)
    url = os.getenv("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not set")
    cache = RedisCacheProvider(
        name="beat-lease-test", driver="redis", options={"url": url}, shared=False
    )
    try:
        client = await cache.raw_client()
        await client.ping()
    except Exception:  # pragma: no cover  # noqa: BLE001 - test Redis unreachable: skip
        pytest.skip("Redis unreachable")
    yield cache
    for key in await client.keys("sandloom:beat:lease:redis-test*"):
        await client.delete(key)
    await cache.aclose()


async def test_two_leases_on_real_redis(redis_cache: Any) -> None:
    """Acquire / standby / release / takeover against the real cache driver."""
    sandloom_cache = redis_cache
    loop = asyncio.get_running_loop()

    def runner(coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=10)

    key = "sandloom:beat:lease:redis-test"
    first = BeatLease(sandloom_cache, key, ttl_seconds=3, runner=runner, identity="a:1")
    second = BeatLease(
        sandloom_cache, key, ttl_seconds=3, runner=runner, identity="b:2"
    )

    assert await asyncio.to_thread(first.ensure) is True
    assert await asyncio.to_thread(second.ensure) is False
    assert await sandloom_cache.get(key) == first.token

    await asyncio.to_thread(first.release)
    assert await sandloom_cache.get(key) is None
    second._retry_at = 0.0  # skip the standby back-off
    assert await asyncio.to_thread(second.ensure) is True
    assert second.fence is not None and first.fence is not None
    assert second.fence > first.fence

    # The holder "dies": its key expires server-side after the TTL.
    await asyncio.sleep(3.2)
    assert await sandloom_cache.get(key) is None
    first._retry_at = 0.0
    assert await asyncio.to_thread(first.ensure) is True
    second._renew_at = 0.0
    assert await asyncio.to_thread(second.ensure) is False  # lost, stops
