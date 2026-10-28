"""Identity's durable token-revocation applier (``core.auth.tokens_revoked.v1``).

A revocation whose mark could not be written after commit (cache outage) is
still in the outbox event every ``revoke_user_tokens_on_commit`` writes in its
transaction; identity's consumer applies it, and keeps failing (so the event
is redelivered) until the mark is written.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sandloom.sdk.security import ensure_token_not_revoked, revoke_user_tokens
from sandloom.sdk.testing import InMemoryCache, revocation_mark_key, use_security_cache
from sandloom_identity.sessions import apply_token_revocation


@pytest.fixture
def mem_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryCache:
    fake = InMemoryCache()
    use_security_cache(monkeypatch, fake)
    return fake


def _envelope(user_id: str, valid_after: float) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "payload": {"user_id": user_id, "valid_after": valid_after, "reason": "t"},
    }


async def test_applier_writes_the_pending_mark_once_the_cache_is_back(
    mem_cache: InMemoryCache,
) -> None:
    sub = str(uuid4())
    mark = time.time() + 2.0
    envelope = _envelope(sub, mark)
    mem_cache.broken = True
    with pytest.raises(RuntimeError) as unavailable:
        await apply_token_revocation(None, envelope)  # -> redelivered
    assert type(unavailable.value).__name__ == "RevocationUnavailableError"

    mem_cache.broken = False
    await apply_token_revocation(None, envelope)
    await apply_token_revocation(None, envelope)  # duplicate delivery: no-op
    stored = mem_cache.store[revocation_mark_key(sub)]
    assert float(stored.removeprefix("v1:after:")) == pytest.approx(mark, abs=1e-6)
    issued_before = {"sub": sub, "iat": int(mark) - 5}
    with pytest.raises(HTTPException) as revoked:
        await ensure_token_not_revoked(None, issued_before)
    assert revoked.value.status_code == 401


async def test_applier_never_moves_a_newer_mark_backwards(
    mem_cache: InMemoryCache,
) -> None:
    sub = str(uuid4())
    newer = time.time() + 60
    await revoke_user_tokens(sub, valid_after=newer)
    await apply_token_revocation(None, _envelope(sub, newer - 30))  # late, stale
    stored = mem_cache.store[revocation_mark_key(sub)]
    assert float(stored.removeprefix("v1:after:")) == pytest.approx(newer, abs=1e-6)
