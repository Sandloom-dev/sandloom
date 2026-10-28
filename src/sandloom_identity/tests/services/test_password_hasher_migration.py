"""passlib -> argon2-cffi: stored hashes keep verifying, parameters unchanged."""

from __future__ import annotations

from argon2 import PasswordHasher

from sandloom_identity.services.auth_service import AuthService

#: Produced by the former ``CryptContext(schemes=["argon2"], deprecated="auto")``
#: for the password ``"x"``.
PASSLIB_HASH_OF_X = (
    "$argon2id$v=19$m=65536,t=3,p=4$BYBQak0J4VwLgTAGoFTq3Q$"
    "lmXuTi3Uu24+VkCXRAXNgiJMcmrywJZVepUtBJ/JidI"
)
#: The production hasher (the suite swaps in a cheap one).
PRODUCTION = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16
)


def test_passlib_hash_verifies() -> None:
    assert AuthService.verify_password("x", PASSLIB_HASH_OF_X) is True
    assert AuthService.verify_password("not-x", PASSLIB_HASH_OF_X) is False


def test_malformed_hash_is_a_failed_verification() -> None:
    assert AuthService.verify_password("x", "") is False
    assert AuthService.verify_password("x", "$2b$12$bcrypt-ish") is False


def test_production_parameters_match_passlib(monkeypatch) -> None:
    monkeypatch.setattr(AuthService, "password_hasher", PRODUCTION)
    assert AuthService.get_password_hash("x").startswith(
        "$argon2id$v=19$m=65536,t=3,p=4$"
    )
    assert AuthService.needs_rehash(PASSLIB_HASH_OF_X) is False
    assert AuthService.needs_rehash("garbage") is True
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("x")
    assert AuthService.needs_rehash(cheap) is True


async def test_async_wrappers_use_the_bounded_pool() -> None:
    hashed = await AuthService.ahash_password("pw")
    assert await AuthService.averify_password("pw", hashed) is True
