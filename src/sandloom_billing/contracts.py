"""Billing's sync contracts: the providers other modules call.

- ``billing.entitlements.v1`` (:class:`EntitlementsContract`): reads for
  cross-domain guards (subscription predicate, credit balance, claims).
- ``billing.credits.v1`` (:class:`CreditsContract`): the command side of
  credits; semantics in :mod:`sandloom_billing.credits`.

Callers never pass a session: every call runs in billing's own unit on
billing's database, identically in a monolith and over internal RPC.
``spend`` is a ``@command(..., risk="money")``: its money risk is recorded
in the contract schema.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException

from sandloom.sdk import command, contract, db
from sandloom.sdk.authz import Category, From, access_check, claim_atom
from sandloom_billing.access import SUBSCRIPTION_ACTIVE_FACT
from sandloom_billing.credits import CreditsService, ensure_tenant_matches
from sandloom_billing.entitlements import EntitlementsService
from sandloom_billing.exceptions import BillingError


@contract("billing.entitlements.v1")
class EntitlementsContract:
    """Live billing facts for other modules' gates and views (reads only).

    ``tenant_type`` is ``"user"`` (a personal account: ``tenant_id`` is the
    user id) or ``"organization"`` (``tenant_id`` is the organization id).
    Each call runs in billing's own read unit, never in the caller's
    transaction, the same in a monolith and over RPC. A tenant without a
    billing customer is not an error: no subscription, balance 0, claims
    ``{"subscription_status": "none"}`` or ``{}``. For a route gate prefer
    ``SubscriptionActive(of=...)`` / ``Credits(n, of=...)``; in tests,
    ``fake_contract(BILLING_ENTITLEMENTS_V1, Fake())``.
    """

    @access_check(
        "SubscriptionActive",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.PAYMENT_REQUIRED,
        describe="active subscription",
        claim=SUBSCRIPTION_ACTIVE_FACT,
    )
    async def check_subscription_active(
        self, tenant_type: str, tenant_id: UUID
    ) -> bool:
        """True while a subscription grants access: active or trialing inside
        its period, or ``past_due`` within the grace window (unless the
        deployment denies on ``past_due``). False without a billing customer.
        Its access check ``SubscriptionActive(of=...)`` asks it live for the
        account ``of=`` names (402 when it answers False)."""
        return await EntitlementsService.check_subscription_active(
            tenant_type, tenant_id, db()
        )

    @access_check(
        "Credits",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.PAYMENT_REQUIRED,
        describe="at least {minimum} credits",
    )
    async def has_credits_at_least(
        self, tenant_type: str, tenant_id: UUID, minimum: int
    ) -> bool:
        """True when the live credit balance is at least ``minimum`` (the
        query of ``get_credit_balance``; False without a billing customer
        unless ``minimum`` is 0 or less). A read for a gate, never a spend:
        to spend, call ``billing.credits.v1.spend`` (atomic). Its access check
        ``Credits(minimum, of=...)`` answers 402 when it is False."""
        return await EntitlementsService.has_credits_at_least(
            tenant_type, tenant_id, minimum, db()
        )

    @claim_atom("Feature", claim_key="features", entry="key")
    @access_check(
        "LiveFeature",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        describe="feature '{key}'",
    )
    async def has_feature(self, tenant_type: str, tenant_id: UUID, key: str) -> bool:
        """True when the account's plan enables the feature flag ``key``.
        Billing keeps no per-plan feature catalogue yet: this raises
        ``AccessUnknown`` (501), never a False it cannot vouch for. Its
        claim atom ``Feature(key, of=...)`` reads the ``features`` claim for
        ``Account.user()`` with no call and asks this method for an
        organization account (indeterminate 501 while billing has no
        catalogue; 403 when a source answers False)."""
        return await EntitlementsService.plan_feature(tenant_type, tenant_id, key)

    @claim_atom("WithinLimit", claim_key="limits", entry="key", below="used")
    @access_check(
        "LiveWithinLimit",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.QUOTA_EXCEEDED,
        describe="within limit '{key}'",
    )
    async def is_within_limit(
        self, tenant_type: str, tenant_id: UUID, key: str, used: int
    ) -> bool:
        """True when ``used`` is strictly below the account's plan limit
        ``key``. Billing keeps no per-plan limits catalogue yet: this raises
        ``AccessUnknown`` (501), never a False it cannot vouch for. Its claim
        atom ``WithinLimit(key, used, of=...)`` reads the ``limits`` claim
        for ``Account.user()`` with no call and asks this method for an
        organization account (indeterminate 501 while billing has no
        catalogue; quota exceeded when a source answers False)."""
        return await EntitlementsService.plan_limit_allows(
            tenant_type, tenant_id, key, used
        )

    async def get_credit_balance(self, tenant_type: str, tenant_id: UUID) -> int:
        """The live credit balance; 0 when the tenant has no billing customer.
        A read for display: to spend, call ``billing.credits.v1.spend`` (it
        checks the balance atomically)."""
        return await EntitlementsService.get_credit_balance(
            tenant_type, tenant_id, db()
        )

    async def subscription_claims(
        self, tenant_type: str, tenant_id: UUID
    ) -> dict[str, Any]:
        """The subscription as token claims: ``subscription_status`` (an
        active status, ``"expired"`` once access lapsed, ``"none"`` without a
        subscription) and ``plan_code`` (the plan's name, while access is
        granted). ``{}`` when the tenant has no billing customer."""
        return await EntitlementsService.subscription_claims(
            tenant_type, tenant_id, db()
        )


@contract("billing.credits.v1")
class CreditsContract:
    """Spend a tenant's credits from another module (the command side).

    Every call runs in billing's own unit and commits when it returns: it is
    NOT atomic with the caller's transaction (design the caller as a saga
    step, or compensate). Errors arrive as ``HTTPException`` with the same
    status locally and over RPC. Credits are granted only by billing itself.
    In tests, ``fake_contract(BILLING_CREDITS_V1, Fake())``.
    """

    @command(
        idempotency_key="operation_key",
        idempotency_scope=("tenant_type", "tenant_id"),
        risk="money",
    )
    async def spend(
        self,
        tenant_type: str,
        tenant_id: UUID,
        amount: int,
        operation_key: str,
        *,
        reason: str = "",
    ) -> int:
        """Spend ``amount`` credits; returns the balance after the spend.

        - idempotent on ``operation_key`` (required; e.g. ``"report:<id>"``):
          a retry with the same key and amount returns the ORIGINAL balance
          and spends nothing; the same key with another amount is 409;
        - not enough credits, or no billing customer yet: 402, nothing spent;
        - ``amount=0`` (a free item, a promo code) succeeds and is recorded
          like any spend, the balance unchanged; a negative amount is 422;
        - an ``"organization"`` spend must name the bound tenant, and a
          ``"user"`` spend the calling user (403 otherwise);
        - committed in billing's unit when it returns (not atomic with the
          caller); publishes ``billing.credits.spent.v1``.
        """
        try:
            ensure_tenant_matches(tenant_type, tenant_id)
            return await CreditsService.spend(
                tenant_type,
                tenant_id,
                amount,
                operation_key,
                db(),
                reason=reason,
            )
        except BillingError as exc:
            # Same HTTPException locally as the RPC proxy raises remotely.
            raise exc.to_http() from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def get_balance(self, tenant_type: str, tenant_id: UUID) -> int:
        """The live balance (0 without a billing customer). The account must
        be the caller's: the bound organization, or the calling user (403)."""
        try:
            ensure_tenant_matches(tenant_type, tenant_id)
        except BillingError as exc:
            raise exc.to_http() from exc
        return await EntitlementsService.get_credit_balance(
            tenant_type, tenant_id, db()
        )
