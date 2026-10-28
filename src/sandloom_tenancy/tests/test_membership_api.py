"""Membership management and role-hierarchy tests."""

import uuid

import pytest
from httpx import AsyncClient


async def _join_via_invitation(
    async_client: AsyncClient, org_id: str, owner_headers, joiner, role="member"
) -> None:
    """Helper: invite + accept to add `joiner` to org."""
    joiner_id, joiner_headers = joiner
    invite = await async_client.post(
        f"/api/v1/orgs/{org_id}/invitations",
        json={"email": f"{joiner_id.hex[:12]}@example.com", "role": role},
        headers=owner_headers,
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["token"]
    accept = await async_client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=joiner_headers
    )
    assert accept.status_code == 200, accept.text


@pytest.mark.asyncio
class TestMembership:
    async def test_member_cannot_admin(
        self, async_client: AsyncClient, user_a, user_b, org_with_owner
    ):
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        await _join_via_invitation(async_client, org_id, headers_a, user_b)

        _, headers_b = user_b
        # member cannot rename the org
        resp = await async_client.patch(
            f"/api/v1/orgs/{org_id}", json={"name": "Hacked"}, headers=headers_b
        )
        assert resp.status_code == 403
        # member cannot invite
        resp = await async_client.post(
            f"/api/v1/orgs/{org_id}/invitations",
            json={"email": "x@example.com"},
            headers=headers_b,
        )
        assert resp.status_code == 403

    async def test_admin_can_manage_members(
        self, async_client: AsyncClient, user_a, user_b, user_c, org_with_owner
    ):
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        await _join_via_invitation(
            async_client, org_id, headers_a, user_b, role="admin"
        )
        await _join_via_invitation(async_client, org_id, headers_a, user_c)

        user_c_id, _ = user_c
        _, headers_b = user_b
        # admin removes a plain member
        resp = await async_client.delete(
            f"/api/v1/orgs/{org_id}/members/{user_c_id}", headers=headers_b
        )
        assert resp.status_code == 204

    async def test_admin_cannot_promote_to_admin_or_touch_admins(
        self, async_client: AsyncClient, user_a, user_b, user_c, org_with_owner
    ):
        """Formerly an admin could demote/remove other admins; owner-only now."""
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        await _join_via_invitation(
            async_client, org_id, headers_a, user_b, role="admin"
        )
        await _join_via_invitation(async_client, org_id, headers_a, user_c)
        user_c_id, _ = user_c
        _, headers_b = user_b

        # an admin cannot mint another admin (owner-only, like invitations)
        resp = await async_client.patch(
            f"/api/v1/orgs/{org_id}/members/{user_c_id}",
            json={"role": "admin"},
            headers=headers_b,
        )
        assert resp.status_code == 403

        # the owner promotes member to admin
        resp = await async_client.patch(
            f"/api/v1/orgs/{org_id}/members/{user_c_id}",
            json={"role": "admin"},
            headers=headers_a,
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

        # ...but cannot demote or remove another admin
        resp = await async_client.patch(
            f"/api/v1/orgs/{org_id}/members/{user_c_id}",
            json={"role": "member"},
            headers=headers_b,
        )
        assert resp.status_code == 403
        resp = await async_client.delete(
            f"/api/v1/orgs/{org_id}/members/{user_c_id}", headers=headers_b
        )
        assert resp.status_code == 403

        # the owner can
        resp = await async_client.patch(
            f"/api/v1/orgs/{org_id}/members/{user_c_id}",
            json={"role": "member"},
            headers=headers_a,
        )
        assert resp.status_code == 200
        user_b_id, _ = user_b
        resp = await async_client.delete(
            f"/api/v1/orgs/{org_id}/members/{user_b_id}", headers=headers_a
        )
        assert resp.status_code == 204

    async def test_member_can_leave_but_not_remove_others(
        self, async_client: AsyncClient, user_a, user_b, user_c, org_with_owner
    ):
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        await _join_via_invitation(async_client, org_id, headers_a, user_b)
        await _join_via_invitation(async_client, org_id, headers_a, user_c)
        user_b_id, headers_b = user_b
        user_c_id, _ = user_c

        resp = await async_client.delete(
            f"/api/v1/orgs/{org_id}/members/{user_c_id}", headers=headers_b
        )
        assert resp.status_code == 403

        resp = await async_client.delete(
            f"/api/v1/orgs/{org_id}/members/{user_b_id}", headers=headers_b
        )
        assert resp.status_code == 204
        # gone: the org is no longer visible to the leaver
        resp = await async_client.get(f"/api/v1/orgs/{org_id}", headers=headers_b)
        assert resp.status_code == 404

    async def test_owner_cannot_leave_without_transfer(
        self, async_client: AsyncClient, user_a, org_with_owner
    ):
        owner_id, headers_a = user_a
        resp = await async_client.delete(
            f"/api/v1/orgs/{org_with_owner['id']}/members/{owner_id}",
            headers=headers_a,
        )
        assert resp.status_code == 409

    async def test_change_role_to_same_role_is_noop(
        self, async_client: AsyncClient, user_a, user_b, org_with_owner, captured
    ):
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        await _join_via_invitation(async_client, org_id, headers_a, user_b)
        user_b_id, _ = user_b
        captured["events"].clear()
        captured["audit"].clear()
        resp = await async_client.patch(
            f"/api/v1/orgs/{org_id}/members/{user_b_id}",
            json={"role": "member"},
            headers=headers_a,
        )
        assert resp.status_code == 200
        assert captured["events"] == []
        assert captured["audit"] == []

    async def test_owner_cannot_be_removed_or_demoted(
        self, async_client: AsyncClient, user_a, user_b, org_with_owner
    ):
        owner_id, headers_a = user_a
        org_id = org_with_owner["id"]
        await _join_via_invitation(
            async_client, org_id, headers_a, user_b, role="admin"
        )
        _, headers_b = user_b

        resp = await async_client.delete(
            f"/api/v1/orgs/{org_id}/members/{owner_id}", headers=headers_b
        )
        assert resp.status_code == 409

        resp = await async_client.patch(
            f"/api/v1/orgs/{org_id}/members/{owner_id}",
            json={"role": "member"},
            headers=headers_b,
        )
        assert resp.status_code == 409

    async def test_ownership_transfer(
        self, async_client: AsyncClient, user_a, user_b, org_with_owner
    ):
        owner_id, headers_a = user_a
        org_id = org_with_owner["id"]
        await _join_via_invitation(async_client, org_id, headers_a, user_b)
        user_b_id, headers_b = user_b

        resp = await async_client.post(
            f"/api/v1/orgs/{org_id}/transfer-ownership/{user_b_id}",
            headers=headers_a,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_id"] == str(user_b_id)

        # Old owner became admin; new owner holds owner role
        members = await async_client.get(
            f"/api/v1/orgs/{org_id}/members", headers=headers_b
        )
        roles = {m["user_id"]: m["role"] for m in members.json()}
        assert roles[str(owner_id)] == "admin"
        assert roles[str(user_b_id)] == "owner"

    async def test_transfer_to_non_member_conflict(
        self, async_client: AsyncClient, user_a, org_with_owner
    ):
        _, headers_a = user_a
        stranger = uuid.uuid4()
        resp = await async_client.post(
            f"/api/v1/orgs/{org_with_owner['id']}/transfer-ownership/{stranger}",
            headers=headers_a,
        )
        assert resp.status_code == 409
