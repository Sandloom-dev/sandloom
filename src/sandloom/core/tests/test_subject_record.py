"""ADR-0009 item 13: one subject-state record replaces the two marks.

``sandloom:auth:subject:{id}`` holds ``v1:after:<us>`` (revocation, TTL, monotonic)
or ``v1:blocked:<since>`` (no TTL), and every ``blocked`` value ranks above
every ``after`` value. Pinned here, against the in-memory
``atomic_store`` (the Redis one: ``packages/sandloom-redis/tests/test_redis_subject_record.py``):

* a revocation concurrent with (or later than) a block never unblocks;
* an unblock swaps ``blocked`` for ``after:<now + skew>`` by compare-and-set,
  so every token issued before the unblock stays revoked (it never deletes);
* a corrupt record fails closed and the issuance check repairs it;
* the hot path reads ONE key (one ``GET``).
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from typing import Any

import pytest

from sandloom.core.auth import blocks, marks, revocation, tokens, verification
from sandloom.core.config.settings import settings
from sandloom.core.infra import access
from sandloom.core.infra.ports.atomic_store import parse_subject_state
from sandloom.core.testing import (
    InMemoryAtomicStore,
    recording_uow,
    run_on_commit,
)


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
def mem_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryAtomicStore:
    # The composed app registers the core event schemas on its bus
    # (block_user publishes core.auth.*): make it the active runtime.
    from sandloom.core import runtime
    from sandloom.core.composition import load_composition_app

    app = load_composition_app()

    monkeypatch.setattr(runtime, "_process_core", app.state.core)
    fake = InMemoryAtomicStore()
    monkeypatch.setattr(access, "atomic_store", lambda role="security": fake)
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


# --------------------------------------------------------------------------- #
# block / revocation / unblock semantics
# --------------------------------------------------------------------------- #
async def test_a_revocation_concurrent_with_a_block_never_unblocks(
    mem_cache: InMemoryAtomicStore,
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
    mem_cache: InMemoryAtomicStore,
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


async def test_a_rolled_back_unblock_keeps_the_block(
    mem_cache: InMemoryAtomicStore,
) -> None:
    sub = str(uuid.uuid4())
    with recording_uow() as session:
        await blocks.block_user(sub, reason="leak", actor_id=None)
        await run_on_commit(session)
    with recording_uow():
        await blocks.unblock_user(sub, reason="cleared", actor_id=None)
        # the UoW never commits: nothing runs on_commit
    assert await blocks.is_user_blocked(sub) is True


async def test_a_corrupt_record_fails_closed_and_issuance_repairs_it(
    mem_cache: InMemoryAtomicStore,
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


async def test_the_hot_path_reads_one_key(mem_cache: InMemoryAtomicStore) -> None:
    sub = str(uuid.uuid4())
    reads: list[str] = []
    original = InMemoryAtomicStore.get

    async def counting(self: InMemoryAtomicStore, key: str) -> str | None:
        reads.append(key)
        return await original(self, key)

    mem_cache.get = counting.__get__(mem_cache)  # type: ignore[method-assign]
    await verification.token_access_status(_claims(sub))
    assert reads == [marks.subject_key(sub)]
