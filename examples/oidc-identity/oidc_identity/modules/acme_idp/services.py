"""The local accounts of external identities: the only code that reads or
writes ``acme_idp_accounts`` (the endpoints in ``api.py`` call it and see ids,
never rows)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ExternalAccount


async def _find(db: AsyncSession, issuer: str, subject: str) -> ExternalAccount | None:
    return (
        await db.execute(
            select(ExternalAccount).where(
                ExternalAccount.issuer == issuer,
                ExternalAccount.subject == subject,
            )
        )
    ).scalar_one_or_none()


async def sign_in_account(
    db: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str,
    email_verified: bool,
    roles: list[str],
) -> UUID:
    """The local account of ``(issuer, subject)``, created on first sign-in
    and refreshed with what the provider said now (address, platform roles)."""
    account = await _find(db, issuer, subject)
    if account is None:
        account = ExternalAccount(issuer=issuer, subject=subject)
        db.add(account)
    account.email = email
    account.email_verified = email_verified
    account.roles = roles
    await db.flush()
    return account.id


async def account_ids_after(
    db: AsyncSession, after: UUID | None, limit: int
) -> list[UUID]:
    """Account ids in id order, strictly after ``after``."""
    stmt = select(ExternalAccount.id).order_by(ExternalAccount.id).limit(limit)
    if after is not None:
        stmt = stmt.where(ExternalAccount.id > after)
    return list((await db.execute(stmt)).scalars())


async def mark_accounts_signed_out(db: AsyncSession, ids: list[UUID]) -> list[UUID]:
    """Record an operator's sign-out of the known accounts among ``ids``;
    their ids, in id order."""
    if not ids:
        return []
    accounts = (
        await db.execute(
            select(ExternalAccount)
            .where(ExternalAccount.id.in_(ids))
            .order_by(ExternalAccount.id)
        )
    ).scalars()
    now = datetime.now(UTC)
    found: list[UUID] = []
    for account in accounts:
        account.signed_out_at = now
        found.append(account.id)
    await db.flush()
    return found


#: The platform administrator's role (``claims.ROLE_PERMISSIONS``).
ADMIN_ROLE = "admin"


async def account_by_email(db: AsyncSession, email: str) -> ExternalAccount | None:
    """The account with this address (the oldest when two subjects share it)."""
    return (
        await db.execute(
            select(ExternalAccount)
            .where(ExternalAccount.email == email.strip())
            .order_by(ExternalAccount.created_at, ExternalAccount.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def accounts_by_id(db: AsyncSession, ids: list[UUID]) -> list[ExternalAccount]:
    """The known accounts among ``ids``, by address."""
    if not ids:
        return []
    return list(
        (
            await db.execute(
                select(ExternalAccount)
                .where(ExternalAccount.id.in_(ids))
                .order_by(ExternalAccount.email)
            )
        ).scalars()
    )


async def roles_of(db: AsyncSession, user_id: UUID) -> list[str] | None:
    """The account's platform roles, or ``None`` without an account."""
    account = await db.get(ExternalAccount, user_id)
    return None if account is None else list(account.roles or ())


async def admin_ids(db: AsyncSession, limit: int) -> list[UUID]:
    """The ids of the accounts holding the ``admin`` role, in id order (the
    role set is a JSON list: filtered here, the table is one row per person)."""
    rows = await db.execute(
        select(ExternalAccount.id, ExternalAccount.roles).order_by(ExternalAccount.id)
    )
    return [row.id for row in rows if ADMIN_ROLE in (row.roles or ())][:limit]


async def add_admin_role(db: AsyncSession, user_id: UUID) -> bool | None:
    """Give the account the ``admin`` role: ``True`` when it was added,
    ``False`` when it held it, ``None`` without an account."""
    account = await db.get(ExternalAccount, user_id)
    if account is None:
        return None
    roles = list(account.roles or ())
    if ADMIN_ROLE in roles:
        return False
    account.roles = sorted({*roles, ADMIN_ROLE})
    await db.flush()
    return True


async def mark_signed_out(
    db: AsyncSession, *, issuer: str, subject: str
) -> UUID | None:
    """Record the provider's logout; the account's id, or ``None`` if the
    subject never signed in here."""
    account = await _find(db, issuer, subject)
    if account is None:
        return None
    account.signed_out_at = datetime.now(UTC)
    return account.id
