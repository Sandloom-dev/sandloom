"""``GET /subscription`` and ``GET /credits``: decisions pinned at the HTTP level.

The two reads resolve the account (``resolve_tenant``: explicit ``org_id`` →
the authoritative ``tenancy.membership.v1`` read; no ``org_id`` → the caller's
PERSONAL account, never inferred from memberships — Public API 1.0 spec §7.4)
and then read the customer + its subscriptions / balance in ONE statement.
These tests pin every decision of that path, for BOTH endpoints: tenant
isolation (own org / foreign org / personal), 501 when tenancy is deployed but
unbound and an organization is asked for, a membership-provider outage
propagating (503, nothing created) — and that no customer is ever created for
a request that is refused (nor for one that succeeds: the reads are pure).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.testing import as_user
from sandloom_billing.models import BillingCustomer
from sandloom_billing.services import BillingService, tenant_resolver
from sandloom_billing.tests import tenancy_api

pytestmark = pytest.mark.asyncio

READS = ("/api/v1/billing/subscription", "/api/v1/billing/credits")


async def _org(
    client: AsyncClient,
    test_db: AsyncSession,
    owner_headers: dict[str, str],
    credits: int,
) -> uuid.UUID:
    org_id = await tenancy_api.create_org(client, owner_headers)
    customer = await BillingService.get_or_create_customer(
        test_db, organization_id=org_id
    )
    if credits:
        await BillingService.add_credits(customer, credits, f"g-{org_id}", test_db)
    await test_db.commit()
    return org_id


async def _customers(test_db: AsyncSession) -> int:
    return int(await test_db.scalar(select(func.count()).select_from(BillingCustomer)))


@pytest.mark.parametrize("path", READS)
async def test_org_reads_are_isolated(
    async_client: AsyncClient, test_db: AsyncSession, path: str
) -> None:
    _, a_headers = as_user()
    _, b_headers = as_user()
    org_a = await _org(async_client, test_db, a_headers, 11)
    org_b = await _org(async_client, test_db, b_headers, 22)

    # an organization account needs org_id; without it the caller's own
    # PERSONAL account answers, never the single organization (spec §7.4)
    implicit = await async_client.get(path, headers=a_headers)
    explicit = await async_client.get(f"{path}?org_id={org_a}", headers=a_headers)
    assert implicit.status_code == explicit.status_code == 200
    if path.endswith("credits"):
        assert explicit.json() == {"balance": 11, "debt": 0, "in_debt": False}
        assert implicit.json() == {"balance": 0, "debt": 0, "in_debt": False}
        own_b = await async_client.get(f"{path}?org_id={org_b}", headers=b_headers)
        assert own_b.json() == {"balance": 22, "debt": 0, "in_debt": False}

    before = await _customers(test_db)
    foreign = await async_client.get(f"{path}?org_id={org_b}", headers=a_headers)
    assert foreign.status_code == 403
    unknown = await async_client.get(f"{path}?org_id={uuid.uuid4()}", headers=a_headers)
    assert unknown.status_code == 403
    assert await _customers(test_db) == before  # refused: nothing created


@pytest.mark.parametrize("path", READS)
async def test_ambiguous_context_is_422(
    async_client: AsyncClient, test_db: AsyncSession, path: str
) -> None:
    """No ambiguity is left: a member of several organizations reads their
    personal account without org_id (and each organization with it)."""
    _, headers = as_user()
    first = await _org(async_client, test_db, headers, 3)
    await _org(async_client, test_db, headers, 0)
    personal = await async_client.get(path, headers=headers)
    assert personal.status_code == 200
    org = await async_client.get(f"{path}?org_id={first}", headers=headers)
    assert org.status_code == 200
    if path.endswith("credits"):
        assert personal.json()["balance"] == 0 and org.json()["balance"] == 3


@pytest.mark.parametrize("path", READS)
async def test_tenancy_deployed_but_unbound_is_501(
    async_client: AsyncClient,
    test_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.setattr(tenant_resolver, "_get_membership_service", lambda _c: None)
    before = await _customers(test_db)
    # an explicit org_id needs the membership contract: 501
    _, tenant_headers = as_user()
    resp = await async_client.get(
        f"{path}?org_id={uuid.uuid4()}", headers=tenant_headers
    )
    assert resp.status_code == 501
    # without org_id the account is personal, tenant claims or not
    assert (await async_client.get(path, headers=tenant_headers)).status_code == 200
    _, personal_headers = as_user(tenant=False)
    resp = await async_client.get(
        f"{path}?org_id={uuid.uuid4()}", headers=personal_headers
    )
    assert resp.status_code == 501
    assert await _customers(test_db) == before

    # a personal-only deployment (no tenant claims anywhere): personal tenant
    # (a pure read: answered without creating its customer)
    resp = await async_client.get(path, headers=personal_headers)
    assert resp.status_code == 200, resp.text
    assert await _customers(test_db) == before


@pytest.mark.parametrize("path", READS)
async def test_membership_outage_propagates(
    async_client: AsyncClient,
    test_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    class _Down:
        async def list_user_organization_ids(self, _user_id: Any) -> Any:
            raise HTTPException(503, detail="tenancy unreachable")

        async def get_role(self, *_a: Any) -> Any:
            raise HTTPException(503, detail="tenancy unreachable")

    monkeypatch.setattr(tenant_resolver, "_get_membership_service", lambda _c: _Down())
    before = await _customers(test_db)
    _, headers = as_user()
    resp = await async_client.get(f"{path}?org_id={uuid.uuid4()}", headers=headers)
    assert resp.status_code == 503
    assert await _customers(test_db) == before  # never degraded to personal
    # the personal account needs no membership read at all
    assert (await async_client.get(path, headers=headers)).status_code == 200
