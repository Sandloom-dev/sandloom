"""Request and response shapes (pydantic only)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    summary: str
    actor_id: UUID
    subject_id: UUID
    created_at: datetime
