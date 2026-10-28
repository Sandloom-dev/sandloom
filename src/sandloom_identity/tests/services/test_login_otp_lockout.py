"""The second factor of a password login is locked per USER, not per IP.

A leaked password must not turn into a distributed TOTP brute force: the
hard lock on the first factor is per (email, IP) so a stranger cannot lock the
owner out, but whoever reaches the OTP phase already proved the password —
the OTP failures therefore count per user across every address.
"""

from __future__ import annotations

from typing import Any

import pyotp
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity import lockout
from sandloom_identity.events import EventActor
from sandloom_identity.schemas.auth import LoginIn
from sandloom_identity.services.auth_service import AuthService
from sandloom_identity.services.twofa_service import TwoFAService

PASSWORD = "TestPassword123!"


def _wrong_otp(secret: str) -> str:
    good = pyotp.TOTP(secret).now()
    return "000000" if good != "000000" else "111111"


@pytest.mark.asyncio
async def test_otp_failures_from_many_ips_lock_the_user(
    test_db: AsyncSession, make_user: Any
) -> None:
    user = await make_user("otp-distributed@example.com")
    init = await TwoFAService.init_2fa(user, test_db, current_password=PASSWORD)
    await TwoFAService.enable_2fa(
        user, pyotp.TOTP(init.secret).now(), test_db, current_password=PASSWORD
    )
    await test_db.commit()

    wrong = _wrong_otp(init.secret)
    for i in range(lockout.FAIL_THRESHOLD):
        with pytest.raises(HTTPException) as exc:
            await AuthService.login(
                LoginIn(email=user.email, password=PASSWORD, otp=wrong),
                test_db,
                actor=EventActor(ip=f"203.0.113.{i + 1}"),
            )
        assert exc.value.status_code == 401

    # A fresh address with the right password AND the right code is refused:
    # the OTP phase is locked for the user, whatever the caller's IP.
    with pytest.raises(HTTPException) as exc:
        await AuthService.login(
            LoginIn(
                email=user.email,
                password=PASSWORD,
                otp=pyotp.TOTP(init.secret).now(),
            ),
            test_db,
            actor=EventActor(ip="198.51.100.77"),
        )
    assert exc.value.status_code == 429
