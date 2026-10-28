"""Bounded API and replay regressions from the 22 October review."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from sandloom.sdk import OrgRole
from sandloom.sdk.contracts import contract_method
from sandloom.sdk.infra import settings
from sandloom.sdk.testing import bind_contract_session
from sandloom_tenancy.claims import tenancy_claims


def test_invalid_org_role_fails_at_configuration_time():
    with pytest.raises(ValueError, match="member, admin or owner"):
        OrgRole("admn")


@pytest.mark.asyncio
async def test_claims_are_bounded_without_silently_dropping_access(monkeypatch):
    monkeypatch.setattr(settings, "tenant_claims_max_organizations", 2)
    with patch(
        "sandloom_tenancy.claims.MembershipService.list_user_organization_ids",
        new_callable=AsyncMock,
        return_value=[uuid.uuid4() for _ in range(3)],
    ) as read:
        with (
            bind_contract_session(object()),
            pytest.raises(ValueError, match="TENANT_CLAIMS_MAX_ORGANIZATIONS"),
        ):
            await contract_method(tenancy_claims)(uuid.uuid4())
    assert read.await_args.kwargs["limit"] == 3


@pytest.mark.asyncio
async def test_invitation_retry_replays_original_token(
    async_client, user_a, org_with_owner, sandloom_cache
):
    _, headers = user_a
    headers = {**headers, "Idempotency-Key": f"invite-{uuid.uuid4()}"}
    url = f"/api/v1/orgs/{org_with_owner['id']}/invitations"
    body = {"email": "retry-invite@example.com", "role": "member"}
    original = await async_client.post(url, json=body, headers=headers)
    retry = await async_client.post(url, json=body, headers=headers)
    assert original.status_code == retry.status_code == 201, (original.text, retry.text)
    assert original.json() == retry.json()
    assert retry.headers["Idempotent-Replay"] == "true"


@pytest.mark.asyncio
async def test_default_member_page_is_bounded(
    async_client, user_a, org_with_owner, test_db
):
    from sandloom_tenancy.models import OrganizationMember

    org_id = uuid.UUID(org_with_owner["id"])
    test_db.add_all(
        OrganizationMember(organization_id=org_id, user_id=uuid.uuid4(), role="member")
        for _ in range(101)
    )
    await test_db.flush()
    _, headers = user_a
    response = await async_client.get(f"/api/v1/orgs/{org_id}/members", headers=headers)
    assert response.status_code == 200, response.text
    assert len(response.json()) == 100
    assert response.headers["X-Next-Cursor"]
