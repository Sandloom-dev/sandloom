"""Map tenancy domain exceptions to HTTP responses (API layer only)."""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, status

from sandloom_tenancy.exceptions import (
    TenancyConflictError,
    TenancyError,
    TenancyForbiddenError,
    TenancyNotFoundError,
)

_STATUS: dict[type[TenancyError], int] = {
    TenancyNotFoundError: status.HTTP_404_NOT_FOUND,
    TenancyConflictError: status.HTTP_409_CONFLICT,
    TenancyForbiddenError: status.HTTP_403_FORBIDDEN,
}


@contextmanager
def http_errors() -> Iterator[None]:
    """Translate a :class:`TenancyError` raised in the block into HTTPException."""
    try:
        yield
    except TenancyError as exc:
        code = next(
            (c for t, c in _STATUS.items() if isinstance(exc, t)),
            status.HTTP_400_BAD_REQUEST,
        )
        raise HTTPException(status_code=code, detail=exc.detail) from exc


__all__ = ["http_errors"]
