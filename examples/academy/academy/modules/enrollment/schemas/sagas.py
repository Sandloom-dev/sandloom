"""Input of the ENROLL saga.

Persisted with the instance and handed to every argument function as
``c.input``: ids and amounts only, never PII.
"""

from uuid import UUID

from pydantic import BaseModel


class EnrollInput(BaseModel):
    enrollment_id: UUID
    student_id: UUID
    course_id: UUID
    price: int
