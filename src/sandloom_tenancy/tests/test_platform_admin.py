"""Platform directory spans organizations without acquiring tenant membership."""

import pytest

from sandloom.sdk.testing import as_user


@pytest.mark.asyncio
async def test_global_directory_is_admin_only_and_keeps_self_service_scoped(
    async_client, user_a, user_b, org_with_owner
):
    org_id = org_with_owner["id"]
    prefix = "/api/v1/platform/tenancy"
    for path in ("/organizations", f"/organizations/{org_id}", "/members"):
        assert (await async_client.get(prefix + path)).status_code == 401
        assert (
            await async_client.get(prefix + path, headers=user_a[1])
        ).status_code == 403
    _, headers = as_user(user_b[0], roles=["admin"], claims_sources=["identity"])
    listing = await async_client.get(prefix + "/organizations", headers=headers)
    assert listing.status_code == 200, listing.text
    assert org_id in [row["id"] for row in listing.json()]
    detail = await async_client.get(
        prefix + f"/organizations/{org_id}", headers=headers
    )
    assert detail.status_code == 200, detail.text
    members = await async_client.get(
        prefix + "/members", headers=headers, params={"organization_id": org_id}
    )
    assert members.status_code == 200 and members.json()[0]["user_id"] == str(user_a[0])
    assert (
        await async_client.get(f"/api/v1/orgs/{org_id}/membership", headers=headers)
    ).status_code == 404
    empty = await async_client.get(
        prefix + "/organizations", headers=headers, params={"q": "NoSuchOrganization"}
    )
    assert empty.json() == []
