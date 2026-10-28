"""Refresh-token cleanup runs in bounded batches and deletes exactly the
rows it did before (docs/design/sql-profile-2026-10-21.md).

The beat tasks used to issue one unbounded DELETE each (650k expired tokens:
one 0.5 s transaction). They now loop ``limit``-sized batches, each in its own
Unit of Work; the revoked-by-age cleanup adds the implied ``expires_at`` bound
so ``ix_refresh_tokens_expires_at`` narrows the scan (``revoked_at`` has no
index on purpose: rotation updates it on every refresh).
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from sandloom.sdk.infra import settings
from sandloom_identity import tasks
from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.services.token_service import TokenService

TTL = timedelta(days=settings.refresh_token_expire_days)


async def _token(
    db: Any, user_id: Any, *, minted_ago: timedelta, revoked_after: timedelta | None
) -> str:
    now = datetime.now(UTC)
    minted = now - minted_ago
    token = RefreshToken(
        user_id=user_id,
        family_id=uuid4(),
        token=uuid4().hex,
        expires_at=minted + TTL,
        is_revoked=revoked_after is not None,
        revoked_at=None if revoked_after is None else minted + revoked_after,
    )
    db.add(token)
    await db.flush()
    return token.token


async def _left(db: Any) -> set[str]:
    return set((await db.scalars(select(RefreshToken.token))).all())


@pytest.mark.asyncio
async def test_expired_cleanup_in_batches(test_db: Any, make_user: Any) -> None:
    user = await make_user("cleanup-exp@example.com")
    expired = TTL + timedelta(days=1)
    for _ in range(3):
        await _token(test_db, user.id, minted_ago=expired, revoked_after=None)
    live = await _token(test_db, user.id, minted_ago=timedelta(0), revoked_after=None)

    assert await TokenService.cleanup_expired_tokens(test_db, limit=2) == 2
    assert await TokenService.cleanup_expired_tokens(test_db, limit=2) == 1
    assert await TokenService.cleanup_expired_tokens(test_db, limit=0) == 0
    assert await _left(test_db) == {live}


@pytest.mark.asyncio
async def test_revoked_by_age_deletes_only_tokens_revoked_before_the_cutoff(
    test_db: Any, make_user: Any
) -> None:
    user = await make_user("cleanup-rev@example.com")
    old = await _token(
        test_db,
        user.id,
        minted_ago=timedelta(days=40),
        revoked_after=timedelta(hours=1),
    )
    # Revoked 20 days ago: younger than the 30-day cutoff, kept.
    recent = await _token(
        test_db, user.id, minted_ago=timedelta(days=21), revoked_after=timedelta(days=1)
    )
    # Never revoked (and possibly expired): not this cleanup's business.
    unrevoked = await _token(
        test_db, user.id, minted_ago=timedelta(days=40), revoked_after=None
    )

    assert await TokenService.cleanup_revoked_tokens_by_age(test_db, 30, limit=10) == 1
    assert await _left(test_db) == {recent, unrevoked}
    assert old not in await _left(test_db)
    # Unbounded form: same rows (none left to delete).
    assert await TokenService.cleanup_revoked_tokens_by_age(test_db, 30) == 0


@pytest.mark.asyncio
async def test_task_loops_until_a_short_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "CLEANUP_BATCH_SIZE", 2)
    sizes = iter([2, 2, 1])
    units = 0

    class _Unit:
        async def __aenter__(self) -> object:
            nonlocal units
            units += 1
            return object()

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(tasks, "open_uow", _Unit)

    async def purge(_sess: object) -> int:
        return next(sizes)

    assert await tasks._purge_in_batches(purge) == 5
    assert units == 3
