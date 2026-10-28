"""Notifications: ``send`` is idempotent per key; the inbox lists them."""

import uuid

from httpx import AsyncClient

from academy.shared.contracts.notifications_mail_v1 import NOTIFICATIONS_MAIL_V1
from sandloom.sdk import Contract
from sandloom.sdk.tenant import (
    bind_tenant,  # sandloom: advanced call a contract as a tenant
)
from sandloom.sdk.testing import as_user

mail = Contract(NOTIFICATIONS_MAIL_V1)


async def test_send_is_idempotent_and_listed(real_uow_client: AsyncClient) -> None:
    tenant = uuid.uuid4()
    user, headers = as_user(tenant_id=tenant)
    with bind_tenant(tenant):
        first = await mail.send(user, "welcome", operation_key="m-1")
        assert await mail.send(user, "welcome", operation_key="m-1") == first
    resp = await real_uow_client.get("/api/v1/notifications/inbox", headers=headers)
    assert resp.status_code == 200
    assert [m["template"] for m in resp.json()] == ["welcome"]
