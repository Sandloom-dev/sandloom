"""Tasks DTOs (ORM-free)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TaskCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    title: str = Field(min_length=1, max_length=255)


class TaskUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = Field(default=None, min_length=1, max_length=32)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    project_id: UUID
    title: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
