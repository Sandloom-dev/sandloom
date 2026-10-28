"""Service-registry contract tests: tenancy.membership.v1.

Other modules (billing) resolve membership through this contract instead of
importing tenancy internals — these tests pin the surface.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.errors import ContractArgumentError
from sandloom.sdk.testing import bind_contract_session
from sandloom_tenancy.exceptions import TenancyConflictError
from sandloom_tenancy.models import ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER
from sandloom_tenancy.schemas import OrganizationCreateIn
from sandloom_tenancy.services import (
    MembershipReadContract,
    MembershipService,
    OrganizationService,
)

READ_METHODS = {
    "get_role",
    "is_member",
    "has_role_at_least",
    "list_user_organization_ids",
    "get_member_state",
    "list_member_user_ids",
}


@pytest.mark.asyncio
async def test_membership_contract_roles(test_db: AsyncSession) -> None:
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    outsider_id = uuid.uuid4()

    org = await OrganizationService.create(
        OrganizationCreateIn(name="Contract Co", slug=f"c-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=test_db,
    )
    await MembershipService.add_member(org.id, member_id, ROLE_MEMBER, test_db)

    assert await MembershipService.get_role(org.id, owner_id, test_db) == ROLE_OWNER
    assert await MembershipService.get_role(org.id, member_id, test_db) == ROLE_MEMBER
    assert await MembershipService.get_role(org.id, outsider_id, test_db) is None

    assert await MembershipService.is_member(org.id, member_id, test_db) is True
    assert await MembershipService.is_member(org.id, outsider_id, test_db) is False

    assert (
        await MembershipService.has_role_at_least(org.id, owner_id, ROLE_ADMIN, test_db)
        is True
    )
    assert (
        await MembershipService.has_role_at_least(
            org.id, member_id, ROLE_ADMIN, test_db
        )
        is False
    )

    org_ids = await MembershipService.list_user_organization_ids(member_id, test_db)
    assert org.id in org_ids


@pytest.mark.asyncio
async def test_membership_service_registered_in_core(sandloom_core: Any) -> None:
    """The module's register() must expose the contract in the registry."""
    core = sandloom_core
    from sandloom_tenancy import MEMBERSHIP_SERVICE_V1

    svc = core.get_service(MEMBERSHIP_SERVICE_V1)
    # Only the read-only facade is published — never the full service, whose
    # write methods would be callable by any module / internal RPC caller.
    # get_service returns the provider bound to the contract runtime (own UoW
    # on tenancy's database); the registered object itself is the read facade.
    assert svc.contract_impl is MembershipReadContract
    assert svc.contract_impl is not MembershipService


def test_contract_surface_is_read_only() -> None:
    public = {
        name
        for name in dir(MembershipReadContract)
        if not name.startswith("_") and callable(getattr(MembershipReadContract, name))
    }
    assert public == READ_METHODS
    for write in ("add_member", "change_role", "remove_member", "list_members"):
        assert not hasattr(MembershipReadContract, write)


@pytest.mark.asyncio
async def test_contract_reads_match_service(test_db: AsyncSession) -> None:
    owner_id = uuid.uuid4()
    org = await OrganizationService.create(
        OrganizationCreateIn(name="Facade Co", slug=f"f-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=test_db,
    )
    c = MembershipReadContract
    # Contract methods take no session; the test owns the transaction, so it
    # binds its session as the contract session (the get_db-override seam).
    with bind_contract_session(test_db):
        assert await c.get_role(org.id, owner_id) == ROLE_OWNER
        assert await c.is_member(org.id, owner_id) is True
        assert await c.has_role_at_least(org.id, owner_id, ROLE_ADMIN) is True
        assert await c.list_user_organization_ids(owner_id) == [org.id]


@pytest.mark.asyncio
async def test_contract_refuses_a_caller_session(
    sandloom_core: Any, test_db: AsyncSession
) -> None:
    """A caller passing its session gets a 422 argument error (as over RPC)."""
    from sandloom_tenancy import MEMBERSHIP_SERVICE_V1

    owner_id = uuid.uuid4()
    org = await OrganizationService.create(
        OrganizationCreateIn(name="Legacy Co", slug=f"l-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=test_db,
    )
    membership = sandloom_core.get_service(MEMBERSHIP_SERVICE_V1)
    with bind_contract_session(test_db):
        with pytest.raises(ContractArgumentError) as positional:
            await membership.get_role(org.id, owner_id, test_db)
        with pytest.raises(ContractArgumentError) as keyword:
            await membership.is_member(org.id, owner_id, db=test_db)
        # Without the session the same calls answer.
        assert await membership.get_role(org.id, owner_id) == ROLE_OWNER
        assert await membership.is_member(org.id, owner_id) is True
    assert positional.value.status_code == keyword.value.status_code == 422


@pytest.mark.asyncio
async def test_add_member_never_grants_owner(test_db: AsyncSession) -> None:
    org = await OrganizationService.create(
        OrganizationCreateIn(name="Two Owners", slug=f"o-{uuid.uuid4().hex[:8]}"),
        owner_id=uuid.uuid4(),
        db=test_db,
    )
    with pytest.raises(TenancyConflictError):
        await MembershipService.add_member(org.id, uuid.uuid4(), ROLE_OWNER, test_db)


@pytest.mark.asyncio
async def test_add_member_duplicate_is_conflict_not_500(test_db: AsyncSession) -> None:
    owner_id = uuid.uuid4()
    org = await OrganizationService.create(
        OrganizationCreateIn(name="Dup Co", slug=f"d-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=test_db,
    )
    member_id = uuid.uuid4()
    await MembershipService.add_member(org.id, member_id, ROLE_MEMBER, test_db)
    with pytest.raises(TenancyConflictError):
        await MembershipService.add_member(org.id, member_id, ROLE_MEMBER, test_db)
    # the session is still usable after the savepoint rollback
    assert await MembershipService.get_role(org.id, member_id, test_db) == ROLE_MEMBER


@pytest.mark.asyncio
async def test_inactive_org_grants_no_role(test_db: AsyncSession) -> None:
    owner_id = uuid.uuid4()
    org = await OrganizationService.create(
        OrganizationCreateIn(name="Gone Co", slug=f"g-{uuid.uuid4().hex[:8]}"),
        owner_id=owner_id,
        db=test_db,
    )
    org_id = org.id
    await OrganizationService.soft_delete(org_id, test_db, actor_id=owner_id)
    with bind_contract_session(test_db):
        assert await MembershipReadContract.get_role(org_id, owner_id) is None
        assert await MembershipReadContract.is_member(org_id, owner_id) is False
        assert (
            await MembershipReadContract.has_role_at_least(
                org_id, owner_id, ROLE_MEMBER
            )
            is False
        )
        assert await MembershipReadContract.list_user_organization_ids(owner_id) == []


def test_role_at_least_unknown_minimum_fails_closed() -> None:
    from sandloom_tenancy.roles import role_at_least

    assert role_at_least("owner", "admin") is True
    assert role_at_least("bogus", "member") is False
    with pytest.raises(ValueError):
        role_at_least("owner", "admn")


def test_prebuilt_membership_reads_are_the_inline_sql() -> None:
    """The prebuilt contract reads compile to exactly the per-call SQL they replaced."""
    import re
    import uuid

    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from sandloom_tenancy.models import Organization, OrganizationMember
    from sandloom_tenancy.services import membership_service as ms

    dialect = postgresql.asyncpg.dialect()  # type: ignore[attr-defined]

    def sql(stmt: object) -> str:
        return re.sub(r"\$\d+(::\w+)?", "?", str(stmt.compile(dialect=dialect)))  # type: ignore[attr-defined]

    x = uuid.uuid4()
    joined = select(OrganizationMember.role).join(
        Organization, Organization.id == OrganizationMember.organization_id
    )
    assert sql(ms._ROLE_IN_ACTIVE_ORG) == sql(
        joined.where(
            OrganizationMember.organization_id == x,
            OrganizationMember.user_id == x,
            Organization.is_active.is_(True),
        )
    )
    assert sql(ms._ACTIVE_ORG_IDS_OF_USER) == sql(
        select(OrganizationMember.organization_id)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(OrganizationMember.user_id == x, Organization.is_active.is_(True))
    )
