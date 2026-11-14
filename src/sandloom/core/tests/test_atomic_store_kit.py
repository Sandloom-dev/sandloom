"""The ``AtomicStoreConformance`` kit against deliberately broken drivers.

Each driver below breaks one obligation of the spec (section 4) and must fail
exactly the kit tests that name it; a driver that keeps the contract passes all
of them. This proves each kit check bites, which a driver that passes cannot.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from contextlib import suppress
from typing import Any

import pytest

from sandloom.core.drivers.memory_atomic_store import MemoryAtomicStoreProvider
from sandloom.core.infra.errors import InvalidArgument, Unavailable
from sandloom.core.infra.ports.atomic_store import (
    MAX_SUBJECT_STATE_CHARS,
    AtomicStoreProvider,
    check_key,
    check_subject_state,
    check_ttl,
    check_value,
    parse_subject_state,
)
from sandloom.driverkit.testing import AtomicStoreConformance


class _Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


class _Kit(AtomicStoreConformance):
    value_limit_bytes = 8192
    health_timeout_seconds = 0.5

    async def let_expire(self, provider: AtomicStoreProvider, ttl_seconds: int) -> None:
        clock = provider.clock  # type: ignore[attr-defined]
        clock.offset += ttl_seconds + 1.1


def _build(cls: type[MemoryAtomicStoreProvider]) -> MemoryAtomicStoreProvider:
    built = cls(name="default", driver="memory", options={"max_value_bytes": 8192})
    built.clock = _Clock()
    return built


class _Base(MemoryAtomicStoreProvider):
    """The memory driver's argument checks, for a broken driver to start with."""

    def _args(
        self,
        key: str,
        value: str | None = None,
        ttl_seconds: int | None = None,
    ) -> int:
        check_key(key)
        if value is not None:
            check_value(value, limit=self._limit)
        return check_ttl(ttl_seconds)


class _Broken:
    """One driver per obligation, each a memory driver with one defect."""

    class NonAtomicAdd(_Base):
        async def add(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> bool:
            ttl = self._args(key, value, ttl_seconds)
            if self._live(key) is not None:
                return False
            await asyncio.sleep(0)  # another add runs between the check and the write
            self._write(key, value, ttl)
            return True

    class NonAtomicAddOrGet(_Base):
        async def add_or_get(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> tuple[bool, str | None]:
            ttl = self._args(key, value, ttl_seconds)
            current = self._live(key)
            await asyncio.sleep(0)
            if current is not None:
                return False, current
            self._write(key, value, ttl)
            return True, None

    class LoserSeesNothing(MemoryAtomicStoreProvider):
        async def add_or_get(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> tuple[bool, str | None]:
            created, _ = await super().add_or_get(key, value, ttl_seconds=ttl_seconds)
            return created, None

    class AfterOverwritesBlocked(_Base):
        """Ranks by instant alone: ``after`` can overwrite ``blocked``."""

        async def set_subject_state(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> str:
            check_subject_state(value)
            ttl = self._args(key, None, ttl_seconds)
            current = self._live(key)
            held, ours = parse_subject_state(current), parse_subject_state(value)
            assert ours is not None
            if current is not None and held is not None and held[1] >= ours[1]:
                return current
            self._write(key, value, ttl)
            return value

    class FloatInstants(_Base):
        """Compares the instants as floats."""

        async def set_subject_state(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> str:
            check_subject_state(value)
            ttl = self._args(key, None, ttl_seconds)
            current = self._live(key)
            held, ours = parse_subject_state(current), parse_subject_state(value)
            assert ours is not None
            if current is not None and held is not None:
                theirs = (held[0] == "blocked", float(held[1]))
                if theirs >= (ours[0] == "blocked", float(ours[1])):
                    return current
            self._write(key, value, ttl)
            return value

    class TextInstants(_Base):
        """Compares the instants as text."""

        async def set_subject_state(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> str:
            check_subject_state(value)
            ttl = self._args(key, None, ttl_seconds)
            current = self._live(key)
            if current is not None and current >= value:
                return current
            self._write(key, value, ttl)
            return value

    class CorruptValueKept(MemoryAtomicStoreProvider):
        """Keeps a stored value outside the domain instead of replacing it."""

        async def set_subject_state(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> str:
            current = self._live(key)
            if current is not None and parse_subject_state(current) is None:
                return current
            return await super().set_subject_state(key, value, ttl_seconds=ttl_seconds)

    class AcceptsAnyValue(MemoryAtomicStoreProvider):
        """Writes a value outside the subject-state domain."""

        async def set_subject_state(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> str:
            if parse_subject_state(value) is None and value:
                self._write(key, value, check_ttl(ttl_seconds))
                return value
            return await super().set_subject_state(key, value, ttl_seconds=ttl_seconds)

    class CasIgnoresExpected(_Base):
        async def compare_and_set(
            self,
            key: str,
            expected: str,
            value: str,
            *,
            ttl_seconds: int | None = None,
        ) -> bool:
            ttl = self._args(key, value, ttl_seconds)
            if self._live(key) is None:
                return False
            self._write(key, value, ttl)
            return True

    class AddLosesToExpiredKey(_Base):
        async def add(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> bool:
            ttl = self._args(key, value, ttl_seconds)
            if key in self._rows:  # the row is still there: expiry is not read
                return False
            self._write(key, value, ttl)
            return True

    class CasOnExpiredKey(_Base):
        async def compare_and_set(
            self,
            key: str,
            expected: str,
            value: str,
            *,
            ttl_seconds: int | None = None,
        ) -> bool:
            ttl = self._args(key, value, ttl_seconds)
            row = self._rows.get(key)  # reads the row without its expiry
            if row is None or row[0] != expected:
                return False
            self._write(key, value, ttl)
            return True

    class DeleteOnExpiredKey(_Base):
        async def compare_and_delete(self, key: str, expected: str) -> bool:
            self._args(key, expected)
            row = self._rows.get(key)
            if row is None or row[0] != expected:
                return False
            del self._rows[key]
            return True

    class LosingAddShortensTheTtl(_Base):
        async def add(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> bool:
            ttl = self._args(key, value, ttl_seconds)
            if self._live(key) is not None:
                if ttl:  # the loser rewrites the TTL
                    self._write(key, self._rows[key][0], ttl)
                return False
            self._write(key, value, ttl)
            return True

    class CasKeepsTheOldTtl(_Base):
        async def compare_and_set(
            self,
            key: str,
            expected: str,
            value: str,
            *,
            ttl_seconds: int | None = None,
        ) -> bool:
            self._args(key, value, ttl_seconds)
            row = self._rows.get(key)
            if self._live(key) != expected or row is None:
                return False
            self._rows[key] = (value, row[1])  # ``None`` does not clear the TTL
            return True

    class IgnoresTheTtlRule(_Base):
        async def set(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> None:
            self._args(key, value)
            self._write(key, value, max(ttl_seconds or 0, 0))

    class DeclaresSharedWithoutSharing(MemoryAtomicStoreProvider):
        supported_features = frozenset({"never_evicts", "shared"})

    class DeclaresNeverEvictsAndEvicts(MemoryAtomicStoreProvider):
        """Its backend evicts, and it opens all the same."""

    class AcceptsAnyTtl(_Base):
        """Hands a TTL past the cap to its backend (a 10**20 s one)."""

        async def set(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> None:
            check_key(key)
            check_value(value, limit=self._limit)
            if ttl_seconds is not None and ttl_seconds >= 1:
                self._write(key, value, ttl_seconds)
            else:
                self._write(key, value, check_ttl(ttl_seconds))

    class UnboundedSubjectState(MemoryAtomicStoreProvider):
        """Matches a subject state of any length."""

        async def set_subject_state(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> str:
            if len(value) > MAX_SUBJECT_STATE_CHARS:
                return value  # a real driver would scan, or store, the long value
            return await super().set_subject_state(key, value, ttl_seconds=ttl_seconds)

    class NeverEvictsNotDevelopmentOnly(MemoryAtomicStoreProvider):
        """Declares ``never_evicts`` as a production driver: the kit needs an
        evicting provider of it, and none is given."""

        development_only = False

    class RefusesAnEvictingBackend(MemoryAtomicStoreProvider):
        """What a driver does on an evicting backend: ``aopen`` raises
        ``InvalidArgument`` and every operation after it raises the same."""

        _refused = False

        async def aopen(self) -> None:
            self._refused = True
            raise InvalidArgument("evicts", capability="atomic_store")

        async def get(self, key: str) -> str | None:
            if self._refused:
                raise InvalidArgument("evicts", capability="atomic_store")
            return await super().get(key)

        async def set(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> None:
            if self._refused:
                raise InvalidArgument("evicts", capability="atomic_store")
            await super().set(key, value, ttl_seconds=ttl_seconds)

        async def add(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> bool:
            if self._refused:
                raise InvalidArgument("evicts", capability="atomic_store")
            return await super().add(key, value, ttl_seconds=ttl_seconds)

        async def add_or_get(
            self, key: str, value: str, *, ttl_seconds: int | None = None
        ) -> tuple[bool, str | None]:
            if self._refused:
                raise InvalidArgument("evicts", capability="atomic_store")
            return await super().add_or_get(key, value, ttl_seconds=ttl_seconds)

    class RefusesAsAnOutage(RefusesAnEvictingBackend):
        """The policy refusal is an ``Unavailable``: not the refusal the kernel
        reads (a transient outage is retried and degrades, never stops)."""

        async def aopen(self) -> None:
            raise Unavailable("evicts", capability="atomic_store")

    class RefusesButServes(MemoryAtomicStoreProvider):
        """``aopen`` refuses, and the operations then run on the evicting store."""

        async def aopen(self) -> None:
            raise InvalidArgument("evicts", capability="atomic_store")


#: What each broken driver must fail: the kit tests that name its defect.
_CASES: dict[str, set[str]] = {
    "NonAtomicAdd": {"test_concurrent_adds_have_one_winner"},
    # ``add`` of the memory driver is an ``add_or_get``, so both races see it.
    "NonAtomicAddOrGet": {
        "test_concurrent_add_or_gets_have_one_creator",
        "test_concurrent_adds_have_one_winner",
    },
    "LoserSeesNothing": {
        "test_add_or_get_creates_then_reports_the_holder",
        "test_concurrent_add_or_gets_have_one_creator",
        "test_a_losing_write_never_shortens_the_ttl",
    },
    "AfterOverwritesBlocked": {
        "test_subject_state_blocked_outranks_after",
        "test_concurrent_subject_state_writers_end_at_the_maximum",
    },
    "FloatInstants": {"test_subject_state_ranks_instants_past_the_15th_digit"},
    "TextInstants": {
        "test_subject_state_compares_numbers_not_text",
        "test_subject_state_ignores_a_lower_instant",
        "test_subject_state_replaces_a_corrupt_stored_value",
    },
    "CorruptValueKept": {"test_subject_state_replaces_a_corrupt_stored_value"},
    "AcceptsAnyValue": {
        "test_subject_state_refuses_a_value_outside_the_domain",
        "test_a_subject_state_over_its_bound_is_too_large",
    },
    "CasIgnoresExpected": {
        "test_compare_and_set_swaps_only_on_the_expected_value",
        "test_compare_and_set_races_have_one_winner",
        "test_a_losing_write_never_shortens_the_ttl",
    },
    "AddLosesToExpiredKey": {"test_add_over_an_expired_key_creates"},
    "CasOnExpiredKey": {"test_compare_and_set_on_an_expired_key_is_false"},
    "DeleteOnExpiredKey": {"test_compare_and_delete_on_an_expired_key_is_false"},
    "LosingAddShortensTheTtl": {"test_a_losing_write_never_shortens_the_ttl"},
    "CasKeepsTheOldTtl": {"test_compare_and_set_without_a_ttl_removes_the_old_one"},
    "IgnoresTheTtlRule": {"test_a_ttl_outside_the_bounds_is_invalid"},
    "AcceptsAnyTtl": {"test_a_ttl_outside_the_bounds_is_invalid"},
    "UnboundedSubjectState": {"test_a_subject_state_over_its_bound_is_too_large"},
}


async def _failures(
    driver: type[MemoryAtomicStoreProvider],
    evicting: type[MemoryAtomicStoreProvider] | None = None,
    *,
    assume: bool = False,
) -> set[str]:
    """Run every test of the kit against ``driver`` as pytest would (a fresh
    opened provider per test) and answer the names of those that failed."""
    instance = _Kit()
    failed: set[str] = set()

    def make() -> MemoryAtomicStoreProvider:
        return _build(driver)

    make_evicting = (lambda: _build(evicting)) if evicting is not None else None
    make_assume = (
        (lambda: _build(evicting)) if assume and evicting is not None else None
    )
    for name in sorted(n for n in dir(_Kit) if n.startswith("test_")):
        method = getattr(instance, name)
        parameters = inspect.signature(method).parameters
        opened = None
        arguments: dict[str, Any] = {}
        if "provider" in parameters:
            opened = make()
            await opened.aopen()
            arguments["provider"] = opened
        if "make_provider" in parameters:
            arguments["make_provider"] = make
        if "make_failing_provider" in parameters:
            arguments["make_failing_provider"] = None
        if "make_evicting_provider" in parameters:
            arguments["make_evicting_provider"] = make_evicting
        if "make_evicting_assume_provider" in parameters:
            arguments["make_evicting_assume_provider"] = make_assume
        run = asyncio.ensure_future(method(**arguments))
        await asyncio.wait([run])
        outcome = run.exception()
        if outcome is not None and not isinstance(outcome, pytest.skip.Exception):
            failed.add(name)
        if opened is not None:
            with suppress(Exception):
                await opened.aclose()
    return failed


async def test_the_kit_passes_a_driver_that_keeps_the_contract() -> None:
    assert await _failures(MemoryAtomicStoreProvider) == set()


@pytest.mark.parametrize("broken", sorted(_CASES))
async def test_each_check_fails_exactly_its_broken_driver(broken: str) -> None:
    driver = getattr(_Broken, broken)
    assert await _failures(driver) == _CASES[broken]


async def test_a_shared_driver_whose_providers_share_nothing_fails_the_shared_group() -> (
    None
):
    failed = await _failures(_Broken.DeclaresSharedWithoutSharing)
    assert failed == {"test_shared_two_providers_of_one_store_see_each_other"}


async def test_a_never_evicts_driver_that_opens_on_an_evicting_backend_fails() -> None:
    failed = await _failures(
        MemoryAtomicStoreProvider, evicting=_Broken.DeclaresNeverEvictsAndEvicts
    )
    assert failed == {"test_never_evicts_the_driver_refuses_an_evicting_backend"}


_REFUSAL = "test_never_evicts_the_driver_refuses_an_evicting_backend"
_VOUCHED = "test_never_evicts_the_refusal_holds_when_the_operator_vouches"


async def test_a_driver_that_refuses_an_evicting_backend_and_stops_passes() -> None:
    failed = await _failures(
        MemoryAtomicStoreProvider, _Broken.RefusesAnEvictingBackend, assume=True
    )
    assert failed == set()


@pytest.mark.parametrize("broken", ["RefusesAsAnOutage", "RefusesButServes"])
async def test_a_refusal_that_is_not_the_policy_refusal_fails(broken: str) -> None:
    failed = await _failures(
        MemoryAtomicStoreProvider, getattr(_Broken, broken), assume=True
    )
    assert failed == {_REFUSAL, _VOUCHED}


async def test_a_production_driver_with_no_evicting_provider_fails_not_skips() -> None:
    failed = await _failures(_Broken.NeverEvictsNotDevelopmentOnly)
    assert failed == {_REFUSAL}
