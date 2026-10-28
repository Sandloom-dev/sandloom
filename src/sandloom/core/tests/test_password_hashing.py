"""argon2-cffi password hashing (passlib removed) + the bounded hash pool."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from sandloom.core.auth import hashing, tokens

#: Produced by the former ``passlib.context.CryptContext(schemes=["argon2"])``
#: (passlib 1.7.4 on argon2-cffi 25.1) for the password ``"x"``.
PASSLIB_HASH_OF_X = (
    "$argon2id$v=19$m=65536,t=3,p=4$BYBQak0J4VwLgTAGoFTq3Q$"
    "lmXuTi3Uu24+VkCXRAXNgiJMcmrywJZVepUtBJ/JidI"
)


def test_passlib_hashes_still_verify() -> None:
    assert hashing.verify_password("x", PASSLIB_HASH_OF_X) is True
    assert hashing.verify_password("y", PASSLIB_HASH_OF_X) is False


def test_same_parameters_as_passlib() -> None:
    new = hashing.hash_password("x")
    # Identical encoded parameter block -> no rehash needed for old hashes.
    assert new.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert hashing.password_needs_rehash(PASSLIB_HASH_OF_X) is False
    assert hashing.verify_password("x", new)


def test_needs_rehash_semantics() -> None:
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("x")
    assert hashing.password_needs_rehash(weak) is True
    assert hashing.password_needs_rehash("not-a-hash") is True


def test_malformed_hash_raises_value_error_like_before() -> None:
    with pytest.raises(ValueError):
        hashing.verify_password("x", "not-a-hash")


def test_concurrency_defaults_to_min_4_cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens.settings, "password_hash_concurrency", None)
    assert 1 <= hashing.password_hash_concurrency() <= 4
    monkeypatch.setattr(tokens.settings, "password_hash_concurrency", 7)
    assert hashing.password_hash_concurrency() == 7


async def test_hash_pool_is_bounded() -> None:
    """32 concurrent password operations never run more than N at a time."""
    limit = hashing.password_hash_concurrency()
    active = 0
    peak = 0
    lock = threading.Lock()

    def _work() -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return 1

    results = await asyncio.gather(
        *(hashing.run_password_hashing(_work) for _ in range(32))
    )
    assert sum(results) == 32
    assert peak <= limit


async def test_async_helpers_use_the_pool() -> None:
    hashed = await hashing.ahash_password("pw")
    assert await hashing.averify_password("pw", hashed) is True
    assert await hashing.averify_password("nope", hashed) is False


# ---- JWT: no header parse when no rotation is configured -------------------
def test_no_header_parse_without_previous_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jwt

    token = tokens.create_access_token(data={"sub": "u", "token_type": "access"})
    monkeypatch.setattr(tokens.settings, "jwt_previous_secrets", [])

    def _boom(_token: str) -> dict[str, str]:
        raise AssertionError("header parsed although no rotation is configured")

    monkeypatch.setattr(jwt, "get_unverified_header", _boom)
    for kid in (None, "k1"):
        monkeypatch.setattr(tokens.settings, "jwt_kid", kid)
        assert tokens._verification_keys(token) == [tokens.settings.jwt_secret]
    assert tokens.decode_token(token) is not None


def test_rotation_still_tries_previous_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jwt

    old = "previous-signing-secret-long-enough-0123456789"
    token = jwt.encode(
        {"sub": "u", "exp": int(time.time()) + 60, "iat": int(time.time())},
        old,
        algorithm=tokens.settings.jwt_algorithm,
    )
    monkeypatch.setattr(tokens.settings, "jwt_previous_secrets", [old])
    monkeypatch.setattr(tokens.settings, "jwt_kid", None)
    claims = tokens.decode_token(token)
    assert claims is not None and claims["sub"] == "u"
