"""The ``CacheConformance`` kit against deliberately broken drivers (each breaks
one obligation of spec section 3 and must fail exactly the kit tests that name
it). The kit against the ``redis`` driver runs in its package
(``packages/sandloom-redis/tests/test_redis_cache.py``).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any

import pytest

from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.errors import InvalidArgument, Unsupported
from sandloom.core.infra.ports.cache import CacheProvider
from sandloom.driverkit.testing import CacheConformance


# ----------------------------------------------- the kit bites a broken driver
class _Dict(CacheProvider):
    """A correct driver: a dict, atomic because nothing awaits inside an
    operation. It keeps no TTL (no clock), so the kit's TTL tests are not run."""

    contract_version = 2
    supported_features = frozenset({"cas"})

    def __init__(
        self,
        *,
        name: str = "default",
        driver: str = "dict",
        options: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(name=name, driver=driver, options=options or {})
        self.store: dict[str, str] = {}

    def _check(self, *keys: str, ttl_seconds: int | None = None) -> None:
        if ttl_seconds is not None and ttl_seconds < 1:
            raise InvalidArgument("ttl below 1", capability="cache")
        if any(len(key.encode("utf-8")) > 1024 for key in keys):
            raise InvalidArgument("key too long", capability="cache")

    async def get(self, key: str) -> str | None:
        self._check(key)
        return self.store.get(key)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        self._check(key, ttl_seconds=ttl_seconds)
        self.store[key] = value

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        self._check(key, ttl_seconds=ttl_seconds)
        if key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key: str) -> None:
        self._check(key)
        self.store.pop(key, None)

    async def get_many(self, keys: Sequence[str]) -> list[str | None]:
        self._check(*keys)
        return [self.store.get(key) for key in keys]

    async def set_many(
        self, items: Mapping[str, str], *, ttl_seconds: int | None = None
    ) -> None:
        self._check(*items, ttl_seconds=ttl_seconds)
        self.store.update(items)

    async def windowed_counter(
        self, key: str, previous_key: str, *, ttl_seconds: int
    ) -> tuple[int, str | None]:
        self._check(key, previous_key, ttl_seconds=ttl_seconds)
        return await super().windowed_counter(
            key, previous_key, ttl_seconds=ttl_seconds
        )

    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        self._check(key, ttl_seconds=ttl_seconds)
        try:
            count = int(self.store.get(key, "0")) + 1
        except ValueError:
            raise InvalidArgument(
                "not an integer", capability="cache", instance=self.name
            ) from None
        self.store[key] = str(count)
        return count

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        if self.store.get(key) != expected:
            return False
        self.store[key] = value
        return True

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        if self.store.get(key) != expected:
            return False
        del self.store[key]
        return True

    async def health(self) -> HealthResult:
        return HealthResult(status="ok")

    async def isolation_identity(self) -> str | None:
        return "dict:kit"


class _NonAtomicAdd(_Dict):
    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        self._check(key, ttl_seconds=ttl_seconds)
        absent = key not in self.store
        await asyncio.sleep(0)  # the check and the write are two steps
        if absent:
            self.store[key] = value
        return absent


class _LostUpdateIncr(_Dict):
    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        self._check(key, ttl_seconds=ttl_seconds)
        if not self.store.get(key, "0").isdigit():
            raise InvalidArgument("not an integer", capability="cache")
        current = int(self.store.get(key, "0"))
        await asyncio.sleep(0)
        self.store[key] = str(current + 1)
        return current + 1


class _PreIncrementCount(_Dict):
    async def windowed_counter(
        self, key: str, previous_key: str, *, ttl_seconds: int
    ) -> tuple[int, str | None]:
        count = await self.incr(key, ttl_seconds=ttl_seconds)
        return count - 1, await self.get(previous_key)


class _NoArgumentChecks(_Dict):
    """Stores a key under a TTL of 0 and a key of any length."""

    def _check(self, *keys: str, ttl_seconds: int | None = None) -> None:
        return None


class _CasDeclaredButUnsupported(_Dict):
    async def compare_and_set(self, *args: Any, **kwargs: Any) -> bool:
        raise Unsupported("cas", capability="cache", instance=self.name)


class _CasWorksWithoutTheFeature(_Dict):
    supported_features = frozenset()


class _IncrOverwritesText(_Dict):
    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        self._check(key, ttl_seconds=ttl_seconds)
        count = int(self.store[key]) + 1 if self.store.get(key, "").isdigit() else 1
        self.store[key] = str(count)
        return count


#: The kit tests a dict driver cannot show: it keeps no TTL, and has no
#: backend to fail.
_NOT_RUN = (
    "test_harness_maps_backend_failures_to_infra_errors",
    "test_harness_redacts_the_health_of_a_failing_backend",
    "test_a_key_is_gone_after_its_ttl",
    "test_incr_sets_the_ttl_on_creation_only",
    "test_windowed_counter_sets_the_ttl_on_creation_only",
    "test_compare_and_set_without_a_ttl_removes_the_old_one",
)


class _Kit(CacheConformance):
    pass


for _name in _NOT_RUN:
    setattr(_Kit, _name, None)


async def _failures(driver: type[_Dict]) -> set[str]:
    """The names of the kit tests ``driver`` fails."""
    instance = _Kit()
    failed: set[str] = set()

    def make() -> _Dict:
        return driver()

    for name in sorted(n for n in dir(_Kit) if n.startswith("test_")):
        method = getattr(instance, name)
        if method is None:
            continue
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
        run = asyncio.ensure_future(method(**arguments))
        await asyncio.wait([run])
        outcome = run.exception()
        if outcome is not None and not isinstance(outcome, pytest.skip.Exception):
            failed.add(name)
        if opened is not None:
            with suppress(Exception):
                await opened.aclose()
    return failed


_CASES: dict[type[_Dict], set[str]] = {
    _NonAtomicAdd: {"test_concurrent_adds_have_one_winner"},
    _LostUpdateIncr: {
        "test_concurrent_incrs_lose_no_update",
        "test_concurrent_windowed_counters_lose_no_update",
    },
    _PreIncrementCount: {
        "test_windowed_counter_counts_and_reads_the_previous_window",
        "test_concurrent_windowed_counters_lose_no_update",
    },
    _CasDeclaredButUnsupported: {
        "test_harness_declared_behaviours_do_not_raise_unsupported",
        "test_compare_and_set_swaps_only_on_the_expected_value",
        "test_compare_and_set_races_have_one_winner",
    },
    _CasWorksWithoutTheFeature: {
        "test_harness_undeclared_behaviours_raise_unsupported",
        "test_without_cas_both_calls_raise_unsupported",
    },
    _IncrOverwritesText: {"test_incr_of_a_non_integer_is_invalid"},
    _NoArgumentChecks: {
        "test_a_ttl_below_one_is_invalid",
        "test_a_key_over_1024_bytes_is_invalid",
    },
}


async def test_the_kit_passes_a_driver_that_keeps_the_contract() -> None:
    assert await _failures(_Dict) == set()


@pytest.mark.parametrize("broken", list(_CASES), ids=lambda cls: cls.__name__)
async def test_each_check_fails_exactly_its_broken_driver(
    broken: type[_Dict],
) -> None:
    assert await _failures(broken) == _CASES[broken]
