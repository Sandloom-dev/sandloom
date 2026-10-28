"""Enrollment API: start the ENROLL saga (202) and read where it is.

``module_router()`` authenticates every route and binds the tenant (the
enrollment table is ``TenantScoped``).
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, status

from sandloom.sdk import CurrentUser, module_router
from sandloom.sdk.sagas import (  # sandloom: advanced start a saga in the request unit
    saga_status,
    start_saga,
)
from sandloom.sdk.web import (  # sandloom: advanced db() lands with H1; durable guard
    DbSession,
    idempotency_guard,
)

from ..sagas import ENROLL
from ..schemas import EnrollIn, EnrollInput, EnrollmentAccepted, EnrollmentOut
from ..services import Enrollments

router = module_router(prefix="/api/v1/enrollments")


@router.post(
    "",
    response_model=EnrollmentAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enroll(
    data: EnrollIn,
    db: DbSession,
    user: CurrentUser,
    _key: Annotated[
        Any, Depends(idempotency_guard(scope="enrollment.enroll", durable=True))
    ],
) -> EnrollmentAccepted:
    """Create a pending enrollment and start its saga in the SAME transaction."""
    row = await Enrollments.create(db, user.user_id, data.course_id, data.price)
    handle = await start_saga(
        ENROLL,
        saga_id=row.id,
        input=EnrollInput(
            enrollment_id=row.id,
            student_id=user.user_id,
            course_id=data.course_id,
            price=data.price,
        ),
    )
    return EnrollmentAccepted(
        enrollment_id=row.id,
        status=handle.status,
        status_url=f"/api/v1/enrollments/{row.id}",
    )


@router.get(
    "/{enrollment_id}",
    response_model=EnrollmentOut,
)
async def get_enrollment(
    enrollment_id: UUID, db: DbSession, user: CurrentUser
) -> EnrollmentOut:
    # The saga FIRST, the row after it: a saga transition commits the
    # enrollment row and the instance status together, and under READ
    # COMMITTED a later read never sees an older commit — so the row is at
    # least as new as the saga status we report (read the other way round, a
    # transition landing between the two reads would pair a terminal saga
    # status with the row's previous state).
    handle = await saga_status(ENROLL, enrollment_id)
    row = await Enrollments.get_or_404(db, enrollment_id)
    return EnrollmentOut(
        id=row.id,
        course_id=row.course_id,
        price=row.price,
        status=row.status,
        saga_status=handle.status if handle else None,
        saga_step=handle.step if handle else None,
        park_reason=handle.park_reason if handle else None,
    )
