"""``AtomicStoreConformance``: what an ``atomic_store`` driver must do (spec 4).

Every operation is atomic, so most of the kit is a race: ``asyncio.gather`` of
50 callers of one key, whose outcomes only one atomic driver can produce. The
rest checks what the kernel's security and idempotency state relies on:
``set_subject_state`` ranking compared exactly as decimals, an expired key
that is absent for every operation although the backend still holds the row,
and a TTL no losing write shortens.

A driver's tests subclass it, override ``make_provider`` (an unopened provider
on a test backend) and, for the features it declares, ``make_evicting_provider``
(``never_evicts``). Expiry is waited out with :meth:`AtomicStoreConformance.let_expire`
(real time by default; a driver with a clock overrides it).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from functools import partial
from typing import ClassVar, cast
from uuid import uuid4

import pytest

from sandloom.core.infra.errors import InfraError, InvalidArgument, TooLarge
from sandloom.core.infra.ports.atomic_store import (
    ATOMIC_STORE_CAPABILITY,
    MAX_KEY_BYTES,
    AtomicStore,
    AtomicStoreProvider,
    subject_state_rank,
)
from sandloom.driverkit.kits._harness import Call, ProviderConformance, require

#: Concurrent callers of one key in a race.
_RACERS = 50


def _key(label: str) -> str:
    return f"kit:{uuid4().hex}:{label}"


def _after(instant: str) -> str:
    return f"v1:after:{instant}"


def _blocked(instant: str) -> str:
    return f"v1:blocked:{instant}"


async def _race(calls: list[Callable[[], Awaitable[object]]]) -> list[object]:
    """Run ``calls`` concurrently, every one started before any is awaited."""
    return list(await asyncio.gather(*(call() for call in calls)))


@pytest.mark.asyncio
class AtomicStoreConformance(ProviderConformance[AtomicStoreProvider]):
    """What an ``atomic_store`` driver must do, after the harness's checks.

    ``value_limit_bytes`` is the largest value the provider from
    ``make_provider`` accepts; ``None`` skips the ``TooLarge`` checks.
    """

    contract = AtomicStoreProvider
    protocol = AtomicStore
    spec = ATOMIC_STORE_CAPABILITY

    value_limit_bytes: ClassVar[int | None] = None

    @pytest.fixture
    def make_evicting_provider(self) -> Callable[[], AtomicStoreProvider] | None:
        """An unopened provider whose backend evicts keys under pressure (an
        eviction policy other than "never"). ``None`` skips the ``never_evicts``
        group's check of the driver's own refusal."""
        return None

    async def let_expire(self, provider: AtomicStoreProvider, ttl_seconds: int) -> None:
        """Wait until a key written with ``ttl_seconds`` is past its TTL and
        the backend has not purged it yet. Real time by default: a driver with
        a clock it controls overrides this to advance it."""
        await asyncio.sleep(ttl_seconds + 1.1)

    def operation_calls(self, provider: AtomicStoreProvider) -> dict[str, Call]:
        key = _key("failing")
        return {
            "get": lambda: provider.get(key),
            "set": lambda: provider.set(key, "v", ttl_seconds=5),
            "delete": lambda: provider.delete(key),
            "add": lambda: provider.add(key, "v", ttl_seconds=5),
            "add_or_get": lambda: provider.add_or_get(key, "v", ttl_seconds=5),
            "compare_and_set": lambda: provider.compare_and_set(key, "v", "w"),
            "compare_and_delete": lambda: provider.compare_and_delete(key, "v"),
            "set_subject_state": lambda: provider.set_subject_state(key, _after("1")),
        }

    # --------------------------------------------------------- key-value
    async def test_absent_key_reads_none(self, provider: AtomicStoreProvider) -> None:
        require(await provider.get(_key("absent")) is None, "an absent key is None")

    async def test_set_get_overwrite_delete(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("kv")
        await provider.set(key, "first")
        require(await provider.get(key) == "first", "get returns what set wrote")
        await provider.set(key, "second")
        require(await provider.get(key) == "second", "set overwrites")
        await provider.delete(key)
        require(await provider.get(key) is None, "delete removes the key")
        await provider.delete(key)  # an absent key succeeds

    async def test_values_round_trip_exactly(
        self, provider: AtomicStoreProvider
    ) -> None:
        for index, value in enumerate(("", "ünï ☃ 🙂", "x" * 4096, " padded \n")):
            key = _key(f"value-{index}")
            await provider.set(key, value)
            require(await provider.get(key) == value, f"value {index} round-trips")

    # --------------------------------------------------------------- add
    async def test_add_creates_only_an_absent_key(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("add")
        require(await provider.add(key, "first") is True, "add creates an absent key")
        require(await provider.add(key, "second") is False, "add of a held key loses")
        require(await provider.get(key) == "first", "a lost add keeps the value")

    async def test_concurrent_adds_have_one_winner(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("add-race")
        answers = await _race(
            [partial(provider.add, key, f"racer-{i}") for i in range(_RACERS)]
        )
        winners = [i for i, won in enumerate(answers) if won is True]
        require(len(winners) == 1, f"{len(winners)} of {_RACERS} adds won, not one")
        require(
            await provider.get(key) == f"racer-{winners[0]}",
            "the stored value is the winner's",
        )

    # -------------------------------------------------------- add_or_get
    async def test_add_or_get_creates_then_reports_the_holder(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("add-or-get")
        require(
            await provider.add_or_get(key, "first") == (True, None),
            "the creator gets (True, None)",
        )
        require(
            await provider.add_or_get(key, "second") == (False, "first"),
            "a loser gets (False, the holder's value)",
        )
        empty = _key("add-or-get-empty")
        await provider.set(empty, "")
        require(
            await provider.add_or_get(empty, "x") == (False, ""),
            "an empty holder's value is '', never None",
        )

    async def test_concurrent_add_or_gets_have_one_creator(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("add-or-get-race")
        answers = await _race(
            [partial(provider.add_or_get, key, f"racer-{i}") for i in range(_RACERS)]
        )
        pairs = cast("list[tuple[bool, str | None]]", answers)
        creators = [i for i, (created, _) in enumerate(pairs) if created]
        require(
            len(creators) == 1, f"{len(creators)} of {_RACERS} created the key, not one"
        )
        winner = f"racer-{creators[0]}"
        for index, answer in enumerate(answers):
            if index == creators[0]:
                require(answer == (True, None), "the creator gets (True, None)")
            else:
                require(
                    answer == (False, winner),
                    f"a loser saw {answer!r}, not the winner's value {winner!r}",
                )

    # ------------------------------------------------- compare_and_set/delete
    async def test_compare_and_set_swaps_only_on_the_expected_value(
        self, provider: AtomicStoreProvider
    ) -> None:
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
        self, provider: AtomicStoreProvider
    ) -> None:
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
        self, provider: AtomicStoreProvider
    ) -> None:
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

    async def test_compare_and_delete_by_a_stale_owner_fails(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("cad")
        await provider.set(key, "owner-1")
        require(
            await provider.compare_and_delete(key, "owner-0") is False,
            "a stale owner does not delete",
        )
        require(await provider.get(key) == "owner-1", "the key survives a stale owner")
        require(
            await provider.compare_and_delete(key, "owner-1") is True,
            "the current owner deletes",
        )
        require(await provider.get(key) is None, "the key is gone")
        require(
            await provider.compare_and_delete(key, "owner-1") is False,
            "an absent key answers False",
        )

    async def test_compare_and_delete_races_have_one_winner(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("cad-race")
        await provider.set(key, "owner")
        answers = await _race(
            [lambda: provider.compare_and_delete(key, "owner") for _ in range(_RACERS)]
        )
        require(
            sum(1 for won in answers if won is True) == 1,
            "exactly one concurrent compare_and_delete wins",
        )

    # --------------------------------------------------- set_subject_state
    async def test_subject_state_blocked_outranks_after(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("blocked-over-after")
        require(
            await provider.set_subject_state(key, _after("9999999999"))
            == _after("9999999999"),
            "an absent key takes the value",
        )
        require(
            await provider.set_subject_state(key, _blocked("1")) == _blocked("1"),
            "blocked outranks after at any instant",
        )
        require(
            await provider.set_subject_state(key, _after("9999999999"))
            == _blocked("1"),
            "after never overwrites blocked",
        )
        require(await provider.get(key) == _blocked("1"), "blocked is still stored")

    async def test_subject_state_ignores_a_lower_instant(
        self, provider: AtomicStoreProvider
    ) -> None:
        for kind in (_after, _blocked):
            key = _key("lower")
            await provider.set_subject_state(key, kind("100"))
            require(
                await provider.set_subject_state(key, kind("99")) == kind("100"),
                f"a lower {kind.__name__[1:]} instant is ignored",
            )
            require(
                await provider.set_subject_state(key, kind("101")) == kind("101"),
                f"a higher {kind.__name__[1:]} instant is written",
            )

    async def test_subject_state_equal_instants_keep_the_stored_value(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("equal")
        await provider.set_subject_state(key, _after("1.50"))
        require(
            await provider.set_subject_state(key, _after("1.5")) == _after("1.50"),
            "an equal instant keeps the stored value as written",
        )
        require(await provider.get(key) == _after("1.50"), "the stored value is kept")

    async def test_subject_state_compares_numbers_not_text(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("numeric")
        await provider.set_subject_state(key, _after("9"))
        require(
            await provider.set_subject_state(key, _after("10")) == _after("10"),
            "10 ranks above 9 (text order says otherwise)",
        )
        other = _key("fraction")
        await provider.set_subject_state(other, _after("1.10"))
        require(
            await provider.set_subject_state(other, _after("1.9")) == _after("1.9"),
            "1.9 ranks above 1.10 (text order says otherwise)",
        )

    async def test_subject_state_ranks_instants_past_the_15th_digit(
        self, provider: AtomicStoreProvider
    ) -> None:
        lower, higher = _after("1700000000.12345678"), _after("1700000000.12345679")
        require(
            float(Decimal("1700000000.12345678"))
            == float(Decimal("1700000000.12345679")),
            "the kit's own premise: these instants are equal as floats",
        )
        up = _key("digits-up")
        await provider.set_subject_state(up, lower)
        require(
            await provider.set_subject_state(up, higher) == higher,
            "the higher 17th digit replaces the lower",
        )
        down = _key("digits-down")
        await provider.set_subject_state(down, higher)
        require(
            await provider.set_subject_state(down, lower) == higher,
            "the lower 17th digit does not replace the higher",
        )
        require(await provider.get(down) == higher, "the higher value is kept")

    async def test_subject_state_replaces_a_corrupt_stored_value(
        self, provider: AtomicStoreProvider
    ) -> None:
        for corrupt in ("garbage", "v1:after:", "v2:after:1", "v1:other:1", "12"):
            key = _key("corrupt")
            await provider.set(key, corrupt)
            require(
                await provider.set_subject_state(key, _after("1")) == _after("1"),
                f"a stored {corrupt!r} is outside the domain and replaced",
            )

    async def test_subject_state_refuses_a_value_outside_the_domain(
        self, provider: AtomicStoreProvider
    ) -> None:
        for bad in (
            "",
            "garbage",
            "after:1",
            "v1:after:",
            "v1:after:-1",
            "v1:after:1e3",
        ):
            with pytest.raises(InvalidArgument):
                await provider.set_subject_state(_key("bad"), bad)

    async def test_concurrent_subject_state_writers_end_at_the_maximum(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("writers")
        values = [_after(f"{1000 + i}.{i}") for i in range(_RACERS)]
        values.append(_blocked("5"))
        values.extend(_blocked(str(i)) for i in (1, 3, 4))
        answers = await _race(
            [partial(provider.set_subject_state, key, v) for v in values]
        )
        require(await provider.get(key) == _blocked("5"), "the maximum is stored")
        for value, answer in zip(values, answers, strict=True):
            mine, held = subject_state_rank(value), subject_state_rank(str(answer))
            require(
                mine is not None and held is not None and held >= mine,
                f"a write of {value!r} answered {answer!r}, below its own value",
            )

    # ------------------------------------------------------------- TTL
    async def test_a_key_is_gone_after_its_ttl(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("ttl")
        await provider.set(key, "v", ttl_seconds=1)
        require(await provider.get(key) == "v", "readable before its TTL")
        await self.let_expire(provider, 1)
        require(await provider.get(key) is None, "not readable after ttl + 1 s")

    async def test_a_losing_write_never_shortens_the_ttl(
        self, provider: AtomicStoreProvider
    ) -> None:
        added, got, swapped, ranked = (
            _key("keep-add"),
            _key("keep-get"),
            _key("keep-cas"),
            _key("keep-state"),
        )
        for key in (added, got, swapped):
            await provider.set(key, "held")
        await provider.set_subject_state(ranked, _blocked("10"))
        require(await provider.add(added, "new", ttl_seconds=1) is False, "add loses")
        require(
            await provider.add_or_get(got, "new", ttl_seconds=1) == (False, "held"),
            "add_or_get loses",
        )
        require(
            await provider.compare_and_set(swapped, "wrong", "new", ttl_seconds=1)
            is False,
            "compare_and_set loses",
        )
        require(
            await provider.set_subject_state(ranked, _after("1"), ttl_seconds=1)
            == _blocked("10"),
            "set_subject_state loses",
        )
        await self.let_expire(provider, 1)
        for key, value in (
            (added, "held"),
            (got, "held"),
            (swapped, "held"),
            (ranked, _blocked("10")),
        ):
            require(
                await provider.get(key) == value,
                f"a losing write with a 1 s TTL expired {key.rsplit(':', 1)[1]!r}",
            )

    # ------------------------------------- expired keys behave as absent
    async def test_add_over_an_expired_key_creates(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("expired-add")
        await provider.set(key, "old", ttl_seconds=1)
        await self.let_expire(provider, 1)
        require(
            await provider.add(key, "new") is True,
            "add over an expired key creates, as over an absent one",
        )
        require(await provider.get(key) == "new", "the new value is stored")

    async def test_add_or_get_over_an_expired_key_creates(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("expired-add-or-get")
        await provider.set(key, "old", ttl_seconds=1)
        await self.let_expire(provider, 1)
        require(
            await provider.add_or_get(key, "new") == (True, None),
            "add_or_get over an expired key creates, never reports the stale value",
        )

    async def test_compare_and_set_on_an_expired_key_is_false(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("expired-cas")
        await provider.set(key, "old", ttl_seconds=1)
        await self.let_expire(provider, 1)
        require(
            await provider.compare_and_set(key, "old", "new") is False,
            "compare_and_set on an expired key answers False, as on an absent one",
        )
        require(await provider.get(key) is None, "nothing was written")

    async def test_compare_and_delete_on_an_expired_key_is_false(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("expired-cad")
        await provider.set(key, "old", ttl_seconds=1)
        await self.let_expire(provider, 1)
        require(
            await provider.compare_and_delete(key, "old") is False,
            "compare_and_delete on an expired key answers False",
        )

    async def test_set_subject_state_over_an_expired_key_writes(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("expired-state")
        await provider.set_subject_state(key, _blocked("100"), ttl_seconds=1)
        await self.let_expire(provider, 1)
        require(
            await provider.set_subject_state(key, _after("1")) == _after("1"),
            "an expired blocked mark is absent: the lower write is stored",
        )

    # ----------------------------------------------------------- errors
    async def test_a_key_outside_the_contract_is_invalid(
        self, provider: AtomicStoreProvider
    ) -> None:
        for key in ("", "k" * (MAX_KEY_BYTES + 1), "é" * (MAX_KEY_BYTES // 2 + 1)):
            for name in self.operation_calls(provider):
                with pytest.raises(InvalidArgument):
                    await _call_with_key(provider, name, key)
        longest = _key("") + "k" * (MAX_KEY_BYTES - len(_key("")))
        await provider.set(longest, "v")
        require(await provider.get(longest) == "v", "a key of 1024 bytes is valid")

    async def test_a_ttl_below_one_is_invalid(
        self, provider: AtomicStoreProvider
    ) -> None:
        key = _key("bad-ttl")
        for ttl in (0, -1):
            with pytest.raises(InvalidArgument):
                await provider.set(key, "v", ttl_seconds=ttl)
            with pytest.raises(InvalidArgument):
                await provider.add(key, "v", ttl_seconds=ttl)
            with pytest.raises(InvalidArgument):
                await provider.add_or_get(key, "v", ttl_seconds=ttl)
            with pytest.raises(InvalidArgument):
                await provider.compare_and_set(key, "v", "w", ttl_seconds=ttl)
            with pytest.raises(InvalidArgument):
                await provider.set_subject_state(key, _after("1"), ttl_seconds=ttl)
        require(await provider.get(key) is None, "a refused write stores nothing")

    async def test_a_value_over_the_limit_is_too_large(
        self, provider: AtomicStoreProvider
    ) -> None:
        limit = self.value_limit_bytes
        if limit is None:
            pytest.skip("value_limit_bytes is not set: the limit is not checked")
        key = _key("too-large")
        await provider.set(key, "x" * limit)  # exactly the limit is valid
        oversized = "x" * (limit + 1)
        with pytest.raises(TooLarge):
            await provider.set(key, oversized)
        with pytest.raises(TooLarge):
            await provider.add(_key("too-large-add"), oversized)
        with pytest.raises(TooLarge):
            await provider.add_or_get(_key("too-large-aog"), oversized)
        with pytest.raises(TooLarge):
            await provider.compare_and_set(key, "x", oversized)
        require(
            await provider.get(key) == "x" * limit, "a refused write changed nothing"
        )

    # --------------------------------------------------- feature: shared
    async def test_shared_two_providers_of_one_store_see_each_other(
        self, make_provider: Callable[[], AtomicStoreProvider]
    ) -> None:
        first, second = make_provider(), make_provider()
        await first.aopen()
        try:
            self.requires_feature(first, "shared")
            await second.aopen()
            try:
                key = _key("shared")
                await first.set(key, "from-first")
                require(
                    await second.get(key) == "from-first",
                    "a write of one provider is not visible to the other",
                )
                require(
                    await second.add(key, "from-second") is False,
                    "the second provider's add does not see the first's key",
                )
                require(
                    await first.compare_and_delete(key, "from-first") is True,
                    "the first provider deletes",
                )
                require(await second.get(key) is None, "the delete is shared")
            finally:
                await second.aclose()
        finally:
            await first.aclose()

    # --------------------------------------------- feature: never_evicts
    async def test_never_evicts_the_driver_refuses_an_evicting_backend(
        self,
        provider: AtomicStoreProvider,
        make_evicting_provider: Callable[[], AtomicStoreProvider] | None,
    ) -> None:
        self.requires_feature(provider, "never_evicts")
        if make_evicting_provider is None:
            pytest.skip("no make_evicting_provider: the driver's check is not run")
        built = make_evicting_provider()
        try:
            try:
                await built.aopen()
            except InfraError:
                return
            raise AssertionError(
                "aopen succeeded on a backend that evicts: a driver that declares "
                "never_evicts verifies it (or refuses) on aopen"
            )
        finally:
            try:
                await built.aclose()
            except InfraError:
                pass


async def _call_with_key(provider: AtomicStoreProvider, name: str, key: str) -> None:
    """The operation ``name`` on ``key`` with otherwise valid arguments."""
    if name == "get":
        await provider.get(key)
    elif name == "set":
        await provider.set(key, "v")
    elif name == "delete":
        await provider.delete(key)
    elif name == "add":
        await provider.add(key, "v")
    elif name == "add_or_get":
        await provider.add_or_get(key, "v")
    elif name == "compare_and_set":
        await provider.compare_and_set(key, "v", "w")
    elif name == "compare_and_delete":
        await provider.compare_and_delete(key, "v")
    else:
        await provider.set_subject_state(key, _after("1"))


__all__ = ["AtomicStoreConformance"]
