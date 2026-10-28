"""Tickets API tests — multi-tenant flow reusing identity/tenancy/billing.

Exercises the cross-module sync contract (tenancy membership), the billing
entitlement gate, RBAC on status changes, idempotent retries, and auth.
"""

import uuid

import pytest
from httpx import AsyncClient

from sandloom.sdk.authz import (  # sandloom: advanced a custom live entitlements adapter
    ENTITLEMENTS,
    register_live_adapter,
)
from sandloom.sdk.testing import as_user, published

from ..events import TicketCreated, TicketStatusChanged


@pytest.mark.asyncio
class TestTickets:
    async def test_member_can_open_and_list(
        self, client: AsyncClient, member_org
    ) -> None:
        org_id, _user_id, headers = member_org
        created = await client.post(
            f"/api/v1/orgs/{org_id}/tickets",
            json={"subject": "Printer is on fire", "priority": "high"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["organization_id"] == str(org_id)
        assert body["status"] == "open"
        assert body["priority"] == "high"

        [event] = published(TicketCreated)
        assert (event.organization_id, event.priority) == (org_id, "high")

        listed = await client.get(f"/api/v1/orgs/{org_id}/tickets", headers=headers)
        assert listed.status_code == 200
        assert len(listed.json()) == 1

    async def test_non_member_is_forbidden(
        self, client: AsyncClient, member_org
    ) -> None:
        org_id, _user_id, _headers = member_org
        # A different, unaffiliated user.
        _, outsider = as_user()
        resp = await client.post(
            f"/api/v1/orgs/{org_id}/tickets",
            json={"subject": "let me in"},
            headers=outsider,
        )
        assert resp.status_code == 403

    async def test_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"/api/v1/orgs/{uuid.uuid4()}/tickets", json={"subject": "x"}
        )
        assert resp.status_code == 401

    async def test_only_admin_changes_status(
        self, client: AsyncClient, member_org, admin_org
    ) -> None:
        # A plain member may open a ticket but not move its status.
        org_id, _user_id, member_headers = member_org
        created = await client.post(
            f"/api/v1/orgs/{org_id}/tickets",
            json={"subject": "need help"},
            headers=member_headers,
        )
        ticket_id = created.json()["id"]
        denied = await client.post(
            f"/api/v1/orgs/{org_id}/tickets/{ticket_id}/status",
            json={"status": "closed"},
            headers=member_headers,
        )
        assert denied.status_code == 403

        # An admin in their own org can.
        admin_org_id, _admin_id, admin_headers = admin_org
        t2 = await client.post(
            f"/api/v1/orgs/{admin_org_id}/tickets",
            json={"subject": "admin ticket"},
            headers=admin_headers,
        )
        t2_id = t2.json()["id"]
        ok = await client.post(
            f"/api/v1/orgs/{admin_org_id}/tickets/{t2_id}/status",
            json={"status": "closed"},
            headers=admin_headers,
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "closed"
        [moved] = published(TicketStatusChanged)
        assert (moved.old_status, moved.new_status) == ("open", "closed")

    async def test_idempotent_create_replays(
        self, client: AsyncClient, member_org
    ) -> None:
        org_id, _user_id, headers = member_org
        key_headers = {**headers, "Idempotency-Key": uuid.uuid4().hex}
        first = await client.post(
            f"/api/v1/orgs/{org_id}/tickets",
            json={"subject": "dup-safe"},
            headers=key_headers,
        )
        assert first.status_code == 201
        second = await client.post(
            f"/api/v1/orgs/{org_id}/tickets",
            json={"subject": "dup-safe"},
            headers=key_headers,
        )
        # Same key -> original response replayed, not a second ticket.
        assert second.status_code == 201
        assert second.json()["id"] == first.json()["id"]
        assert second.headers.get("Idempotent-Replay") == "true"

    async def test_export_requires_subscription(
        self, client: AsyncClient, member_org
    ) -> None:
        # The gate checks the ORGANIZATION's subscription (spec §7): a caller's
        # personal subscription never opens an organization's premium export.
        org_id, user_id, _seed_headers = member_org
        # a token minted after joining carries the organization in its
        # signed tenant allowlist
        _uid, headers = as_user(user_id, tenant_id=org_id)
        resp = await client.get(
            f"/api/v1/orgs/{org_id}/tickets/export", headers=headers
        )
        assert resp.status_code == 402

        # Personal subscription active, organization not: still 402.
        _uid, personal = as_user(
            user_id, tenant_id=org_id, subscription_status="active"
        )
        resp = await client.get(
            f"/api/v1/orgs/{org_id}/tickets/export", headers=personal
        )
        assert resp.status_code == 402

        # Organization subscribed, personal not: allowed.
        class _Entitlements:
            """The default entitlements adapter, except that ``org`` (when
            set) has an active subscription."""

            service = "billing.entitlements.v1"

            def __init__(self, org):
                self.org = org

            async def subscription_active(
                self, service, *, tenant_type, tenant_id, ctx
            ):
                if self.org is not None and tenant_id == self.org:
                    return tenant_type == "organization"
                return bool(
                    await service.check_subscription_active(tenant_type, tenant_id)
                )

            async def credit_balance(self, service, *, tenant_type, tenant_id, ctx):
                return int(await service.get_credit_balance(tenant_type, tenant_id))

        register_live_adapter(ENTITLEMENTS, _Entitlements(org_id))
        try:
            ok = await client.get(
                f"/api/v1/orgs/{org_id}/tickets/export", headers=headers
            )
        finally:
            register_live_adapter(ENTITLEMENTS, _Entitlements(None))
        assert ok.status_code == 200, ok.text
        assert ok.json()["organization_id"] == str(org_id)
