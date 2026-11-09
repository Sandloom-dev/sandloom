"""acme_idp's claims contributor: the replacement authority's claims.

The composition root assigns ``roles`` and ``permissions`` to acme_idp and
names it the ``issuer``, which gives it ``email`` / ``email_verified`` too
(the issuer fields). The kernel runs this in acme_idp's own Unit of Work,
or, at the sign-in, in the sign-in's own uncommitted transaction
(``collect_claims(issuer=contribute, issuer_session=db)``), so the account
the sign-in just wrote is read.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from sandloom.sdk import db

from .models import ExternalAccount

#: The permissions each platform role grants.
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {"admin": ("acme.platform.manage",)}


async def contribute(user_id: UUID) -> dict[str, Any]:
    """Roles, permissions and the verified e-mail of ``user_id``'s account.

    No account: no rights and no e-mail (an authoritative "no").
    """
    uid = user_id if isinstance(user_id, UUID) else UUID(str(user_id))
    row = (
        await db().execute(
            select(
                ExternalAccount.roles,
                ExternalAccount.email,
                ExternalAccount.email_verified,
            ).where(ExternalAccount.id == uid)
        )
    ).one_or_none()
    if row is None:
        return {"roles": [], "permissions": []}
    roles = sorted(set(row.roles or ()))
    permissions = sorted({p for r in roles for p in ROLE_PERMISSIONS.get(r, ())})
    return {
        "roles": roles,
        "permissions": permissions,
        "email": row.email,
        "email_verified": bool(row.email_verified),
    }
