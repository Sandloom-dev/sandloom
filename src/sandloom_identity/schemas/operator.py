"""``identity.security.v1`` DTO: what an operator command needs to know about an
account before it acts (``sandloom admin create`` / ``invite``,
``sandloom security grant-admin``).

Callers import the generated copy (``sandloom.shared.contracts.identity_dto``),
written by ``sandloom generate``.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AccountStanding(BaseModel):
    """An account as an operator command sees it."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    #: Holds the platform ``admin`` role.
    is_platform_admin: bool
    #: The address was confirmed (by its owner, or vouched for by an operator).
    email_verified: bool
    #: A deactivated account cannot sign in.
    is_active: bool


__all__ = ["AccountStanding"]
