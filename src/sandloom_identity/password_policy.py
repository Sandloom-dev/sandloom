"""Baseline password policy.

Length bounds live on the schemas (8..100). On top of that we reject the most
common passwords (a small built-in list — the top of every breach corpus) and
passwords equal to the account email or its local part. Composition rules
("one symbol, one digit…") are deliberately NOT imposed (NIST SP 800-63B).
"""

from __future__ import annotations

_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password12",
        "password123",
        "password1234",
        "password!",
        "passw0rd",
        "p@ssw0rd",
        "p@ssword",
        "12345678",
        "123456789",
        "1234567890",
        "12341234",
        "11111111",
        "00000000",
        "88888888",
        "87654321",
        "qwertyui",
        "qwertyuiop",
        "qwerty123",
        "qwerty12",
        "1q2w3e4r",
        "1qaz2wsx",
        "asdfghjk",
        "asdfasdf",
        "zxcvbnm1",
        "abc12345",
        "abcd1234",
        "aa123456",
        "iloveyou",
        "iloveyou1",
        "admin123",
        "administrator",
        "letmein1",
        "letmein123",
        "welcome1",
        "welcome123",
        "sunshine",
        "princess",
        "football",
        "baseball",
        "superman",
        "trustno1",
        "dragon12",
        "monkey12",
        "starwars",
        "whatever",
        "changeme",
        "changeme1",
        "secret123",
        "computer",
        "internet",
        "michael1",
        "jennifer",
    }
)


def password_policy_violation(password: str, email: str | None = None) -> str | None:
    """Return a human-readable violation, or ``None`` when acceptable."""
    lowered = password.strip().lower()
    if lowered in _COMMON_PASSWORDS:
        return "Password is too common; choose a less guessable one"
    if email:
        addr = email.strip().lower()
        local = addr.split("@", 1)[0]
        if lowered in {addr, local}:
            return "Password must not be the same as your email address"
    return None


__all__ = ["password_policy_violation"]
