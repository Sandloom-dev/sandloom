"""ADR-0009 item 13: one subject-state record replaces the two marks.

``sandloom:auth:subject:{id}`` holds ``v1:after:<us>`` (revocation, TTL, monotonic)
or ``v1:blocked:<since>`` (no TTL), and every ``blocked`` value ranks above
every ``after`` value. Pinned here, against both the portable writer (an
in-memory cache with ``compare_and_set``) and the Redis Lua script:

* a revocation concurrent with (or later than) a block never unblocks;
* an unblock swaps ``blocked`` for ``after:<now + skew>`` by compare-and-set,
  so every token issued before the unblock stays revoked (it never deletes);
* a corrupt record fails closed and the issuance check repairs it;
* the hot path reads ONE key (one ``GET``).
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import random
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandloom.core.auth import blocks, marks, revocation, tokens, verification
from sandloom.core.config.settings import settings
from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.infra import access
from sandloom.core.infra.contracts import (
    CacheProvider,
    parse_subject_state,
    portable_set_subject_state,
)
from sandloom.core.testing import InMemoryCache, recording_uow, run_on_commit


def _after(seconds: float) -> str:
    return marks._format_mark(marks._seconds_to_us(seconds))


def _blocked(seconds: float) -> str:
    return marks._format_blocked(marks._seconds_to_us(seconds))


def _claims(sub: str) -> dict[str, Any]:
    claims = tokens.decode_token(
        tokens.create_access_token({"sub": sub, "token_type": "access"})
    )
    assert claims is not None
    return claims


@pytest.fixture
def mem_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryCache:
    # The composed app registers the core event schemas on its bus
    # (block_user publishes core.auth.*): make it the active runtime.
    from sandloom.core import runtime
    from sandloom.main import app

    monkeypatch.setattr(runtime, "_process_core", app.state.core)
    fake = InMemoryCache()
    monkeypatch.setattr(access, "cache", lambda name="default": fake)
    monkeypatch.setattr(
        settings, "auth_revocation_clock_skew_seconds", 1.0, raising=False
    )
    monkeypatch.setattr(
        settings, "auth_revocation_retry_base_seconds", 0.0, raising=False
    )
    return fake


# --------------------------------------------------------------------------- #
# the value domain and the order
# --------------------------------------------------------------------------- #
def test_value_domain() -> None:
    assert parse_subject_state("v1:after:12.5") is not None
    assert parse_subject_state("v1:blocked:12") is not None
    for bad in ("12.5", "v1:after:", "v1:after:-1", "v2:after:1", "v1:x:1", "", None):
        assert parse_subject_state(bad) is None


@pytest.mark.parametrize(
    ("stored", "offered", "kept"),
    [
        (None, "v1:after:5", "v1:after:5"),
        ("v1:after:5", "v1:after:4.999999", "v1:after:5"),
        ("v1:after:5", "v1:after:5.000001", "v1:after:5.000001"),
        ("v1:after:5", "v1:blocked:1", "v1:blocked:1"),  # a block outranks
        ("v1:blocked:1", "v1:after:9999999999", "v1:blocked:1"),  # never unblocks
        ("v1:blocked:1", "v1:blocked:2", "v1:blocked:2"),
        ("garbage", "v1:after:0", "v1:after:0"),  # corrupt: replaced
    ],
)
async def test_portable_order(stored: str | None, offered: str, kept: str) -> None:
    cache = InMemoryCache()
    if stored is not None:
        cache.store["k"] = stored
    assert await portable_set_subject_state(cache, "k", offered) == kept
    assert cache.store["k"] == kept


async def test_portable_writer_refuses_without_cas() -> None:
    class _NoCas(InMemoryCache):
        compare_and_set = CacheProvider.compare_and_set  # type: ignore[assignment]

    cache = _NoCas()
    cache.store["k"] = "v1:blocked:1"
    with pytest.raises(NotImplementedError):
        await portable_set_subject_state(cache, "k", "v1:blocked:2")
    with pytest.raises(ValueError):
        await portable_set_subject_state(cache, "k", "12")


# --------------------------------------------------------------------------- #
# block / revocation / unblock semantics
# --------------------------------------------------------------------------- #
async def test_a_revocation_concurrent_with_a_block_never_unblocks(
    mem_cache: InMemoryCache,
) -> None:
    sub = str(uuid.uuid4())
    token = _claims(sub)
    with recording_uow() as session:
        await blocks.block_user(sub, reason="leak", actor_id=None)
        # a revocation racing the block, then its post-commit re-mark and a
        # late durable applier: none of them may lower the record
        await asyncio.gather(
            *(
                revocation.revoke_user_tokens(sub, valid_after=time.time() + d)
                for d in (-10, 0, 10, 3600)
            )
        )
        await run_on_commit(session)
    await revocation.apply_pending_revocation(
        {"user_id": sub, "valid_after": time.time() + 7200, "reason": "late"}
    )
    assert marks.state_is_blocked(mem_cache.store[marks.subject_key(sub)])
    assert await verification.token_access_status(token) == verification.TOKEN_BLOCKED
    assert (
        await verification.token_access_status(_claims(sub))
        == verification.TOKEN_BLOCKED
    )


async def test_unblock_never_resurrects_pre_block_tokens(
    mem_cache: InMemoryCache,
) -> None:
    sub = str(uuid.uuid4())
    pre_block = _claims(sub)
    with recording_uow() as session:
        await blocks.block_user(sub, reason="leak", actor_id=None)
        await run_on_commit(session)
    # minted while blocked by ANOTHER context (no issue floor of this one)
    during = contextvars.Context().run(_claims, sub)
    with recording_uow() as session:
        await blocks.unblock_user(sub, reason="cleared", actor_id=None)
        # not lifted before the unblock commits
        assert await blocks.is_user_blocked(sub) is True
        await run_on_commit(session)
    raw = mem_cache.store[marks.subject_key(sub)]
    assert parse_subject_state(raw) is not None
    assert not marks.state_is_blocked(raw)  # swapped, never deleted
    for token in (pre_block, during):
        assert (
            await verification.token_access_status(token) == verification.TOKEN_REVOKED
        )


async def test_a_rolled_back_unblock_keeps_the_block(mem_cache: InMemoryCache) -> None:
    sub = str(uuid.uuid4())
    with recording_uow() as session:
        await blocks.block_user(sub, reason="leak", actor_id=None)
        await run_on_commit(session)
    with recording_uow():
        await blocks.unblock_user(sub, reason="cleared", actor_id=None)
        # the UoW never commits: nothing runs on_commit
    assert await blocks.is_user_blocked(sub) is True


async def test_a_corrupt_record_fails_closed_and_issuance_repairs_it(
    mem_cache: InMemoryCache,
) -> None:
    sub = str(uuid.uuid4())
    stale = _claims(sub)
    mem_cache.store[marks.subject_key(sub)] = "corrupt"
    assert await verification.token_access_status(stale) == verification.TOKEN_REVOKED
    await blocks.ensure_user_not_blocked(sub)  # the issuer's live check repairs
    fresh = _claims(sub)
    assert marks._parse_mark_us(mem_cache.store[marks.subject_key(sub)]) is not None
    assert await verification.token_access_status(stale) == verification.TOKEN_REVOKED
    assert await verification.token_access_status(fresh) == verification.TOKEN_OK


async def test_the_hot_path_reads_one_key(mem_cache: InMemoryCache) -> None:
    sub = str(uuid.uuid4())
    reads: list[str] = []
    original = InMemoryCache.get

    async def counting(self: InMemoryCache, key: str) -> str | None:
        reads.append(key)
        return await original(self, key)

    mem_cache.get = counting.__get__(mem_cache)  # type: ignore[method-assign]
    await verification.token_access_status(_claims(sub))
    assert reads == [marks.subject_key(sub)]


# --------------------------------------------------------------------------- #
# Redis: the fused op is one atomic Lua script with the same order
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def live() -> AsyncIterator[RedisCacheProvider]:
    from sandloom.core.testing.workers import worker_env_values

    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    url = os.getenv("CACHE_URL") or worker_env_values(env_test).get("CACHE_URL")
    if not url:
        pytest.skip("CACHE_URL not configured")
    provider = RedisCacheProvider(
        name="subject", driver="redis", options={"url": url}, shared=False
    )
    try:
        await asyncio.wait_for(provider.get("sandloom:test:ping"), 5)
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip
        await provider.aclose()
        pytest.skip("test Redis not reachable")
    yield provider
    await provider.aclose()


async def test_redis_subject_state_order_and_ttl(live: RedisCacheProvider) -> None:
    assert "subject_state" in live.features()
    key = f"t:subject:{uuid.uuid4()}"
    client = await live.raw_client()
    assert await live.set_subject_state(key, _after(100.5), ttl_seconds=30) == (
        _after(100.5)
    )
    assert 0 < await client.ttl(key) <= 30
    assert await live.set_subject_state(key, _after(100.4), ttl_seconds=5) == (
        _after(100.5)
    )
    # a block outranks and clears the TTL (no expiry)
    assert await live.set_subject_state(key, _blocked(1)) == _blocked(1)
    assert await client.ttl(key) == -1
    # no revocation, however late, unblocks
    assert await live.set_subject_state(
        key, _after(9_999_999_999), ttl_seconds=30
    ) == _blocked(1)
    # a corrupt value is replaced
    await live.set(key, "1700000000")
    assert await live.set_subject_state(key, _after(0)) == _after(0)
    with pytest.raises(ValueError):
        await live.set_subject_state(key, "12")
    await live.delete(key)


async def test_redis_concurrent_block_and_revocations_end_blocked(
    live: RedisCacheProvider,
) -> None:
    key = f"t:subject:{uuid.uuid4()}"
    values = [_after(1_700_000_000 + i) for i in range(200)] + [_blocked(5)]
    random.Random(3).shuffle(values)
    await asyncio.gather(
        *(live.set_subject_state(key, v, ttl_seconds=30) for v in values)
    )
    assert await live.get(key) == _blocked(5)
    await live.delete(key)


async def test_redis_unblock_swaps_and_never_deletes(
    live: RedisCacheProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = str(uuid.uuid4())
    key = marks.subject_key(sub)
    await live.set_subject_state(key, _blocked(time.time()))
    assert await blocks._swap_blocked_for_after(live, sub) is True
    raw = await live.get(key)
    assert raw is not None and not marks.state_is_blocked(raw)
    assert 0 < await (await live.raw_client()).ttl(key)
    # nothing to lift any more
    assert await blocks._swap_blocked_for_after(live, sub) is False
    await live.delete(key)
