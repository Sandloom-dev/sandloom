"""Tenancy domain exceptions (transport-agnostic).

Services raise these instead of ``HTTPException`` so in-process and RPC callers
get domain semantics, not HTTP ones. The API layer maps them to HTTP status
codes (see ``sandloom_tenancy.api._errors``).
"""


class TenancyError(Exception):
    """Base class for tenancy domain errors."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class TenancyNotFoundError(TenancyError):
    """The organization / member / invitation does not exist (or is not visible)."""


class TenancyConflictError(TenancyError):
    """The operation conflicts with the current state (duplicate, owner rules)."""


class TenancyForbiddenError(TenancyError):
    """The actor is not allowed to perform the operation on this target."""


__all__ = [
    "TenancyError",
    "TenancyNotFoundError",
    "TenancyConflictError",
    "TenancyForbiddenError",
]
