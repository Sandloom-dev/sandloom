"""Notifications API DTOs (persistence-free)."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    template: str
