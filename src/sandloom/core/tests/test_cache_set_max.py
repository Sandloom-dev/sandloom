"""The revocation mark's behaviour under contention on the security ``atomic_store``.

Regression: the revocation mark used a read / add / compare-and-set loop
bounded at 8 attempts. Under contention (40 concurrent revocations of one
subject on a loaded machine) a writer could lose 8 races in a row to LOWER
marks and give up — ``revoke_user_tokens`` returned ``False`` ("mark kept
changing under contention"), the stored mark stayed below its value (tokens
minted in between remained valid until the durable applier ran) and a
``require_immediate`` change was refused with a spurious 503. The store's
``set_subject_state`` is one atomic ranked write, so a lost race cannot make a
writer give up.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid

import pytest

from sandloom.core.auth import marks, revocation
from sandloom.core.testing import InMemoryAtomicStore


def _marks(n: int, seed: int) -> tuple[list[float], list[float]]:
    base = time.time()
    mark_values = [base + i / 1000 for i in range(n)]
    order = mark_values[:]
    random.Random(seed).shuffle(order)
    return mark_values, order


# --------------------------------------------------------------------------- #
# revoke_user_tokens under contention (the security atomic_store)
# --------------------------------------------------------------------------- #
class _AdversarialStore(InMemoryAtomicStore):
    """The security store, yielding to the event loop a seeded-random number of
    times around every read and write: a loaded machine's scheduling, made
    reproducible. ``set_subject_state`` itself stays atomic (the port's
    contract); the race the old read / add / compare-and-set loop had to
    retry is the revocation's own read-then-write."""

    def __init__(self, seed: int = 0) -> None:
        super().__init__()
        self._rng = random.Random(seed)

    async def _yield(self) -> None:
        for _ in range(self._rng.randrange(6)):
            await asyncio.sleep(0)

    async def get(self, key: str) -> str | None:
        value = await super().get(key)
        await self._yield()
        return value

    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        await self._yield()
        return await super().set_subject_state(key, value, ttl_seconds=ttl_seconds)


@pytest.mark.parametrize("seed", [1, 7, 42])
async def test_concurrent_revocations_all_succeed_and_keep_the_max(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, seed: int
) -> None:
    """40 shuffled concurrent revocations on a store that forces maximal
    interleaving: every one succeeds and the mark ends at the maximum."""
    import sandloom.core.infra.access as access

    store = _AdversarialStore(seed)
    monkeypatch.setattr(access, "atomic_store", lambda role="security": store)
    sub = str(uuid.uuid4())
    mark_values, order = _marks(40, seed)
    results = await asyncio.gather(
        *(revocation.revoke_user_tokens(sub, valid_after=m) for m in order)
    )
    assert all(results)
    stored = store.store[marks.subject_key(sub)]
    # at the maximum, or past it by a microsecond per lower revocation that
    # found a covering mark stored (it must land above what issuers stamped)
    highest = marks._seconds_to_us(max(mark_values))
    assert highest <= marks._parse_mark_us(stored) <= highest + len(mark_values)
    assert "Could not record token revocation" not in caplog.text


class _OvertakingStore(_AdversarialStore):
    """Between the target writer's read and its write, a competing (monotonic)
    revocation lands a mark that is higher than before but still LOWER than
    the target's, ``rounds`` times in a row. The write is atomic, so the target
    still ends with its own mark."""

    def __init__(self, target: str, competitor_marks: list[str]) -> None:
        super().__init__()
        self.target = target
        self.competitors = competitor_marks
        self.overtaken = 0

    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        # the target writer's value reads as the target instant (a revocation
        # carries its identity past the microsecond)
        reads_as_target = marks._parse_mark_us(value) == marks._parse_mark_us(
            self.target
        )
        if reads_as_target and self.competitors:
            self.store[key] = self.competitors.pop(0)
            self.overtaken += 1
        return await super().set_subject_state(key, value, ttl_seconds=ttl_seconds)


@pytest.mark.parametrize("immediate", [False, True])
async def test_a_writer_overtaken_many_times_still_records_its_mark(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, immediate: bool
) -> None:
    """Deterministic: a lower competitor lands before the target's write, once
    per write, 20 times (the old loop gave up after 8, leaving the mark BELOW
    ours and, with ``require_immediate``, refusing the change with 503)."""
    import sandloom.core.infra.access as access

    target_us = marks._seconds_to_us(time.time() + 60)
    target = marks._format_mark(target_us)
    competitors = [marks._format_mark(target_us - 1000 + i) for i in range(20)]
    store = _OvertakingStore(target, competitors)
    monkeypatch.setattr(access, "atomic_store", lambda role="security": store)
    sub = str(uuid.uuid4())
    valid_after = target_us / 1_000_000
    if immediate:
        await revocation.revoke_user_tokens_on_commit(
            sub, reason="race", valid_after=valid_after, require_immediate=True
        )
    else:
        assert await revocation.revoke_user_tokens(sub, valid_after=valid_after)
    assert store.overtaken >= 1
    stored = marks._parse_mark_us(store.store[marks.subject_key(sub)])
    # outside a unit the post-commit re-mark runs at once: one microsecond
    # past the early mark it found covering its own instant
    assert stored == target_us + (1 if immediate else 0)
    assert "Could not record token revocation" not in caplog.text


async def test_require_immediate_is_not_refused_by_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sandloom.core.infra.access as access

    store = _AdversarialStore(5)
    monkeypatch.setattr(access, "atomic_store", lambda role="security": store)
    sub = str(uuid.uuid4())
    mark_values, order = _marks(30, 5)
    await asyncio.gather(
        *(
            revocation.revoke_user_tokens_on_commit(
                sub, reason="race", valid_after=m, require_immediate=True
            )
            for m in order
        )
    )  # would raise RevocationUnavailableError on a lost-race give-up
    stored = store.store[marks.subject_key(sub)]
    assert marks._parse_mark_us(stored) >= marks._seconds_to_us(max(mark_values))
