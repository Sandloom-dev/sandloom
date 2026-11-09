"""``identity.security.v1`` DTO: what an operator command needs to know about an
account before it acts (``sandloom admin create`` / ``invite``,
``sandloom security grant-admin``).

Callers import the generated copy (``identity_dto``, written by
``sandloom generate`` into the consuming module), never this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class OperatorAccount(BaseModel):
    """An account as the token issuer knows it, without any role: the
    issuer's side of ``sandloom admin`` / ``grant-admin`` (whether it holds
    the platform ``admin`` role is the roles owner's answer,
    ``is_platform_admin`` / ``platform_admin_ids``)."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    email: str
    #: The address was confirmed (by its owner, or vouched for by an operator).
    email_verified: bool
    #: A deactivated account cannot sign in.
    is_active: bool
    #: A password was set (an account from ``admin create --invite`` has none
    #: until its link is used).
    password_set: bool
    #: The last successful sign-in, or ``None``.
    last_login_at: datetime | None = None


class SessionsEnded(BaseModel):
    """What the token issuer's ``end_sessions`` ended, kind by kind.

    ``sandloom security revoke-sessions`` reports each kind from this answer:
    the kernel revokes every access token itself (any issuer); the issuer ends
    what only it holds.
    """

    model_config = ConfigDict(frozen=True)

    #: Users of the batch the issuer knows (unknown ids are left out).
    affected: int
    #: The kinds of session the issuer holds for them, now ended:
    #: ``refresh_sessions`` (its own refresh sessions), ``provider_sessions``
    #: (sessions at an external identity provider). A kind it does not hold
    #: is left out; identity reports ``refresh_sessions``. ``access_tokens``
    #: is the kernel's to revoke and changes nothing in the report.
    ended: list[Literal["access_tokens", "refresh_sessions", "provider_sessions"]]


__all__ = ["OperatorAccount", "SessionsEnded"]
