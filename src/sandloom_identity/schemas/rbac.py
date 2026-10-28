"""Role and permission schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RoleBase(BaseModel):
    """Base role schema."""

    name: str = Field(..., min_length=1, max_length=100)
    code: str = Field(..., min_length=1, max_length=50)
    description: str | None = None


class RoleCreate(RoleBase):
    """Role creation schema."""

    pass


class RoleCreateIn(RoleBase):
    """Role creation input schema."""

    pass


class RoleUpdate(BaseModel):
    """Role update schema."""

    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None


class RoleUpdateIn(BaseModel):
    """Role update input schema."""

    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None


class UserRoleAssignIn(BaseModel):
    """User role assignment input schema."""

    role_id: UUID


class RoleOut(RoleBase):
    """Role output schema."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_system: bool
    created_at: datetime
    updated_at: datetime


class PermissionBase(BaseModel):
    """Base permission schema."""

    name: str = Field(..., min_length=1, max_length=100)
    code: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    resource: str | None = Field(None, max_length=50)
    action: str | None = Field(None, max_length=50)


class PermissionCreate(PermissionBase):
    """Permission creation schema."""

    pass


class PermissionUpdate(BaseModel):
    """Permission update schema."""

    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    resource: str | None = Field(None, max_length=50)
    action: str | None = Field(None, max_length=50)


class PermissionOut(PermissionBase):
    """Permission output schema."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_system: bool
    created_at: datetime


class RoleWithPermissions(RoleOut):
    """Role with permissions output schema."""

    permissions: list[PermissionOut] = Field(default_factory=list)


class UserRoleAssign(BaseModel):
    """User role assignment schema."""

    user_id: UUID
    role_id: UUID


class RolePermissionAssign(BaseModel):
    """Role permission assignment schema."""

    role_id: UUID
    permission_id: UUID
