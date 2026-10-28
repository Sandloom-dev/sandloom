"""Public operator membership view."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PlatformMember(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    organization_id: UUID
    user_id: UUID
    role: str
    created_at: datetime
