"""Contract errors: :class:`ContractCallError` and its framework subclasses
(identical locally and over RPC)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

import sandloom.core.http.error_handlers as _error_handlers


class ContractCallError(HTTPException):
    """A contract call failed with a declared status — identical locally and over RPC.

    Every domain / protocol error a contract raises surfaces to the caller as
    this exception (an ``HTTPException``): a provider ``HTTPException`` or an
    ``http_status``-protocol exception is converted with the SAME status,
    JSON-encoded ``detail``, ``headers`` (e.g. ``Retry-After``) and ``code``
    whether the provider is in-process or behind internal RPC. Framework errors
    keep their own subclass on both paths (:class:`ContractIdempotencyConflict`,
    :class:`ContractArgumentError`, …).
    """

    http_status: int = 500
    code: str | None = None

    def __init__(
        self,
        detail: Any = None,
        *,
        status_code: int | None = None,
        headers: Mapping[str, str] | None = None,
        code: str | None = None,
    ) -> None:
        resolved = status_code or type(self).http_status
        HTTPException.__init__(
            self,
            status_code=resolved,
            detail=detail,
            headers=dict(headers) if headers else None,
        )
        self.http_status = resolved
        if code is not None:
            self.code = code


class ContractArgumentError(ContractCallError, TypeError):
    """The call's arguments do not bind to / validate against the signature (422).

    Raised BEFORE the provider runs, locally and remotely alike: unknown or
    missing arguments, positional-only / keyword-only violations, or a value
    that does not validate against the parameter's annotation.
    """

    http_status = 422
    code = "contract_argument_invalid"


class ContractIdempotencyKeyMissing(ContractCallError, ValueError):
    """A command contract was called without its idempotency/operation key."""

    http_status = 422
    code = "contract_idempotency_key_missing"


class ContractIdempotencyConflict(ContractCallError, RuntimeError):
    """A command's idempotency key was reused with DIFFERENT arguments (409)."""

    http_status = 409
    code = "contract_idempotency_conflict"


class ContractCallInProgress(ContractCallError, RuntimeError):
    """A command with this idempotency key is still running (503, retry later)."""

    http_status = 503
    code = "contract_call_in_progress"


class ContractIdempotencyUnavailable(ContractCallError, RuntimeError):
    """The command dedupe store is unreachable: the command is refused (503).

    Commands fail CLOSED — running one without dedupe could duplicate its side
    effect on a caller retry.
    """

    http_status = 503
    code = "contract_idempotency_unavailable"


class ContractResultNotReplayable(ContractCallError, RuntimeError):
    """A retried command already completed but its result was too large to keep (409).

    The effect happened exactly once; only the result body is unavailable.
    """

    http_status = 409
    code = "contract_result_not_replayable"


#: Framework error classes, rebuilt by ``code`` on the remote caller's side.
CONTRACT_ERROR_CLASSES: dict[str, type[ContractCallError]] = {
    cls.code: cls
    for cls in (
        ContractArgumentError,
        ContractIdempotencyKeyMissing,
        ContractIdempotencyConflict,
        ContractCallInProgress,
        ContractIdempotencyUnavailable,
        ContractResultNotReplayable,
    )
    if cls.code is not None
}


def contract_error(
    *,
    status_code: int,
    detail: Any,
    headers: Mapping[str, str] | None = None,
    code: str | None = None,
) -> ContractCallError:
    """Build the caller-side exception for a contract error (local or RPC)."""
    cls = CONTRACT_ERROR_CLASSES.get(code or "", ContractCallError)
    return cls(detail, status_code=status_code, headers=headers, code=code)


def normalize_contract_error(exc: BaseException) -> ContractCallError | None:
    """The caller-side form of a provider exception, or ``None`` (a 5xx bug).

    ``HTTPException`` and ``http_status``-protocol exceptions become a
    :class:`ContractCallError` with the same status, JSON-encoded detail,
    headers and code — exactly what the RPC server transmits.
    """
    if isinstance(exc, ContractCallError):
        return exc

    if isinstance(exc, HTTPException):
        return ContractCallError(
            jsonable_encoder(exc.detail),
            status_code=exc.status_code,
            headers=exc.headers,
            code=getattr(exc, "code", None),
        )
    declared = _error_handlers.http_status_of(exc)
    if declared is None:
        return None
    detail = getattr(exc, "detail", None)
    if detail is None:
        detail = str(exc) or None
    headers = getattr(exc, "headers", None)
    return ContractCallError(
        jsonable_encoder(detail),
        status_code=declared,
        headers=headers if isinstance(headers, dict) else None,
        code=str(getattr(exc, "code", None) or "error"),
    )


class ReadContractWriteError(RuntimeError):
    """A read contract tried to write (its UoW is always rolled back).

    Raised for published events, queued ``on_commit`` / ``before_commit``
    effects, ``record_audit`` rows and ORM / DML writes — which a rolled-back
    read would otherwise drop silently while returning success. Declare the
    method ``@contract_method(command=True, idempotency_key=...)``.
    """


def read_contract_write_error(qualname: str) -> Callable[[str], ReadContractWriteError]:
    """Factory: ``what`` (e.g. ``"published events"``) -> the error to raise."""

    def make(what: str) -> ReadContractWriteError:
        return ReadContractWriteError(
            f"read contract {qualname!r} {what}; a read contract's Unit of Work "
            "is always rolled back — declare it "
            "@contract_method(command=True, idempotency_key=...)"
        )

    return make


class _ReadOnlyRollback(Exception):
    """Internal: forces the read contract's UoW to roll back."""
