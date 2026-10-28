"""``enforce`` — the single chokepoint every enforcement point routes through.

HTTP guards (``require`` / ``authorize_resource``), use-case calls inside a
service, and background tasks all evaluate the SAME policy through ``enforce``;
there is no parallel code path. ``enforce`` builds the :class:`AuthzContext`,
runs ``policy.decide``, records a denial (structured log + metric — never
``record_audit``, which would be rolled back with the request) and raises a
typed :class:`AccessDenied` carrying the structured decision.

Why log+metric instead of ``record_audit`` on deny: a denied request raises, so
its Unit of Work rolls back and any audit row with it. ``audit_log`` is for
*committed* actions; a denied attempt has no committed change to attach to. Use
the structured deny log + ``access_denied_*`` metrics for deny observability;
reserve ``record_audit`` for the *allowed* sensitive actions modules already
record.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.metrics import increment
from sandloom.shared.authz.results import (
    AccessDecision,
    AccessDenied,
    PolicyIndeterminate,
)
from sandloom.shared.subject import UserContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from sandloom.shared.authz.algebra import Requirement

logger = logging.getLogger("sandloom.access")

SOURCE_HTTP = "http"
SOURCE_TASK = "task"
SOURCE_INTERNAL = "internal"
SOURCE_SYSTEM = "system"

#: Sentinel id for non-user actors (system/service). Distinguishable in audit.
SYSTEM_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000000")

_UNSET = object()


def system_subject(*, roles: frozenset[str] = frozenset({"system"})) -> UserContext:
    """A subject representing the platform itself (trusted background work)."""
    return UserContext(user_id=SYSTEM_ACTOR_ID, roles=set(roles))


def service_subject(*, roles: frozenset[str] = frozenset({"service"})) -> UserContext:
    """A subject representing another service calling across a module boundary."""
    return UserContext(user_id=SYSTEM_ACTOR_ID, roles=set(roles))


def record_denial(ctx: AuthzContext, decision: AccessDecision) -> None:
    logger.warning(
        "access denied: %s (%s)",
        decision.code,
        decision.failed_requirement,
        extra={
            "access_code": decision.code,
            "access_category": decision.category,
            "failed_requirement": decision.failed_requirement,
            "policy_code": decision.policy_code,
            "access_indeterminate": decision.indeterminate,
            "access_http_status": decision.http_status,
            "source": ctx.source,
            "actor_id": str(ctx.subject.user_id),
        },
    )
    core = ctx.core()
    for name in ("access_denied_total", f"access_denied_{decision.category}_total"):
        increment(core, name)
    if decision.indeterminate:
        increment(core, "access_indeterminate_total")


async def enforce(
    policy: Requirement,
    *,
    subject: UserContext,
    db: AsyncSession | None = None,
    request: Request | None = None,
    resource: Any = _UNSET,
    loader: Callable[[AuthzContext], Awaitable[Any]] | None = None,
    source: str = SOURCE_INTERNAL,
    core: Any = None,
    policy_code: str | None = None,
) -> AccessDecision:
    """Evaluate ``policy`` for ``subject``; raise :class:`AccessDenied` on deny.

    The one chokepoint. HTTP/use-case/task adapters all call this. Returns the
    allow :class:`AccessDecision` on success so callers may inspect it.
    """
    kwargs: dict[str, Any] = {
        "request": request,
        "db": db,
        "source": source,
        "core": core,
    }
    if loader is not None:
        kwargs["loader"] = loader
    if resource is not _UNSET:
        kwargs["resource"] = resource
    ctx = AuthzContext(subject, **kwargs)

    decision = await decide(policy, ctx)
    if policy_code is not None:
        decision = decision.with_policy_code(policy_code)
    if not decision.allowed:
        record_denial(ctx, decision)
        raise AccessDenied(decision)
    return decision


async def decide(policy: Requirement, ctx: AuthzContext) -> AccessDecision:
    """``policy.decide(ctx)``, with a stray :class:`PolicyIndeterminate` mapped.

    Custom requirements may call a child's ``evaluate`` inside their own
    ``decide``; an indeterminate child then surfaces as an exception, which is
    converted to its (deny) decision here rather than escaping as a 500.
    """
    try:
        return await policy.decide(ctx)
    except PolicyIndeterminate as exc:
        return exc.decision
