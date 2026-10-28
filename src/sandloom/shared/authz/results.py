"""Structured access decisions — the machine-readable result of evaluation.

A :class:`Requirement` answers a boolean (``evaluate``); the engine wraps that
into an :class:`AccessDecision` that carries *why* (``code`` / ``category`` /
``failed_requirement``) and *how to report it* (``http_status``). This is what
``enforce`` returns/raises, what the HTTP layer maps to a response, and what the
deny audit/metric records — no more anonymous ``Exception("Access denied")``.

Decisions are **three-valued**: allow, deny, and *indeterminate* (the policy
could not be evaluated — a fact is unregistered, a service is absent in this
deployment, the tenant context is missing). An indeterminate decision is always
a denial and combinators never turn it into an allow: ``Not`` does not invert
it, and ``And`` returns it as the denial. ``Or`` allows only when another
operand *genuinely* allows (an independent sufficient grant — the
indeterminate branch's own answer is never inverted or assumed true); when no
operand allows and one was indeterminate, the ``Or`` is indeterminate.
Authorization fails closed on misconfiguration.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Denial categories — the fixed, machine-readable vocabulary.
# ---------------------------------------------------------------------------


class Category:
    """Mandatory denial categories (TZ §10.2)."""

    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    TENANT_INACTIVE = "tenant_inactive"
    PAYMENT_REQUIRED = "payment_required"
    QUOTA_EXCEEDED = "quota_exceeded"
    RESOURCE_FORBIDDEN = "resource_forbidden"
    POLICY_MISCONFIGURED = "policy_misconfigured"


#: Default HTTP status for each category (the transport layer may override).
CATEGORY_HTTP_STATUS: dict[str, int] = {
    Category.UNAUTHENTICATED: 401,
    Category.FORBIDDEN: 403,
    Category.TENANT_INACTIVE: 403,
    Category.PAYMENT_REQUIRED: 402,
    Category.QUOTA_EXCEEDED: 402,
    Category.RESOURCE_FORBIDDEN: 403,
    Category.POLICY_MISCONFIGURED: 500,
}


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """The outcome of evaluating a policy against an :class:`AuthzContext`.

    ``allowed`` is the only field that matters for the boolean question; the
    rest exist so a denial is reportable, auditable and observable.
    """

    allowed: bool
    code: str = "ok"
    category: str | None = None
    reason: str = ""
    http_status: int = 200
    failed_requirement: str | None = None
    policy_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: True when the requirement could NOT be evaluated (fact unregistered,
    #: service absent in this deployment, tenant context missing, …). Always a
    #: denial; combinators propagate it instead of inverting / out-voting it.
    indeterminate: bool = False

    @classmethod
    def allow(cls, *, policy_code: str | None = None) -> AccessDecision:
        return cls(allowed=True, code="ok", http_status=200, policy_code=policy_code)

    @classmethod
    def deny(
        cls,
        *,
        code: str,
        category: str = Category.FORBIDDEN,
        reason: str = "",
        http_status: int | None = None,
        failed_requirement: str | None = None,
        policy_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AccessDecision:
        return cls(
            allowed=False,
            code=code,
            category=category,
            reason=reason,
            http_status=http_status
            if http_status is not None
            else CATEGORY_HTTP_STATUS.get(category, 403),
            failed_requirement=failed_requirement,
            policy_code=policy_code,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def error(
        cls,
        *,
        code: str,
        category: str = Category.POLICY_MISCONFIGURED,
        reason: str = "",
        http_status: int | None = None,
        failed_requirement: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AccessDecision:
        """An *indeterminate* denial: the requirement could not be evaluated.

        Distinct from :meth:`deny` (the subject definitively lacks the
        requirement): combinators propagate it unchanged, so ``~Entitled(x)``
        with ``x`` unregistered / its service down DENIES with this category and
        status instead of inverting the failure into an allow.
        """
        return replace(
            cls.deny(
                code=code,
                category=category,
                reason=reason,
                http_status=http_status,
                failed_requirement=failed_requirement,
                metadata=metadata,
            ),
            indeterminate=True,
        )

    def with_policy_code(self, policy_code: str | None) -> AccessDecision:
        """Copy of this decision tagged with ``policy_code``."""
        return replace(self, policy_code=policy_code)

    def audit_fields(self) -> dict[str, Any]:
        """The subset safe and useful to record on a deny (no PII)."""
        return {
            "code": self.code,
            "category": self.category,
            "failed_requirement": self.failed_requirement,
            "policy_code": self.policy_code,
            "indeterminate": self.indeterminate,
        }


class PolicyIndeterminate(Exception):
    """Raised by an atom's ``evaluate`` when it cannot answer at all.

    ``evaluate`` is boolean, so an atom that cannot evaluate (service absent,
    fact unregistered, no tenant context) must not answer ``False`` — a caller
    composing ``not await atom.evaluate(ctx)`` would turn the failure into an
    allow. It raises this instead; ``Requirement.decide`` (and ``enforce``)
    convert it into the carried indeterminate :class:`AccessDecision`. Never an
    ``HTTPException`` — atoms run in workers too.
    """

    def __init__(self, decision: AccessDecision) -> None:
        if not decision.indeterminate or decision.allowed:
            decision = replace(decision, indeterminate=True, allowed=False)
        self.decision = decision
        super().__init__(decision.reason or decision.code)


class AccessDenied(HTTPException):
    """Raised by ``enforce`` (and the HTTP guards) when a policy is not satisfied.

    Carries the full :class:`AccessDecision` so the caller (HTTP adapter, task
    runner) can map it to the right transport response. Subclasses FastAPI's
    ``HTTPException`` so a module calling ``enforce()`` inside an endpoint gets
    the decision's status (402/403/501/…) and ``{"detail", "code"}`` body from
    the framework HTTP handler instead of an unhandled 500. Non-HTTP callers
    (workers, services) still catch the typed ``AccessDenied`` and read
    ``.decision``. It also exposes the framework error-protocol attributes
    ``http_status`` / ``code`` / ``detail``.

    ``status_code`` explicitly overrides the decision's status (transport
    override, e.g. ``require(..., status_code=402)``).
    """

    def __init__(
        self, decision: AccessDecision, *, status_code: int | None = None
    ) -> None:
        self.decision = decision
        self.http_status: int = status_code or decision.http_status
        self.code: str = decision.code
        super().__init__(status_code=self.http_status, detail=_detail_for(decision))

    def __reduce__(self) -> tuple[Any, ...]:
        # Picklable across process boundaries (task results / RPC errors).
        return (_rebuild_access_denied, (self.decision, self.http_status))


def _rebuild_access_denied(decision: AccessDecision, status_code: int) -> AccessDenied:
    return AccessDenied(decision, status_code=status_code)


def _detail_for(decision: AccessDecision) -> str:
    if decision.reason:
        return decision.reason
    return f"Access denied: {decision.code}"
