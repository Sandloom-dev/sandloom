"""Billing down, login 200 (ADR-0014 decision 7, billing option (c); KB-10).

The standard composition assigns the billing claims to billing, whose claims
contributor is non-critical: the startup check accepts that only for claims
every reader treats as absent (the subscription check escalates to the live
fact). So a billing outage at login mints the token without billing's claims,
with ``billing`` in ``claims_sources_failed``, instead of refusing the login.
Runs on the standard root (``sandloom_standard.main``, the suite's root when it
is installed).
"""

from __future__ import annotations

from typing import Any

import jwt
import pytest

from sandloom.core.testing.fixtures import *  # noqa: F403 - client, db
from sandloom_billing.entitlements import EntitlementsService
from sandloom_identity.models.user import User
from sandloom_identity.services.auth_service import AuthService

PASSWORD = "BillingDownPassw0rd!x"


async def _make_user(db: Any, email: str) -> User:
    user = User(
        email=email,
        full_name="Billing Down",
        hashed_password=AuthService.get_password_hash(PASSWORD),
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    return user


async def test_billing_down_still_logs_in(
    client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login is 200; the token carries no billing claim and says why."""
    core = client._transport.app.state.core  # type: ignore[attr-defined]
    authorities = core.claim_authorities
    assert authorities.owner_of("subscription_status") == "billing"
    assert authorities.owner_of("roles") == "identity"

    async def down(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("billing is down")

    monkeypatch.setattr(EntitlementsService, "subscription_claims", down)
    await _make_user(db, "billing-down@example.com")
    response = await client.post(
        "/auth/login",
        json={"email": "billing-down@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    claims = jwt.decode(
        response.json()["access_token"], options={"verify_signature": False}
    )
    assert claims["claims_sources_failed"] == ["billing"]
    assert "identity" in claims["claims_sources"]
    assert "subscription_status" not in claims
    assert "roles" in claims
