"""Shared JWT guard: current user id from Bearer token (no DB, no identity).

Business modules use this to authenticate requests without importing the
identity module — only the core JWT adapter is involved.

It projects the subject's id from the canonical authorization path
(:func:`sandloom.shared.authz.current_context`), where authentication is decoded
and cached once per request; depend on ``current_context`` (or a
``require(...)`` policy) when more than the user id is needed.
"""

from uuid import UUID

from fastapi import Depends

from sandloom.shared.authz.deps import current_context
from sandloom.shared.subject import UserContext

__all__ = ["get_current_user_id"]


async def get_current_user_id(
    subject: UserContext = Depends(current_context),
) -> UUID:
    """Extract the authenticated user id from the Bearer access token."""
    return subject.user_id
