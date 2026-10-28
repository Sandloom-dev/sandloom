"""Configurable argon2 parameters in identity: a successful login re-hashes a
stored hash made with other parameters (upgrade AND downgrade) in the same
UoW; failures never block the login; registration / password change / reset
hash with the configured hasher; old hashes keep verifying."""

from collections.abc import Iterator
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException

from sandloom.sdk.security import password_hasher as configured_password_hasher
from sandloom_identity.schemas.auth import LoginIn, RegisterIn
from sandloom_identity.services.auth_service import AuthService

PW = "TestPassword123!"
#: Two cheap, DIFFERENT parameter sets standing in for "old" and "new" config.
LIGHT = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
HEAVY = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
LIGHT_PREFIX = "$argon2id$v=19$m=8,t=1,p=1$"
HEAVY_PREFIX = "$argon2id$v=19$m=16,t=2,p=1$"


@pytest.fixture
def use_hasher(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def _use(hasher: PasswordHasher) -> None:
        monkeypatch.setattr(AuthService, "password_hasher", hasher)

    yield _use


async def _login(test_db: Any, email: str, password: str = PW) -> None:
    await AuthService.login(LoginIn(email=email, password=password), test_db)


def test_service_hasher_uses_the_configured_parameters() -> None:
    fresh = configured_password_hasher()
    # The class attribute is built by the same factory at import time (tests
    # swap it for a cheap one, so compare a fresh build to the settings).
    assert (fresh.time_cost, fresh.memory_cost, fresh.parallelism) == (3, 65536, 4)


@pytest.mark.asyncio
async def test_login_upgrades_a_weaker_hash(test_db, make_user, use_hasher) -> None:
    use_hasher(LIGHT)
    user = await make_user("up@example.com")
    assert user.hashed_password.startswith(LIGHT_PREFIX)

    use_hasher(HEAVY)  # operator raised the work factor
    await _login(test_db, "up@example.com")
    await test_db.flush()
    assert user.hashed_password.startswith(HEAVY_PREFIX)
    assert AuthService.verify_password(PW, user.hashed_password)


@pytest.mark.asyncio
async def test_login_downgrades_a_stronger_hash(test_db, make_user, use_hasher) -> None:
    use_hasher(HEAVY)
    user = await make_user("down@example.com")
    use_hasher(LIGHT)  # operator chose a lighter preset
    await _login(test_db, "down@example.com")
    assert user.hashed_password.startswith(LIGHT_PREFIX)


@pytest.mark.asyncio
async def test_matching_hash_is_left_alone(test_db, make_user, use_hasher) -> None:
    use_hasher(LIGHT)
    user = await make_user("same@example.com")
    before = user.hashed_password
    await _login(test_db, "same@example.com")
    assert user.hashed_password == before


@pytest.mark.asyncio
async def test_failed_login_never_rehashes(test_db, make_user, use_hasher) -> None:
    use_hasher(LIGHT)
    user = await make_user("fail@example.com")
    before = user.hashed_password
    use_hasher(HEAVY)
    with pytest.raises(HTTPException):
        await _login(test_db, "fail@example.com", "wrong-password")
    assert user.hashed_password == before


class _BrokenHashing(PasswordHasher):
    """Verifies normally; hashing (the rehash) blows up."""

    def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_rehash_failure_does_not_block_login(
    test_db, make_user, use_hasher
) -> None:
    use_hasher(LIGHT)
    user = await make_user("broken@example.com")
    before = user.hashed_password
    use_hasher(_BrokenHashing(time_cost=2, memory_cost=16, parallelism=1))
    await _login(test_db, "broken@example.com")  # no exception
    assert user.hashed_password == before
    assert user.last_login_at is not None


@pytest.mark.asyncio
async def test_old_strong_hash_verifies_under_a_lighter_config(use_hasher) -> None:
    passlib_hash_of_x = (
        "$argon2id$v=19$m=65536,t=3,p=4$BYBQak0J4VwLgTAGoFTq3Q$"
        "lmXuTi3Uu24+VkCXRAXNgiJMcmrywJZVepUtBJ/JidI"
    )
    use_hasher(LIGHT)
    assert AuthService.verify_password("x", passlib_hash_of_x)
    assert AuthService.needs_rehash(passlib_hash_of_x)


@pytest.mark.asyncio
async def test_registration_and_password_change_use_the_configured_hasher(
    test_db, use_hasher
) -> None:
    use_hasher(HEAVY)
    user = await AuthService.register(
        RegisterIn(email="cfg@example.com", full_name="Cfg", password=PW), test_db
    )
    assert user is not None
    assert user.hashed_password.startswith(HEAVY_PREFIX)

    use_hasher(LIGHT)
    await AuthService.change_password(user.id, PW, "BrandNewPass#1", test_db)
    assert user.hashed_password.startswith(LIGHT_PREFIX)
    assert AuthService.verify_password("BrandNewPass#1", user.hashed_password)
