"""Invitation lifecycle tests."""

import pytest
from httpx import AsyncClient

from sandloom_tenancy.tests.conftest import email_of


@pytest.mark.asyncio
class TestInvitations:
    async def test_invite_accept_flow(
        self, async_client: AsyncClient, user_a, user_b, org_with_owner
    ):
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        user_b_id, headers_b = user_b

        invite = await async_client.post(
            f"/api/v1/orgs/{org_id}/invitations",
            # Case differences in the invited address must not matter.
            json={"email": email_of(user_b_id).upper(), "role": "member"},
            headers=headers_a,
        )
        assert invite.status_code == 201, invite.text
        body = invite.json()
        assert body["email"].lower() == email_of(user_b_id)
        assert "token" in body and len(body["token"]) >= 16

        accept = await async_client.post(
            "/api/v1/invitations/accept",
            json={"token": body["token"]},
            headers=headers_b,
        )
        assert accept.status_code == 200
        assert accept.json()["user_id"] == str(user_b_id)
        assert accept.json()["role"] == "member"

    async def test_token_is_single_use(
        self, async_client: AsyncClient, user_a, user_b, user_c, org_with_owner
    ):
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        user_b_id, headers_b = user_b
        invite = await async_client.post(
            f"/api/v1/orgs/{org_id}/invitations",
            json={"email": email_of(user_b_id)},
            headers=headers_a,
        )
        token = invite.json()["token"]

        first = await async_client.post(
            "/api/v1/invitations/accept", json={"token": token}, headers=headers_b
        )
        assert first.status_code == 200

        _, headers_c = user_c
        second = await async_client.post(
            "/api/v1/invitations/accept", json={"token": token}, headers=headers_c
        )
        assert second.status_code == 404

    async def test_duplicate_pending_invitation_conflict(
        self, async_client: AsyncClient, user_a, org_with_owner
    ):
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        payload = {"email": "dup@example.com"}
        first = await async_client.post(
            f"/api/v1/orgs/{org_id}/invitations", json=payload, headers=headers_a
        )
        assert first.status_code == 201
        second = await async_client.post(
            f"/api/v1/orgs/{org_id}/invitations", json=payload, headers=headers_a
        )
        assert second.status_code == 409

    async def test_revoked_invitation_cannot_be_accepted(
        self, async_client: AsyncClient, user_a, user_b, org_with_owner
    ):
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        user_b_id, headers_b = user_b
        invite = await async_client.post(
            f"/api/v1/orgs/{org_id}/invitations",
            json={"email": email_of(user_b_id)},
            headers=headers_a,
        )
        inv = invite.json()

        revoke = await async_client.delete(
            f"/api/v1/orgs/{org_id}/invitations/{inv['id']}", headers=headers_a
        )
        assert revoke.status_code == 204

        accept = await async_client.post(
            "/api/v1/invitations/accept",
            json={"token": inv["token"]},
            headers=headers_b,
        )
        assert accept.status_code == 404

    async def test_owner_role_cannot_be_invited(
        self, async_client: AsyncClient, user_a, org_with_owner
    ):
        _, headers_a = user_a
        resp = await async_client.post(
            f"/api/v1/orgs/{org_with_owner['id']}/invitations",
            json={"email": "owner@example.com", "role": "owner"},
            headers=headers_a,
        )
        assert resp.status_code == 422

    async def test_list_pending_invitations(
        self, async_client: AsyncClient, user_a, org_with_owner
    ):
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        await async_client.post(
            f"/api/v1/orgs/{org_id}/invitations",
            json={"email": "pending1@example.com"},
            headers=headers_a,
        )
        listing = await async_client.get(
            f"/api/v1/orgs/{org_id}/invitations", headers=headers_a
        )
        assert listing.status_code == 200
        emails = [i["email"] for i in listing.json()]
        assert "pending1@example.com" in emails
        # Tokens must never appear in listings
        assert all("token" not in i for i in listing.json())

    async def test_only_the_owner_can_invite_an_admin(
        self, async_client: AsyncClient, user_a, user_b, org_with_owner
    ):
        """An admin could mint more admins by invitation (bypassing the
        owner-only rule for changing an admin's role)."""
        _, headers_a = user_a
        org_id = org_with_owner["id"]
        user_b_id, headers_b = user_b
        invite = await async_client.post(
            f"/api/v1/orgs/{org_id}/invitations",
            json={"email": email_of(user_b_id), "role": "admin"},
            headers=headers_a,
        )
        assert invite.status_code == 201, invite.text  # owner may
        accept = await async_client.post(
            "/api/v1/invitations/accept",
            json={"token": invite.json()["token"]},
            headers=headers_b,
        )
        assert accept.status_code == 200
        assert accept.json()["role"] == "admin"

        as_admin = await async_client.post(
            f"/api/v1/orgs/{org_id}/invitations",
            json={"email": "second-admin@example.com", "role": "admin"},
            headers=headers_b,
        )
        assert as_admin.status_code == 403, as_admin.text
        member = await async_client.post(
            f"/api/v1/orgs/{org_id}/invitations",
            json={"email": "plain-member@example.com", "role": "member"},
            headers=headers_b,
        )
        assert member.status_code == 201, member.text
