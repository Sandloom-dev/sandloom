"""tenancy.membership.v1 / tenancy.claims.v1 behave identically local vs RPC."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.testing import (
    ContractCase,
    assert_contract_conformance,
    bind_contract_session,
)
from sandloom_tenancy.models import ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER
from sandloom_tenancy.schemas import OrganizationCreateIn
from sandloom_tenancy.services import MembershipService, OrganizationService


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.sdk.infra import settings

    monkeypatch.setattr(settings, "internal_api_token", "conformance-secret")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["asgi", "aiohttp", "httpx"])
async def test_tenancy_contracts_conform(
    sandloom_core: Any, test_db: AsyncSession, transport: str
) -> None:
    core = sandloom_core

    owner_id, member_id, outsider_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    org = await OrganizationService.create(
        OrganizationCreateIn(name="Conform Co", slug=f"cf-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=test_db,
    )
    await MembershipService.add_member(org.id, member_id, ROLE_MEMBER, test_db)
    with bind_contract_session(test_db):
        await assert_contract_conformance(
            core,
            "tenancy.membership.v1",
            [
                ContractCase("get_role", (org.id, owner_id)),
                ContractCase("get_role", (org.id, outsider_id)),
                ContractCase("get_role", (str(org.id), str(member_id))),
                ContractCase("is_member", (org.id, member_id)),
                ContractCase("is_member", (org.id, outsider_id), tenant=org.id),
                ContractCase("has_role_at_least", (org.id, owner_id, ROLE_ADMIN)),
                ContractCase("has_role_at_least", (org.id, member_id, ROLE_OWNER)),
                ContractCase("list_user_organization_ids", (member_id,)),
                ContractCase("get_role", ("not-a-uuid", owner_id), label="bad uuid"),
                ContractCase("get_role", (org.id,), label="missing argument"),
            ],
            transport=transport,
        )
        await assert_contract_conformance(
            core,
            "tenancy.claims.v1",
            [
                ContractCase("contribute", (owner_id,)),
                ContractCase("contribute", (outsider_id,)),
            ],
            transport=transport,
        )
