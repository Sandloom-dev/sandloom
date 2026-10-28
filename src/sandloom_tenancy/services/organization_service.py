"""Organization lifecycle operations."""

from uuid import UUID

from sqlalchemy import literal, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import record_audit
from sandloom.sdk.effects import sensitive_effect
from sandloom.sdk.security import revoke_user_tokens_on_commit
from sandloom_tenancy import events
from sandloom_tenancy.exceptions import (
    TenancyConflictError,
    TenancyForbiddenError,
    TenancyNotFoundError,
)
from sandloom_tenancy.models import (
    ROLE_ADMIN,
    ROLE_OWNER,
    Organization,
    OrganizationMember,
)
from sandloom_tenancy.schemas import OrganizationCreateIn, OrganizationUpdateIn
from sandloom_tenancy.services.membership_service import MembershipService


class OrganizationService:
    """CRUD + ownership semantics for organizations."""

    @classmethod
    async def create(
        cls, data: OrganizationCreateIn, owner_id: UUID, db: AsyncSession
    ) -> Organization:
        taken = TenancyConflictError(
            f"Organization slug '{data.slug}' is already taken"
        )
        existing = await db.execute(
            select(Organization.id).where(Organization.slug == data.slug)
        )
        if existing.scalar_one_or_none() is not None:
            raise taken

        org = Organization(name=data.name, slug=data.slug, owner_id=owner_id)
        try:
            # The pre-check is a fast path; the unique index is the arbiter
            # when two requests race for the same slug.
            async with db.begin_nested():
                db.add(org)
                await db.flush()  # obtain org.id inside the same transaction
        except IntegrityError as exc:
            raise taken from exc
        db.add(
            OrganizationMember(
                organization_id=org.id, user_id=owner_id, role=ROLE_OWNER
            )
        )
        await db.flush()
        await db.refresh(org)
        await events.OrganizationCreated(
            organization_id=org.id, actor_id=owner_id
        ).publish()
        # The owner's membership is a member like any other for consumers.
        version = await MembershipService.next_version(org.id, owner_id, db)
        await events.publish_member(
            events.MemberAdded(
                organization_id=org.id,
                user_id=owner_id,
                role=ROLE_OWNER,
                actor_id=owner_id,
                version=version,
            )
        )
        return org

    @classmethod
    async def get_or_404(cls, organization_id: UUID, db: AsyncSession) -> Organization:
        """Active organization or :class:`TenancyNotFoundError` (name kept for b/c)."""
        result = await db.execute(
            select(Organization).where(
                Organization.id == organization_id,
                Organization.is_active.is_(True),
            )
        )
        org = result.scalar_one_or_none()
        if org is None:
            raise TenancyNotFoundError("Organization not found")
        return org

    @classmethod
    async def _get_active_for_update(
        cls, organization_id: UUID, db: AsyncSession
    ) -> Organization:
        """Lock the active organization row; fresh values even if cached."""
        result = await db.execute(
            select(Organization)
            .where(
                Organization.id == organization_id,
                Organization.is_active.is_(True),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        org = result.scalar_one_or_none()
        if org is None:
            raise TenancyNotFoundError("Organization not found")
        return org

    @classmethod
    async def list_for_user(
        cls,
        user_id: UUID,
        db: AsyncSession,
        *,
        limit: int | None = None,
        after: UUID | None = None,
    ) -> list[Organization]:
        """Active organizations of ``user_id`` (creation order), optionally paged.

        Keyset pagination: ``after`` is the ``id`` of the last organization of
        the previous page; without ``limit`` every organization is returned
        (the original behaviour).
        """
        query = (
            select(Organization)
            .join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id,
            )
            .where(
                OrganizationMember.user_id == user_id,
                Organization.is_active.is_(True),
            )
        )
        if after is not None:
            cursor_created = (
                select(Organization.created_at)
                .where(Organization.id == after)
                .scalar_subquery()
            )
            query = query.where(
                tuple_(Organization.created_at, Organization.id)
                > tuple_(cursor_created, literal(after))
            )
        query = query.order_by(Organization.created_at, Organization.id)
        if limit is not None:
            query = query.limit(limit)
        result = await db.execute(query)
        return list(result.scalars().all())

    @classmethod
    async def update(
        cls,
        organization_id: UUID,
        data: OrganizationUpdateIn,
        db: AsyncSession,
        actor_id: UUID | None = None,
    ) -> Organization:
        """Update mutable attributes (name). Deactivation is ``soft_delete`` only.

        ``actor_id`` must be an admin+ of the organization AT COMMIT: the
        organization row is locked first, then the actor's membership row
        ``FOR SHARE`` (ADR-0003 G3; org → member lock order, as in
        ``transfer_ownership``). ``actor_id=None`` is a trusted system call.
        """
        org = await cls._get_active_for_update(organization_id, db)
        if actor_id is not None:
            await MembershipService.lock_actor_role(organization_id, actor_id, db)
        if data.name is not None and data.name != org.name:
            org.name = data.name
            await db.flush()
            await db.refresh(org)
            await events.OrganizationUpdated(
                organization_id=org.id, actor_id=actor_id
            ).publish()
        return org

    @classmethod
    @sensitive_effect("admin")
    async def soft_delete(
        cls, organization_id: UUID, db: AsyncSession, actor_id: UUID | None = None
    ) -> None:
        """Deactivate the organization (owner-only at the API)."""
        org = await cls._get_active_for_update(organization_id, db)
        if actor_id is not None and org.owner_id != actor_id:
            raise TenancyForbiddenError("Only the owner can delete the organization")
        org.is_active = False
        await db.flush()
        await events.OrganizationDeleted(
            organization_id=org.id, actor_id=actor_id
        ).publish()
        # Every member's token still lists the org in tenant_ids: revoke them
        # so TenantScope / require(...) stop honouring the stale claim —
        # immediately (mark written before commit, or the deletion is refused).
        member_ids = (
            await db.execute(
                select(OrganizationMember.user_id).where(
                    OrganizationMember.organization_id == org.id
                )
            )
        ).scalars()
        for member_id in set(member_ids):
            await revoke_user_tokens_on_commit(
                member_id, reason="tenancy.organization.deleted", require_immediate=True
            )
        record_audit(
            "tenancy.organization.deleted",
            actor_id=actor_id,
            resource_type="organization",
            resource_id=str(org.id),
            data={"slug": org.slug},
            required=True,
        )

    @classmethod
    @sensitive_effect("admin")
    async def transfer_ownership(
        cls,
        organization_id: UUID,
        new_owner_id: UUID,
        db: AsyncSession,
        actor_id: UUID | None = None,
    ) -> Organization:
        """Atomically swap owner: the previous owner becomes admin.

        The organization row is locked (``SELECT ... FOR UPDATE``) and the
        actor is re-verified as the CURRENT owner under that lock, so two
        concurrent transfers serialize: the second sees the new owner and is
        rejected instead of producing two owners. ``actor_id=None`` is a trusted
        system call.
        """
        org = await cls._get_active_for_update(organization_id, db)
        previous_owner_id = org.owner_id
        if actor_id is not None and actor_id != previous_owner_id:
            raise TenancyForbiddenError("Only the current owner can transfer ownership")
        if new_owner_id == previous_owner_id:
            raise TenancyConflictError("User is already the owner")

        result = await db.execute(
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id.in_([previous_owner_id, new_owner_id]),
            )
            # user_id order: the order every member-row locker uses (no deadlock
            # with a concurrent role change / removal of the same pair).
            .order_by(OrganizationMember.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        members = {m.user_id: m for m in result.scalars().all()}
        new_owner = members.get(new_owner_id)
        if new_owner is None:
            raise TenancyConflictError(
                "New owner must already be a member of the organization"
            )
        previous_owner = members.get(previous_owner_id)

        if previous_owner is not None:
            previous_owner.role = ROLE_ADMIN
        new_owner.role = ROLE_OWNER
        org.owner_id = new_owner_id
        await db.flush()
        await db.refresh(org)
        version = await MembershipService.next_version(org.id, new_owner_id, db)
        await events.publish_member(
            events.MemberRoleChanged(
                organization_id=org.id,
                user_id=new_owner_id,
                role=ROLE_OWNER,
                actor_id=actor_id,
                version=version,
            )
        )
        if previous_owner is not None:
            version = await MembershipService.next_version(
                org.id, previous_owner_id, db
            )
            await events.publish_member(
                events.MemberRoleChanged(
                    organization_id=org.id,
                    user_id=previous_owner_id,
                    role=ROLE_ADMIN,
                    actor_id=actor_id,
                    version=version,
                )
            )
        # Both roles changed: revoke both members' outstanding access tokens,
        # immediately (the transferring owner included — they re-authenticate
        # and continue as admin).
        for changed_id in (new_owner_id, previous_owner_id):
            await revoke_user_tokens_on_commit(
                changed_id,
                reason="tenancy.organization.ownership_transferred",
                require_immediate=True,
            )
        record_audit(
            "tenancy.organization.ownership_transferred",
            actor_id=actor_id,
            resource_type="organization",
            resource_id=str(org.id),
            data={
                "from_user_id": str(previous_owner_id),
                "to_user_id": str(new_owner_id),
            },
            required=True,
        )
        return org
