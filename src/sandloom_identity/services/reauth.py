"""Step-up re-authentication for sensitive account actions.

Changing the second factor (2FA init/enable/disable/backup-code regeneration)
or the password with only a bearer token would let anyone holding a stolen
access token lock the owner out. These actions therefore require proof of a
fresh authentication:

* accounts with a password: the **current password**;
* password-less (SSO-only) accounts: an access token whose ``auth_time``
  claim (the PRIMARY authentication instant, carried unchanged through
  refresh) is within :data:`RECENT_AUTH_SECONDS` — a just-completed SSO login.
  ``iat`` is never trusted: a refresh mints a fresh ``iat``, so a stolen
  refresh token alone would otherwise pass.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException, status

from sandloom.sdk.effects import mark_reauthenticated
from sandloom_identity import lockout
from sandloom_identity.models.user import User
from sandloom_identity.services import auth_service  # cycle: auth -> twofa -> reauth

#: How recent the primary authentication (``auth_time``) must be to count as
#: re-authentication for an account that has no password.
RECENT_AUTH_SECONDS = 300


async def require_reauth(
    user: User,
    current_password: str | None,
    *,
    token_claims: dict[str, Any] | None = None,
) -> None:
    """Raise 401 unless the caller re-proved their identity."""
    subject = lockout.subject_for_user(user.id, "stepup")
    await lockout.ensure_not_locked(subject)
    if user.hashed_password:
        if not current_password or not await auth_service.AuthService.averify_password(
            current_password, user.hashed_password
        ):
            await lockout.register_failure(subject)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is required and must be correct",
            )
        await lockout.reset_failures(subject)
        mark_reauthenticated()
        return

    auth_time = (token_claims or {}).get("auth_time")
    if (
        isinstance(auth_time, (int, float))
        and not isinstance(auth_time, bool)
        and 0 <= time.time() - float(auth_time) <= RECENT_AUTH_SECONDS
    ):
        mark_reauthenticated()
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Recent sign-in required: sign in again and retry",
    )


__all__ = ["RECENT_AUTH_SECONDS", "require_reauth"]
