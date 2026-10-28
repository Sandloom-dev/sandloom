"""Payments: the API and the charges command contract's semantics."""

import uuid

import pytest
from httpx import AsyncClient

from academy.shared.contracts.payments_charges_v1 import PAYMENTS_CHARGES_V1
from sandloom.sdk import Contract
from sandloom.sdk.tenant import (
    bind_tenant,  # sandloom: advanced call a contract as a tenant
)
from sandloom.sdk.testing import as_user

charges = Contract(PAYMENTS_CHARGES_V1)


async def test_topup_and_balance(client: AsyncClient) -> None:
    _, headers = as_user()
    resp = await client.post(
        "/api/v1/payments/topup", json={"amount": 50}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["balance"] == 50
    assert (await client.get("/api/v1/payments/balance", headers=headers)).json()[
        "balance"
    ] == 50


async def test_charge_refuses_a_short_balance_and_refund_accepts_nothing(
    real_uow_client: AsyncClient,
) -> None:
    """Real commits: the contract runs in its own UoW, through the command log."""
    tenant = uuid.uuid4()
    user, headers = as_user(tenant_id=tenant)
    await real_uow_client.post(
        "/api/v1/payments/topup", json={"amount": 40}, headers=headers
    )
    with bind_tenant(tenant):
        assert await charges.charge(user, 30, operation_key="k-1") == 10
        # same key: replayed, never charged twice
        assert await charges.charge(user, 30, operation_key="k-1") == 10
        with pytest.raises(Exception) as refused:
            await charges.charge(user, 30, operation_key="k-2")
        assert getattr(refused.value, "status_code", None) == 402
        assert await charges.refund(user, "k-1", operation_key="r-1") == 40
        assert await charges.refund(user, "missing", operation_key="r-2") == 40
