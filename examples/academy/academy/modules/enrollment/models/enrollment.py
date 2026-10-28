"""An enrollment: a student's seat in a course (status = the semantic lock)."""

import uuid

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, TenantScoped, new_id


class EnrollmentEnrollment(TenantScoped, Base):
    __tablename__ = "enrollment_enrollments"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    # Other modules' entities by plain UUID — never a cross-module FK.
    student_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False, index=True)
    course_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    # pending -> reserved -> active | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[CreatedAt]
