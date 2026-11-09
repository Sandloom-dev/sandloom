"""Billing's declared settings (ADR-0015).

The module owns the payment-provider selection and its Stripe credentials (with
their requirements and the production refusal of the fake provider), the
past-due policy and the intent-reconciliation and portal-attribution knobs.
Env names are unchanged; the kernel no longer lists them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, SecretStr, ValidationInfo, model_validator

from sandloom.sdk.module import RemovedSetting, Risk, SemanticSetting, settings_of


class BillingSettings(BaseModel):
    billing_provider: Literal["fake", "stripe"] = Field(
        default="fake",
        alias="BILLING_PROVIDER",
        description=(
            "Payment provider selector: fake for local/testing flows or stripe "
            "for real Stripe integration."
        ),
    )
    stripe_secret_key: SecretStr | None = Field(
        default=None,
        alias="STRIPE_SECRET_KEY",
        description="Stripe API secret used by the Stripe payment-provider adapter.",
    )
    stripe_webhook_secret: SecretStr | None = Field(
        default=None,
        alias="STRIPE_WEBHOOK_SECRET",
        description=(
            "Stripe webhook signing secret used to authenticate incoming webhook "
            "payloads."
        ),
    )
    billing_deny_on_past_due: bool = Field(
        default=False,
        alias="BILLING_DENY_ON_PAST_DUE",
        description=(
            "Deny subscription entitlements while payment is past due; disabled "
            "preserves the configured past-due grace behavior."
        ),
    )
    # ADR-0003 open item 9: a subscription created WITHOUT an operation intent
    # (operator-created in the PSP, migrated, restored) is synced but grants
    # entitlements only when this is true (and its price is in the catalog).
    # Default false = fail closed; rows that existed before the policy
    # (origin "legacy") and intent-backed ones are unaffected.
    billing_grant_intentless_subscriptions: bool = Field(
        default=False,
        alias="BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS",
        description=(
            "Allow catalog-backed subscriptions created without a framework "
            "operation intent to grant access; default denies those new "
            "intentless grants."
        ),
    )
    # #72: the periodic reconciler (billing.tasks.reconcile_operation_intents)
    # closes a ``pending`` operation intent older than this (its client never
    # retried) after looking its PSP session up. Must exceed the longest
    # request (PSP timeouts + retries), hence the floor.
    billing_intent_reconcile_after_minutes: int = Field(
        default=30,
        ge=5,
        le=24 * 60,
        alias="BILLING_INTENT_RECONCILE_AFTER_MINUTES",
        description=(
            "Minimum pending-intent age in minutes before periodic reconciliation "
            "looks up or expires its payment-provider operation."
        ),
    )
    # Failed reconciliation steps per intent (PSP lookup / expire) before it is
    # left to an operator (``followup_at``).
    billing_intent_reconcile_max_attempts: int = Field(
        default=5,
        ge=1,
        le=100,
        alias="BILLING_INTENT_RECONCILE_MAX_ATTEMPTS",
        description=(
            "Failed reconciliation attempts per operation intent before leaving "
            "it for operator follow-up."
        ),
    )
    # ADR-0003 open item 10: a user-actionable subscription change (plan /
    # cancel_at_period_end) is attributed to the customer portal when the
    # customer has a portal intent issued within this window before it;
    # otherwise it is audited ``billing.subscription.unattributed_change``
    # (never blocked).
    billing_portal_attribution_window_minutes: int = Field(
        default=60,
        ge=1,
        le=24 * 60,
        alias="BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES",
        description=(
            "Minutes during which a preceding portal intent attributes "
            "subscription changes to that portal visit; changes outside it are "
            "audited as unattributed."
        ),
    )

    #: The one semantic setting billing classifies (ratcheted: the baseline in
    #: ``test_billing_settings`` only shrinks).
    semantic: ClassVar[tuple[SemanticSetting, ...]] = (
        SemanticSetting(
            "BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS",
            "A subscription without an operation intent grants entitlements.",
            profile={"dev": False, "test": False, "prod": False, "prod_strict": False},
            strength=(True, False),
        ),
    )
    risks: ClassVar[tuple[Risk, ...]] = (
        Risk(
            "fake_psp_in_prod",
            "BILLING_PROVIDER=fake with ENVIRONMENT=prod: fake checkout sessions, "
            "no real payment (demo stacks).",
            "billing's settings check (sandloom_billing.config.BillingSettings)",
        ),
    )
    #: What the kernel's tools read from this declaration (ADR-0015 decision 11).
    #: The console's settings view lists the provider (no secret).
    console: ClassVar[tuple[str, ...]] = ("billing_provider",)
    #: Identical in every process that loads billing (deploy profile check
    #: SANDLOOM-T014): a worker and the API must apply one past-due policy.
    shared: ClassVar[tuple[str, ...]] = ("BILLING_DENY_ON_PAST_DUE",)
    #: ``(keep in the secret manager, what losing it costs)`` for the backup plan.
    backup: ClassVar[tuple[tuple[str, str], ...]] = (
        (
            "STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET (also kept by the PSP dashboard)",
            "",
        ),
    )
    #: The compose environment of a process that loads billing
    #: (``sandloom add deploy``).
    deploy: ClassVar[tuple[str, ...]] = (
        "# billing refuses the fake PSP in prod: the operator must choose.",
        "BILLING_PROVIDER: ${BILLING_PROVIDER:?set BILLING_PROVIDER (stripe, or "
        "fake with SANDLOOM_ACCEPTED_RISKS="
        "local_storage_in_prod,billing.fake_psp_in_prod for demos)}",
        "STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY:-}",
        "STRIPE_WEBHOOK_SECRET: ${STRIPE_WEBHOOK_SECRET:-}",
        "# Past-due policy: billing's setting (other services read it through the",
        "# access fact billing describes, never from their own environment).",
        "BILLING_DENY_ON_PAST_DUE: ${BILLING_DENY_ON_PAST_DUE:-false}",
    )
    #: A development / CI stack (``sandloom keys dev-env``) runs a demo on the
    #: fake PSP and accepts the risk that names.
    dev_env: ClassVar[Mapping[str, str | Callable[[], str]]] = {
        "BILLING_PROVIDER": "fake"
    }
    dev_risks: ClassVar[tuple[str, ...]] = ("fake_psp_in_prod",)
    removed: ClassVar[tuple[RemovedSetting, ...]] = (
        RemovedSetting(
            "BILLING_FAKE_ALLOW_UNSIGNED_WEBHOOKS",
            "0.8",
            "the fake PSP always rejects unsigned webhooks; sign test payloads "
            "with sandloom_billing.psp.sign_stripe_payload",
        ),
        RemovedSetting(
            "BILLING_ALLOW_FAKE_PSP_IN_PROD",
            "0.8",
            "accept the risk explicitly: "
            'SANDLOOM_ACCEPTED_RISKS=["billing.fake_psp_in_prod"]',
        ),
    )

    @model_validator(mode="after")
    def _provider_requirements(self, info: ValidationInfo) -> BillingSettings:
        """The PSP configuration must be usable, or startup stops here.

        Stripe needs both its keys (a missing one must not surface only at the
        first webhook or charge). In production the fake PSP (which issues fake
        checkout sessions and, without a secret, rejects every webhook) is
        refused, the implicit default included, unless the deployment accepts
        the risk ``billing.fake_psp_in_prod``.
        """
        if self.billing_provider == "stripe":
            missing = [
                name
                for name, value in (
                    ("STRIPE_SECRET_KEY", self.stripe_secret_key),
                    ("STRIPE_WEBHOOK_SECRET", self.stripe_webhook_secret),
                )
                if value is None or not value.get_secret_value()
            ]
            if missing:
                raise ValueError(
                    f"BILLING_PROVIDER=stripe requires variables: {', '.join(missing)}"
                )
            return self
        context = info.context or {}
        if context.get("environment") == "prod" and not context["accepts_risk"](
            "fake_psp_in_prod"
        ):
            raise ValueError(
                "BILLING_PROVIDER=fake is not allowed with ENVIRONMENT=prod: set "
                "BILLING_PROVIDER=stripe with STRIPE_SECRET_KEY and "
                "STRIPE_WEBHOOK_SECRET (or, for a demo stack, "
                'SANDLOOM_ACCEPTED_RISKS=["billing.fake_psp_in_prod"])'
            )
        return self


def billing_settings() -> BillingSettings:
    """Billing's validated settings of the running application."""
    return settings_of(BillingSettings)
