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
