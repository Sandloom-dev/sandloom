"""The ``lease`` capability: the kit against each driver, and the kit against
broken drivers (spec 5).

The kit runs on ``memory``, ``redis`` (a real Redis: ``SANDLOOM_TEST_CACHE_URL``)
and ``database`` on SQLite and PostgreSQL (``SANDLOOM_TEST_DATABASE_URL``; a
scratch database of its own, created for the run). Each broken driver must fail
the kit test that names its fault, and only that one.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Table, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sandloom.core.drivers.bootstrap import (
    build_infra_registry,
    with_default_beat_lease,
)
from sandloom.core.drivers.database_lease import DatabaseLeaseProvider
from sandloom.core.drivers.memory_lease import MemoryLeaseProvider
from sandloom.core.drivers.redis_lease import RedisLeaseProvider
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.errors import InvalidArgument, PermissionDenied
from sandloom.core.infra.ports.lease import Lease, LeaseHolder
from sandloom.core.infra.registry import DriverLoadError
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.sql_alchemy.models.lease import InfraLease
from sandloom.driverkit.kits.lease import LeaseConformance

_REDIS_URL = os.getenv("SANDLOOM_TEST_CACHE_URL")
_PG_URL = os.getenv("SANDLOOM_TEST_DATABASE_URL")


# ----------------------------------------------------------- the drivers
class TestMemoryLease(LeaseConformance):
    @pytest.fixture
    def make_provider(self) -> Callable[[], MemoryLeaseProvider]:
        return lambda: MemoryLeaseProvider(name="beat", driver="memory", options={})


@pytest.mark.skipif(not _REDIS_URL, reason="SANDLOOM_TEST_CACHE_URL is not set")
class TestRedisLease(LeaseConformance):
    @pytest.fixture
    def make_provider(self) -> Callable[[], RedisLeaseProvider]:
        return lambda: RedisLeaseProvider(
            name="beat", driver="redis", options={"url": _REDIS_URL}
        )

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], RedisLeaseProvider]:
        url = f"redis://:{self.leak_marker}@127.0.0.1:1/0"
        return lambda: RedisLeaseProvider(
            name="beat",
            driver="redis",
            options={"url": url, "socket_connect_timeout_seconds": 0.2},
        )


async def _create_table(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            table: Table = InfraLease.__table__  # type: ignore[assignment]
            await conn.run_sync(table.create, checkfirst=True)
    finally:
        await engine.dispose()


class TestDatabaseLeaseOnSqlite(LeaseConformance):
    @pytest_asyncio.fixture
    async def sqlite_url(self, tmp_path: Path) -> str:
        url = f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}"
        await _create_table(url)
        return url

    @pytest.fixture
    def make_provider(self, sqlite_url: str) -> Callable[[], DatabaseLeaseProvider]:
        return lambda: DatabaseLeaseProvider(
            name="beat", driver="database", options={"url": sqlite_url}
        )

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], DatabaseLeaseProvider]:
        url = f"sqlite+aiosqlite:////no/such/dir/{self.leak_marker}/lease.db"
        return lambda: DatabaseLeaseProvider(
            name="beat", driver="database", options={"url": url}
        )


@pytest.mark.skipif(not _PG_URL, reason="SANDLOOM_TEST_DATABASE_URL is not set")
class TestDatabaseLeaseOnPostgres(LeaseConformance):
    @pytest_asyncio.fixture
    async def scratch_url(self) -> AsyncIterator[str]:
        assert _PG_URL is not None
        admin_url = make_url(_PG_URL)
        scratch = f"lease_kit_{uuid.uuid4().hex[:10]}"
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{scratch}"'))
        url = admin_url.set(database=scratch).render_as_string(hide_password=False)
        try:
            await _create_table(url)
            yield url
        finally:
            async with admin.connect() as conn:
                await conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
                )
            await admin.dispose()

    @pytest.fixture
    def make_provider(self, scratch_url: str) -> Callable[[], DatabaseLeaseProvider]:
        return lambda: DatabaseLeaseProvider(
            name="beat", driver="database", options={"url": scratch_url}
        )

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], DatabaseLeaseProvider]:
        url = f"postgresql+asyncpg://user:{self.leak_marker}@127.0.0.1:1/none"
        return lambda: DatabaseLeaseProvider(
            name="beat", driver="database", options={"url": url}
        )


# ----------------------------------------------- the kit against broken drivers
class _Broken:
    """One broken driver per obligation (each a memory lease with one fault)."""

    class RepeatsTheFenceAfterRelease(MemoryLeaseProvider):
        async def release(self, lease: Lease) -> bool:
            ended = await super().release(lease)
            self._fences[lease.name] = 0  # a restarting counter
            return ended

    class RenewIgnoresTheToken(MemoryLeaseProvider):
        async def renew(self, lease: Lease) -> bool:
            held = self._held.get(lease.name)
            if held is None or held.expires_at <= self._clock():
                return False
            held.expires_at = self._clock() + lease.ttl_seconds
            return True

    class ReleaseIgnoresTheToken(MemoryLeaseProvider):
        async def release(self, lease: Lease) -> bool:
            held = self._held.pop(lease.name, None)
            return held is not None

    class AcquireIsNotExclusive(MemoryLeaseProvider):
        async def acquire(
            self, name: str, *, ttl_seconds: int, owner: str
        ) -> Lease | None:
            # a get-then-set with a suspension point between them
            import asyncio

            free = await super().holder(name) is None
            await asyncio.sleep(0)
            if not free:
                return None
            self._held.pop(name, None)
            return await super().acquire(name, ttl_seconds=ttl_seconds, owner=owner)

    class ExpiryNeverHandsOver(MemoryLeaseProvider):
        async def acquire(
            self, name: str, *, ttl_seconds: int, owner: str
        ) -> Lease | None:
            if name in self._held:  # forgets that the TTL passed
                return None
            return await super().acquire(name, ttl_seconds=ttl_seconds, owner=owner)

    class RenewDoesNotExtend(MemoryLeaseProvider):
        async def renew(self, lease: Lease) -> bool:
            held = self._held.get(lease.name)
            return held is not None and held.token == lease.token

    class RenewBumpsTheFence(MemoryLeaseProvider):
        async def renew(self, lease: Lease) -> bool:
            kept = await super().renew(lease)
            if kept:
                self._held[lease.name].fence += 1
            return kept

    class HolderForgetsTheOwner(MemoryLeaseProvider):
        async def holder(self, name: str) -> LeaseHolder | None:
            found = await super().holder(name)
            return None if found is None else LeaseHolder(owner="", fence=found.fence)

    class HolderReadsAnExpiredLease(MemoryLeaseProvider):
        async def holder(self, name: str) -> LeaseHolder | None:
            held = self._held.get(name)
            return None if held is None else LeaseHolder(held.owner, held.fence)

    class AcceptsAShortTtl(MemoryLeaseProvider):
        async def acquire(
            self, name: str, *, ttl_seconds: int, owner: str
        ) -> Lease | None:
            return await super().acquire(
                name, ttl_seconds=max(ttl_seconds, 3), owner=owner
            )

    class GrantsAHeldName(MemoryLeaseProvider):
        async def acquire(
            self, name: str, *, ttl_seconds: int, owner: str
        ) -> Lease | None:
            self._held.pop(name, None)  # takes the lease over from its holder
            return await super().acquire(name, ttl_seconds=ttl_seconds, owner=owner)

    class GrantsALeaseOfAnotherOwner(MemoryLeaseProvider):
        async def acquire(
            self, name: str, *, ttl_seconds: int, owner: str
        ) -> Lease | None:
            granted = await super().acquire(name, ttl_seconds=ttl_seconds, owner=owner)
            return None if granted is None else replace(granted, owner="someone-else")

    class OneLeaseForEveryName(MemoryLeaseProvider):
        async def acquire(
            self, name: str, *, ttl_seconds: int, owner: str
        ) -> Lease | None:
            granted = await super().acquire("all", ttl_seconds=ttl_seconds, owner=owner)
            return None if granted is None else replace(granted, name=name)

        async def release(self, lease: Lease) -> bool:
            return await super().release(replace(lease, name="all"))

        async def holder(self, name: str) -> LeaseHolder | None:
            return await super().holder("all")

    class ReleaseAnswersTrueTwice(MemoryLeaseProvider):
        async def release(self, lease: Lease) -> bool:
            await super().release(lease)
            return True

    class ReusesTheToken(MemoryLeaseProvider):
        async def acquire(
            self, name: str, *, ttl_seconds: int, owner: str
        ) -> Lease | None:
            granted = await super().acquire(name, ttl_seconds=ttl_seconds, owner=owner)
            if granted is None:
                return None
            self._held[name].token = "the-one-token"
            return replace(granted, token="the-one-token")

    class SharedInName(MemoryLeaseProvider):
        """Declares ``shared`` but keeps its leases in the provider."""

        supported_features = frozenset({"shared"})

    class AcceptsAnEmptyName(MemoryLeaseProvider):
        async def acquire(
            self, name: str, *, ttl_seconds: int, owner: str
        ) -> Lease | None:
            return await super().acquire(
                name or "x", ttl_seconds=ttl_seconds, owner=owner
            )


class _Kit(LeaseConformance):
    """The kit with a memory provider swapped in by each test."""


async def _fails(broken: type[MemoryLeaseProvider], test: str) -> bool:
    """Whether kit test ``test`` fails on ``broken`` (an assertion, not an error)."""
    provider = broken(name="beat", driver="memory", options={})
    await provider.aopen()
    try:
        await getattr(_Kit(), f"test_{test}")(provider)
    except AssertionError:
        return True
    except Failed:
        return True
    finally:
        await provider.aclose()
    return False


Failed = pytest.fail.Exception

_FAULTS: list[tuple[type[MemoryLeaseProvider], str]] = [
    (_Broken.RepeatsTheFenceAfterRelease, "the_fence_grows_across_release"),
    (
        _Broken.RepeatsTheFenceAfterRelease,
        "release_hands_over_at_once_with_a_greater_fence",
    ),
    (_Broken.RenewIgnoresTheToken, "renew_by_a_stale_token_answers_false"),
    (_Broken.RenewIgnoresTheToken, "renew_by_a_displaced_holder_answers_false"),
    (_Broken.ReleaseIgnoresTheToken, "release_by_a_stale_token_is_a_noop"),
    (_Broken.AcquireIsNotExclusive, "one_of_many_concurrent_acquirers_wins"),
    (_Broken.ExpiryNeverHandsOver, "expiry_hands_over_with_a_greater_fence"),
    (_Broken.RenewDoesNotExtend, "renew_extends_past_the_original_ttl"),
    (_Broken.RenewBumpsTheFence, "renew_keeps_the_fence"),
    (_Broken.HolderForgetsTheOwner, "holder_reports_owner_and_fence"),
    (_Broken.HolderReadsAnExpiredLease, "expiry_hands_over_with_a_greater_fence"),
    (_Broken.GrantsAHeldName, "a_second_acquire_of_a_held_name_answers_none"),
    (_Broken.GrantsAHeldName, "one_of_many_concurrent_acquirers_wins"),
    (_Broken.GrantsALeaseOfAnotherOwner, "acquire_grants_a_free_name"),
    (_Broken.OneLeaseForEveryName, "names_are_independent"),
    (_Broken.ReleaseAnswersTrueTwice, "release_twice_answers_false_the_second_time"),
    (_Broken.ReusesTheToken, "release_hands_over_at_once_with_a_greater_fence"),
]


@pytest.mark.parametrize(("broken", "test"), _FAULTS)
async def test_each_broken_driver_fails_the_test_that_names_its_fault(
    broken: type[MemoryLeaseProvider], test: str
) -> None:
    assert await _fails(broken, test), f"{broken.__name__} passed {test}"


@pytest.mark.parametrize(
    ("broken", "test"),
    [
        (
            _Broken.RepeatsTheFenceAfterRelease,
            "a_second_acquire_of_a_held_name_answers_none",
        ),
        (_Broken.RenewIgnoresTheToken, "the_fence_grows_across_release"),
        (_Broken.AcquireIsNotExclusive, "a_second_acquire_of_a_held_name_answers_none"),
        (_Broken.RenewBumpsTheFence, "release_by_a_stale_token_is_a_noop"),
    ],
)
async def test_a_broken_driver_passes_the_tests_of_what_it_keeps(
    broken: type[MemoryLeaseProvider], test: str
) -> None:
    assert not await _fails(broken, test)


@pytest.mark.parametrize(
    ("broken", "kit_test"),
    [
        (_Broken.AcceptsAShortTtl, "a_ttl_below_the_minimum_raises"),
        (_Broken.AcceptsAnEmptyName, "an_empty_or_overlong_name_raises"),
    ],
)
async def test_a_driver_that_accepts_bad_arguments_fails_the_kit(
    broken: type[MemoryLeaseProvider], kit_test: str
) -> None:
    provider = broken(name="beat", driver="memory", options={})
    with pytest.raises(pytest.fail.Exception):
        await getattr(_Kit(), f"test_{kit_test}")(provider)


async def test_a_driver_declaring_shared_but_not_sharing_fails_the_kit() -> None:
    def make() -> MemoryLeaseProvider:
        return _Broken.SharedInName(name="beat", driver="memory", options={})

    provider = make()
    await provider.aopen()
    with pytest.raises(AssertionError, match="shared"):
        await _Kit().test_a_second_provider_contends_for_the_same_name(provider, make)
    await provider.aclose()


# ------------------------------------------------------------- the registry
def test_the_three_lease_drivers_are_registered_and_deliver() -> None:
    registry = build_infra_registry(InMemorySchemaRegistry(), load_plugins=False)
    for driver in ("redis", "memory", "database"):
        registration = registry._registration("lease", driver)
        assert registration is not None
        assert not registration.delivered_by


def test_the_kernel_adds_the_beat_lease_instance_when_infra_names_none() -> None:
    config: dict[str, dict[str, InstanceConfig]] = {}
    with_default_beat_lease(config)
    beat = config["lease"]["beat"]
    assert (beat.driver, beat.implicit) == ("redis", True)
    explicit = InstanceConfig("lease", "beat", "database", {"url": "x"})
    named: dict[str, dict[str, InstanceConfig]] = {"lease": {"beat": explicit}}
    assert with_default_beat_lease(named)["lease"]["beat"] is explicit


def test_the_default_registry_builds_the_beat_lease_on_the_cache_url() -> None:
    from sandloom.core.config.settings import settings

    registry = build_infra_registry(InMemorySchemaRegistry(), load_plugins=False)
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
        InMemorySchemaRegistry(), settings=settings, config=config, load_plugins=False
    )
    return registry.provider("lease", "beat")


def test_assume_in_production_needs_the_accepted_risk() -> None:
    options = {"url": "redis://h/0", "never_evicts_check": "assume"}
    with pytest.raises(DriverLoadError, match="unverified_noeviction"):
        _beat_redis(_prod(), **options)
    built = _beat_redis(_prod("unverified_noeviction"), **options)
    assert isinstance(built, RedisLeaseProvider)


def test_verify_in_production_needs_no_risk() -> None:
    assert isinstance(_beat_redis(_prod(), url="redis://h/0"), RedisLeaseProvider)


async def test_a_non_noeviction_server_is_refused() -> None:
    provider = RedisLeaseProvider(
        name="beat", driver="redis", options={"url": "redis://h/0"}
    )

    class _Client:
        async def config_get(self, _: str) -> dict[str, str]:
            return {"maxmemory-policy": "allkeys-lru"}

    with pytest.raises(PermissionDenied, match="allkeys-lru"):
        await provider._policy_refused_read(_Client())


async def test_the_driver_options_reject_unknown_keys_and_bad_ttl() -> None:
    with pytest.raises(ValueError):
        RedisLeaseProvider(name="b", driver="redis", options={"ur1": "x"})
    provider = MemoryLeaseProvider(name="b", driver="memory", options={})
    with pytest.raises(InvalidArgument):
        await provider.acquire("n", ttl_seconds=2, owner="o")
