"""Request and response shapes (pydantic only)."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ItemIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    owner_id: UUID
