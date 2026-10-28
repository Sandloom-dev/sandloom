"""``identity.contact.v1`` DTO: how to reach a user.

Events carry no PII (a user is a ``user_id``), so a module that must write
to a user — a welcome email, a receipt, an invitation reminder — asks
identity at send time through ``identity.contact.v1`` instead of copying the
address into its own tables or editing identity. Callers import the
generated copy (``sandloom.shared.contracts.identity_dto``), written by
``sandloom generate``.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserContact(BaseModel):
    """A user's contact details, as identity holds them now."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    email: str
    full_name: str
    #: The address was confirmed (do not send marketing to an unverified one).
    email_verified: bool
    #: A deactivated user should not be written to.
    is_active: bool


__all__ = ["UserContact"]
