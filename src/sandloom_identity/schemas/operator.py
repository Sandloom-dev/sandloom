"""``identity.security.v1`` DTO: what an operator command needs to know about an
account before it acts (``sandloom admin create`` / ``invite``,
``sandloom security grant-admin``).

Callers import the generated copy (``identity_dto``, written by
``sandloom generate`` into the consuming module), never this module.
"""

from __future__ import annotations

from datetime import datetime
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


class PlatformAdmin(BaseModel):
    """A platform administrator as ``sandloom admin list`` shows it."""

    model_config = ConfigDict(frozen=True)

    email: str
    #: A password was set (an account from ``admin create --invite`` has none
    #: until its link is used).
    password_set: bool
    #: A deactivated account cannot sign in.
    is_active: bool
    #: The last successful sign-in, or ``None``.
    last_login_at: datetime | None = None


__all__ = ["AccountStanding", "PlatformAdmin"]
