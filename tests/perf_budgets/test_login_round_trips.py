"""Round-trip budget of a successful password login (``POST /auth/login``).

Measured through the real app (identity + every co-loaded claims
contributor) on the test Postgres / Redis.

Postgres statements: 5 (BUDGET_LOGIN_SQL; 9 before the unused
``selectinload(User.user_roles)`` and the second claims query went — roles
and permissions are one LEFT JOIN — and 6 before the user and its enabled
TOTP secret became one lookup).
Redis commands: lockout check (1 MGET: lock + failure counter + the
per-account throttle) + the live emergency-block check (one read). The
failure-counter reset (DEL) runs only when that counter existed. The rate
limiter adds one EVALSHA when enabled.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core.config.settings import settings

from ._counters import RedisCommandCounter

PASSWORD = "BudgetPassw0rd!x"

BUDGET_LOGIN_SQL = 5
# 1 MGET for the lockout keys + 1 read for the emergency-block check
# (sandloom.core.di.ensure_user_not_blocked); was 3 with an unconditional DEL.
BUDGET_LOGIN_REDIS = 2
BUDGET_LOGIN_REDIS_RATE_LIMITED = BUDGET_LOGIN_REDIS + 1


async def _make_user(db: Any, email: str) -> Any:
    from sandloom_identity.models.user import User
    from sandloom_identity.services.auth_service import AuthService

    user = User(
        email=email,
        full_name="Budget",
        hashed_password=AuthService.get_password_hash(PASSWORD),
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    return user


@pytest.mark.parametrize("rate_limited", [False, True])
async def test_login_round_trips(
    client: Any,
    db: Any,
    sandloom_cache: Any,
    sql_counter: Any,
    monkeypatch: pytest.MonkeyPatch,
    rate_limited: bool,
) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", rate_limited)
    await _make_user(db, "budget-login@example.com")
    redis = RedisCommandCounter()
    redis.wrap(await sandloom_cache.raw_client(), monkeypatch)
    # Warm the Lua script cache (a one-off NOSCRIPT reload per server).
    await sandloom_cache.incr_window("warm:cur", "warm:prev", ttl_seconds=5)

    sql_counter.enabled = redis.enabled = True
    resp = await client.post(
        "/auth/login",
        json={"email": "budget-login@example.com", "password": PASSWORD},
    )
    sql_counter.enabled = redis.enabled = False

    assert resp.status_code == 200, resp.text
    redis_budget = (
        BUDGET_LOGIN_REDIS_RATE_LIMITED if rate_limited else BUDGET_LOGIN_REDIS
    )
    assert redis.count <= redis_budget, redis.commands
    assert sql_counter.count <= BUDGET_LOGIN_SQL, sql_counter.statements
    assert not any("user_roles.user_id IN" in s for s in sql_counter.statements)


#: Statements of one refresh-token rotation: the token lookup, the CAS consume
#: returning the user AND identity's roles / permissions (one data-modifying
#: CTE with a LATERAL aggregate; was the consume + a user SELECT + the roles
#: query), the other owners' claims queries (tenancy, billing) and the new
#: token's INSERT.
BUDGET_REFRESH_SQL = 5


async def test_refresh_round_trips(
    client: Any, db: Any, sql_counter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    await _make_user(db, "budget-refresh@example.com")
    login = await client.post(
        "/auth/login",
        json={"email": "budget-refresh@example.com", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    # The harness shares one session: write the login's own pending rows now.
    await db.flush()

    sql_counter.enabled = True
    resp = await client.post(
        "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    sql_counter.enabled = False

    assert resp.status_code == 200, resp.text
    assert sql_counter.count <= BUDGET_REFRESH_SQL, sql_counter.statements
    assert not any(s.startswith("SELECT users.") for s in sql_counter.statements)
    # The old token is consumed exactly once; the new one works.
    again = await client.post(
        "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert again.status_code in (401, 409), again.text
    fresh = await client.post(
        "/auth/refresh", json={"refresh_token": resp.json()["refresh_token"]}
    )
    assert fresh.status_code == 200, fresh.text
