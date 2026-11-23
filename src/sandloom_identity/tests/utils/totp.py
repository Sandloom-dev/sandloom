"""Enable 2FA in tests without a TOTP window boundary able to fail the test.

``TwoFAService.enable_2fa`` accepts a code within one step of the clock the
service reads (``twofa_service.time.time``). A test that computes "the previous
step's code" from the wall clock and then enables 2FA after a slow password
check (argon2, slower under coverage) can cross a 30 s boundary in between: the
code is then two steps old and the service answers 400. Here the code and the
clock the service reads come from one timestamp, so no boundary can separate
them.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pyotp
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.models.user import User
from sandloom_identity.services import twofa_service as twofa_mod
from sandloom_identity.services.twofa_service import TwoFAService


async def enable_2fa(
    user: User,
    secret: str,
    db: AsyncSession,
    *,
    current_password: str,
    now: float | None = None,
) -> int:
    """Enable 2FA with the previous step's code, the clock held at ``now``.

    ``now`` (default: the wall clock) is read once. The code is the one of the
    step before it, so the current step's code stays unused for the test body
    (replay protection remembers the enabling step), and the service reads a
    clock held one second into the step of ``now`` while it verifies. Returns
    the step the code was made for.
    """
    reading = time.time() if now is None else now
    totp = pyotp.TOTP(secret)
    step = int(reading) // totp.interval
    # Replace the service's view of the clock only: other users of
    # ``time.time`` keep the real one.
    held = SimpleNamespace(time=lambda: step * totp.interval + 1.0)
    with patch.object(twofa_mod, "time", held):
        await TwoFAService.enable_2fa(
            user,
            totp.at((step - 1) * totp.interval),
            db,
            current_password=current_password,
        )
    return step - 1
