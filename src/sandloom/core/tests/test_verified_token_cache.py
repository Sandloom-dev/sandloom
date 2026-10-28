"""``decode_token``'s verified-token cache: a hit answers exactly like PyJWT.

Only a token that verified is cached; a hit re-checks ``exp`` / ``nbf`` /
``iat`` against the clock with the configured leeway, and any change of a
verification setting (secret, previous secrets, kid, public keys, algorithm,
audience, issuer, leeway) makes the next decode verify again.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import jwt
import pytest

from sandloom.core.auth import tokens
from sandloom.core.config.settings import settings

_SECRET = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_OTHER = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"


@pytest.fixture(autouse=True)
def _hs256(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "jwt_algorithm", "HS256")
    monkeypatch.setattr(settings, "jwt_secret", _SECRET)
    monkeypatch.setattr(settings, "jwt_previous_secrets", [])
    monkeypatch.setattr(settings, "jwt_kid", None)
    monkeypatch.setattr(settings, "jwt_audience", None)
    monkeypatch.setattr(settings, "jwt_issuer", None)
    monkeypatch.setattr(settings, "jwt_leeway_seconds", 10)
    tokens._verified_tokens.clear()
    yield
    tokens._verified_tokens.clear()


def _token(secret: str = _SECRET, **claims: Any) -> str:
    now = int(time.time())
    body: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "token_type": "access",
        "iat": now,
        "exp": now + 900,
        "tenant_ids": [str(uuid.uuid4())],
        **claims,
    }
    return jwt.encode({k: v for k, v in body.items() if v is not None}, secret)


_REAL_TIME = time.time


def _clock(monkeypatch: pytest.MonkeyPatch, offset: float) -> None:
    monkeypatch.setattr(time, "time", lambda: _REAL_TIME() + offset)


def test_a_hit_skips_verification_and_returns_fresh_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _token()
    first = tokens.decode_token(token)
    assert first is not None

    def _never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a cached token must not be verified again")

    monkeypatch.setattr(jwt, "decode", _never)
    again = tokens.decode_token(token)
    assert again == first
    again["tenant_ids"].append("mutated")
    assert tokens.decode_token(token) == first


def test_a_hit_expires_exactly_like_pyjwt(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _token()
    assert tokens.decode_token(token) is not None
    _clock(monkeypatch, 900 + 9)  # inside the leeway
    assert tokens.decode_token(token) is not None
    _clock(monkeypatch, 900 + 10)  # exp <= now - leeway
    assert tokens.decode_token(token) is None
    assert token not in tokens._verified_tokens


def test_a_hit_is_immature_when_the_clock_steps_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    token = _token(nbf=now)
    assert tokens.decode_token(token) is not None
    _clock(monkeypatch, -11)  # nbf / iat > now + leeway
    assert tokens.decode_token(token) is None
    _clock(monkeypatch, 0)
    assert tokens.decode_token(token) is not None


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("jwt_secret", _OTHER),
        ("jwt_audience", "another-audience"),
        ("jwt_issuer", "another-issuer"),
        ("jwt_algorithm", "HS512"),
    ],
)
def test_a_changed_verification_setting_verifies_again(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: Any
) -> None:
    token = _token()
    assert tokens.decode_token(token) is not None
    monkeypatch.setattr(settings, setting, value)
    assert tokens.decode_token(token) is None


def test_failures_are_never_cached() -> None:
    forged = _token(secret=_OTHER)
    expired = _token(iat=int(time.time()) - 3600, exp=int(time.time()) - 60)
    for token in (forged, expired, "not-a-jwt"):
        assert tokens.decode_token(token) is None
        assert token not in tokens._verified_tokens


def test_the_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "VERIFIED_TOKEN_CACHE_SIZE", 3)
    minted = [_token() for _ in range(5)]
    for token in minted:
        assert tokens.decode_token(token) is not None
    assert list(tokens._verified_tokens) == minted[2:]
