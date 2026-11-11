"""``acme_idp.security.v1`` DTO (callers import the generated copy)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SessionsEnded(BaseModel):
    """What ``end_sessions`` ended, kind by kind."""

    model_config = ConfigDict(frozen=True)

    #: Users of the batch acme_idp has an account for.
    affected: int
    #: The kinds of session acme_idp holds for them, now ended. A stateless
    #: issuer: its sessions are the access tokens it minted (the kernel
    #: revokes them); no refresh sessions, and the provider's own sessions are
    #: not acme_idp's to end.
    ended: list[Literal["access_tokens", "refresh_sessions", "provider_sessions"]]


class OperatorAccount(BaseModel):
    """An account as acme_idp knows it (``sandloom admin`` / ``grant-admin``).

    No password and no "active" flag: the provider keeps both.
    """

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    email: str
    #: The provider said the address is verified, at the last sign-in.
    email_verified: bool


__all__ = ["OperatorAccount", "SessionsEnded"]
