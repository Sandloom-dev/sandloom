"""Tenancy test fixtures, on top of the framework harness (``sandloom.core.testing``).

Users are simulated by JWTs only — tenancy never touches identity tables.
"""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient

from sandloom.sdk.testing import Captured, email_of
from sandloom.sdk.testing import as_user as _mint
from sandloom.sdk.testing.fixtures import *  # noqa: F403
from sandloom_tenancy import events
from sandloom_tenancy.services import (
    invitation_service,
    membership_service,
    organization_service,
)

__all__ = ["email_of", "make_user_headers"]


@pytest.fixture(autouse=True)
def _ambient_uow(ambient_uow: Any) -> Any:
    """Every tenancy test runs under a recording UoW scope.

    Tenancy's security audits are ``required=True`` (a missing UoW is an error,
    never a dropped audit row). Services exercised directly (not through the
    API, whose session the ``client`` binds as the UoW) record into this
    in-memory scope instead.
    """
    return ambient_uow


@pytest.fixture(autouse=True)
def revoked_users(revoked_tokens: list[Any]) -> list[Any]:
    """Who ``revoke_user_tokens`` was called for (the cache is never written)."""
    return revoked_tokens


def make_user_headers(
    user_id: uuid.UUID | None = None,
    *,
    email: str | None = "",
    email_verified: bool | None = True,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, dict]:
    """A tenant-less access token with ``email`` / ``email_verified`` claims
    (defaults :func:`email_of` / ``True``; ``None`` omits either)."""
    claims = {"email": email, "email_verified": email_verified, **(extra_claims or {})}
    return _mint(user_id, tenant=False, **claims)


@pytest.fixture
def user_a() -> tuple[uuid.UUID, dict[str, Any]]:
    return make_user_headers()


@pytest.fixture
def user_b() -> tuple[uuid.UUID, dict[str, Any]]:
    return make_user_headers()


@pytest.fixture
def user_c() -> tuple[uuid.UUID, dict[str, Any]]:
    return make_user_headers()


@pytest_asyncio.fixture
async def org_with_owner(
    async_client: AsyncClient, user_a: tuple[uuid.UUID, dict[str, Any]]
) -> dict[str, Any]:
    """Create an organization owned by user_a; returns org payload."""
    _, headers = user_a
    resp = await async_client.post(
        "/api/v1/orgs",
        json={"name": "Acme Inc", "slug": f"acme-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Captured:
    """The exact events published and ``record_audit`` calls of tenancy
    services (``{"events": [(channel, payload)], "audit": [(action, kwargs)]}``).

    Events are typed producers (``events.MemberAdded(...).publish()``), so the
    capture replaces ``publish`` on their shared base and records the wire
    payload; nothing reaches a Unit of Work.
    """
    calls: Captured = {"events": [], "audit": []}

    async def fake_publish(self: Any, *, database: str | None = None) -> None:
        calls["events"].append((self.channel, self.model_dump(mode="json")))

    def fake_audit(action: str, **kwargs: Any) -> None:
        calls["audit"].append((action, kwargs))

    monkeypatch.setattr(events._TenancyEvent, "publish", fake_publish)
    for module in (invitation_service, membership_service, organization_service):
        monkeypatch.setattr(module, "record_audit", fake_audit)
    return calls
