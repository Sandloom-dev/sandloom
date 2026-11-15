"""``CacheConformance``: what a ``cache`` driver must do (spec 3).

The cache may evict and may lose data, so the kit never asserts that a key
survives its TTL, only what the operations promise while it lives: a round
trip, an atomic ``add`` and ``incr`` (a race of 50 callers of one key, whose
outcomes only an atomic driver can produce), a TTL set on creation only,
``windowed_counter`` and, for a driver that declares ``cas``, the swap group.
A driver without ``cas`` must raise ``Unsupported("cas")`` from both calls.

A driver's tests subclass it and override ``make_provider`` (an unopened
provider on a test backend). Expiry is waited out with
:meth:`CacheConformance.let_expire` (real time by default; a driver with a clock
overrides it). A driver that does not declare ``cas`` sets the ``cas`` group's
tests to ``None`` in its class; a driver that declares it sets
``test_without_cas_both_calls_raise_unsupported`` to ``None``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import cast
from uuid import uuid4

import pytest

from sandloom.core.infra.errors import InvalidArgument, Unsupported
from sandloom.core.infra.ports.cache import CACHE_CAPABILITY, Cache, CacheProvider
from sandloom.driverkit.kits._harness import Call, ProviderConformance, require

#: Concurrent callers of one key in a race.
_RACERS = 50


def _key(label: str) -> str:
    return f"kit:{uuid4().hex}:{label}"


async def _race(calls: list[Callable[[], Awaitable[object]]]) -> list[object]:
    """Run ``calls`` concurrently, every one started before any is awaited."""
    return list(await asyncio.gather(*(call() for call in calls)))


@pytest.mark.asyncio
class CacheConformance(ProviderConformance[CacheProvider]):
    """What a ``cache`` driver must do, after the harness's checks."""

    contract = CacheProvider
    protocol = Cache
    spec = CACHE_CAPABILITY

    async def let_expire(self, provider: CacheProvider, ttl_seconds: int) -> None:
        """Wait until a key written with ``ttl_seconds`` is past its TTL (a key
        is not readable after ``ttl_seconds + 1`` s). Real time by default: a
        driver with a clock it controls overrides this to advance it."""
        await asyncio.sleep(ttl_seconds + 1.1)

    def feature_calls(self, provider: CacheProvider) -> dict[str, Call]:
        key = _key("cas-call")
        return {"cas": lambda: provider.compare_and_set(key, "a", "b")}

    def operation_calls(self, provider: CacheProvider) -> dict[str, Call]:
        key = _key("failing")
        return {
            "get": lambda: provider.get(key),
            "set": lambda: provider.set(key, "v", ttl_seconds=5),
            "delete": lambda: provider.delete(key),
            "add": lambda: provider.add(key, "v", ttl_seconds=5),
            "incr": lambda: provider.incr(key, ttl_seconds=5),
            "get_many": lambda: provider.get_many([key]),
            "set_many": lambda: provider.set_many({key: "v"}, ttl_seconds=5),
            "windowed_counter": lambda: provider.windowed_counter(
                key, key + ":previous", ttl_seconds=5
            ),
        }

    # ------------------------------------------------------------ values
    async def test_absent_key_reads_none(self, provider: CacheProvider) -> None:
        require(await provider.get(_key("absent")) is None, "an absent key is None")

    async def test_set_get_overwrite_delete(self, provider: CacheProvider) -> None:
        key = _key("round-trip")
        await provider.set(key, "one")
        require(await provider.get(key) == "one", "a stored value reads back")
        await provider.set(key, "two")
        require(await provider.get(key) == "two", "set overwrites")
        await provider.delete(key)
        require(await provider.get(key) is None, "delete removes the key")
        await provider.delete(key)  # a delete of an absent key is not an error

    async def test_values_round_trip_exactly(self, provider: CacheProvider) -> None:
        key = _key("exact")
        for value in ("", " padded ", "żółw 🐢", "line\nbreak", "0123456789" * 100):
            await provider.set(key, value)
            require(await provider.get(key) == value, f"{value!r} did not round trip")

    async def test_a_key_is_gone_after_its_ttl(self, provider: CacheProvider) -> None:
        key = _key("ttl")
        await provider.set(key, "v", ttl_seconds=1)
        await self.let_expire(provider, 1)
        require(await provider.get(key) is None, "a key past ttl_seconds + 1 s is read")

    async def test_get_many_and_set_many_keep_the_order(
        self, provider: CacheProvider
    ) -> None:
        first, missing, last = _key("many-a"), _key("many-missing"), _key("many-z")
        await provider.set_many({first: "1", last: "3"}, ttl_seconds=30)
        require(
            await provider.get_many([last, missing, first]) == ["3", None, "1"],
            "get_many answers in the order asked, None for an absent key",
        )
        require(await provider.get_many([]) == [], "no keys, no values")

    # --------------------------------------------------------------- add
    async def test_add_creates_only_an_absent_key(
        self, provider: CacheProvider
    ) -> None:
        key = _key("add")
        require(await provider.add(key, "first", ttl_seconds=30) is True, "created")
        require(await provider.add(key, "second", ttl_seconds=30) is False, "exists")
        require(await provider.get(key) == "first", "a lost add keeps the value")

    async def test_concurrent_adds_have_one_winner(
        self, provider: CacheProvider
    ) -> None:
        key = _key("add-race")
        answers = await _race(
            [
                partial(provider.add, key, f"racer-{i}", ttl_seconds=30)
                for i in range(_RACERS)
            ]
        )
        winners = [i for i, won in enumerate(answers) if won is True]
        require(len(winners) == 1, f"{len(winners)} of {_RACERS} adds won, not one")
        require(
            await provider.get(key) == f"racer-{winners[0]}",
            "the stored value is the winner's",
        )

    # -------------------------------------------------------------- incr
    async def test_incr_counts_from_one(self, provider: CacheProvider) -> None:
        key = _key("incr")
        counts = [await provider.incr(key, ttl_seconds=30) for _ in range(3)]
        require(counts == [1, 2, 3], f"incr answered {counts}, not [1, 2, 3]")
        require(await provider.get(key) == "3", "the counter reads as its decimal")

    async def test_concurrent_incrs_lose_no_update(
        self, provider: CacheProvider
    ) -> None:
        key = _key("incr-race")
        counts = await _race(
            [partial(provider.incr, key, ttl_seconds=30) for _ in range(_RACERS)]
        )
        require(
            sorted(cast("list[int]", counts)) == list(range(1, _RACERS + 1)),
            "the racing incrs did not answer 1..50, each once",
        )
        require(await provider.get(key) == str(_RACERS), "an update was lost")

    async def test_incr_sets_the_ttl_on_creation_only(
        self, provider: CacheProvider
    ) -> None:
        key = _key("incr-ttl")
        require(await provider.incr(key, ttl_seconds=1) == 1, "created")
        require(await provider.incr(key, ttl_seconds=600) == 2, "incremented")
        await self.let_expire(provider, 1)
        require(
            await provider.get(key) is None,
            "a later incr extended the TTL the creating incr set",
        )

    async def test_incr_of_a_non_integer_is_invalid(
        self, provider: CacheProvider
    ) -> None:
        key = _key("incr-text")
        await provider.set(key, "not a number")
        with pytest.raises(InvalidArgument):
            await provider.incr(key, ttl_seconds=30)
        require(
            await provider.get(key) == "not a number", "a refused incr changes nothing"
        )

    # ----------------------------------------------------------- arguments
    async def test_a_ttl_below_one_is_invalid(self, provider: CacheProvider) -> None:
        """``None`` is "no expiry"; ``0`` or a negative TTL is never that."""
        key = _key("bad-ttl")
        for ttl in (0, -1):
            calls: dict[str, Call] = {
                "set": partial(provider.set, key, "v", ttl_seconds=ttl),
                "add": partial(provider.add, key, "v", ttl_seconds=ttl),
                "incr": partial(provider.incr, key, ttl_seconds=ttl),
                "set_many": partial(provider.set_many, {key: "v"}, ttl_seconds=ttl),
                "windowed_counter": partial(
                    provider.windowed_counter, key, key + ":p", ttl_seconds=ttl
                ),
            }
            for name, call in calls.items():
                with pytest.raises(InvalidArgument):
                    await call()
                require(
                    await provider.get(key) is None,
                    f"{name} with ttl_seconds={ttl} stored a key",
                )

    async def test_a_key_over_1024_bytes_is_invalid(
        self, provider: CacheProvider
    ) -> None:
        longest = "k" * 1024
        await provider.set(longest, "v", ttl_seconds=30)  # at the limit: valid
        await provider.delete(longest)
        for key in ("k" * 1025, "é" * 513):  # 1025 bytes; 1026 bytes
            calls: dict[str, Call] = {
                "get": partial(provider.get, key),
                "set": partial(provider.set, key, "v", ttl_seconds=30),
                "delete": partial(provider.delete, key),
                "add": partial(provider.add, key, "v", ttl_seconds=30),
                "incr": partial(provider.incr, key, ttl_seconds=30),
                "get_many": partial(provider.get_many, [key]),
                "set_many": partial(provider.set_many, {key: "v"}, ttl_seconds=30),
            }
            for call in calls.values():
                with pytest.raises(InvalidArgument):
                    await call()

    # -------------------------------------------------- windowed_counter
    async def test_windowed_counter_counts_and_reads_the_previous_window(
        self, provider: CacheProvider
    ) -> None:
        key, previous = _key("window"), _key("window-previous")
        require(
            await provider.windowed_counter(key, previous, ttl_seconds=30) == (1, None),
            "the first call counts 1 and finds no previous window",
        )
        await provider.set(previous, "7", ttl_seconds=30)
        require(
            await provider.windowed_counter(key, previous, ttl_seconds=30) == (2, "7"),
            "the count is the post-increment value, the previous one as stored",
        )
        require(await provider.get(key) == "2", "the counter is the plain incr key")

    async def test_windowed_counter_sets_the_ttl_on_creation_only(
        self, provider: CacheProvider
    ) -> None:
        key, previous = _key("window-ttl"), _key("window-ttl-previous")
        await provider.windowed_counter(key, previous, ttl_seconds=1)
        await provider.windowed_counter(key, previous, ttl_seconds=600)
        await self.let_expire(provider, 1)
        require(await provider.get(key) is None, "a later call extended the TTL")

    async def test_concurrent_windowed_counters_lose_no_update(
        self, provider: CacheProvider
    ) -> None:
        key, previous = _key("window-race"), _key("window-race-previous")
        answers = await _race(
            [
                partial(provider.windowed_counter, key, previous, ttl_seconds=30)
                for _ in range(_RACERS)
            ]
        )
        counts = sorted(cast("tuple[int, str | None]", a)[0] for a in answers)
        require(counts == list(range(1, _RACERS + 1)), "a count was lost or repeated")

    # ------------------------------------------------------- cas group
    async def test_compare_and_set_swaps_only_on_the_expected_value(
        self, provider: CacheProvider
    ) -> None:
        self.requires_feature(provider, "cas")
        key = _key("cas")
        require(
            await provider.compare_and_set(key, "a", "b") is False,
            "an absent key never swaps",
        )
        require(await provider.get(key) is None, "a refused swap creates nothing")
        await provider.set(key, "a")
        require(
            await provider.compare_and_set(key, "other", "b") is False,
            "a wrong expected value does not swap",
        )
        require(await provider.get(key) == "a", "a refused swap keeps the value")
        require(await provider.compare_and_set(key, "a", "b") is True, "swap on match")
        require(await provider.get(key) == "b", "the swap wrote the new value")

    async def test_compare_and_set_races_have_one_winner(
        self, provider: CacheProvider
    ) -> None:
        self.requires_feature(provider, "cas")
        key = _key("cas-race")
        await provider.set(key, "start")
        answers = await _race(
            [
                partial(provider.compare_and_set, key, "start", f"racer-{i}")
                for i in range(_RACERS)
            ]
        )
        winners = [i for i, won in enumerate(answers) if won is True]
        require(len(winners) == 1, f"{len(winners)} of {_RACERS} swaps won, not one")
        require(
            await provider.get(key) == f"racer-{winners[0]}",
            "the stored value is the winner's",
        )

    async def test_compare_and_set_without_a_ttl_removes_the_old_one(
        self, provider: CacheProvider
    ) -> None:
        self.requires_feature(provider, "cas")
        key = _key("cas-ttl")
        await provider.set(key, "a", ttl_seconds=1)
        require(
            await provider.compare_and_set(key, "a", "b", ttl_seconds=None) is True,
            "the swap on match",
        )
        await self.let_expire(provider, 1)
        require(
            await provider.get(key) == "b",
            "ttl_seconds=None means no expiry: the earlier TTL is gone",
        )

    async def test_compare_and_delete_deletes_only_on_equality(
        self, provider: CacheProvider
    ) -> None:
        self.requires_feature(provider, "cas")
        key = _key("cad")
        require(
            await provider.compare_and_delete(key, "a") is False,
            "an absent key is not deleted",
        )
        await provider.set(key, "owner-1")
        require(
            await provider.compare_and_delete(key, "owner-0") is False,
            "a stale owner does not delete",
        )
        require(await provider.get(key) == "owner-1", "the key survives a stale owner")
        require(
            await provider.compare_and_delete(key, "owner-1") is True,
            "the owner deletes",
        )
        require(await provider.get(key) is None, "the key is gone")

    async def test_without_cas_both_calls_raise_unsupported(
        self, provider: CacheProvider
    ) -> None:
        if "cas" in provider.features():
            pytest.skip("the driver declares 'cas'")
        key = _key("no-cas")
        await provider.set(key, "a")
        calls: tuple[Call, Call] = (
            lambda: provider.compare_and_set(key, "a", "b"),
            lambda: provider.compare_and_delete(key, "a"),
        )
        for call in calls:
            with pytest.raises(Unsupported) as raised:
                await call()
            require(
                raised.value.feature == "cas",
                f"raised Unsupported({raised.value.feature!r}), not Unsupported('cas')",
            )
        require(await provider.get(key) == "a", "a refused call changes nothing")
