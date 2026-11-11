"""``tenancy.security.v1``: the ``tenant_ids`` owner's operator contract.

``sandloom security revoke-sessions --tenant ORG`` asks the owner of the
``tenant_ids`` claim for the organization's members (confirmed by describe
before it acts). Reserved to tenancy (SANDLOOM-B034), reads only.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.testing import bind_contract_session
from sandloom_tenancy.contracts import TenancySecurityContract
from sandloom_tenancy.models import ROLE_MEMBER
from sandloom_tenancy.schemas import OrganizationCreateIn
from sandloom_tenancy.services import MembershipService, OrganizationService


def test_security_contract_lists_members_and_nothing_else() -> None:
    public = {n for n in vars(TenancySecurityContract) if not n.startswith("_")}
    assert public == {"list_member_user_ids"}


@pytest.mark.asyncio
async def test_members_are_paged_in_id_order(test_db: AsyncSession) -> None:
    owner_id = uuid.uuid4()
    org = await OrganizationService.create(
        OrganizationCreateIn(name="Ops Co", slug=f"o-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=test_db,
    )
    members = sorted([owner_id, *(uuid.uuid4() for _ in range(2))])
    for user_id in members:
        if user_id != owner_id:
            await MembershipService.add_member(org.id, user_id, ROLE_MEMBER, test_db)
    with bind_contract_session(test_db):
        first = await TenancySecurityContract.list_member_user_ids(org.id, None, 2)
        rest = await TenancySecurityContract.list_member_user_ids(org.id, first[-1], 2)
        stranger = await TenancySecurityContract.list_member_user_ids(
            uuid.uuid4(), None, 10
        )
    assert first + rest == members
    assert stranger == []
