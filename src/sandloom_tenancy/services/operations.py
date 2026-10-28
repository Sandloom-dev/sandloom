"""Declared read operations of the tenancy module (execution planner).

The module states WHAT it reads; the kernel's planner decides HOW (the ORM
general form inside a Unit of Work, or a generated statement on the
optimized executor in a read-only scope). Only tenancy's own entities are
referenced.
"""

from sandloom.sdk.data import JoinRead, ListOperation, ReadOperation
from sandloom_tenancy.models import Organization, OrganizationMember
from sandloom_tenancy.schemas.membership import (
    MemberRoleOut,
    OrganizationIdOut,
)
from sandloom_tenancy.schemas.organization import MemberOut, OrganizationOut

#: The membership guard: the member's role in an ACTIVE organization.
#: ``tenancy_members`` JOIN ``tenancy_organizations`` on the declared FK,
#: keyed by the unique (organization_id, user_id). Neither table is
#: TenantScoped (the organization IS the tenant root; see OrganizationMember).
member_role_in_active_org = JoinRead(
    entity=OrganizationMember,
    join=Organization,
    via="organization_id",
    by=("organization_id", "user_id"),
    join_where={"is_active": True},
    result=MemberRoleOut,
    name="tenancy.member.role_in_active_org",
)

#: ``GET /orgs/{id}``: one organization by id, as the API DTO (the route's
#: membership guard has already required it to be active).
org_by_id = ReadOperation(
    entity=Organization,
    result=OrganizationOut,
    by=("id",),
    name="tenancy.org.by_id",
)

#: ``GET /orgs/{id}/members``: one keyset page of an organization's members in
#: join order (index ``ix_tenancy_members_org_created``).
members_of_org = ListOperation(
    entity=OrganizationMember,
    result=MemberOut,
    by=("organization_id",),
    order_by=("created_at",),
    name="tenancy.members.of_org",
)

#: ``GET /orgs``: the caller's ACTIVE organizations in creation order —
#: organizations JOIN their membership row of ``user_id`` (one-to-many on the
#: declared FK, pinned to one row by the unique (organization_id, user_id)).
active_orgs_of_user = ListOperation(
    entity=Organization,
    result=OrganizationOut,
    join=OrganizationMember,
    via="organization_id",
    join_by={"user_id": "user_id"},
    where={"is_active": True},
    order_by=("created_at",),
    name="tenancy.orgs.active_of_user",
)

#: ``tenancy.membership.v1.list_user_organization_ids``: the same list, ids only.
active_org_ids_of_user = ListOperation(
    entity=Organization,
    result=OrganizationIdOut,
    join=OrganizationMember,
    via="organization_id",
    join_by={"user_id": "user_id"},
    where={"is_active": True},
    order_by=("created_at",),
    name="tenancy.orgs.active_ids_of_user",
)

#: Everything tenancy declares (registered at startup, see ``sandloom_tenancy``).
OPERATIONS = (
    member_role_in_active_org,
    org_by_id,
    members_of_org,
    active_orgs_of_user,
    active_org_ids_of_user,
)

__all__ = [
    "OPERATIONS",
    "active_org_ids_of_user",
    "active_orgs_of_user",
    "member_role_in_active_org",
    "members_of_org",
    "org_by_id",
]
