"""Tenancy DTOs (API contracts are ORM-free per architecture rules)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from sandloom_tenancy.roles import ORG_ROLES, ROLE_MEMBER, ROLE_OWNER

_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class OrganizationCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=2, max_length=100, pattern=_SLUG_PATTERN)


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
