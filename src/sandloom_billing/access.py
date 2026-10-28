"""Billing's access facts — configurable claim/live resolution.

Two facts, one per billing tenant kind:

- ``subscription.active`` — the subject's PERSONAL tenant. Answerable from the
  claim contributor (token ``subscription_status``) or live via
  ``billing.entitlements.v1`` with ``("user", subject.user_id)``. Both modes use
  the same predicate (period + past_due grace policy), so the deployment can
  pick claim (cheap) / live (fresh) / auto via ``ACCESS`` without an edit.
- ``organization.subscription.active`` — the ORGANIZATION the request targets.
  Live only (the token carries no per-organization billing claim). The
  organization comes from the request (``org_id`` path or query parameter),
  else the bound tenant (``TenantScope``); the subject must be a member of it
  (``tenancy.membership.v1``), otherwise it denies 403 (``FactDenied``
  ``not_a_member``). No organization in context ⇒ 403 ``organization_required``
  (fail closed). An org without an active subscription ⇒ 402 (both facts
  declare ``deny_category=PAYMENT_REQUIRED``, like ``Subscribed`` /
  ``SubscriptionActive`` / ``Credits``). Tenancy not deployed ⇒ ``FactUnavailable``
  (501, misconfiguration — never a silent allow).

Both facts are part of billing's PUBLIC contract: registering them exposes
``billing.facts.v1`` (``describe`` / ``evaluate``), so a process WITHOUT
billing that binds it resolves ``Entitled(...)`` /
``Subscribed(of=Account.user())`` exactly
like the monolith (claim read with :func:`subscription_claim_rule`, live
answered by billing).

The fixed-tier ``Subscribed`` (claim, personal) / ``SubscriptionActive`` (live)
atoms remain for callers that want to pin a mode explicitly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sandloom.sdk.authz import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    MODE_LIVE,
    AccessFact,
    Category,
    ClaimRule,
    FactDenied,
    FactUnavailable,
)
from sandloom.sdk.errors import ServiceNotFound
from sandloom.sdk.infra import settings
from sandloom.sdk.tenant import current_tenant_id_optional
from sandloom_billing.entitlements import ENTITLEMENTS_SERVICE_V1

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.sdk.authz import AuthzContext

MEMBERSHIP_SERVICE_V1 = "tenancy.membership.v1"
ORG_PARAM = "org_id"
_PAST_DUE = "past_due"


async def _live_subscription_active(service: object, ctx: AuthzContext) -> bool:
    return bool(
        await service.check_subscription_active(  # type: ignore[attr-defined]
            "user", ctx.subject.user_id
        )
    )


def subscription_claim_rule() -> ClaimRule:
    """The DECLARATIVE claim rule of ``subscription.active`` (single source).

    Same predicate as the live check: ``active`` / ``trialing`` grant access,
    and ``past_due`` does during the grace period unless
    ``BILLING_DENY_ON_PAST_DUE``. Evaluated with billing's settings — locally,
    and at ``billing.facts.v1.describe()`` time for consumers in other
    processes (so they interpret the claim exactly like billing does).
    """
    statuses = set(ACTIVE_SUBSCRIPTION_STATUSES)
    if not settings.billing_deny_on_past_due:
        statuses.add(_PAST_DUE)
    return ClaimRule.one_of(statuses)


def _parse_uuid(raw: object) -> UUID | None:
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (TypeError, ValueError):
        return None


def context_org_id(ctx: AuthzContext) -> UUID | None:
    """The organization this evaluation targets, or ``None``.

    Request ``org_id`` path param → ``org_id`` query param → bound tenant.
    """
    request = ctx.request
    if request is not None:
        org_id = _parse_uuid(request.path_params.get(ORG_PARAM))
        if org_id is None:
            org_id = _parse_uuid(request.query_params.get(ORG_PARAM))
        if org_id is not None:
            return org_id
    return current_tenant_id_optional()


async def _live_org_subscription_active(service: object, ctx: AuthzContext) -> bool:
    org_id = context_org_id(ctx)
    if org_id is None:
        # Fail closed, but as "forbidden" (403): there is nothing to pay for.
        raise FactDenied("no organization in context", code="organization_required")
    core = ctx.core()
    if core is None:
        raise FactUnavailable(
            "fact 'organization.subscription.active': no core bound to resolve "
            f"{MEMBERSHIP_SERVICE_V1!r}"
        )
    # Local provider OR the SERVICE_BINDINGS RPC proxy (tenancy deployed as a
    # separate service) — never a local-registry-only probe.
    try:
        membership = core.get_service(MEMBERSHIP_SERVICE_V1)
    except ServiceNotFound as exc:  # incl. ServiceUnavailableInDeployment
        raise FactUnavailable(
            "fact 'organization.subscription.active': "
            f"{MEMBERSHIP_SERVICE_V1!r} is not configured"
        ) from exc
    # A caller-supplied org id is only a selector: never answer for an
    # organization the subject does not belong to.
    if await membership.get_role(org_id, ctx.subject.user_id) is None:
        raise FactDenied(
            "subject is not a member of the organization", code="not_a_member"
        )
    return bool(
        await service.check_subscription_active(  # type: ignore[attr-defined]
            "organization", org_id
        )
    )


SUBSCRIPTION_ACTIVE_FACT = AccessFact(
    name="subscription.active",
    owner_module="billing",
    claim_key="subscription_status",
    # Declarative, so it travels to consumers via billing.facts.v1.
    claim_rule=subscription_claim_rule,
    live_service=ENTITLEMENTS_SERVICE_V1,
    live_resolver=_live_subscription_active,
    min_freshness="claim",
    default_mode="auto",
    # Same 402 as Subscribed / SubscriptionActive / Credits.
    deny_category=Category.PAYMENT_REQUIRED,
)

ORG_SUBSCRIPTION_ACTIVE_FACT = AccessFact(
    name="organization.subscription.active",
    owner_module="billing",
    live_service=ENTITLEMENTS_SERVICE_V1,
    live_resolver=_live_org_subscription_active,
    min_freshness=MODE_LIVE,
    default_mode=MODE_LIVE,
    # 402 when the org simply has no active subscription; no org in context /
    # not a member deny 403 (FactDenied in the resolver).
    deny_category=Category.PAYMENT_REQUIRED,
)
