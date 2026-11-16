"""The ``lease`` capability: the kit against each driver, and the kit against
broken drivers (spec 5).

The kit runs on ``memory`` and ``database`` on SQLite and PostgreSQL
(``settings.database_url``; a scratch database of its own, created for the
run); the ``redis`` driver's kit run lives in ``packages/sandloom-redis/tests``.
Each broken driver must fail the kit test that names its fault, and only that
one. The registry tests that build the ``redis`` beat lease the kernel selects
by default are in ``packages/sandloom-redis/tests/test_lease_registry.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Table, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core.config.settings import settings
from sandloom.core.drivers import database_lease, memory_lease
from sandloom.core.drivers.database_lease import DatabaseLeaseProvider
from sandloom.core.drivers.memory_lease import MemoryLeaseProvider
from sandloom.core.infra.errors import InvalidArgument
from sandloom.core.infra.ports.lease import Lease, LeaseHolder
from sandloom.core.migrations.backup import LEASE_FENCE_OFFSET, lift_fences
from sandloom.core.sql_alchemy.models.lease import InfraLease
from sandloom.driverkit.kits.lease import LeaseConformance

# The harness resolves the URLs into ``settings`` (SANDLOOM_TEST_* on a shared
# machine, DATABASE_URL / CACHE_URL on CI): never read the environment here.
_REDIS_URL = settings.cache_url
_PG_URL = settings.database_url


# ----------------------------------------------------------- the drivers
class TestMemoryLease(LeaseConformance):
    @pytest.fixture
    def make_provider(self) -> Callable[[], MemoryLeaseProvider]:
        return lambda: MemoryLeaseProvider(name="beat", driver="memory", options={})

    # A backend that cannot fail and does not declare ``shared``: these groups do
    # not apply (set to None, so no lane reports them skipped on every lane).
    test_a_second_provider_contends_for_the_same_name = None  # type: ignore[assignment]
    test_harness_redacts_the_health_of_a_failing_backend = None  # type: ignore[assignment]
    test_harness_maps_backend_failures_to_infra_errors = None  # type: ignore[assignment]


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

    # SQLite does not declare ``shared`` (a file on one host): the group does not
    # apply and would be skipped on every lane.
    test_a_second_provider_contends_for_the_same_name = None  # type: ignore[assignment]


@pytest_asyncio.fixture
async def pg_scratch_url() -> AsyncIterator[str]:
    """A scratch PostgreSQL database holding ``infra_lease``, dropped after."""
    if make_url(_PG_URL).get_backend_name() != "postgresql":
        pytest.skip("the database lane is not PostgreSQL (the second-db lane)")
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


@pytest.mark.skipif(
    make_url(_PG_URL).get_backend_name() != "postgresql",
    reason="the database lane is not PostgreSQL (the second-db lane)",
)
class TestDatabaseLeaseOnPostgres(LeaseConformance):
    @pytest.fixture
    def make_provider(self, pg_scratch_url: str) -> Callable[[], DatabaseLeaseProvider]:
        return lambda: DatabaseLeaseProvider(
            name="beat", driver="database", options={"url": pg_scratch_url}
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

    class ExclusiveInsideOneProviderOnly(MemoryLeaseProvider):
        """Declares ``shared`` over one backend and excludes only inside one
        provider (a local lock around a get-then-set): one provider's race
        passes, two providers' race does not."""

        supported_features = frozenset({"shared"})

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._lock = asyncio.Lock()

        async def acquire(
            self, name: str, *, ttl_seconds: int, owner: str
        ) -> Lease | None:
            async with self._lock:
                free = await super().holder(name) is None
                await asyncio.sleep(0)
                if not free:
                    return None
                self._held.pop(name, None)
                return await super().acquire(name, ttl_seconds=ttl_seconds, owner=owner)

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


async def test_a_driver_exclusive_in_one_provider_only_fails_the_shared_race() -> None:
    held: dict[str, Any] = {}
    fences: dict[str, int] = {}

    def make() -> MemoryLeaseProvider:
        provider = _Broken.ExclusiveInsideOneProviderOnly(
            name="beat", driver="memory", options={}
        )
        provider._held, provider._fences = held, fences  # one backend
        return provider

    provider = make()
    await provider.aopen()
    # One provider's race passes: the local lock excludes ...
    await _Kit().test_one_of_many_concurrent_acquirers_wins(provider)
    # ... and the race across two providers of one backend does not.
    with pytest.raises(AssertionError, match="two providers"):
        await _Kit().test_a_second_provider_contends_for_the_same_name(provider, make)
    await provider.aclose()


# ----- the registry (the redis beat lease: packages/sandloom-redis/tests/test_lease_registry.py)


def test_the_database_lease_is_shared_only_on_postgresql() -> None:
    pg = {"url": "postgresql+asyncpg://u:p@h/db"}
    lite = {"url": "sqlite+aiosqlite:///x.db"}
    assert DatabaseLeaseProvider.features_for(pg) == {"shared"}
    assert DatabaseLeaseProvider.features_for(lite) == frozenset()
    assert DatabaseLeaseProvider.features_for({}) == frozenset()
    sqlite_provider = DatabaseLeaseProvider(name="b", driver="database", options=lite)
    assert sqlite_provider.features() == frozenset()


def test_the_kernel_drivers_share_the_limits_of_the_port() -> None:
    from sandloom.core.infra.ports.lease import (
        MAX_LEASE_NAME_LENGTH,
        MIN_LEASE_TTL_SECONDS,
    )
    from sandloom.driverkit import lease_limits

    assert lease_limits.MIN_LEASE_TTL_SECONDS == MIN_LEASE_TTL_SECONDS
    assert lease_limits.MAX_LEASE_NAME_LENGTH == MAX_LEASE_NAME_LENGTH
    for module in (database_lease, memory_lease):
        assert module.MAX_LEASE_NAME_LENGTH is lease_limits.MAX_LEASE_NAME_LENGTH
        assert module.MIN_LEASE_TTL_SECONDS is lease_limits.MIN_LEASE_TTL_SECONDS
        assert not hasattr(module, "_MAX_NAME_LENGTH")
        assert not hasattr(module, "_MIN_TTL_SECONDS")


async def test_the_memory_driver_rejects_a_bad_ttl() -> None:
    provider = MemoryLeaseProvider(name="b", driver="memory", options={})
    with pytest.raises(InvalidArgument):
        await provider.acquire("n", ttl_seconds=2, owner="o")


# ------------------------------------------------- fences after a restore
async def test_a_new_name_starts_above_the_highest_fence(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'fence.db'}"
    await _create_table(url)
    provider = DatabaseLeaseProvider(
        name="beat", driver="database", options={"url": url}
    )
    await provider.aopen()
    try:
        first = await provider.acquire("a", ttl_seconds=30, owner="o")
        assert first is not None
        assert await provider.release(first)
        second = await provider.acquire("a", ttl_seconds=30, owner="o")
        assert second is not None
        assert second.fence == first.fence + 1
        other = await provider.acquire("b", ttl_seconds=30, owner="o")
        assert other is not None
        assert other.fence == second.fence + 1  # not 1: above every fence seen
    finally:
        await provider.aclose()


async def test_lift_fences_lifts_releases_and_new_names_start_above(
    pg_scratch_url: str,
) -> None:
    engine = create_async_engine(pg_scratch_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    provider = DatabaseLeaseProvider(
        name="beat", driver="database", options={"url": pg_scratch_url}
    )
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO infra_lease (name, owner, token, expires_at, fence) "
                    "VALUES ('held', 'a', 't1', now() + interval '1 hour', 5), "
                    "('free', 'b', 't2', now() - interval '1 hour', 9)"
                )
            )
        async with sessions() as session, session.begin():
            dry = await lift_fences(session, apply=False)
            assert [(x.name, x.fence_before, x.held) for x in dry] == [
                ("free", 9, False),
                ("held", 5, True),
            ]
        async with sessions() as session:
            rows = await session.execute(text("SELECT fence FROM infra_lease"))
            assert sorted(r[0] for r in rows) == [5, 9]  # a dry-run writes nothing
        async with sessions() as session, session.begin():
            applied = await lift_fences(session, apply=True)
        assert [x.fence_after for x in applied] == [
            9 + LEASE_FENCE_OFFSET,
            5 + LEASE_FENCE_OFFSET,
        ]
        async with sessions() as session, session.begin():
            after = await lift_fences(session, apply=False)
        assert [(x.fence_before, x.held) for x in after] == [
            (9 + LEASE_FENCE_OFFSET, False),
            (5 + LEASE_FENCE_OFFSET, False),
        ]
        await provider.aopen()
        fresh = await provider.acquire("never-seen", ttl_seconds=30, owner="o")
        assert fresh is not None
        assert fresh.fence > 9 + LEASE_FENCE_OFFSET  # a stale token cannot outrank it
        reacquired = await provider.acquire("held", ttl_seconds=30, owner="o")
        assert reacquired is not None
        assert reacquired.fence == 5 + LEASE_FENCE_OFFSET + 1
    finally:
        await provider.aclose()
        await engine.dispose()


async def test_lift_fences_without_the_table_is_empty(pg_scratch_url: str) -> None:
    engine = create_async_engine(pg_scratch_url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(text("DROP TABLE infra_lease"))
            assert await lift_fences(session, apply=True) == []
    finally:
        await engine.dispose()


async def test_lift_fences_refuses_a_non_positive_offset(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    try:
        async with AsyncSession(engine) as session:
            with pytest.raises(ValueError, match="positive"):
                await lift_fences(session, apply=True, offset=0)
    finally:
        await engine.dispose()
