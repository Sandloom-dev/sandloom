"""A TOTP code in non-ASCII digits is a clean miss (review hand-off).

``str.isdigit()`` accepts e.g. Arabic-Indic digits; such a 6-char code then
reached ``hmac.compare_digest(str, str)``, which raises ``TypeError`` on
non-ASCII input (a 500 on the 2FA endpoints instead of "invalid code").
"""

from __future__ import annotations

import pyotp

from sandloom_identity.services.twofa_service import _match_totp_step


def test_non_ascii_digit_code_is_rejected_without_error() -> None:
    secret = pyotp.random_base32()
    assert _match_totp_step(secret, "١٢٣٤٥٦") is None


def test_current_ascii_code_still_matches() -> None:
    secret = pyotp.random_base32()
    assert _match_totp_step(secret, pyotp.TOTP(secret).now()) is not None
