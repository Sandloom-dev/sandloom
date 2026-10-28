"""Login writes the user row only AFTER minting the tokens.

``UPDATE users SET last_login_at …`` row-locks the user until commit. Issued
before ``TokenService.create_tokens`` it held that lock across claims
collection (contract reads; RPC when split) and the refresh-token insert, so
concurrent logins of one account queued behind each other: on the SQL profile
mix (docs/design/sql-profile-2026-10-21.md) the UPDATE waited 222-259 ms per
login at concurrency 8 and 3.6-3.9 ms once it moved to the end of the
transaction. The outcome is unchanged: same UoW, same values.
"""

from typing import Any

import pytest
from argon2 import PasswordHasher

from sandloom_identity.schemas.auth import LoginIn
from sandloom_identity.services.auth_service import AuthService
from sandloom_identity.services.token_service import TokenService

PW = "TestPassword123!"


@pytest.fixture
def mint_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    original = TokenService.create_tokens.__func__  # type: ignore[attr-defined]

    async def spy(cls: Any, user: Any, db: Any, *args: Any, **kw: Any) -> Any:
        seen.append(
            {
                "dirty": user in db.dirty,
                "last_login_at": user.last_login_at,
                "hash": user.hashed_password,
            }
        )
        return await original(cls, user, db, *args, **kw)

    monkeypatch.setattr(TokenService, "create_tokens", classmethod(spy))
    return seen


@pytest.mark.asyncio
async def test_password_login_writes_the_user_row_after_minting(
    test_db: Any, make_user: Any, mint_spy: list[dict[str, Any]]
) -> None:
    user = await make_user("order@example.com", password=PW)
    assert user.last_login_at is None

    await AuthService.login(LoginIn(email="order@example.com", password=PW), test_db)

    [at_mint] = mint_spy
    assert at_mint == {"dirty": False, "last_login_at": None, "hash": at_mint["hash"]}
    await test_db.flush()  # what the request UoW commit does
    await test_db.refresh(user)
    assert user.last_login_at is not None


@pytest.mark.asyncio
async def test_rehash_is_applied_after_minting_too(
    test_db: Any,
    make_user: Any,
    mint_spy: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    light = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    heavy = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    monkeypatch.setattr(AuthService, "password_hasher", light)
    user = await make_user("rehash-order@example.com", password=PW)
    old_hash = user.hashed_password
    monkeypatch.setattr(AuthService, "password_hasher", heavy)

    await AuthService.login(
        LoginIn(email="rehash-order@example.com", password=PW), test_db
    )

    [at_mint] = mint_spy
    assert at_mint["dirty"] is False and at_mint["hash"] == old_hash
    await test_db.flush()  # what the request UoW commit does
    await test_db.refresh(user)
    assert user.hashed_password != old_hash
    assert user.hashed_password.startswith("$argon2id$v=19$m=16,t=2,p=1$")
