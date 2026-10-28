"""The public ``tenancy.membership.v1`` read contract.

This is the ONLY object registered in the service registry for other modules
(and, via ``SERVICE_BINDINGS``, for internal RPC callers). It exposes exactly
six read methods; the write side of :class:`MembershipService` (add / change
role / remove) stays internal to the tenancy module so no other module — or
RPC caller holding the internal token — can mint owners or remove members.

Contract semantics (identical locally and over RPC): the methods take NO
database session. Each call runs in tenancy's OWN read-only Unit of Work on
the database the tenancy module declares (see
``@contract``), never in the caller's
transaction — so a caller cannot see uncommitted tenancy writes and tenancy is
free to live in a different database or process. Passing a session is an
argument error (``422``).

Keep the public surface to these six methods: the RPC server publishes every
public, wire-safe callable on the registered object.
"""

from uuid import UUID

from sqlalchemy import and_, select

from sandloom.core.module_api import contract_session
from sandloom.sdk import contract
from sandloom.sdk.contracts import contract_reads
from sandloom.sdk.infra import settings
from sandloom_tenancy.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberVersion,
    role_at_least,
)
from sandloom_tenancy.schemas.membership import MemberState
from sandloom_tenancy.services.operations import (
    active_org_ids_of_user,
    member_role_in_active_org,
)


async def _role(organization_id: UUID, user_id: UUID) -> str | None:
    found = await contract_reads().one(
        member_role_in_active_org,
        organization_id=organization_id,
        user_id=user_id,
    )
    return None if found is None else found.role


@contract("tenancy.membership.v1")
class MembershipReadContract:
    """Who belongs to which organization, read live from tenancy.

    Reads only, never in the caller's transaction (a caller cannot see
    uncommitted tenancy writes), the same in a monolith and over RPC. An
    inactive (deleted) organization has no members for every method but
    ``list_member_user_ids``. Roles rank ``member`` < ``admin`` < ``owner``.
    For a route gate prefer ``OrgRole("admin")``; in tests,
    ``fake_contract(TENANCY_MEMBERSHIP_V1, Fake())`` with the methods you call.
    """

    # Every @contract.read(planned=True) method runs only declared operations
    # (contract_reads()): no Unit of Work, a read-only lease from tenancy's
    # contract pool (one round trip with PLANNER_OPTIMIZED_EXECUTOR on a
    # database without tenant_rls).

    @contract.read(planned=True)
    async def get_role(self, organization_id: UUID, user_id: UUID) -> str | None:
        """The user's role (``"member"``, ``"admin"``, ``"owner"``) in an
        active organization; ``None`` when not a member."""
        return await _role(organization_id, user_id)

    @contract.read(planned=True)
    async def is_member(self, organization_id: UUID, user_id: UUID) -> bool:
        """True when the user is a member of the active organization."""
        return await _role(organization_id, user_id) is not None

    @contract.read(planned=True)
    async def has_role_at_least(
        self, organization_id: UUID, user_id: UUID, minimum: str
    ) -> bool:
        """True when the user's role ranks at least ``minimum`` (``member`` <
        ``admin`` < ``owner``). Live: a removal is seen at once, unlike the
        token's ``tenant_ids`` claim."""
        role = await _role(organization_id, user_id)
        return role is not None and role_at_least(role, minimum)

    async def get_member_state(
        self, organization_id: UUID, user_id: UUID
    ) -> MemberState:
        """The pair's current role (``None`` when not a member of an active
        organization) and its last member-event ``version`` (``0`` when tenancy
        never issued one), read in one snapshot. What a projection's
        ``rebuild_from`` returns, and what a v1 consumer re-reads when an event
        has no version (``sandloom.shared.membership``)."""
        db = contract_session()
        member = and_(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
        active = and_(
            Organization.id == OrganizationMember.organization_id,
            Organization.is_active.is_(True),
        )
        role = (
            select(OrganizationMember.role)
            .join(Organization, active)
            .where(member)
            .scalar_subquery()
        )
        version = (
            select(OrganizationMemberVersion.version)
            .where(
                OrganizationMemberVersion.organization_id == organization_id,
                OrganizationMemberVersion.user_id == user_id,
            )
            .scalar_subquery()
        )
        row = (await db.execute(select(role, version))).one()
        return MemberState(
            organization_id=organization_id,
            user_id=user_id,
            role=row[0],
            version=int(row[1] or 0),
        )

    @contract.read(planned=True)
    async def list_user_organization_ids(self, user_id: UUID) -> list[UUID]:
        """Every active organization the user belongs to, in creation order
        (an error above ``TENANT_CLAIMS_MAX_ORGANIZATIONS``)."""
        reads = contract_reads()
        ids: list[UUID] = []
        cap = settings.tenant_claims_max_organizations
        cursor = None
        while True:
            page = await reads.page(
                active_org_ids_of_user,
                limit=min(active_org_ids_of_user.page_cap(), cap + 1 - len(ids)),
                cursor=cursor,
                user_id=user_id,
            )
            ids.extend(item.id for item in page.items)
            if len(ids) > cap:
                raise ValueError(
                    "Organization list exceeds TENANT_CLAIMS_MAX_ORGANIZATIONS"
                )
            if page.next_cursor is None:
                return ids
            cursor = page.next_cursor

    async def list_member_user_ids(
        self, organization_id: UUID, after: UUID | None, limit: int
    ) -> list[UUID]:
        """Member user ids of an organization (active or not), in id order,
        strictly after ``after``: keyset paging, at most 1000 per call (a
        larger ``limit`` is capped). Page until fewer than ``limit`` return."""
        db = contract_session()
        stmt = (
            select(OrganizationMember.user_id)
            .where(OrganizationMember.organization_id == organization_id)
            .order_by(OrganizationMember.user_id)
            .limit(max(1, min(limit, 1000)))
        )
        if after is not None:
            stmt = stmt.where(OrganizationMember.user_id > after)
        return list((await db.execute(stmt)).scalars())


__all__ = ["MembershipReadContract"]
