"""The service authenticator of internal calls (spec §2.3).

It lives in ``sandloom.shared`` because it returns the shared ``UserContext``;
the kernel layer (``sandloom.core``) does not import ``sandloom.shared``.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from sandloom.core.di import SERVICE_PRINCIPAL_ID, authenticates, has_internal_access
from sandloom.shared.subject import UserContext

__all__ = ["current_service"]


@authenticates(kind="service")
async def current_service(
    x_internal_token: str | None = Header(default=None),
) -> UserContext:
    """The service principal of an internal call (``SOURCE_INTERNAL``): the
    internal token authenticates it (404 otherwise, existence hidden). A route
    taking it is service-authenticated, never user-authenticated (spec §2.3).
    """
    if not has_internal_access(x_internal_token):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return UserContext(user_id=SERVICE_PRINCIPAL_ID, roles={"service"})
