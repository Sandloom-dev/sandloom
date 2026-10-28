"""Enrollment API DTOs (persistence-free)."""

from uuid import UUID

from pydantic import BaseModel, Field


class EnrollIn(BaseModel):
    course_id: UUID
    price: int = Field(gt=0, le=1_000_000)


class EnrollmentAccepted(BaseModel):
    enrollment_id: UUID
    status: str
    status_url: str


class EnrollmentOut(BaseModel):
    id: UUID
    course_id: UUID
    price: int
    status: str
    #: The saga instance behind it (running / compensating / completed /
    #: compensated / parked).
    saga_status: str | None = None
    saga_step: str | None = None
    park_reason: str | None = None
