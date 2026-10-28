"""billing.entitlements.v1 / billing.credits.v1 / billing.claims.v1: local == RPC.

Includes the regression the split review found: a domain 403 (an
organization spend for a foreign tenant) used to surface over RPC as a 503
"not authorized" while the local call raised 403.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.testing import (
    ContractCase,
    assert_contract_conformance,
    bind_contract_session,
    rpc_settings,
)
from sandloom_billing.services.billing_service import BillingService

_CONTRACTS = ["billing.claims.v1", "billing.credits.v1", "billing.entitlements.v1"]


@pytest.fixture(autouse=True)
def _keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process keyed as service ``billing`` calling itself over RPC."""
    from sandloom.sdk.infra import settings

    keyed = rpc_settings("billing", allowed_calls={"billing": _CONTRACTS})
    for name, value in keyed.items():
        monkeypatch.setattr(settings, name, value)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["asgi", "aiohttp", "httpx"])
async def test_billing_contracts_conform(
    sandloom_core: Any, test_db: AsyncSession, transport: str
) -> None:
    core = sandloom_core

    org_id, foreign_org, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    customer = await BillingService.get_or_create_customer(
        test_db, organization_id=org_id
    )
    await BillingService.add_credits(customer, 50, "seed-conformance", test_db)
    with bind_contract_session(test_db):
        await assert_contract_conformance(
            core,
            "billing.entitlements.v1",
            [
                ContractCase("check_subscription_active", ("organization", org_id)),
                ContractCase("check_subscription_active", ("user", user_id)),
                ContractCase("get_credit_balance", ("organization", org_id)),
                ContractCase("subscription_claims", ("organization", org_id)),
                ContractCase(
                    "get_credit_balance", ("organization", "nope"), label="bad uuid"
                ),
            ],
            transport=transport,
        )

        await assert_contract_conformance(
            core,
            "billing.credits.v1",
            [
                ContractCase("get_balance", ("organization", org_id)),
                # Same operation key on both paths: the provider's own
                # idempotent ledger replays the balance.
                ContractCase(
                    "spend", ("organization", org_id, 5, "op-conf"), tenant=org_id
                ),
                ContractCase(
                    "spend",
                    ("organization", foreign_org, 1, "op-foreign"),
                    tenant=org_id,
                    label="foreign organization -> 403 both ways",
                ),
                ContractCase(
                    "spend",
                    ("organization", foreign_org, 1, "op-empty"),
                    tenant=foreign_org,
                    label="no credits -> 402 both ways",
                ),
                ContractCase(
                    "spend",
                    ("organization", org_id, 1, ""),
                    tenant=org_id,
                    label="missing operation key -> 422 both ways",
                ),
                ContractCase(
                    "spend",
                    ("organization", org_id, 1, "op-typo"),
                    {"reasn": "typo"},
                    tenant=org_id,
                    label="unknown kwarg -> 422 both ways",
                ),
            ],
            transport=transport,
        )
        await assert_contract_conformance(
            core,
            "billing.claims.v1",
            [ContractCase("contribute", (user_id,))],
            transport=transport,
        )


@pytest.mark.asyncio
async def test_foreign_org_spend_is_403_not_503_over_rpc(
    sandloom_core: Any, test_db: AsyncSession
) -> None:
    from fastapi import HTTPException

    from sandloom.sdk.tenant import bind_tenant
    from sandloom.sdk.testing import in_process_rpc_client, rpc_proxy_for

    core = sandloom_core

    org_id, foreign_org = uuid.uuid4(), uuid.uuid4()
    with bind_contract_session(test_db):
        async with in_process_rpc_client(core) as client:
            remote = rpc_proxy_for(core, "billing.credits.v1", client)
            for credits in (core.get_service("billing.credits.v1"), remote):
                with bind_tenant(org_id), pytest.raises(HTTPException) as exc:
                    await credits.spend("organization", foreign_org, 1, "op-403")
                assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_personal_customers_can_independently_use_same_operation_key(
    sandloom_core, test_db
):
    first, second = uuid.uuid4(), uuid.uuid4()
    for user_id in (first, second):
        customer = await BillingService.get_or_create_customer(test_db, user_id=user_id)
        await BillingService.add_credits(customer, 20, "seed-personal", test_db)
    with bind_contract_session(test_db):
        credits = sandloom_core.get_service("billing.credits.v1")
        assert await credits.spend("user", first, 3, "same-operation-key") == 17
        assert await credits.spend("user", second, 3, "same-operation-key") == 17
