"""Identity's contracts.

``identity.contact.v1`` — how to reach a user (email, name), read at send
time by any module that writes to users; events carry no PII.

``identity.security.v1`` — operator security commands (incident response, and
the first platform administrator: ``sandloom admin create`` / ``invite``).

The contract behind ``sandloom security revoke-sessions`` and ``sandloom security
force-password-reset`` (:mod:`sandloom.core.cli.security`), found as the
issuer's ``<issuer>.security.v1`` and confirmed by describe: the kernel CLI
pages the target users (all users through ``list_user_ids`` here, or a
tenant's members through the ``tenant_ids`` owner's
``tenancy.security.v1``), sends them in batches (``end_sessions``,
``require_password_reset``) and then revokes their access tokens itself.
Every command:

* runs in identity's own UoW and is deduplicated durably on
  ``operation_key`` (a retried batch never repeats its effect);
* revokes every refresh token of the batch in the database and every access
  token through the kernel mark (``require_immediate``: written before commit
  or the batch is refused with 503 and rolls back);
* writes one ``required=True`` audit row per affected user
  (``actor_type="operator"``, ``data`` = operator, reason, operation key).

``sandloom admin`` and ``sandloom security grant-admin`` ask two owners, both
identity here: the issuer side (``account_by_email``, ``accounts_by_id``,
the password and set-password-link methods) and the ``roles`` owner side
(``is_platform_admin``, ``platform_admin_ids``, ``appoint_platform_admin``).
``account_standing`` and ``list_platform_admins`` mix the two and stay one
release.

The first administrator: ``create_platform_admin`` (and
``take_over_platform_admin`` for an unverified account that holds the
address) leaves the account with no usable password and a pending
set-password link; ``set_password_token`` mints that link. The command takes
no password and returns no token, because a command's result is stored for
replay; the read that mints is never stored. ``set_password_with_token``
sets the password a terminal prompt read through identity's reset path: the
token and the password are ``undigested``, so its stored record (command log
or cache reservation) holds a digest over the operation key alone and a
boolean; nothing stored depends on either secret, not even as a hash.

Not part of any user-facing API: a caller needs the contract locally (the CLI
runs in identity's process) or an ``RPC_ALLOWED_CALLS`` grant.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import command, contract, db, record_audit
from sandloom.sdk.contracts import undigested  # sandloom: advanced secret command args
from sandloom.sdk.effects import sensitive_effect
from sandloom.sdk.security import is_user_blocked
from sandloom_identity.events import EventActor
from sandloom_identity.models.external_identity import ExternalIdentity
from sandloom_identity.models.refresh_token import RefreshToken
from sandloom_identity.models.role import Role
from sandloom_identity.models.twofa_backup_code import TwoFABackupCode
from sandloom_identity.models.twofa_secret import TwoFASecret
from sandloom_identity.models.user import User
from sandloom_identity.models.user_role import UserRole
from sandloom_identity.password_policy import password_policy_violation
from sandloom_identity.schemas.contact import UserContact
from sandloom_identity.schemas.operator import (
    AccountStanding,
    OperatorAccount,
    PlatformAdmin,
    SessionsEnded,
)
from sandloom_identity.services.auth_service import AuthService
from sandloom_identity.services.role_service import ADMIN_ROLE_CODE
from sandloom_identity.services.setup_state import platform_admin_exists
from sandloom_identity.sessions import revoke_sessions
from sandloom_identity.utils import email_token_utils

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
    session = db()
    rows = await session.execute(
        select(User.id).where(User.id.in_(user_ids)).order_by(User.id)
    )
    return list(rows.scalars())


@sensitive_effect("admin")
async def _end_sessions(
    users: list[UUID], *, action: str, reason: str, operator: str, key: str
) -> None:
    session = db()
    await session.execute(
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


_EMAIL = TypeAdapter(EmailStr)


async def _assign_admin_role(user: User) -> None:
    session = db()
    role = await _admin_role(session)
    assigned = await session.scalar(
        select(UserRole.id).where(
            UserRole.user_id == user.id, UserRole.role_id == role.id
        )
    )
    if assigned is None:
        session.add(UserRole(user_id=user.id, role_id=role.id))
    await session.flush()


def _operator_audit(
    action: str, user_id: UUID, *, reason: str, operator: str, key: str
) -> None:
    record_audit(
        action,
        actor_id=None,
        actor_type="operator",
        resource_type="user",
        resource_id=str(user_id),
        data={"operator": operator, "reason": reason, "operation_key": key},
        required=True,
    )


@sensitive_effect("admin")
async def _grant_admin(user: User, *, reason: str, operator: str, key: str) -> None:
    # An unverified address may belong to anyone who registered it first: the
    # operator takes it over instead (``take_over_platform_admin``), which
    # also invalidates that person's password.
    if not user.is_verified:
        raise HTTPException(
            409,
            "the account's email address is not verified: take it over with "
            "`sandloom admin create --email <address> --take-over`",
        )
    await _assign_admin_role(user)
    _operator_audit(
        "identity.user.platform_admin_granted",
        user.id,
        reason=reason,
        operator=operator,
        key=key,
    )


def _require_set_password(user: User) -> None:
    """No usable password until the link is used; one outstanding link."""
    user.password_reset_required = True
    user.password_reset_nonce = secrets.token_urlsafe(24)
    user.updated_at = datetime.now(UTC)


async def _is_platform_admin(db: AsyncSession, user_id: UUID) -> bool:
    found = await db.scalar(
        select(
            exists().where(
                UserRole.user_id == user_id,
                UserRole.role_id == Role.id,
                Role.code == ADMIN_ROLE_CODE,
            )
        )
    )
    return bool(found)


def _normalized_email(email: str) -> str:
    try:
        return str(_EMAIL.validate_python(email.strip()))
    except ValidationError as exc:
        raise HTTPException(422, f"not an email address: {email!r}") from exc


@sensitive_effect("admin")
async def _create_admin(
    email: str, full_name: str, *, reason: str, operator: str, key: str
) -> UUID:
    session = db()
    exists_already = await session.scalar(select(User.id).where(User.email == email))
    if exists_already is not None:
        raise HTTPException(409, f"an account with the address {email} already exists")
    now = datetime.now(UTC)
    user = User(
        email=email,
        full_name=full_name.strip() or email.split("@", 1)[0],
        hashed_password=None,
        is_active=True,
        # The operator vouches for the address, so no mail server is needed.
        is_verified=True,
        email_verified_at=now,
    )
    _require_set_password(user)
    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
    except IntegrityError as exc:
        # Another create for the same address committed first (unique email).
        raise HTTPException(
            409, f"an account with the address {email} already exists"
        ) from exc
    await _assign_admin_role(user)
    _operator_audit(
        "identity.user.platform_admin_created",
        user.id,
        reason=reason,
        operator=operator,
        key=key,
    )
    return user.id


@sensitive_effect("admin")
async def _take_over(user: User, *, reason: str, operator: str, key: str) -> None:
    session = db()
    # Whoever registered the address first loses every way in: the password,
    # a second factor, linked sign-in providers and every session. A
    # deactivated account is reactivated: the administrator it becomes must
    # be able to use the link (never an admin without a password and a link).
    user.hashed_password = None
    user.is_active = True
    _require_set_password(user)
    if not user.is_verified:
        user.is_verified = True
        user.email_verified_at = datetime.now(UTC)
    for model in (TwoFASecret, TwoFABackupCode, ExternalIdentity):
        await session.execute(delete(model).where(model.user_id == user.id))
    await _assign_admin_role(user)
    await _end_sessions(
        [user.id],
        action="identity.user.platform_admin_taken_over",
        reason=reason,
        operator=operator,
        key=key,
    )


def _operator_account(user: User) -> OperatorAccount:
    return OperatorAccount(
        user_id=user.id,
        email=user.email,
        email_verified=bool(user.is_verified),
        is_active=bool(user.is_active),
        password_set=user.hashed_password is not None,
        last_login_at=user.last_login_at,
    )


@contract(SECURITY_SERVICE_V1)
class SecurityOperationsContract:
    """Operator commands: sessions, password resets, the first platform admin."""

    async def list_user_ids(self, after: UUID | None, limit: int) -> list[UUID]:
        """User ids in id order, strictly after ``after`` (keyset paging)."""
        session = db()
        stmt = select(User.id).order_by(User.id).limit(max(1, min(limit, MAX_BATCH)))
        if after is not None:
            stmt = stmt.where(User.id > after)
        return list((await session.execute(stmt)).scalars())

    async def find_user_id(self, email: str) -> UUID | None:
        """The id of the user with this address, or ``None``."""
        session = db()
        found: UUID | None = await session.scalar(
            select(User.id).where(User.email == email)
        )
        return found

    async def account_by_email(self, email: str) -> OperatorAccount | None:
        """The account with this address as the token issuer knows it, or
        ``None`` (the issuer's side of ``sandloom admin`` / ``grant-admin``;
        the role is :meth:`is_platform_admin`'s answer)."""
        session = db()
        user = await session.scalar(select(User).where(User.email == email.strip()))
        return None if user is None else _operator_account(user)

    async def accounts_by_id(self, user_ids: list[UUID]) -> list[OperatorAccount]:
        """The accounts among ``user_ids`` (at most 1000), by address; unknown
        ids are left out (``sandloom admin list`` joins the roles owner's
        holders with this)."""
        if not user_ids:
            return []
        session = db()
        rows = await session.scalars(
            select(User).where(User.id.in_(user_ids[:MAX_BATCH])).order_by(User.email)
        )
        return [_operator_account(user) for user in rows]

    async def is_platform_admin(self, user_id: UUID) -> bool:
        """Whether the user holds the platform ``admin`` role (the roles
        owner's side of ``sandloom admin``)."""
        return await _is_platform_admin(db(), user_id)

    async def platform_admin_ids(self) -> list[UUID]:
        """The ids of every account holding the platform ``admin`` role, at
        most 1000 (``sandloom admin list`` asks the issuer for their address)."""
        session = db()
        rows = await session.scalars(
            select(UserRole.user_id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.code == ADMIN_ROLE_CODE)
            .order_by(UserRole.user_id)
            .limit(MAX_BATCH)
        )
        return list(dict.fromkeys(rows))

    async def account_standing(self, email: str) -> AccountStanding | None:
        """The account with this address as an operator sees it, or ``None``.

        Kept for one release: ``sandloom admin`` asks the issuer's
        :meth:`account_by_email` and the roles owner's
        :meth:`is_platform_admin`."""
        session = db()
        user = await session.scalar(select(User).where(User.email == email.strip()))
        if user is None:
            return None
        return AccountStanding(
            user_id=user.id,
            is_platform_admin=await _is_platform_admin(session, user.id),
            email_verified=bool(user.is_verified),
            is_active=bool(user.is_active),
        )

    async def list_platform_admins(self) -> list[PlatformAdmin]:
        """Every account holding the platform ``admin`` role, by address.

        Kept for one release: ``sandloom admin list`` joins the roles owner's
        :meth:`platform_admin_ids` with the issuer's :meth:`accounts_by_id`."""
        session = db()
        rows = await session.scalars(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.code == ADMIN_ROLE_CODE)
            .order_by(User.email)
            .limit(MAX_BATCH)
        )
        return [
            PlatformAdmin(
                email=user.email,
                password_set=user.hashed_password is not None,
                is_active=bool(user.is_active),
                last_login_at=user.last_login_at,
            )
            for user in rows.unique()
        ]

    @command(idempotency_key="operation_key")
    async def record_admin_listing(
        self, reason: str, operator: str, show_emails: bool, operation_key: str
    ) -> bool:
        """Record that an operator listed the platform administrators
        (``sandloom admin list`` in production): who, why, and whether the
        full addresses were shown. The addresses themselves stay out."""
        if not reason.strip() or not operator.strip():
            raise HTTPException(422, "reason and operator are required")
        record_audit(
            "identity.admin.listed",
            actor_id=None,
            actor_type="operator",
            resource_type="user",
            data={
                "operator": operator,
                "reason": reason,
                "show_emails": bool(show_emails),
                "operation_key": operation_key,
            },
            required=True,
        )
        return True

    async def platform_admin_exists(self) -> bool:
        """Whether any active account holds the platform ``admin`` role."""
        return await platform_admin_exists(db())

    async def password_problem(self, email: str, password: str) -> str | None:
        """Why identity's password policy refuses ``password`` for this
        address, or ``None`` (a read: nothing is stored or logged)."""
        return password_policy_violation(password, email.strip())

    async def set_password_token(self, user_id: UUID) -> str | None:
        """A 15-minute, single-use set-password token for an active platform
        administrator who has a set-password link pending; ``None`` otherwise.

        A read, never stored: it signs the account's current reset nonce.
        Identity's ``POST /auth/reset-password`` accepts it once.
        """
        session = db()
        user = await session.get(User, user_id)
        if (
            user is None
            or not user.is_active
            or not user.password_reset_required
            or not user.password_reset_nonce
            or not await _is_platform_admin(session, user.id)
        ):
            return None
        return email_token_utils.generate_set_password_token(
            user.id,
            nonce=user.password_reset_nonce,
            hashed_password=user.hashed_password,
        )

    @command(idempotency_key="operation_key")
    async def create_platform_admin(
        self,
        email: str,
        full_name: str,
        reason: str,
        operator: str,
        operation_key: str,
    ) -> UUID:
        """Create a platform administrator with a verified address and no
        usable password (a set-password link is pending); 409 when the address
        already has an account. Takes no password and returns no token."""
        _check_batch([], reason, operator)
        address = _normalized_email(email)
        return await _create_admin(
            address, full_name, reason=reason, operator=operator, key=operation_key
        )

    @command(idempotency_key="operation_key")
    async def take_over_platform_admin(
        self, user_id: UUID, reason: str, operator: str, operation_key: str
    ) -> bool:
        """Make an existing account a platform administrator and take it from
        whoever holds it: the password, second factor, linked providers and
        sessions end, the address counts as verified and a set-password link
        is pending; a deactivated account is reactivated. 409 for a blocked
        account (nothing changes). Returns whether the user exists."""
        _check_batch([user_id], reason, operator)
        session = db()
        user = await session.get(User, user_id)
        if user is None:
            return False
        if await is_user_blocked(user.id):
            raise HTTPException(
                409, "the account is blocked: unblock it first, then take it over"
            )
        await _take_over(user, reason=reason, operator=operator, key=operation_key)
        return True

    @command(idempotency_key="operation_key")
    async def reissue_set_password(
        self, user_id: UUID, reason: str, operator: str, operation_key: str
    ) -> bool:
        """A new set-password link for a platform administrator: the older
        link and every session end. 409 for an account that is not an active,
        unblocked administrator; returns whether the user exists."""
        _check_batch([user_id], reason, operator)
        session = db()
        user = await session.get(User, user_id)
        if user is None:
            return False
        if not await _is_platform_admin(session, user.id):
            raise HTTPException(409, "the account is not a platform administrator")
        if not user.is_active or await is_user_blocked(user.id):
            raise HTTPException(
                409, "the account is deactivated or blocked: unblock it first"
            )
        _require_set_password(user)
        await session.flush()
        await _end_sessions(
            [user.id],
            action="identity.user.set_password_link_issued",
            reason=reason,
            operator=operator,
            key=operation_key,
        )
        return True

    @command(idempotency_key="operation_key")
    @undigested("token", "new_password")
    async def set_password_with_token(
        self, token: str, new_password: str, operation_key: str
    ) -> bool:
        """Set a password through identity's reset path (the terminal prompt
        of ``sandloom admin create``). 400 for a used or expired token, 422 for
        a password the policy refuses. Neither secret is in the stored record:
        it holds a digest over ``operation_key`` alone and ``True`` (use a
        fresh key per attempt)."""
        session = db()
        await AuthService.reset_password(
            token, new_password, session, actor=EventActor()
        )
        return True

    @command(idempotency_key="operation_key")
    async def appoint_platform_admin(
        self, user_id: UUID, reason: str, operator: str, operation_key: str
    ) -> bool:
        """Make the user a platform administrator; returns whether the user
        exists. 409 for an unverified address (``take_over_platform_admin``)."""
        _check_batch([user_id], reason, operator)
        session = db()
        user = await session.get(User, user_id)
        if user is None:
            return False
        await _grant_admin(user, reason=reason, operator=operator, key=operation_key)
        return True

    @command(idempotency_key="operation_key")
    async def revoke_sessions(
        self, user_ids: list[UUID], reason: str, operator: str, operation_key: str
    ) -> int:
        """End every session of the users; returns how many exist.

        Kept for one release: ``sandloom security revoke-sessions`` calls
        :meth:`end_sessions`, which also says which kinds of session ended."""
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
    async def end_sessions(
        self, user_ids: list[UUID], reason: str, operator: str, operation_key: str
    ) -> SessionsEnded:
        """End every session identity holds for the users: their refresh
        sessions (revoked in the database) and their access tokens. Audited
        per user (``identity.user.sessions_revoked``). ``affected``: how many
        of the users exist. The kernel revokes the access tokens of every
        issuer itself (``sandloom security revoke-sessions``); this is
        identity's own part, called before it."""
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
        return SessionsEnded(
            affected=len(users), ended=["access_tokens", "refresh_sessions"]
        )

    @command(idempotency_key="operation_key")
    async def require_password_reset(
        self, user_ids: list[UUID], reason: str, operator: str, operation_key: str
    ) -> int:
        """Refuse password sign-in until a reset, and end every session."""
        _check_batch(user_ids, reason, operator)
        users = await _existing(user_ids)
        if users:
            session = db()
            await session.execute(
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
        session = db()
        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        return _contact(user) if user is not None else None

    async def get_contacts(self, user_ids: list[UUID]) -> list[UserContact]:
        """Contact details of the known users among ``user_ids``, in id order
        (unknown ids are left out). At most 1000 ids per call (422 above)."""
        if len(user_ids) > MAX_BATCH:
            raise HTTPException(422, f"at most {MAX_BATCH} users per call")
        if not user_ids:
            return []
        session = db()
        rows = await session.execute(
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
