"""Identity's contracts.

``identity.contact.v1`` — how to reach a user (email, name), read at send
time by any module that writes to users; events carry no PII.

``identity.security.v1`` — operator security commands (incident response, and
granting the first platform administrator).

The contract behind ``sandloom security revoke-sessions`` and ``sandloom security
force-password-reset`` (:mod:`sandloom.core.cli.security`): the kernel CLI pages
the target users (all users here, or a tenant's members through
``tenancy.membership.v1``) and sends them in batches. Every command:

* runs in identity's own UoW and is deduplicated durably on
  ``operation_key`` (a retried batch never repeats its effect);
* revokes every refresh token of the batch in the database and every access
  token through the kernel mark (``require_immediate``: written before commit
  or the batch is refused with 503 and rolls back);
* writes one ``required=True`` audit row per affected user
  (``actor_type="operator"``, ``data`` = operator, reason, operation key).

Not part of any user-facing API: a caller needs the contract locally (the CLI
runs in identity's process) or an ``RPC_ALLOWED_CALLS`` grant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.module_api import contract_session
from sandloom.sdk import command, contract, record_audit
from sandloom.sdk.effects import sensitive_effect
from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.models.role import Role
from sandloom_identity.models.user import User
from sandloom_identity.models.user_role import UserRole
from sandloom_identity.schemas.contact import UserContact
from sandloom_identity.services.role_service import ADMIN_ROLE_CODE
from sandloom_identity.sessions import revoke_sessions

SECURITY_SERVICE_V1 = "identity.security.v1"
CONTACT_SERVICE_V1 = "identity.contact.v1"
#: Largest batch one command accepts (one transaction).
MAX_BATCH = 1000


def _check_batch(user_ids: list[UUID], reason: str, operator: str) -> None:
    if len(user_ids) > MAX_BATCH:
        raise HTTPException(422, f"at most {MAX_BATCH} users per call")
    if not reason.strip() or not operator.strip():
        raise HTTPException(422, "reason and operator are required")


async def _existing(user_ids: list[UUID]) -> list[UUID]:
    if not user_ids:
        return []
    db = contract_session()
    rows = await db.execute(
        select(User.id).where(User.id.in_(user_ids)).order_by(User.id)
    )
    return list(rows.scalars())


@sensitive_effect("admin")
async def _end_sessions(
    users: list[UUID], *, action: str, reason: str, operator: str, key: str
) -> None:
    db = contract_session()
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id.in_(users), RefreshToken.is_revoked.is_(False))
        .values(is_revoked=True, revoked_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    for user_id in users:
        record_audit(
            action,
            actor_id=None,
            actor_type="operator",
            resource_type="user",
            resource_id=str(user_id),
            data={"reason": reason, "operator": operator, "operation_key": key},
            required=True,
        )
    # Last fallible step: each mark is written before commit (or 503 + rollback).
    for user_id in users:
        await revoke_sessions(user_id, reason=f"operator ({operator}): {reason}")


async def _admin_role(db: AsyncSession) -> Role:
    role = await db.scalar(select(Role).where(Role.code == ADMIN_ROLE_CODE))
    if role is None:
        role = Role(code=ADMIN_ROLE_CODE, name="Platform administrator", is_system=True)
        db.add(role)
        await db.flush()
    return role


@sensitive_effect("admin")
async def _grant_admin(user: User, *, reason: str, operator: str, key: str) -> None:
    db = contract_session()
    role = await _admin_role(db)
    assigned = await db.scalar(
        select(UserRole.id).where(
            UserRole.user_id == user.id, UserRole.role_id == role.id
        )
    )
    if assigned is None:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    if not user.is_verified:
        # The operator vouches for the address, so no mail server is needed.
        user.is_verified = True
        user.email_verified_at = datetime.now(UTC)
    await db.flush()
    record_audit(
        "identity.user.platform_admin_granted",
        actor_id=None,
        actor_type="operator",
        resource_type="user",
        resource_id=str(user.id),
        data={"operator": operator, "reason": reason, "operation_key": key},
        required=True,
    )


@contract(SECURITY_SERVICE_V1)
class SecurityOperationsContract:
    """Operator commands: sessions, password resets, the first platform admin."""

    async def list_user_ids(self, after: UUID | None, limit: int) -> list[UUID]:
        """User ids in id order, strictly after ``after`` (keyset paging)."""
        db = contract_session()
        stmt = select(User.id).order_by(User.id).limit(max(1, min(limit, MAX_BATCH)))
        if after is not None:
            stmt = stmt.where(User.id > after)
        return list((await db.execute(stmt)).scalars())

    async def find_user_id(self, email: str) -> UUID | None:
        """The id of the user with this address, or ``None``."""
        db = contract_session()
        found: UUID | None = await db.scalar(select(User.id).where(User.email == email))
        return found

    @command(idempotency_key="operation_key")
    async def appoint_platform_admin(
        self, user_id: UUID, reason: str, operator: str, operation_key: str
    ) -> bool:
        """Make the user a platform administrator and mark their address verified;
        returns whether the user exists."""
        _check_batch([user_id], reason, operator)
        db = contract_session()
        user = await db.get(User, user_id)
        if user is None:
            return False
        await _grant_admin(user, reason=reason, operator=operator, key=operation_key)
        return True

    @command(idempotency_key="operation_key")
    async def revoke_sessions(
        self, user_ids: list[UUID], reason: str, operator: str, operation_key: str
    ) -> int:
        """End every session of the users; returns how many exist."""
        _check_batch(user_ids, reason, operator)
        users = await _existing(user_ids)
        if users:
            await _end_sessions(
                users,
                action="identity.user.sessions_revoked",
                reason=reason,
                operator=operator,
                key=operation_key,
            )
        return len(users)

    @command(idempotency_key="operation_key")
    async def require_password_reset(
        self, user_ids: list[UUID], reason: str, operator: str, operation_key: str
    ) -> int:
        """Refuse password sign-in until a reset, and end every session."""
        _check_batch(user_ids, reason, operator)
        users = await _existing(user_ids)
        if users:
            db = contract_session()
            await db.execute(
                update(User)
                .where(User.id.in_(users))
                .values(password_reset_required=True, password_reset_nonce=None)
                .execution_options(synchronize_session=False)
            )
            await _end_sessions(
                users,
                action="identity.user.password_reset_required",
                reason=reason,
                operator=operator,
                key=operation_key,
            )
        return len(users)


def _contact(user: User) -> UserContact:
    return UserContact(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        email_verified=bool(user.is_verified),
        is_active=bool(user.is_active),
    )


@contract(CONTACT_SERVICE_V1)
class ContactContract:
    """How to reach a user: email and display name, read at send time.

    Events carry no PII: a module that mails a user calls ``get_contact`` when
    it sends, and never copies the address into its own tables. Reads only,
    the same in a monolith and over RPC. In tests,
    ``fake_contract(IDENTITY_CONTACT_V1, Fake())``.
    """

    async def get_contact(self, user_id: UUID) -> UserContact | None:
        """The user's contact details (email, full name, whether the email is
        verified, whether the account is active); ``None`` for an unknown user."""
        db = contract_session()
        user = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        return _contact(user) if user is not None else None

    async def get_contacts(self, user_ids: list[UUID]) -> list[UserContact]:
        """Contact details of the known users among ``user_ids``, in id order
        (unknown ids are left out). At most 1000 ids per call (422 above)."""
        if len(user_ids) > MAX_BATCH:
            raise HTTPException(422, f"at most {MAX_BATCH} users per call")
        if not user_ids:
            return []
        db = contract_session()
        rows = await db.execute(
            select(User).where(User.id.in_(user_ids)).order_by(User.id)
        )
        return [_contact(user) for user in rows.scalars()]


__all__ = [
    "CONTACT_SERVICE_V1",
    "MAX_BATCH",
    "SECURITY_SERVICE_V1",
    "ContactContract",
    "SecurityOperationsContract",
]
