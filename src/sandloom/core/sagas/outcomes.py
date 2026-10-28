"""Step outcome classification (ADR-0008 §7) — an explicit, closed table.

The class of a failed step call is decided here and nowhere else, never by
the first digit of a status code:

==================  ========================================================
class               what maps to it
==================  ========================================================
``success``         the contract (or local step) returned
``transient``       408, 423, 425, 429, every 5xx (incl. 501 while a binding
                    rolls out, 503 ``RemoteServiceUnavailable`` /
                    ``ContractCallInProgress``), transport timeouts and
                    connection errors, a local ``lock_timeout`` — and **any
                    status or exception not listed below** (retrying is the
                    safe default: the command log makes a repeat harmless and
                    the deadline bounds it)
``refused``         400, 402, 403, 404, 410, 422 raised by the provider's
                    domain code — plus the step's own ``refuse_on``
``conflict``        409 ``ContractIdempotencyConflict`` (same key, other
                    arguments: a non-deterministic ``args``) and 409
                    ``ContractResultNotReplayable``
``misconfigured``   401, ``ContractArgumentError`` /
                    ``ContractIdempotencyKeyMissing`` (422 from the binding
                    layer, not the provider's domain code) and a contract
                    nobody provides (``ServiceNotFound`` that is not the 501
                    deployment gap): a wiring bug, not a business answer
==================  ========================================================

A step narrows the table with ``refuse_on`` (e.g. ``{409}`` for a provider
whose domain conflict is a real refusal); the definition refuses a
``refuse_on`` that would widen a transient status (408/423/425/429/5xx).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import HTTPException

import sandloom.core.http.error_handlers as _error_handlers
from sandloom.core.module_api._service_registry import (
    ServiceNotFound,
    ServiceUnavailableInDeployment,
)


class Outcome(StrEnum):
    SUCCESS = "success"
    TRANSIENT = "transient"
    REFUSED = "refused"
    CONFLICT = "conflict"
    MISCONFIGURED = "misconfigured"


#: Definitive business refusals raised by the provider's domain code.
REFUSED_STATUSES = frozenset({400, 402, 403, 404, 410, 422})
#: Listed transient statuses below 500 (every 5xx is transient too).
TRANSIENT_STATUSES = frozenset({408, 423, 425, 429})
#: Statuses that mean "the call never reached a business decision".
MISCONFIGURED_STATUSES = frozenset({401})
#: Framework error codes of the binding layer (argument / key wiring).
MISCONFIGURED_CODES = frozenset(
    {"contract_argument_invalid", "contract_idempotency_key_missing"}
)
#: Framework 409s -> the park reason each one maps to.
CONFLICT_CODES: Mapping[str, str] = {
    "contract_idempotency_conflict": "nondeterministic_args",
    "contract_result_not_replayable": "result_not_replayable",
}


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """The classified result of one step call (``error`` fields on failure)."""

    outcome: Outcome
    status: int | None = None
    code: str | None = None
    detail: Any = None
    error_class: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCESS

    def error_json(self) -> dict[str, Any] | None:
        """The ``last_error`` column (JSON; ``None`` on success)."""
        if self.ok:
            return None
        detail = self.detail
        if not isinstance(detail, str | int | float | bool | type(None)):
            detail = str(detail)
        return {
            "outcome": str(self.outcome),
            "status": self.status,
            "code": self.code,
            "detail": detail,
            "class": self.error_class,
        }

    def conflict_reason(self) -> str:
        return CONFLICT_CODES.get(self.code or "", "nondeterministic_args")


SUCCESS = StepOutcome(Outcome.SUCCESS)


def classify_status(
    status: int, code: str | None = None, *, refuse_on: frozenset[int] = frozenset()
) -> Outcome:
    """The class of a contract error with HTTP ``status`` and framework ``code``."""
    if code in CONFLICT_CODES:
        return Outcome.CONFLICT
    if code in MISCONFIGURED_CODES or status in MISCONFIGURED_STATUSES:
        return Outcome.MISCONFIGURED
    if status in TRANSIENT_STATUSES or status >= 500:
        return Outcome.TRANSIENT
    if status in REFUSED_STATUSES or status in refuse_on:
        return Outcome.REFUSED
    return Outcome.TRANSIENT


def classify_exception(
    exc: BaseException, *, refuse_on: frozenset[int] = frozenset()
) -> StepOutcome:
    """Classify an exception a step call raised (see the module table)."""
    name = type(exc).__name__
    if isinstance(exc, HTTPException):
        status = int(exc.status_code)
        code = getattr(exc, "code", None)
        code = str(code) if code is not None else None
        return StepOutcome(
            classify_status(status, code, refuse_on=refuse_on),
            status,
            code,
            exc.detail,
            name,
        )
    if isinstance(exc, ServiceNotFound) and not isinstance(
        exc, ServiceUnavailableInDeployment
    ):
        return StepOutcome(
            Outcome.MISCONFIGURED, None, "service_not_found", str(exc), name
        )
    declared = _error_handlers.http_status_of(exc)
    if declared is not None:
        code = getattr(exc, "code", None)
        code = str(code) if code is not None else None
        detail = getattr(exc, "detail", None) or str(exc) or None
        return StepOutcome(
            classify_status(declared, code, refuse_on=refuse_on),
            declared,
            code,
            detail,
            name,
        )
    # Transport timeouts / connection errors / lock timeouts — and anything
    # unknown: retried until the deadline.
    return StepOutcome(Outcome.TRANSIENT, None, None, str(exc) or name, name)


#: Fixed remediation printed by ``sandloom sagas show`` for every park reason.
PARK_REMEDIATIONS: Mapping[str, str] = {
    "retriable_refused": (
        "a step after the pivot refused, but retriable steps must not refuse: "
        "fix the provider (or the step's arguments), then `sandloom sagas retry`; "
        "if the effect is not needed, `sandloom sagas resolve --step S --as skipped`"
    ),
    "compensation_refused": (
        "a compensation refused definitively: undo the effect by hand at the "
        "provider (search its records for the step's operation key), then "
        "`sandloom sagas resolve --step S --as applied`"
    ),
    "compensation_deadline_exceeded": (
        "the compensation kept failing past compensation_deadline: fix the "
        "provider, then `sandloom sagas retry`"
    ),
    "deadline_exceeded": (
        "the saga passed its deadline after the pivot (forward recovery only): "
        "fix the failing step's provider, then `sandloom sagas retry` (grants a "
        "fresh deadline)"
    ),
    "nondeterministic_args": (
        "the provider saw the step's operation key with OTHER arguments: the "
        "args function is not a pure function of input and results — fix it, "
        "ship a new definition version, then resolve the step by hand"
    ),
    "result_not_replayable": (
        "the step ran but its result was too large to replay: establish the "
        "outcome at the provider and `sandloom sagas resolve --step S --as applied`"
    ),
    "misconfigured": (
        "a wiring error (401, argument names, a missing contract): fix the "
        "binding / the args function and deploy, then `sandloom sagas retry`"
    ),
    "unknown_definition": (
        "no definition (type, version) is registered in this process: deploy "
        "the version that started it (register it alongside the new one), "
        "then `sandloom sagas retry`"
    ),
    "outcome_unknown_after_ttl": (
        "the current step's last attempt was ambiguous and its dedupe record "
        "may have expired: establish the outcome at the provider (search for "
        "the step's operation key) and `sandloom sagas resolve --step S --as "
        "applied|skipped`"
    ),
    "operator": "parked by an operator: `sandloom sagas retry` when it may continue",
}


def remediation(reason: str | None) -> str:
    return PARK_REMEDIATIONS.get(reason or "", "see docs/concepts/sagas.md")


__all__ = [
    "CONFLICT_CODES",
    "MISCONFIGURED_CODES",
    "MISCONFIGURED_STATUSES",
    "PARK_REMEDIATIONS",
    "REFUSED_STATUSES",
    "SUCCESS",
    "TRANSIENT_STATUSES",
    "Outcome",
    "StepOutcome",
    "classify_exception",
    "classify_status",
    "remediation",
]
