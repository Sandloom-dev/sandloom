"""Projects DTOs (ORM-free API contracts per architecture rules)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


class ProjectUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = Field(default=None, min_length=1, max_length=32)


class ProjectState(BaseModel):
    """A project's state as the ``projects.read.v1`` contract returns it.

    Callers receive a copy of this class (``sandloom generate`` writes it into
    ``projecthub.shared.contracts``), so a caller reads ``project.name``
    instead of ``project.get("name", "")``.
    """

    project_id: UUID
    tenant_id: UUID
    name: str
    status: str
    version: int


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    name: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
