"""``CacheProvider.set_max`` — the monotonic "set if greater" behind the token
revocation mark.

Regression: the revocation mark used a read / add / compare-and-set loop
bounded at 8 attempts. Under contention (40 concurrent revocations of one
subject on a loaded machine) a writer could lose 8 races in a row to LOWER
marks and give up — ``revoke_user_tokens`` returned ``False`` ("mark kept
changing under contention"), the stored mark stayed below its value (tokens
minted in between remained valid until the durable applier ran) and a
``require_immediate`` change was refused with a spurious 503. Every lost race
means another writer changed the key, so the decision only has to be re-taken
while the stored mark is still below ours; Redis now does it in one atomic
Lua round trip.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from sandloom.core.auth import marks, revocation
from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.infra.contracts import (
    CacheContentionError,
    CacheProvider,
    parse_plain_decimal,
    portable_set_max,
)
from sandloom.core.testing import InMemoryCache


class _AdversarialCache:
    """Duck-typed cache (get / add / compare_and_set / set, NO ``set_max``)
    that yields to the event loop a seeded-random number of times between
    every read and every write — a loaded machine's scheduling, made
    reproducible. Counts lost races."""

    def __init__(self, seed: int = 0) -> None:
        self.store: dict[str, str] = {}
        self.lost = 0
        self._rng = random.Random(seed)

    async def _yield(self) -> None:
        for _ in range(self._rng.randrange(6)):
            await asyncio.sleep(0)

    async def get(self, key: str) -> str | None:
        value = self.store.get(key)
        await self._yield()
        return value

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        await self._yield()
        self.store[key] = value

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        await self._yield()
        if key in self.store:
            self.lost += 1
            return False
        self.store[key] = value
        return True

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        await self._yield()
        if self.store.get(key) != expected:
            self.lost += 1
            return False
        self.store[key] = value
        return True


def _marks(n: int, seed: int) -> tuple[list[float], list[float]]:
    base = time.time()
    mark_values = [base + i / 1000 for i in range(n)]
    order = mark_values[:]
    random.Random(seed).shuffle(order)
    return mark_values, order


# --------------------------------------------------------------------------- #
# Value domain
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("17", Decimal("17")),
        ("1700000000.000001", Decimal("1700000000.000001")),
        (" 5. ", Decimal("5")),
        (b"007.50", Decimal("7.5")),
        ("1e9", None),
        ("-1", None),
        (".5", None),
        ("nan", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_plain_decimal(raw: Any, expected: Decimal | None) -> None:
    assert parse_plain_decimal(raw) == expected


# --------------------------------------------------------------------------- #
# Portable loop and the in-memory double
# --------------------------------------------------------------------------- #
async def test_portable_set_max_semantics() -> None:
    cache = _AdversarialCache()
    assert await portable_set_max(cache, "k", "10.5") == "10.5"
    assert await portable_set_max(cache, "k", "10.49") == "10.5"  # lower: kept
    assert await portable_set_max(cache, "k", "10.500") == "10.5"  # equal: kept
    assert await portable_set_max(cache, "k", "11") == "11"
    cache.store["k"] = "garbage"  # corrupt: replaced
    assert await portable_set_max(cache, "k", "1") == "1"
    with pytest.raises(ValueError):
        await portable_set_max(cache, "k", "1e3")


async def test_portable_set_max_under_forced_interleaving_ends_at_max() -> None:
    """200 concurrent writers, a yield between every read and write: nobody
    fails, the stored value is the maximum, and the scenario really is
    contended (far more lost races than the old 8-attempt bound absorbed)."""
    cache = _AdversarialCache()
    values = [f"{1_700_000_000 + i}.{i:06d}" for i in range(200)]
    random.Random(3).shuffle(values)
    results = await asyncio.gather(*(portable_set_max(cache, "k", v) for v in values))
    top = max(values, key=Decimal)
    assert cache.store["k"] == top
    assert all(Decimal(r) >= Decimal(v) for r, v in zip(results, values, strict=True))
    assert cache.lost > 8


async def test_portable_set_max_gives_up_only_on_a_livelock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer that keeps resetting the key LOWER (non-monotonic) is the only
    way to exhaust the safety bound."""
    import sandloom.core.infra.contracts as contracts

    monkeypatch.setattr(contracts, "SET_MAX_MAX_ROUNDS", 5)

    class _Resetting(_AdversarialCache):
        async def compare_and_set(self, *a: Any, **kw: Any) -> bool:
            self.store["k"] = "0"
            return False

    cache = _Resetting()
    cache.store["k"] = "0"
    with pytest.raises(CacheContentionError):
        await portable_set_max(cache, "k", "5")


async def test_in_memory_cache_set_max() -> None:
    mem = InMemoryCache()
    assert await mem.set_max("k", "2") == "2"
    assert await mem.set_max("k", "1.999999") == "2"
    assert await mem.set_max("k", "2.000001") == "2.000001"
    mem.store["k"] = "x"
    assert await mem.set_max("k", "0") == "0"
    with pytest.raises(ValueError):
        await mem.set_max("k", "-3")
    with pytest.raises(ConnectionError):
        await InMemoryCache(broken=True).set_max("k", "1")


# --------------------------------------------------------------------------- #
# revoke_user_tokens under contention
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", [1, 7, 42])
async def test_concurrent_revocations_all_succeed_and_keep_the_max(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, seed: int
) -> None:
    """The failing CI scenario, made deterministic: 40 shuffled concurrent
    revocations on a cache that forces maximal interleaving (portable path)."""
    import sandloom.core.infra.access as access

    cache = _AdversarialCache(seed)
    monkeypatch.setattr(access, "cache", lambda name="default": cache)
    sub = str(uuid.uuid4())
    mark_values, order = _marks(40, seed)
    results = await asyncio.gather(
        *(revocation.revoke_user_tokens(sub, valid_after=m) for m in order)
    )
    assert all(results)
    stored = cache.store[marks.subject_key(sub)]
    assert marks._parse_mark_us(stored) == marks._seconds_to_us(max(mark_values))
    assert "Could not record token revocation" not in caplog.text


class _OvertakingCache(_AdversarialCache):
    """Between the target writer's read and its write, a competing (monotonic)
    revocation lands a mark that is higher than before but still LOWER than
    the target's — ``rounds`` times in a row. Each lost race is real progress
    by someone else; the target must keep going, not give up."""

    def __init__(self, target: str, competitor_marks: list[str]) -> None:
        super().__init__()
        self.target = target
        self.competitors = competitor_marks

    def _overtake(self, key: str, value: str) -> None:
        if value == self.target and self.competitors:
            self.store[key] = self.competitors.pop(0)

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        self._overtake(key, value)
        return await super().add(key, value, ttl_seconds=ttl_seconds)

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        self._overtake(key, value)
        return await super().compare_and_set(
            key, expected, value, ttl_seconds=ttl_seconds
        )


@pytest.mark.parametrize("immediate", [False, True])
async def test_a_writer_overtaken_many_times_still_records_its_mark(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, immediate: bool
) -> None:
    """Deterministic: 20 lost races in a row (the old loop gave up after 8,
    leaving the mark BELOW ours and, with ``require_immediate``, refusing the
    change with 503)."""
    import sandloom.core.infra.access as access

    target_us = marks._seconds_to_us(time.time() + 60)
    target = marks._format_mark(target_us)
    competitors = [marks._format_mark(target_us - 1000 + i) for i in range(20)]
    cache = _OvertakingCache(target, competitors)
    monkeypatch.setattr(access, "cache", lambda name="default": cache)
    sub = str(uuid.uuid4())
    valid_after = target_us / 1_000_000
    if immediate:
        await revocation.revoke_user_tokens_on_commit(
            sub, reason="race", valid_after=valid_after, require_immediate=True
        )
    else:
        assert await revocation.revoke_user_tokens(sub, valid_after=valid_after)
    assert cache.lost == 20
    assert cache.store[marks.subject_key(sub)] == target
    assert "Could not record token revocation" not in caplog.text


async def test_require_immediate_is_not_refused_by_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sandloom.core.infra.access as access

    cache = _AdversarialCache(5)
    monkeypatch.setattr(access, "cache", lambda name="default": cache)
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
    stored = cache.store[marks.subject_key(sub)]
    assert marks._parse_mark_us(stored) >= marks._seconds_to_us(max(mark_values))


# --------------------------------------------------------------------------- #
# Redis: one atomic Lua round trip
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def live() -> AsyncIterator[RedisCacheProvider]:
    from pathlib import Path

    from sandloom.core.testing.workers import worker_env_values

    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    url = os.getenv("CACHE_URL") or worker_env_values(env_test).get("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not configured")
    provider = RedisCacheProvider(
        name="set_max", driver="redis", options={"url": url}, shared=False
    )
    try:
        await asyncio.wait_for(provider.get("sandloom:test:ping"), 5)
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip
        await provider.aclose()
        pytest.skip("test Redis not reachable")
    yield provider
    await provider.aclose()


async def test_redis_set_max_semantics_and_ttl(live: RedisCacheProvider) -> None:
    assert "set_max" in live.features()
    key = f"t:setmax:{uuid.uuid4()}"
    client = await live.raw_client()
    assert await live.set_max(key, "100.5", ttl_seconds=30) == "100.5"
    assert 0 < await client.ttl(key) <= 30
    # Lower / equal: kept, and its TTL is neither shortened nor extended.
    assert await live.set_max(key, "100.4999999", ttl_seconds=5) == "100.5"
    assert await live.set_max(key, "100.50", ttl_seconds=5) == "100.5"
    assert await client.ttl(key) > 5
    # Greater: written with the new TTL.
    assert await live.set_max(key, "100.5000001", ttl_seconds=90) == "100.5000001"
    assert 30 < await client.ttl(key) <= 90
    # Corrupt: replaced; no TTL -> persistent.
    await live.set(key, "not-a-mark")
    assert await live.set_max(key, "1") == "1"
    assert await client.ttl(key) == -1
    with pytest.raises(ValueError):
        await live.set_max(key, "1e20")
    await live.delete(key)


@pytest.mark.parametrize(
    ("low", "high"),
    [
        # Differ beyond double precision (17 significant digits).
        ("1700000000.0000001", "1700000000.0000002"),
        ("9007199254740993", "9007199254740994"),
        ("99", "100"),
        ("0099.9", "100"),
        ("5.09", "5.1"),
    ],
)
async def test_redis_set_max_compares_exactly(
    live: RedisCacheProvider, low: str, high: str
) -> None:
    key = f"t:setmax:{uuid.uuid4()}"
    assert await live.set_max(key, high, ttl_seconds=30) == high
    assert await live.set_max(key, low, ttl_seconds=30) == high
    await live.delete(key)
    assert await live.set_max(key, low, ttl_seconds=30) == low
    assert await live.set_max(key, high, ttl_seconds=30) == high
    await live.delete(key)


async def test_redis_set_max_concurrent_writers_end_at_max(
    live: RedisCacheProvider,
) -> None:
    key = f"t:setmax:{uuid.uuid4()}"
    values = [f"{1_700_000_000 + i // 7}.{i:06d}" for i in range(300)]
    random.Random(11).shuffle(values)
    # 16 writers in flight at a time: still concurrent read-compare-write
    # races on one key, without opening 300 connections at once (each a
    # getaddrinfo + connect that times out on a loaded machine).
    gate = asyncio.Semaphore(16)

    async def write(value: str) -> str:
        async with gate:
            return await live.set_max(key, value, ttl_seconds=30)

    results = await asyncio.gather(*(write(v) for v in values))
    top = max(values, key=Decimal)
    assert await live.get(key) == top
    assert all(Decimal(r) >= Decimal(v) for r, v in zip(results, values, strict=True))
    await live.delete(key)


# --------------------------------------------------------------------------- #
# Fallbacks never hide bugs, and never lower a revocation mark
# --------------------------------------------------------------------------- #
class _NoCasCache(InMemoryCache):
    """A cache WITHOUT ``cas`` and without its own ``set_max``: the contract's
    default (portable loop) and ``compare_and_set`` raises NotImplementedError."""

    set_max = CacheProvider.set_max  # type: ignore[assignment]
    compare_and_set = CacheProvider.compare_and_set  # type: ignore[assignment]


async def test_portable_set_max_without_cas_overwrites_unless_strict() -> None:
    cache = _NoCasCache()
    cache.store["k"] = "5"
    # Documented best-effort: without ``cas`` the default overwrites (can lower
    # a value a concurrent writer raised between the read and the write).
    assert await portable_set_max(cache, "k", "6") == "6"
    with pytest.raises(NotImplementedError):
        await portable_set_max(cache, "k", "7", overwrite_without_cas=False)
    assert cache.store["k"] == "6"


@pytest.mark.parametrize("immediate", [False, True])
async def test_revocation_refuses_a_cache_without_set_max_or_cas(
    monkeypatch: pytest.MonkeyPatch, immediate: bool
) -> None:
    """Rather than risk lowering the mark with a plain SET, the write is
    refused: ``False`` (retried, then durable) / 503 for ``require_immediate``."""
    import sandloom.core.infra.access as access

    cache = _NoCasCache()
    monkeypatch.setattr(access, "cache", lambda name="default": cache)
    sub = str(uuid.uuid4())
    key = marks.subject_key(sub)
    cache.store[key] = marks._format_mark(marks._seconds_to_us(1_000.0))
    if immediate:
        with pytest.raises(marks.RevocationUnavailableError):
            await revocation.revoke_user_tokens_on_commit(
                sub, reason="x", valid_after=2_000.0, require_immediate=True
            )
    else:
        assert await revocation.revoke_user_tokens(sub, valid_after=2_000.0) is False
    assert marks._parse_mark_us(cache.store[key]) == marks._seconds_to_us(1_000.0)


def _redis_with(client: Any) -> RedisCacheProvider:
    provider = RedisCacheProvider(
        name="fake", driver="redis", options={"url": "redis://unused:1/0"}, shared=False
    )

    async def _client() -> Any:
        return client

    provider._client_obj = _client  # type: ignore[method-assign]
    return provider


async def test_redis_set_max_falls_back_only_for_a_client_without_scripting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _NoScripting:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        async def get(self, key: str) -> str | None:
            return self.store.get(key)

        async def set(self, key: str, value: str, **kw: Any) -> Any:
            if kw.get("nx") and key in self.store:
                return None
            self.store[key] = value
            return True

    client = _NoScripting()
    provider = _redis_with(client)
    with caplog.at_level("WARNING"):
        assert await provider.set_max("k", "3", ttl_seconds=5) == "3"
        assert await provider.set_max("k", "2", ttl_seconds=5) == "3"
    assert "no Lua scripting" in caplog.text


async def test_redis_set_max_does_not_swallow_attribute_errors() -> None:
    """A genuine bug inside the Lua path (here: an AttributeError raised by the
    client call) propagates instead of silently switching to the loop."""

    class _Broken:
        async def evalsha(self, *args: Any) -> Any:
            raise AttributeError("bug in the script path")

    with pytest.raises(AttributeError, match="bug in the script path"):
        await _redis_with(_Broken()).set_max("k", "1")
