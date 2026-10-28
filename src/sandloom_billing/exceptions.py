"""Billing domain exceptions (transport-agnostic).

Billing services raise these instead of ``fastapi.HTTPException`` so the domain
layer carries no HTTP semantics. They are mapped to HTTP at the edges, keeping
the statuses clients have always seen:

* **API layer** — each error carries the framework's ``http_status`` protocol
  (``http_status`` / ``code`` / ``detail``), so ``sandloom.core.http.error_handlers``
  renders it as that status with ``{"detail", "code"}``;
* **contract boundary** — ``billing.credits.v1`` converts them with
  :meth:`BillingError.to_http` so an in-process consumer sees the SAME
  ``HTTPException`` (status + detail) a remote consumer gets from the RPC proxy.
"""

from __future__ import annotations

from fastapi import HTTPException


class BillingError(Exception):
    """Base class for billing domain errors (``http_status`` protocol)."""

    http_status: int = 400
    code: str = "billing_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def to_http(self) -> HTTPException:
        """The equivalent ``HTTPException`` (used at the contract boundary)."""
        return HTTPException(status_code=self.http_status, detail=self.detail)


class InsufficientCreditsError(BillingError):
    """Not enough credits (or no billing customer yet): nothing was written."""

    http_status = 402
    code = "insufficient_credits"

    def __init__(self, detail: str = "Insufficient credits") -> None:
        super().__init__(detail)


class CreditOperationConflictError(BillingError):
    """``operation_key`` was already used for a DIFFERENT operation (spec §8)."""

    http_status = 409
    code = "credit_operation_conflict"

    def __init__(self, operation_key: str) -> None:
        super().__init__(
            f"operation_key {operation_key!r} was already used for a "
            "different credit operation"
        )


class BillingTenantMismatchError(BillingError):
    """A contract call targets another organization than the bound tenant."""

    http_status = 403
    code = "billing_tenant_mismatch"

    def __init__(
        self, detail: str = "The organization does not match the bound tenant"
    ) -> None:
        super().__init__(detail)


__all__ = [
    "BillingError",
    "BillingTenantMismatchError",
    "CreditOperationConflictError",
    "InsufficientCreditsError",
]
