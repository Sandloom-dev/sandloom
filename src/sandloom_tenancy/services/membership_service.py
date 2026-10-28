"""Membership queries and mutations.

Only the READ side is exposed to other modules — through
:class:`~sandloom_tenancy.contracts.MembershipReadContract`, registered as
``tenancy.membership.v1``. The write side (add/change/remove) is internal to
the tenancy module and enforces the role hierarchy itself:

* nobody gains the owner role except via ownership transfer;
* admins manage *members*; only the owner may change, remove or invite an admin;
* any member may leave (remove themselves); the owner must transfer first.

Every read joins the organization and ignores inactive (soft-deleted)
organizations, so a deleted org stops granting any role.
"""

from uuid import UUID

from sqlalchemy import bindparam, literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    ORG_ROLES,
    ROLE_ADMIN,
    ROLE_OWNER,
    Organization,
    OrganizationMember,
    OrganizationMemberVersion,
    role_at_least,
)

#: The ``tenancy.membership.v1`` reads as PREBUILT statements (values bound
#: per call). In the contract's own Unit of Work, building the ORM
#: ``select().join().where()`` and its cache key on every call cost more than
#: executing it: -0.6 M instructions per call (cachegrind, ladder rung C2).
#: Same SQL. (Request-session reads showed no gain, so they stay inline.)
_ROLE_IN_ACTIVE_ORG = (
    select(OrganizationMember.role)
    .join(Organization, Organization.id == OrganizationMember.organization_id)
    .where(
        OrganizationMember.organization_id == bindparam("organization_id"),
        OrganizationMember.user_id == bindparam("user_id"),
        Organization.is_active.is_(True),
    )
)
_ACTIVE_ORG_IDS_OF_USER = (
    select(OrganizationMember.organization_id)
    .join(
        Organization,
        Organization.id == OrganizationMember.organization_id,
    )
    .where(
        OrganizationMember.user_id == bindparam("user_id"),
        Organization.is_active.is_(True),
    )
)


class MembershipService:
    """Operations on organization members."""

    # --- Read side (exposed via MembershipReadContract) ---------------------

    @classmethod
    async def get_role(
        cls, organization_id: UUID, user_id: UUID, db: AsyncSession
    ) -> str | None:
        """User's role in an ACTIVE organization, or None (not a member/inactive)."""
        result = await db.execute(
            _ROLE_IN_ACTIVE_ORG,
            {"organization_id": organization_id, "user_id": user_id},
        )
        return result.scalar_one_or_none()

    @classmethod
    async def is_member(
        cls, organization_id: UUID, user_id: UUID, db: AsyncSession
    ) -> bool:
        return await cls.get_role(organization_id, user_id, db) is not None

    @classmethod
    async def has_role_at_least(
        cls, organization_id: UUID, user_id: UUID, minimum: str, db: AsyncSession
    ) -> bool:
        role = await cls.get_role(organization_id, user_id, db)
        return role is not None and role_at_least(role, minimum)

    @classmethod
    async def list_user_organization_ids(
        cls, user_id: UUID, db: AsyncSession, *, limit: int | None = None
    ) -> list[UUID]:
        """All active organization ids the user belongs to (any role)."""
        query = _ACTIVE_ORG_IDS_OF_USER
        if limit is not None:
            query = query.limit(limit)
        result = await db.execute(query, {"user_id": user_id})
        return list(result.scalars().all())

    @classmethod
    async def list_members(
        cls,
        organization_id: UUID,
        db: AsyncSession,
        *,
        limit: int | None = None,
        after: UUID | None = None,
    ) -> list[OrganizationMember]:
        """Members in join order, optionally one keyset page.

        ``after`` is the ``id`` of the last member of the previous page (an id
        that is not a member of this organization yields an empty page);
        without ``limit`` every member is returned (the original behaviour).
        """
        query = select(OrganizationMember).where(
            OrganizationMember.organization_id == organization_id
        )
        if after is not None:
            cursor_created = (
                select(OrganizationMember.created_at)
                .where(
                    OrganizationMember.id == after,
                    OrganizationMember.organization_id == organization_id,
                )
                .scalar_subquery()
            )
            query = query.where(
                tuple_(OrganizationMember.created_at, OrganizationMember.id)
                > tuple_(cursor_created, literal(after))
            )
        query = query.order_by(OrganizationMember.created_at, OrganizationMember.id)
        if limit is not None:
            query = query.limit(limit)
        result = await db.execute(query)
        return list(result.scalars().all())

    # --- Write side (tenancy-internal) --------------------------------------

    @classmethod
    async def next_version(
        cls, organization_id: UUID, user_id: UUID, db: AsyncSession
    ) -> int:
        """Bump and return the membership's event ``version`` (this UoW).

        One atomic upsert on ``tenancy_member_versions``: the row lock it takes
        serializes concurrent lifecycle changes of the pair until commit, so
        versions increase in commit order — and the sequence survives a
        removal, continuing on a re-add. Call it once per published member
        event, after the change itself succeeded.
        """
        stmt = (
            pg_insert(OrganizationMemberVersion)
            .values(organization_id=organization_id, user_id=user_id, version=1)
            .on_conflict_do_update(
                index_elements=[
                    OrganizationMemberVersion.organization_id,
                    OrganizationMemberVersion.user_id,
                ],
                set_={"version": OrganizationMemberVersion.version + 1},
            )
            .returning(OrganizationMemberVersion.version)
        )
        return int((await db.execute(stmt)).scalar_one())

    @classmethod
    async def add_member(
        cls,
        organization_id: UUID,
        user_id: UUID,
        role: str,
        db: AsyncSession,
        actor_id: UUID | None = None,
    ) -> OrganizationMember:
        """Insert a membership row. Never grants the owner role."""
        if role not in ORG_ROLES:
            raise ValueError(f"Unknown organization role {role!r}")
        if role == ROLE_OWNER:
            raise TenancyConflictError(
                "The owner role is assigned only by creating the organization "
                "or transferring ownership"
            )
        member = OrganizationMember(
            organization_id=organization_id, user_id=user_id, role=role
        )
        try:
            async with db.begin_nested():
                db.add(member)
                await db.flush()
        except IntegrityError as exc:
            raise TenancyConflictError(
                "User is already a member of this organization"
            ) from exc
        await db.refresh(member)
        version = await cls.next_version(organization_id, user_id, db)
        await events.publish_member(
            events.MemberAdded(
                organization_id=organization_id,
                user_id=user_id,
                role=role,
                actor_id=actor_id,
                version=version,
            )
        )
        return member

    @classmethod
    @sensitive_effect("admin")
    async def change_role(
        cls,
        organization_id: UUID,
        user_id: UUID,
        new_role: str,
        db: AsyncSession,
        actor_id: UUID | None = None,
    ) -> OrganizationMember:
        """Change a member's role. ``actor_id=None`` means a trusted system call."""
        if new_role not in ORG_ROLES:
            raise ValueError(f"Unknown organization role {new_role!r}")
        member, actor_role = await cls._lock_target_and_actor(
            organization_id, user_id, actor_id, db
        )
        if member.role == ROLE_OWNER:
            raise TenancyConflictError(
                "Owner role cannot be changed; transfer ownership instead"
            )
        if new_role == ROLE_OWNER:
            raise TenancyConflictError(
                "Use ownership transfer to assign the owner role"
            )
        if actor_id is not None:
            actor_role = cls._require_actor(actor_role)
            if not role_at_least(actor_role, ROLE_ADMIN):
                raise TenancyForbiddenError("Insufficient organization role")
            if member.role == ROLE_ADMIN and actor_role != ROLE_OWNER:
                raise TenancyForbiddenError("Only the owner can change an admin's role")
            # Same rule as ``InvitationService.create``: only the owner mints
            # admins, or an admin could promote accomplices it cannot demote.
            if new_role == ROLE_ADMIN and actor_role != ROLE_OWNER:
                raise TenancyForbiddenError("Only the owner can grant the admin role")
        if member.role == new_role:
            return member  # no-op: no event, no audit

        old_role = member.role
        member.role = new_role
        # No refresh: the row was loaded whole by the FOR UPDATE above and the
        # UPDATE sets only ``role`` (no server-side column changes), so a
        # re-SELECT would read back exactly what this object already holds.
        await db.flush()
        version = await cls.next_version(organization_id, user_id, db)
        await events.publish_member(
            events.MemberRoleChanged(
                organization_id=organization_id,
                user_id=user_id,
                role=new_role,
                actor_id=actor_id,
                version=version,
            )
        )
        # Revoke the member's outstanding access tokens (owner decision): roles
        # are read live today, but a token minted under the old role must not
        # outlive the change once roles are cached / baked into claims.
        # Immediate: the mark is written before commit, or the change is
        # refused (503). UX: the member re-authenticates (refresh or log in).
        await revoke_user_tokens_on_commit(
            user_id, reason="tenancy.member.role_changed", require_immediate=True
        )
        record_audit(
            "tenancy.member.role_changed",
            actor_id=actor_id,
            resource_type="organization_member",
            resource_id=str(member.id),
            data={
                "organization_id": str(organization_id),
                "user_id": str(user_id),
                "from": old_role,
                "to": new_role,
            },
            required=True,
        )
        return member

    @classmethod
    @sensitive_effect("admin")
    async def remove_member(
        cls,
        organization_id: UUID,
        user_id: UUID,
        db: AsyncSession,
        actor_id: UUID | None = None,
    ) -> None:
        """Remove a member, or let a member leave (``actor_id == user_id``).

        ``actor_id=None`` means a trusted system call (no actor rank checks).
        """
        member, actor_role = await cls._lock_target_and_actor(
            organization_id, user_id, actor_id, db
        )
        leaving = actor_id is not None and actor_id == user_id
        if member.role == ROLE_OWNER:
            raise TenancyConflictError(
                "The owner cannot leave or be removed; transfer ownership first"
                if leaving
                else "The owner cannot be removed from the organization"
            )
        if actor_id is not None and not leaving:
            actor_role = cls._require_actor(actor_role)
            if not role_at_least(actor_role, ROLE_ADMIN):
                raise TenancyForbiddenError("Insufficient organization role")
            if member.role == ROLE_ADMIN and actor_role != ROLE_OWNER:
                raise TenancyForbiddenError("Only the owner can remove an admin")

        member_pk = str(member.id)
        removed_role = member.role
        await db.delete(member)
        await db.flush()
        version = await cls.next_version(organization_id, user_id, db)
        await events.publish_member(
            events.MemberRemoved(
                organization_id=organization_id,
                user_id=user_id,
                actor_id=actor_id,
                version=version,
            )
        )
        # The removed user's access token still lists this org in tenant_ids:
        # revoke it so TenantScope / require(...) stop honouring the claim.
        # Immediate: the mark is written before commit (or the removal is
        # refused, 503), so a committed removal never leaves a usable token.
        await revoke_user_tokens_on_commit(
            user_id,
            reason="tenancy.member.left" if leaving else "tenancy.member.removed",
            require_immediate=True,
        )
        record_audit(
            "tenancy.member.left" if leaving else "tenancy.member.removed",
            actor_id=actor_id,
            resource_type="organization_member",
            resource_id=member_pk,
            data={
                "organization_id": str(organization_id),
                "user_id": str(user_id),
                "role": removed_role,
            },
            required=True,
        )

    # --- helpers -------------------------------------------------------------

    @classmethod
    async def lock_actor_role(
        cls,
        organization_id: UUID,
        actor_id: UUID,
        db: AsyncSession,
        *,
        minimum: str = ROLE_ADMIN,
    ) -> str:
        """The actor's role, read ``FOR SHARE`` in THIS transaction (ADR-0003 G3).

        Class-A writes authorize on it: the shared row lock holds until the
        write commits, so a concurrent demotion / removal of the actor (which
        takes the row ``FOR UPDATE`` / deletes it) either committed before —
        the locking read then returns the NEW role (READ COMMITTED re-reads
        the locked row) — or waits for this write to commit. 404 for a
        non-member / inactive organization, 403 below ``minimum``.
        """
        row = await cls._lock_member_row(organization_id, actor_id, db, share=True)
        role = cls._require_actor(None if row is None else row.role)
        if not role_at_least(role, minimum):
            raise TenancyForbiddenError("Insufficient organization role")
        return role

    @staticmethod
    def _require_actor(role: str | None) -> str:
        """The actor's role; 404 when the actor is not a member (hides the org)."""
        if role is None:
            raise TenancyNotFoundError("Organization not found")
        return role

    @classmethod
    async def _lock_target_and_actor(
        cls,
        organization_id: UUID,
        user_id: UUID,
        actor_id: UUID | None,
        db: AsyncSession,
    ) -> tuple[OrganizationMember, str | None]:
        """Lock the target row ``FOR UPDATE`` and the actor's ``FOR SHARE``.

        Both in ONE transaction, in ``user_id`` order (the same order
        ``transfer_ownership`` locks member rows in), so two actors acting on
        each other wait instead of deadlocking. Returns the target (404 when it
        is not a member of an active organization) and the actor's role
        (``None`` when the actor is not a member; ``actor_id=None`` — a trusted
        system call — or the actor leaving locks only the target).
        """
        target: OrganizationMember | None = None
        actor_role: str | None = None
        rows = {user_id} if actor_id is None else {user_id, actor_id}
        for uid in sorted(rows):
            row = await cls._lock_member_row(
                organization_id, uid, db, share=uid != user_id
            )
            if uid == user_id:
                target = row
            elif row is not None:
                actor_role = row.role
        if target is None:
            raise TenancyNotFoundError("Member not found")
        if actor_id == user_id:
            actor_role = target.role
        return target, actor_role

    @classmethod
    async def _lock_member_row(
        cls, organization_id: UUID, user_id: UUID, db: AsyncSession, *, share: bool
    ) -> OrganizationMember | None:
        """The membership row (in an ACTIVE organization), row-locked."""
        stmt = (
            select(OrganizationMember)
            .join(Organization, Organization.id == OrganizationMember.organization_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
                Organization.is_active.is_(True),
            )
            .with_for_update(of=OrganizationMember, read=share)
            .execution_options(populate_existing=True)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
