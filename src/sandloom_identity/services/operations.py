"""Declared read operations of the identity module (execution planner).

The module states WHAT it reads; the kernel's planner decides HOW (the
optimized executor's generated statement in a read-only transaction, or the
ORM general form). Only identity's own entities are referenced.
"""

from sandloom.sdk.data import ReadOperation
from sandloom_identity.models.user import User
from sandloom_identity.schemas.user import UserOut

#: ``GET /auth/me``: the caller's own user row, as the API DTO (primary key).
user_by_id = ReadOperation(
    entity=User,
    result=UserOut,
    by=("id",),
    name="identity.user.by_id",
)

#: Everything identity declares (registered at startup, see ``sandloom_identity``).
OPERATIONS = (user_by_id,)

__all__ = ["OPERATIONS", "user_by_id"]
