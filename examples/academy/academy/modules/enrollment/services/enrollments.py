"""Enrollment rows: create, the saga's local steps, reads."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EnrollmentEnrollment


class AlreadyEnrolled(Exception):
    """A definitive refusal of the reserve step (422): the saga compensates."""

    http_status = 422
    code = "already_enrolled"


class Enrollments:
    @classmethod
    async def create(
        cls, db: AsyncSession, student_id: UUID, course_id: UUID, price: int
    ) -> EnrollmentEnrollment:
        row = EnrollmentEnrollment(
            student_id=student_id, course_id=course_id, price=price, status="pending"
        )
        db.add(row)
        await db.flush()
        return row

    @classmethod
    async def get_or_404(
        cls, db: AsyncSession, enrollment_id: UUID
    ) -> EnrollmentEnrollment:
        row = await db.get(EnrollmentEnrollment, enrollment_id)
        if row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="Enrollment not found"
            )
        return row

    @classmethod
    async def _locked(
        cls, db: AsyncSession, enrollment_id: UUID
    ) -> EnrollmentEnrollment:
        row = (
            await db.execute(
                select(EnrollmentEnrollment)
                .where(EnrollmentEnrollment.id == enrollment_id)
                .with_for_update()
            )
        ).scalar_one()
        return row

    @classmethod
    async def reserve(cls, db: AsyncSession, enrollment_id: UUID) -> str:
        """pending -> reserved; refused when the student already holds a seat."""
        row = await cls._locked(db, enrollment_id)
        if row.status == "reserved":
            return row.status
        taken = (
            await db.execute(
                select(EnrollmentEnrollment.id).where(
                    EnrollmentEnrollment.student_id == row.student_id,
                    EnrollmentEnrollment.course_id == row.course_id,
                    EnrollmentEnrollment.status.in_(("reserved", "active")),
                    EnrollmentEnrollment.id != row.id,
                )
            )
        ).first()
        if taken is not None:
            raise AlreadyEnrolled("the student already holds a seat in this course")
        row.status = "reserved"
        return row.status

    @classmethod
    async def cancel(cls, db: AsyncSession, enrollment_id: UUID) -> str:
        row = await cls._locked(db, enrollment_id)
        row.status = "cancelled"
        return row.status

    @classmethod
    async def activate(cls, db: AsyncSession, enrollment_id: UUID) -> str:
        row = await cls._locked(db, enrollment_id)
        row.status = "active"
        return row.status
