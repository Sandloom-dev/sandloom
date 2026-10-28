"""Organization membership model."""

import uuid

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, new_id
from sandloom_tenancy.roles import ROLE_MEMBER


class OrganizationMember(Base):
    """Link between an organization and a user (UUID, no cross-module FK).

    Tenant scoping decision: this table is deliberately NOT ``TenantScoped``.
    The organization *is* the tenant root, and its membership table is the
    tenant directory itself — it must be readable across tenants:

    * ``list_user_organization_ids`` (the access-token ``tenant_ids`` claims
      contributor) runs at login with no tenant bound and spans every org the
      user belongs to;
    * the ``tenancy.membership.v1`` contract (billing, the shared ``OrgRole``
      atom) asks "is user U in org O?" while a *different* tenant (or none) may
      be bound — auto-filtering on the bound tenant would answer wrongly.

    Wrapping every one of those in ``unscoped()`` would invert the safety
    default without adding protection. Instead, every query in
    ``sandloom_tenancy.services`` filters on ``organization_id`` explicitly (or on the
    caller's own ``user_id`` for cross-org listings), and the tenancy tests
    cover cross-org isolation of each endpoint.
    """

    __tablename__ = "tenancy_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_tenancy_member"),
        # the member list's keyset order (ListOperation members_of_org)
        Index("ix_tenancy_members_org_created", "organization_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    # No standalone index: uq_tenancy_member (organization_id, user_id) leads
    # with it and serves every per-organization lookup (dropped in 0003).
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("tenancy_organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_MEMBER)

    created_at: Mapped[CreatedAt]


class OrganizationMemberVersion(Base):
    """Per-membership event version of an ``(organization_id, user_id)`` pair.

    Every member lifecycle event (added / role changed / removed, ownership
    transfer) carries ``version``: bumped atomically
    (``INSERT … ON CONFLICT DO UPDATE SET version = version + 1``) in the
    transaction of the change, so the row lock serializes concurrent changes of
    one pair and versions increase in commit order. It lives OUTSIDE
    ``tenancy_members`` because it must survive the member row: a removal
    followed by a re-add continues the sequence, so a version-guarded consumer
    (``monotonic_upsert``) never drops the re-add as older than the removal.
    Same tenant-scoping decision as :class:`OrganizationMember`.
    """

    __tablename__ = "tenancy_member_versions"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("tenancy_organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
