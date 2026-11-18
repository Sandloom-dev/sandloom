"""Tenancy DTOs (API contracts are ORM-free per architecture rules)."""

import re
from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from sandloom_tenancy.roles import ORG_ROLES, ROLE_MEMBER, ROLE_OWNER

_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_SLUG_MIN_LENGTH = 2
_SLUG_MAX_LENGTH = 100


#: The refusal of a name that yields no slug (the schema's 422, the service's
#: fallback): fewer than two ASCII ``a-z0-9`` characters are left, as for
#: ``"A"`` or a name written in another script.
NO_DERIVED_SLUG = "the name does not produce a valid slug: pass an explicit slug"


def derive_slug(name: str) -> str | None:
    """The slug an organization name stands for: lowercase, every run of
    characters outside ASCII ``a-z0-9`` one ``-``, trimmed of ``-`` and cut to
    the length bound; ``None`` when fewer than ``_SLUG_MIN_LENGTH`` characters
    are left (a name of one letter, or without ASCII letters and digits)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug[:_SLUG_MAX_LENGTH].strip("-")
    return slug if len(slug) >= _SLUG_MIN_LENGTH else None


class OrganizationCreateIn(BaseModel):
    """``slug`` is optional: absent, it is derived from ``name`` (``derive_slug``)."""

    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(
        default=None,
        min_length=_SLUG_MIN_LENGTH,
        max_length=_SLUG_MAX_LENGTH,
        pattern=_SLUG_PATTERN,
    )

    @model_validator(mode="after")
    def _name_yields_a_slug(self) -> Self:
        if self.slug is None and derive_slug(self.name) is None:
            raise ValueError(NO_DERIVED_SLUG)
        return self


class OrganizationUpdateIn(BaseModel):
    """Mutable organization attributes (admin+).

    ``is_active`` is deliberately NOT updatable here: deactivation is the
    owner-only ``DELETE /orgs/{id}`` (which publishes ``organization_deleted``
    and is audited). Unknown fields are rejected (422) rather than ignored.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    owner_id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    user_id: UUID
    role: str
    created_at: datetime


class MemberRoleUpdateIn(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in ORG_ROLES:
            raise ValueError(f"role must be one of {ORG_ROLES}")
        return v


class InvitationCreateIn(BaseModel):
    """``role="admin"`` is accepted here but only the OWNER may use it (403)."""

    email: EmailStr
    role: str = ROLE_MEMBER

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in ORG_ROLES or v == ROLE_OWNER:
            raise ValueError("role must be 'member' or 'admin'")
        return v


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    email: str
    role: str
    invited_by: UUID
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class InvitationTokenOut(InvitationOut):
    """Returned only to the inviter at creation time (contains the raw token).

    The token is never persisted (only its sha256) and is also e-mailed to
    the invitee; this response is the only other place it ever appears.
    """

    token: str


class InvitationAcceptIn(BaseModel):
    token: str = Field(min_length=16, max_length=64)
