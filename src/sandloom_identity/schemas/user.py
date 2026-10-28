"""User schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserBase(BaseModel):
    """Base user schema."""

    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=255)
    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    username: str | None = Field(None, max_length=50)


class UserCreate(UserBase):
    """User creation schema."""

    password: str = Field(..., min_length=8, max_length=100)


class UserUpdate(BaseModel):
    """User update schema."""

    full_name: str | None = Field(None, min_length=1, max_length=255)
    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    username: str | None = Field(None, max_length=50)
    is_active: bool | None = None


class UserOut(UserBase):
    """User output schema."""

    model_config = ConfigDict(from_attributes=True)

    # The stored address was validated (EmailStr) when it was written; an
    # output model only copies it. EmailStr re-validation costs ~30 µs per
    # value (DNS-name / IDNA checks) on every response; the OpenAPI schema
    # keeps ``format: email``.
    email: str = Field(..., json_schema_extra={"format": "email"})
    id: UUID
    is_active: bool
    is_superuser: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None
    email_verified_at: datetime | None


class UserWithRoles(UserOut):
    """User with roles output schema."""

    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class UserWithRolesOut(UserOut):
    """User with roles output schema."""

    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class UserListOut(BaseModel):
    """User list output schema."""

    users: list[UserOut]
    #: ``None`` only when the caller asked for no total (``include_total``).
    total: int | None
    page: int
    per_page: int


class Principal(BaseModel):
    """The authenticated caller, as handed to identity's API layer.

    A DTO (never the live ORM entity): endpoints pass ``id`` to services, and
    ``token_claims`` lets services evaluate step-up re-auth (token age).
    """

    id: UUID
    email: str
    has_password: bool = False
    token_claims: dict[str, Any] = Field(default_factory=dict)
