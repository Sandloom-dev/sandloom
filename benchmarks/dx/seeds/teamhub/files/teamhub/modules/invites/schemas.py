"""Request and response shapes (pydantic only)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InvitationIn(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    status: str
    expires_at: datetime
    invited_by: UUID
    created_at: datetime
