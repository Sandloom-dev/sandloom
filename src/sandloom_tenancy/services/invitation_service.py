"""Invitation lifecycle: create, list, revoke, accept, notify.

Security model:

* tokens are bearer secrets — only ``sha256(token)`` is stored; the raw token
  is returned once to the inviter and e-mailed to the invitee;
* acceptance is single-use under concurrency: the invitation is claimed with
  one atomic ``UPDATE ... WHERE <still pending> RETURNING`` (the row lock makes
  a concurrent second claim see it as used);
* acceptance is bound to the invited address: the accepting user's e-mail
  (from the access token) must match the invitation, case-insensitively;
* invitations into an inactive (deleted) organization cannot be accepted.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import record_audit
from sandloom.sdk.infra import Notification, notifier, settings
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
    OrganizationInvitation,
    OrganizationMember,
)
from sandloom_tenancy.models.invitation import (
    hash_invitation_token,
    new_invitation_token,
)
from sandloom_tenancy.schemas import InvitationCreateIn
from sandloom_tenancy.services.membership_service import MembershipService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CreatedInvitation:
    """A new invitation plus its raw token (available only at creation)."""

    invitation: OrganizationInvitation
    token: str


def _same_email(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


class InvitationService:
    """Email-token based invitations into an organization."""

    @classmethod
    async def create(
        cls,
        organization_id: UUID,
        data: InvitationCreateIn,
        invited_by: UUID,
        db: AsyncSession,
    ) -> CreatedInvitation:
        """Create an invitation (the endpoint already required admin+).

        Only the OWNER may invite with ``role="admin"`` — the same rule as
        changing an admin's role (``MembershipService.change_role``): an admin
        must not be able to mint further admins by invitation.
        """
        # The inviter's role is read FOR SHARE in this transaction (ADR-0003
        # G3): a concurrent demotion / removal either commits first (and is
        # seen here) or waits for this invitation to commit.
        inviter_role = await MembershipService.lock_actor_role(
            organization_id, invited_by, db
        )
        if data.role == ROLE_ADMIN and inviter_role != ROLE_OWNER:
            raise TenancyForbiddenError("Only the owner can invite an admin")
        # Expired-but-open invitations can never be accepted; supersede them so
        # the "one open invitation per org+email" unique index admits a new one.
        await db.execute(
            update(OrganizationInvitation)
            .where(
                OrganizationInvitation.organization_id == organization_id,
                OrganizationInvitation.email == data.email,
                OrganizationInvitation.accepted_at.is_(None),
                OrganizationInvitation.revoked_at.is_(None),
                OrganizationInvitation.expires_at <= func.now(),
            )
            .values(revoked_at=func.now())
            .execution_options(synchronize_session=False)
        )

        token = new_invitation_token()
        invitation = OrganizationInvitation(
            organization_id=organization_id,
            email=data.email,
            role=data.role,
            invited_by=invited_by,
            token_hash=hash_invitation_token(token),
        )
        try:
            # The partial unique index (organization_id, email) WHERE open is
            # the arbiter — no check-then-insert race.
            async with db.begin_nested():
                db.add(invitation)
                await db.flush()
        except IntegrityError as exc:
            raise TenancyConflictError(
                "A pending invitation for this email already exists"
            ) from exc
        await db.refresh(invitation)
        await events.InvitationCreated(
            organization_id=organization_id,
            invitation_id=invitation.id,
            role=invitation.role,
            actor_id=invited_by,
        ).publish()
        record_audit(
            "tenancy.invitation.created",
            actor_id=invited_by,
            resource_type="organization_invitation",
            resource_id=str(invitation.id),
            data={"organization_id": str(organization_id), "role": invitation.role},
            required=True,
        )
        return CreatedInvitation(invitation=invitation, token=token)

    @classmethod
    async def list_pending(
        cls, organization_id: UUID, db: AsyncSession, *, limit: int = 100, skip: int = 0
    ) -> list[OrganizationInvitation]:
        result = await db.execute(
            select(OrganizationInvitation)
            .where(
                OrganizationInvitation.organization_id == organization_id,
                OrganizationInvitation.accepted_at.is_(None),
                OrganizationInvitation.revoked_at.is_(None),
                OrganizationInvitation.expires_at > func.now(),
            )
            .order_by(OrganizationInvitation.created_at, OrganizationInvitation.id)
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all())

    @classmethod
    async def revoke(
        cls,
        organization_id: UUID,
        invitation_id: UUID,
        db: AsyncSession,
        actor_id: UUID | None = None,
    ) -> None:
        """Revoke a pending invitation; ``actor_id`` must be admin+ at commit
        (its membership row is read ``FOR SHARE``, ADR-0003 G3).
        ``actor_id=None`` is a trusted system call."""
        if actor_id is not None:
            await MembershipService.lock_actor_role(organization_id, actor_id, db)
        result = await db.execute(
            update(OrganizationInvitation)
            .where(
                OrganizationInvitation.id == invitation_id,
                OrganizationInvitation.organization_id == organization_id,
                OrganizationInvitation.accepted_at.is_(None),
                OrganizationInvitation.revoked_at.is_(None),
                OrganizationInvitation.expires_at > func.now(),
            )
            .values(revoked_at=func.now())
            .returning(OrganizationInvitation.id)
            .execution_options(synchronize_session=False)
        )
        if result.scalar_one_or_none() is None:
            raise TenancyNotFoundError("Pending invitation not found")
        await events.InvitationRevoked(
            organization_id=organization_id,
            invitation_id=invitation_id,
            actor_id=actor_id,
        ).publish()
        record_audit(
            "tenancy.invitation.revoked",
            actor_id=actor_id,
            resource_type="organization_invitation",
            resource_id=str(invitation_id),
            data={"organization_id": str(organization_id)},
            required=True,
        )

    @classmethod
    async def accept(
        cls,
        token: str,
        user_id: UUID,
        db: AsyncSession,
        *,
        email: str | None,
        email_verified: bool | None = None,
    ) -> OrganizationMember:
        """Accept an invitation by token on behalf of the authenticated user.

        ``email`` is the accepting user's address (the access token's ``email``
        claim); it must match the invited address, and it must be VERIFIED:
        ``email_verified`` has to be exactly ``True`` (the token's
        ``email_verified`` claim) — a missing claim (``None``) is rejected just
        like ``False``, so an unverified or legacy token cannot claim an
        invitation addressed to an e-mail its holder has not proven to own.
        Nothing is consumed unless the whole acceptance succeeds: the claim,
        the e-mail check and the membership insert share one SAVEPOINT.
        """
        if not email:
            raise TenancyForbiddenError(
                "Your account e-mail is required to accept an invitation"
            )
        if email_verified is not True:
            raise TenancyForbiddenError(
                "Verify your e-mail address before accepting an invitation"
            )

        active_orgs = select(Organization.id).where(Organization.is_active.is_(True))
        claim = (
            update(OrganizationInvitation)
            .where(
                OrganizationInvitation.token_hash == hash_invitation_token(token),
                OrganizationInvitation.accepted_at.is_(None),
                OrganizationInvitation.revoked_at.is_(None),
                OrganizationInvitation.expires_at > func.now(),
                OrganizationInvitation.organization_id.in_(active_orgs),
            )
            .values(accepted_at=func.now())
            .returning(
                OrganizationInvitation.id,
                OrganizationInvitation.organization_id,
                OrganizationInvitation.email,
                OrganizationInvitation.role,
            )
            .execution_options(synchronize_session=False)
        )
        try:
            async with db.begin_nested():
                row = (await db.execute(claim)).one_or_none()
                if row is None:
                    raise TenancyNotFoundError(
                        "Invitation is invalid, expired, or already used"
                    )
                invitation_id, organization_id, invited_email, role = row
                if not _same_email(invited_email, email):
                    raise TenancyForbiddenError(
                        "This invitation was issued to a different e-mail address"
                    )
                member = OrganizationMember(
                    organization_id=organization_id, user_id=user_id, role=role
                )
                db.add(member)
                await db.flush()
        except IntegrityError as exc:
            raise TenancyConflictError(
                "You are already a member of this organization"
            ) from exc

        await db.refresh(member)
        await events.InvitationAccepted(
            organization_id=organization_id,
            invitation_id=invitation_id,
            role=role,
            actor_id=user_id,
        ).publish()
        # Consumers / projections of the membership learn about the new member
        # from the member channel — the invitation event carries no user_id.
        version = await MembershipService.next_version(organization_id, user_id, db)
        await events.publish_member(
            events.MemberAdded(
                organization_id=organization_id,
                user_id=user_id,
                role=role,
                actor_id=user_id,
                version=version,
            )
        )
        record_audit(
            "tenancy.invitation.accepted",
            actor_id=user_id,
            resource_type="organization_invitation",
            resource_id=str(invitation_id),
            data={"organization_id": str(organization_id), "role": role},
            required=True,
        )
        return member

    @classmethod
    async def send_invitation_email(
        cls,
        *,
        email: str,
        token: str,
        organization_id: UUID,
        organization_name: str,
        invitation_id: UUID,
    ) -> None:
        """Best-effort invitation e-mail via the notifier capability.

        Run AFTER the creating transaction committed (the API schedules it as a
        background task), so a rolled-back invitation never gets an e-mail. The
        token travels only in the body (as a URL fragment, kept out of server
        logs / Referer); it is never logged here.
        """
        link = f"{settings.frontend_url.rstrip('/')}/invitations/accept#token={token}"
        try:
            await notifier().send(
                Notification(
                    channel="email",
                    recipient=email,
                    subject=f"You're invited to join {organization_name}",
                    body=(
                        f"You have been invited to join {organization_name}.\n\n"
                        f"Accept the invitation: {link}\n\n"
                        "If you did not expect this invitation, ignore this e-mail."
                    ),
                    data={
                        "organization_id": str(organization_id),
                        "invitation_id": str(invitation_id),
                    },
                    dedupe_key=f"tenancy.invitation:{invitation_id}",
                )
            )
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            # Only the error type: a driver message could echo the body (token).
            logger.warning(
                "tenancy: invitation e-mail delivery failed",
                extra={
                    "invitation_id": str(invitation_id),
                    "error_type": type(exc).__name__,
                },
            )
