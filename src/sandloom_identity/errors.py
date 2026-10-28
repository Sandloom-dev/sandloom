"""Identity error types.

``CommittedHTTPError`` is an ``HTTPException`` whose side effects MUST commit.

The request Unit of Work rolls back on any exception, which is right for
ordinary failures but wrong for security reactions that have to persist even
though the request itself fails — e.g. revoking a whole refresh-token family on
reuse detection, or the audit row of an account lockout. Services flush those
writes and raise ``CommittedHTTPError``; the identity endpoints catch it and
*return* the equivalent JSON error response, so the UoW sees a normal return
and commits. Code that calls the service directly (tests, scripts) still sees an
``HTTPException`` with the right status.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from fastapi import HTTPException
from fastapi.responses import JSONResponse

P = ParamSpec("P")
R = TypeVar("R")


class CommittedHTTPError(HTTPException):
    """An HTTP error whose already-flushed side effects must be committed."""

    def to_response(self) -> JSONResponse:
        """Render like the framework's HTTPException handler does."""
        return JSONResponse(
            status_code=self.status_code,
            content={"detail": self.detail, "code": None},
            headers=self.headers,
        )


def commit_on_security_error(
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[Any]]:
    """Endpoint decorator: turn ``CommittedHTTPError`` into a returned response.

    Returning (instead of raising) lets the request UoW commit the security
    writes the service flushed before failing.
    """

    @wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
        try:
            return await fn(*args, **kwargs)
        except CommittedHTTPError as exc:
            return exc.to_response()

    return wrapper


__all__ = ["CommittedHTTPError", "commit_on_security_error"]
