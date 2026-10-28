"""Revocation windows found by the ADR review (PR #44).

1. Granularity: a mark of whole seconds let a token issued EARLIER in the same
   second as the revocation survive it.
2. Clock skew: an issuer whose clock runs ahead of the revoker's stamps a token
   minted shortly BEFORE the revocation with a later ``iat``.
3. Durability: a failed mark write (cache outage) was logged and dropped.

Rule pinned here: a token is revoked iff its issue instant (``iat_us``, the
microsecond issued-at; legacy tokens: whole-second ``iat``) is ``<=`` the mark.
The mark is written at ``now + AUTH_REVOCATION_CLOCK_SKEW_SECONDS``; a token
minted AFTER the mark is visible to its issuer (or later in the very request
that revoked) is stamped past it and stays valid.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from sandloom.core.auth import marks, revocation, tokens, verification
from sandloom.core.config.settings import settings
from sandloom.core.tests.test_auth_rotation_revocation import _MemoryCache


@pytest.fixture
def mem_cache(monkeypatch: pytest.MonkeyPatch) -> _MemoryCache:
    import sandloom.core.infra.access as access

    fake = _MemoryCache()
    monkeypatch.setattr(access, "cache", lambda name="default": fake)
    return fake


@pytest.fixture
def skew(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _set(seconds: float) -> None:
        monkeypatch.setattr(
            settings, "auth_revocation_clock_skew_seconds", seconds, raising=False
        )

    _set(0.0)
    return _set


def _claims_issued_at(sub: str, instant: float) -> dict[str, Any]:
    """Decoded claims of a token minted at ``instant`` (unix seconds)."""
    iat = datetime.fromtimestamp(instant, UTC)
    token = tokens.create_access_token({"sub": sub, "token_type": "access", "iat": iat})
    claims = tokens.decode_token(token)
    assert claims is not None
    return claims


async def _revoke_at(monkeypatch: pytest.MonkeyPatch, sub: str, instant: float) -> None:
    """``revoke_user_tokens(sub)`` with the revoker's clock reading ``instant``."""
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: instant)
        assert await revocation.revoke_user_tokens(sub) is True


# --------------------------------------------------------------------------- #
# 1. Sub-second precision
# --------------------------------------------------------------------------- #
async def test_token_issued_earlier_in_the_same_second_is_revoked(
    mem_cache: _MemoryCache, skew: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = str(uuid4())
    second = float(int(time.time()) - 30)
    before = _claims_issued_at(sub, second + 0.1)
    after = _claims_issued_at(sub, second + 0.95)

    await _revoke_at(monkeypatch, sub, second + 0.9)

    assert await verification.is_token_revoked(before) is True
    assert await verification.is_token_revoked(after) is False


async def test_a_token_without_iat_us_is_revoked_by_any_mark(
    mem_cache: _MemoryCache, skew: Any
) -> None:
    """A token without ``iat_us`` cannot prove it was issued after the mark:
    any mark rejects it (fail closed); a whole-second mark still parses."""
    sub = str(uuid4())
    second = int(time.time()) - 30
    mem_cache.store[marks.subject_key(sub)] = f"v1:after:{second}.5"
    assert await verification.is_token_revoked({"sub": sub, "iat": second + 1}) is True
    us = (second + 1) * 1_000_000
    assert await verification.is_token_revoked({"sub": sub, "iat_us": us}) is False
    mem_cache.store[marks.subject_key(sub)] = f"v1:after:{second}"
    assert await verification.is_token_revoked({"sub": sub, "iat_us": us}) is False


async def test_mark_never_moves_backwards_at_sub_second_precision(
    mem_cache: _MemoryCache, skew: Any
) -> None:
    sub = str(uuid4())
    base = float(int(time.time()))
    await revocation.revoke_user_tokens(sub, valid_after=base + 0.7)
    await revocation.revoke_user_tokens(sub, valid_after=base + 0.2)
    stored = mem_cache.store[marks.subject_key(sub)]
    assert (marks._parse_mark_us(stored) / 1_000_000) == pytest.approx(
        base + 0.7, abs=1e-6
    )


# --------------------------------------------------------------------------- #
# 2. Clock skew between issuer and revoker
# --------------------------------------------------------------------------- #
async def test_skew_margin_rejects_tokens_from_an_issuer_clock_running_ahead(
    mem_cache: _MemoryCache, skew: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    skew(2.0)
    sub = str(uuid4())
    revoked_at = float(int(time.time()) - 30)
    # Minted 0.5 s BEFORE the revocation by an issuer whose clock is 1 s ahead.
    ahead = _claims_issued_at(sub, revoked_at - 0.5 + 1.0)

    await _revoke_at(monkeypatch, sub, revoked_at)

    assert await verification.is_token_revoked(ahead) is True


async def test_issuer_that_saw_the_mark_mints_past_the_margin(
    mem_cache: _MemoryCache, skew: Any
) -> None:
    """The margin costs nothing to a framework issuer: the issuance check
    (``ensure_user_not_blocked``) reads the mark, and the token is stamped
    after it — so a re-login right after a removal is accepted."""
    from sandloom.core.auth.blocks import ensure_user_not_blocked

    skew(2.0)
    sub = str(uuid4())
    await revocation.revoke_user_tokens(sub)
    await ensure_user_not_blocked(sub)
    fresh = tokens.decode_token(
        tokens.create_access_token({"sub": sub, "token_type": "access"})
    )
    assert fresh is not None
    assert await verification.is_token_revoked(fresh) is False
    # A token minted WITHOUT the issuance check (it never saw the mark) is
    # inside the margin and rejected: that is the documented trade-off.
    other = str(uuid4())
    # revoked by ANOTHER context (another request / service): never seen here
    await asyncio.create_task(revocation.revoke_user_tokens(other))
    blind = tokens.decode_token(
        tokens.create_access_token({"sub": other, "token_type": "access"})
    )
    assert blind is not None
    assert await verification.is_token_revoked(blind) is True


async def test_tokens_minted_later_in_the_revoking_request_stay_valid(
    mem_cache: _MemoryCache, skew: Any
) -> None:
    """A password change revokes and returns a fresh pair in ONE request."""
    from sandloom.core.testing import recording_uow, run_on_commit

    skew(2.0)
    sub = str(uuid4())
    with recording_uow() as session:
        await revocation.revoke_user_tokens_on_commit(sub, reason="password_changed")
        fresh = tokens.decode_token(
            tokens.create_access_token({"sub": sub, "token_type": "access"})
        )
        await run_on_commit(session)
    assert fresh is not None
    assert marks.subject_key(sub) in mem_cache.store
    assert await verification.is_token_revoked(fresh) is False


# --------------------------------------------------------------------------- #
# 3. Durable, retried mark writes
# --------------------------------------------------------------------------- #
class _FlakyCache(_MemoryCache):
    """Fails the first ``failures`` writes, then behaves."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("cache blip")
        return await super().add(key, value, ttl_seconds=ttl_seconds)


async def test_on_commit_revocation_retries_a_transient_cache_failure(
    monkeypatch: pytest.MonkeyPatch, skew: Any
) -> None:
    import sandloom.core.infra.access as access
    from sandloom.core.testing import recording_uow, run_on_commit

    fake = _FlakyCache(failures=2)
    monkeypatch.setattr(access, "cache", lambda name="default": fake)
    monkeypatch.setattr(
        settings, "auth_revocation_retry_base_seconds", 0.0, raising=False
    )
    sub = str(uuid4())
    with recording_uow() as session:
        await revocation.revoke_user_tokens_on_commit(sub, reason="test")
        await run_on_commit(session)
    assert marks.subject_key(sub) in fake.store


async def test_revocation_is_persisted_in_the_uow_for_the_durable_applier(
    mem_cache: _MemoryCache, skew: Any
) -> None:
    """The pending revocation is written to the outbox IN the business
    transaction, so a mark write that fails after commit is applied later."""
    from sandloom.core.testing import recording_uow

    sub = str(uuid4())
    with recording_uow() as session:
        mark = await revocation.revoke_user_tokens_on_commit(sub, reason="test")
        events = list(session.events)
    revoked = [p for c, p, _ in events if c == marks.TOKENS_REVOKED_CHANNEL]
    assert revoked == [{"user_id": sub, "valid_after": mark, "reason": "test"}]


async def test_exhausted_retries_count_a_deferred_revocation(
    monkeypatch: pytest.MonkeyPatch, skew: Any
) -> None:
    import sandloom.core.infra.access as access
    from sandloom.core.testing import recording_uow, run_on_commit

    broken = _MemoryCache(broken=True)
    monkeypatch.setattr(access, "cache", lambda name="default": broken)
    monkeypatch.setattr(
        settings, "auth_revocation_retry_base_seconds", 0.0, raising=False
    )
    before = marks.revocation_metrics_snapshot()["deferred"]
    with recording_uow() as session:
        await revocation.revoke_user_tokens_on_commit(str(uuid4()), reason="test")
        await run_on_commit(session)
    assert marks.revocation_metrics_snapshot()["deferred"] == before + 1


async def test_apply_pending_revocation_is_idempotent_and_raises_on_outage(
    mem_cache: _MemoryCache, skew: Any
) -> None:
    sub = str(uuid4())
    payload = {"user_id": sub, "valid_after": time.time(), "reason": "test"}
    await revocation.apply_pending_revocation(payload)
    await revocation.apply_pending_revocation(payload)  # at-least-once: no-op
    assert (
        marks._parse_mark_us(mem_cache.store[marks.subject_key(sub)]) / 1_000_000
    ) == pytest.approx(payload["valid_after"], abs=1e-6)
    mem_cache.broken = True
    with pytest.raises(marks.RevocationUnavailableError):
        await revocation.apply_pending_revocation(payload)
    mem_cache.broken = False
    await revocation.apply_pending_revocation(payload)  # clears the stuck entry
    assert revocation.pending_revocations()["count"] == 0
