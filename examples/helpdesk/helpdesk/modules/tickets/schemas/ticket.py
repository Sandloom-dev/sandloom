"""Ticket DTOs (ORM-free API contracts)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TicketCreateIn(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(default="", max_length=10_000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"


class TicketStatusIn(BaseModel):
    status: Literal["open", "pending", "closed"]


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    created_by: UUID
    subject: str
    body: str
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
