from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from sandloom.sdk.contracts import contract_method
from sandloom.sdk.testing import bind_contract_session
from sandloom_tenancy.claims import tenancy_claims
from sandloom_tenancy.services import MembershipService


@pytest.mark.asyncio
async def test_tenancy_claims_include_all_authorized_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    tenant_ids = [uuid.uuid4(), uuid.uuid4()]
    list_ids = AsyncMock(return_value=tenant_ids)
    monkeypatch.setattr(MembershipService, "list_user_organization_ids", list_ids)

    session = AsyncMock()
    with bind_contract_session(session):
        claims = await contract_method(tenancy_claims)(user_id)

    assert claims == {"tenant_ids": [str(value) for value in tenant_ids]}
    list_ids.assert_awaited_once()
    assert list_ids.await_args is not None
    assert list_ids.await_args.args[1] is session  # read in the contract session
