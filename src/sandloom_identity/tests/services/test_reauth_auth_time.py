"""Step-up for password-less accounts trusts ``auth_time``, never ``iat``.

A refresh mints an access token with a fresh ``iat``; if step-up trusted
``iat``, a stolen refresh token alone would pass the "recent sign-in" check
and could enrol the thief's 2FA (account takeover). ``auth_time`` is the
instant of the PRIMARY authentication and is carried unchanged through
refresh.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.security import decode_token
from sandloom_identity.services.reauth import require_reauth
from sandloom_identity.services.token_service import TokenService


@pytest.mark.asyncio
async def test_fresh_iat_with_old_auth_time_is_not_recent(
    test_db: AsyncSession, make_user: Any
) -> None:
    user = await make_user("stepup-old@example.com", password=None)
    now = time.time()
    with pytest.raises(HTTPException) as exc:
        await require_reauth(
            user, None, token_claims={"iat": now, "auth_time": now - 3600}
        )
    assert exc.value.status_code == 401
    # No auth_time at all (a token minted before the claim existed): refused.
    with pytest.raises(HTTPException):
        await require_reauth(user, None, token_claims={"iat": now})
    await require_reauth(user, None, token_claims={"iat": now, "auth_time": now - 5})


@pytest.mark.asyncio
async def test_refresh_carries_auth_time_unchanged(
    test_db: AsyncSession, make_user: Any
) -> None:
    user = await make_user("stepup-refresh@example.com", password=None)
    login = await TokenService.create_tokens(user, test_db)
    claims = decode_token(login.access_token)
    assert claims is not None
    auth_time = claims["auth_time"]
    assert isinstance(auth_time, int)
    assert abs(time.time() - auth_time) < 60

    refreshed = await TokenService.refresh_token(login.refresh_token, test_db)
    new_claims = decode_token(refreshed.access_token)
    assert new_claims is not None
    assert new_claims["auth_time"] == auth_time
    again = await TokenService.refresh_token(refreshed.refresh_token, test_db)
    again_claims = decode_token(again.access_token)
    assert again_claims is not None
    assert again_claims["auth_time"] == auth_time
