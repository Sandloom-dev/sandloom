"""Revocation hardening (PR #46 review): corrupt marks fail closed, and
``require_immediate`` revocations close the commit -> access-stops window.

1. A mark that exists but cannot be parsed used to be IGNORED (fail-open).
   Now every token of the subject is rejected (``auth_revocation_corrupt_mark
   _total``) and the next issuance check repairs the mark, so a re-login works.
2. ``revoke_user_tokens_on_commit(..., require_immediate=True)`` writes the
   mark BEFORE commit or refuses the change (``RevocationUnavailableError``,
   503) — so a committed change always has its mark, whatever happens to the
   cache afterwards; a second post-commit mark covers tokens an issuer minted
   between the early mark and the commit.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest

from sandloom.core.auth import blocks, marks, revocation, tokens, verification
from sandloom.core.config.settings import settings
from sandloom.core.testing import InMemoryCache, recording_uow, run_on_commit


@pytest.fixture
def mem_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryCache:
    import sandloom.core.infra.access as access

    fake = InMemoryCache()
    monkeypatch.setattr(access, "cache", lambda name="default": fake)
    monkeypatch.setattr(
        settings, "auth_revocation_clock_skew_seconds", 2.0, raising=False
    )
    monkeypatch.setattr(
        settings, "auth_revocation_retry_base_seconds", 0.0, raising=False
    )
    return fake


def _claims(sub: str) -> dict[str, Any]:
    claims = tokens.decode_token(
        tokens.create_access_token({"sub": sub, "token_type": "access"})
    )
    assert claims is not None
    return claims


def _key(sub: str) -> str:
    return marks.subject_key(sub)


# --------------------------------------------------------------------------- #
# 1. Corrupt marks fail closed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "corrupt",
    [
        "garbage",
        "nan",
        "inf",
        "",
        "1.2.3",
        "1e9",
        "-5",
        "+7",
        # the pre-ADR-0009 plain-decimal mark and malformed record values
        "1700000000.5",
        "v1:after:",
        "v1:after:-5",
        "v1:blocked:x",
        "v2:after:1",
        "v1:frozen:1",
    ],
)
async def test_corrupt_mark_rejects_every_token_and_is_counted(
    mem_cache: InMemoryCache, corrupt: str
) -> None:
    sub = str(uuid4())
    token = _claims(sub)
    mem_cache.store[_key(sub)] = corrupt
    before = marks.revocation_metrics_snapshot()["corrupt"]

    assert await verification.is_token_revoked(token) is True
    assert await verification.token_access_status(token) == verification.TOKEN_REVOKED
    assert marks.revocation_metrics_snapshot()["corrupt"] == before + 2


async def test_issuance_check_repairs_a_corrupt_mark_so_relogin_works(
    mem_cache: InMemoryCache,
) -> None:
    from sandloom.core.auth.blocks import ensure_user_not_blocked

    sub = str(uuid4())
    stale = _claims(sub)
    mem_cache.store[_key(sub)] = "corrupt"

    before_repair = time.time()
    await ensure_user_not_blocked(sub)  # the issuer's live check
    fresh = _claims(sub)

    # Repaired to NOW (+ skew) — never removed, never lowered: every token
    # issued before the repair stays revoked.
    repaired = marks._parse_mark_us(mem_cache.store[_key(sub)])
    assert repaired is not None
    assert repaired >= marks._seconds_to_us(before_repair)
    assert await verification.is_token_revoked(stale) is True
    assert await verification.is_token_revoked(fresh) is False


async def test_a_revocation_overwrites_a_corrupt_mark(mem_cache: InMemoryCache) -> None:
    sub = str(uuid4())
    mem_cache.store[_key(sub)] = "corrupt"
    assert await revocation.revoke_user_tokens(sub, valid_after=time.time()) is True
    assert marks._parse_mark_us(mem_cache.store[_key(sub)]) is not None


async def test_corrupt_mark_rejects_on_budgeted_reads_too(
    mem_cache: InMemoryCache,
) -> None:
    """There are no budgeted reads any more (ADR-0009): the corrupt record is
    read live by every check, and every check rejects."""
    sub = str(uuid4())
    mem_cache.store[_key(sub)] = "not-a-record"
    for _ in range(3):
        assert await verification.is_token_revoked(_claims(sub)) is True


async def test_any_block_value_blocks(mem_cache: InMemoryCache) -> None:
    """ADR-0009 item 13: the block is a value of the one subject-state record;
    it blocks tokens minted before AND after it. An unparsable value is not a
    block but still fails closed (revoked)."""
    sub = str(uuid4())
    before = _claims(sub)
    mem_cache.store[_key(sub)] = marks._format_blocked(marks._seconds_to_us(1.0))
    after = _claims(sub)
    for token in (before, after):
        assert (
            await verification.token_access_status(token) == verification.TOKEN_BLOCKED
        )
    mem_cache.store[_key(sub)] = "\x00garbage"
    assert await verification.token_access_status(after) == verification.TOKEN_REVOKED


# --------------------------------------------------------------------------- #
# 2. require_immediate
# --------------------------------------------------------------------------- #
async def test_immediate_revocation_writes_the_mark_before_commit(
    mem_cache: InMemoryCache,
) -> None:
    sub = str(uuid4())
    old = _claims(sub)
    with recording_uow() as session:
        await revocation.revoke_user_tokens_on_commit(
            sub, reason="test", require_immediate=True
        )
        # Before commit: the mark is there, the old token is already rejected.
        assert _key(sub) in mem_cache.store
        assert await verification.is_token_revoked(old) is True
        channels = [c for c, _, _ in session.events]
        assert channels == [marks.TOKENS_REVOKED_CHANNEL]  # still durable
        await run_on_commit(session)


async def test_immediate_revocation_refuses_the_change_on_a_cache_outage(
    mem_cache: InMemoryCache,
) -> None:
    from sandloom.core.http.error_handlers import http_status_of

    mem_cache.broken = True
    before = marks.revocation_metrics_snapshot()["immediate_refused"]
    with recording_uow() as session:
        with pytest.raises(marks.RevocationUnavailableError) as exc:
            await revocation.revoke_user_tokens_on_commit(
                str(uuid4()), reason="test", require_immediate=True
            )
        assert session.events == []  # nothing durable: the UoW rolls back
    assert http_status_of(exc.value) == 503
    assert marks.revocation_metrics_snapshot()["immediate_refused"] == before + 1


async def test_immediate_post_commit_remark_revokes_tokens_minted_before_commit(
    mem_cache: InMemoryCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An issuer that read the EARLY mark (before commit) collected claims
    from the pre-change state and stamped its token after that mark: the
    post-commit re-mark (``commit time + skew``) revokes it."""
    import asyncio
    import contextvars

    sub = str(uuid4())
    with recording_uow() as session:
        mark = await revocation.revoke_user_tokens_on_commit(
            sub, reason="test", require_immediate=True
        )

        async def _other_issuer() -> dict[str, Any]:
            from sandloom.core.auth.blocks import ensure_user_not_blocked

            await ensure_user_not_blocked(sub)  # reads the early mark
            return _claims(sub)

        # Another request (its own context), before this one commits.
        gap = await asyncio.create_task(_other_issuer(), context=contextvars.Context())
        assert gap[marks.IAT_US_CLAIM] > marks._seconds_to_us(mark)
        # Commit happens a little later than the early mark was taken.
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 0.05)
        await run_on_commit(session)
    assert await verification.is_token_revoked(gap) is True


async def test_tokens_the_revoking_request_mints_survive_the_post_commit_remark(
    mem_cache: InMemoryCache,
) -> None:
    sub = str(uuid4())
    with recording_uow() as session:
        await revocation.revoke_user_tokens_on_commit(
            sub, reason="password_changed", require_immediate=True
        )
        fresh = _claims(sub)  # e.g. the new pair of a password change
        await run_on_commit(session)
    assert await verification.is_token_revoked(fresh) is False


async def test_immediate_revocation_survives_a_post_commit_cache_failure(
    mem_cache: InMemoryCache,
) -> None:
    """The early mark already holds: losing the post-commit write (deferred to
    the applier) cannot re-open the window for tokens minted before it."""
    sub = str(uuid4())
    old = _claims(sub)
    with recording_uow() as session:
        await revocation.revoke_user_tokens_on_commit(
            sub, reason="test", require_immediate=True
        )
        mem_cache.broken = True  # the cache dies right after the early write
        await run_on_commit(session)  # -> deferred, logged
    mem_cache.broken = False
    assert await verification.is_token_revoked(old) is True


async def test_a_commit_slower_than_the_skew_revokes_the_requests_own_tokens_loudly(
    mem_cache: InMemoryCache,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review GLM-1: the post-commit re-mark is ``commit time + skew``, the
    request's own tokens are stamped at ``call + 2 * skew``. A commit more than
    one skew after the call therefore revokes the fresh pair too. That is the
    fail-closed side of an inherent trade-off (an issuer's stale-claims token
    minted in that gap is indistinguishable at the subject mark) — kept, but
    it must be observable, never silent."""
    import logging

    sub = str(uuid4())
    before = marks.revocation_metrics_snapshot()["remark_overtook_floor"]
    with recording_uow() as session:
        await revocation.revoke_user_tokens_on_commit(
            sub, reason="password_changed", require_immediate=True
        )
        fresh = _claims(sub)
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 2.5)
        with caplog.at_level(logging.WARNING, logger=blocks.logger.name):
            await run_on_commit(session)
    assert await verification.is_token_revoked(fresh) is True  # fail-closed
    after = marks.revocation_metrics_snapshot()["remark_overtook_floor"]
    assert after == before + 1
    assert any("re-authenticate" in r.getMessage() for r in caplog.records)


async def test_a_prompt_commit_does_not_count_an_overtaken_floor(
    mem_cache: InMemoryCache,
) -> None:
    sub = str(uuid4())
    before = marks.revocation_metrics_snapshot()["remark_overtook_floor"]
    with recording_uow() as session:
        await revocation.revoke_user_tokens_on_commit(
            sub, reason="password_changed", require_immediate=True
        )
        await run_on_commit(session)
    after = marks.revocation_metrics_snapshot()["remark_overtook_floor"]
    assert after == before


# --------------------------------------------------------------------------- #
# 3. Mark-TTL drift (review GLM-2)
# --------------------------------------------------------------------------- #
def _aged_claims(sub: str, age_s: float) -> dict[str, Any]:
    claims = _claims(sub)
    claims[marks.IAT_US_CLAIM] -= marks._seconds_to_us(age_s)
    claims["iat"] = int(claims["iat"]) - int(age_s)
    return claims


async def test_a_token_older_than_the_mark_ttl_is_rejected_when_no_mark_exists(
    mem_cache: InMemoryCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token older than the revocation-mark TTL may have been revoked by a
    mark that has since expired (an issuer configured with a longer
    ACCESS_TOKEN_EXPIRE_MINUTES than the writer): the absence of a mark proves
    nothing for it, so it fails closed — loudly — instead of being revived."""
    monkeypatch.setattr(settings, "access_token_expire_minutes", 15)
    monkeypatch.setattr(
        settings, "auth_revocation_mark_ttl_seconds", None, raising=False
    )
    ttl = marks._revocation_ttl_seconds()
    sub = str(uuid4())
    before = marks.revocation_metrics_snapshot()["outlived_mark_ttl"]

    stale = _aged_claims(sub, ttl + 60)
    assert await verification.is_token_revoked(stale) is True
    assert await verification.token_access_status(stale) == verification.TOKEN_REVOKED
    assert marks.revocation_metrics_snapshot()["outlived_mark_ttl"] == before + 2

    young = _aged_claims(sub, 600)
    assert await verification.is_token_revoked(young) is False


async def test_mark_ttl_guard_follows_a_configured_ttl(
    mem_cache: InMemoryCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "access_token_expire_minutes", 15)
    monkeypatch.setattr(
        settings, "auth_revocation_mark_ttl_seconds", 7200, raising=False
    )
    sub = str(uuid4())
    assert await verification.is_token_revoked(_aged_claims(sub, 3600)) is False
    assert await verification.is_token_revoked(_aged_claims(sub, 7300)) is True


def test_an_explicit_zero_lifetime_is_not_replaced_by_the_default() -> None:
    """Review GLM-7: ``expires_delta or default`` turned an explicit ``0`` into
    the full default lifetime."""
    import jwt

    token = tokens.create_access_token(
        {"sub": str(uuid4()), "token_type": "access"}, expires_delta=0
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] <= 1
