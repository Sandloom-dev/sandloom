"""The UI's own-role read does not enumerate other organizations or members."""

import uuid

import pytest


@pytest.mark.asyncio
async def test_current_membership_is_live_and_does_not_trust_platform_role(
    async_client, user_a, user_b, org_with_owner
):
    org_id = org_with_owner["id"]
    owner = await async_client.get(
        f"/api/v1/orgs/{org_id}/membership", headers=user_a[1]
    )
    assert owner.status_code == 200, owner.text
    assert owner.json() == {"role": "owner"}
    from sandloom_tenancy.tests.conftest import make_user_headers

    _, platform_headers = make_user_headers(
        user_b[0], extra_claims={"roles": ["admin"], "claims_sources": ["identity"]}
    )
    outsider = await async_client.get(
        f"/api/v1/orgs/{org_id}/membership", headers=platform_headers
    )
    missing = await async_client.get(
        f"/api/v1/orgs/{uuid.uuid4()}/membership", headers=user_b[1]
    )
    assert outsider.status_code == missing.status_code == 404
    assert outsider.json() == missing.json()
    anonymous = await async_client.get(f"/api/v1/orgs/{org_id}/membership")
    assert anonymous.status_code == 401


@pytest.mark.asyncio
async def test_current_membership_tracks_role_changes_removal_and_inactive_org(
    async_client, user_a, user_b, org_with_owner
):
    from sandloom_tenancy.tests.test_membership_api import _join_via_invitation

    org_id = org_with_owner["id"]
    member_id, member_headers = user_b
    await _join_via_invitation(async_client, org_id, user_a[1], user_b, role="admin")
    read = await async_client.get(
        f"/api/v1/orgs/{org_id}/membership", headers=member_headers
    )
    assert read.status_code == 200
    assert read.json() == {"role": "admin"}
    changed = await async_client.patch(
        f"/api/v1/orgs/{org_id}/members/{member_id}",
        json={"role": "member"},
        headers=user_a[1],
    )
    assert changed.status_code == 200
    read = await async_client.get(
        f"/api/v1/orgs/{org_id}/membership", headers=member_headers
    )
    assert read.json() == {"role": "member"}
    removed = await async_client.delete(
        f"/api/v1/orgs/{org_id}/members/{member_id}", headers=user_a[1]
    )
    assert removed.status_code == 204
    assert (
        await async_client.get(
            f"/api/v1/orgs/{org_id}/membership", headers=member_headers
        )
    ).status_code == 404
    deleted = await async_client.delete(f"/api/v1/orgs/{org_id}", headers=user_a[1])
    assert deleted.status_code == 204
    assert (
        await async_client.get(f"/api/v1/orgs/{org_id}/membership", headers=user_a[1])
    ).status_code == 404
