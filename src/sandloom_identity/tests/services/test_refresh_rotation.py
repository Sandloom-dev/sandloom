"""Refresh-token rotation: one-time use, reuse detection, CAS against races."""

import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom_identity.errors import CommittedHTTPError
from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.services.token_service import TokenService
from sandloom_identity.utils import RefreshTokenUtils


async def age_rotation(db: AsyncSession, raw_token: str, seconds: int = 60) -> None:
    """Move a rotated token's ``revoked_at`` past the rotation grace window."""
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.token == RefreshTokenUtils.hash_token(raw_token))
        .values(revoked_at=RefreshToken.revoked_at - timedelta(seconds=seconds))
    )
    await db.commit()


async def _family_rows(db: AsyncSession, family_id) -> list[RefreshToken]:
    db.expire_all()
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.family_id == family_id)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_refresh_rotates_within_family(test_db, make_user) -> None:
    user = await make_user("rot@example.com")
    first = await TokenService.create_tokens(user, test_db)
    await test_db.commit()

    second = await TokenService.refresh_token(first.refresh_token, test_db)
    await test_db.commit()

    assert second.refresh_token != first.refresh_token
    rows = (await test_db.execute(select(RefreshToken))).scalars().all()
    assert len({r.family_id for r in rows}) == 1  # same family
    revoked = [r for r in rows if r.is_revoked]
    assert len(rows) == 2 and len(revoked) == 1
    assert revoked[0].revoked_at is not None


@pytest.mark.asyncio
async def test_reusing_rotated_token_revokes_whole_family(test_db, make_user) -> None:
    user = await make_user("reuse@example.com")
    first = await TokenService.create_tokens(user, test_db)
    await test_db.commit()
    second = await TokenService.refresh_token(first.refresh_token, test_db)
    await test_db.commit()
    await age_rotation(test_db, first.refresh_token)

    # Attacker (or victim) replays the already-rotated token (beyond grace).
    with pytest.raises(CommittedHTTPError) as exc:
        await TokenService.refresh_token(first.refresh_token, test_db)
    assert exc.value.status_code == 401
    await test_db.commit()  # what the endpoint does: the revocation persists

    family = (
        await test_db.execute(select(RefreshToken.family_id).limit(1))
    ).scalar_one()
    assert all(r.is_revoked for r in await _family_rows(test_db, family))

    # The legitimately rotated token is dead too.
    with pytest.raises(HTTPException):
        await TokenService.refresh_token(second.refresh_token, test_db)


@pytest.mark.asyncio
async def test_other_families_survive_reuse_detection(test_db, make_user) -> None:
    user = await make_user("fam@example.com")
    device_a = await TokenService.create_tokens(user, test_db)
    device_b = await TokenService.create_tokens(user, test_db)
    await test_db.commit()
    await TokenService.refresh_token(device_a.refresh_token, test_db)
    await test_db.commit()
    await age_rotation(test_db, device_a.refresh_token)
    with pytest.raises(CommittedHTTPError):
        await TokenService.refresh_token(device_a.refresh_token, test_db)
    await test_db.commit()

    # Device B's session (separate family) is untouched.
    rotated = await TokenService.refresh_token(device_b.refresh_token, test_db)
    assert rotated.refresh_token


@pytest.mark.asyncio
async def test_concurrent_refresh_only_one_wins(
    test_engine: AsyncEngine, test_db, make_user
) -> None:
    user = await make_user("race@example.com")
    pair = await TokenService.create_tokens(user, test_db)
    await test_db.commit()

    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def attempt() -> str:
        async with maker() as sess:
            try:
                await TokenService.refresh_token(pair.refresh_token, sess)
                await sess.commit()
                return "ok"
            except CommittedHTTPError:
                await sess.commit()
                return "reuse"
            except HTTPException:
                await sess.rollback()
                return "rejected"

    results = await asyncio.gather(attempt(), attempt())
    assert results.count("ok") == 1, results
    # The loser hit the rotation grace window: a benign race, no revocation.
    assert "reuse" not in results, results
    family_id = (
        await test_db.execute(
            select(RefreshToken.family_id)
            .where(RefreshToken.user_id == user.id)
            .limit(1)
        )
    ).scalar_one()
    live = [r for r in await _family_rows(test_db, family_id) if not r.is_revoked]
    assert len(live) == 1  # the winner's successor survives


@pytest.mark.asyncio
async def test_replay_within_grace_window_is_409_without_revocation(
    test_db, make_user
) -> None:
    user = await make_user("grace@example.com")
    first = await TokenService.create_tokens(user, test_db)
    await test_db.commit()
    second = await TokenService.refresh_token(first.refresh_token, test_db)
    await test_db.commit()

    # A second tab / a retry after a lost response, right after the rotation.
    with pytest.raises(HTTPException) as exc:
        await TokenService.refresh_token(first.refresh_token, test_db)
    assert exc.value.status_code == 409
    assert not isinstance(exc.value, CommittedHTTPError)
    await test_db.rollback()

    # Nothing was revoked: the successor still rotates.
    third = await TokenService.refresh_token(second.refresh_token, test_db)
    assert third.refresh_token


@pytest.mark.asyncio
async def test_grace_window_needs_a_live_successor(test_db, make_user) -> None:
    """A token revoked by logout (no successor) is reuse even within the window."""
    user = await make_user("grace-logout@example.com")
    pair = await TokenService.create_tokens(user, test_db)
    await test_db.commit()
    await TokenService.revoke_token(pair.refresh_token, test_db)
    await test_db.commit()
    with pytest.raises(CommittedHTTPError) as exc:
        await TokenService.refresh_token(pair.refresh_token, test_db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_refresh_inactive_user_rejected(test_db, make_user) -> None:
    user = await make_user("inactive-rot@example.com")
    pair = await TokenService.create_tokens(user, test_db)
    user.is_active = False
    await test_db.commit()
    with pytest.raises(HTTPException) as exc:
        await TokenService.refresh_token(pair.refresh_token, test_db)
    assert exc.value.status_code == 401
    assert "not active" in exc.value.detail
