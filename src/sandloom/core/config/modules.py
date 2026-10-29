"""Settings of the bundled business modules (2FA, billing, SSO providers)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ModulesConfig(BaseModel):
    """Settings of the bundled business modules (2FA, billing, SSO providers)."""

    # Dedicated key(s) for encrypting TOTP secrets at rest (comma-separated,
    # newest first, for rotation). Required in production when identity is
    # loaded (its startup check fails without it); outside production an unset
    # key falls back to one derived from JWT_SECRET.
    twofa_encryption_key: str | None = Field(
        default=None,
        alias="TWOFA_ENCRYPTION_KEY",
        description=(
            "Comma-separated TOTP encryption keys, newest first, for rotation; "
            "production identity requires explicit keys, while development can "
            "derive a fallback from JWT_SECRET."
        ),
    )

    # Billing (Stripe)
    billing_provider: Literal["fake", "stripe"] = Field(
        default="fake",
        alias="BILLING_PROVIDER",
        description=(
            "Payment provider selector: fake for local/testing flows or stripe "
            "for real Stripe integration."
        ),
    )
    stripe_secret_key: str | None = Field(
        default=None,
        alias="STRIPE_SECRET_KEY",
        description=("Stripe API secret used by the Stripe payment-provider adapter."),
    )
    stripe_webhook_secret: str | None = Field(
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

    # SSO
    google_client_id: str | None = Field(
        default=None,
        alias="GOOGLE_CLIENT_ID",
        description=("OAuth client ID for Google sign-in."),
    )
    google_client_secret: str | None = Field(
        default=None,
        alias="GOOGLE_CLIENT_SECRET",
        description=("OAuth client secret for Google sign-in."),
    )
    github_client_id: str | None = Field(
        default=None,
        alias="GITHUB_CLIENT_ID",
        description=("OAuth client ID for GitHub sign-in."),
    )
    github_client_secret: str | None = Field(
        default=None,
        alias="GITHUB_CLIENT_SECRET",
        description=("OAuth client secret for GitHub sign-in."),
    )
    microsoft_client_id: str | None = Field(
        default=None,
        alias="MICROSOFT_CLIENT_ID",
        description=("OAuth client ID for Microsoft sign-in."),
    )
    microsoft_client_secret: str | None = Field(
        default=None,
        alias="MICROSOFT_CLIENT_SECRET",
        description=("OAuth client secret for Microsoft sign-in."),
    )
    linkedin_client_id: str | None = Field(
        default=None,
        alias="LINKEDIN_CLIENT_ID",
        description=("OAuth client ID for LinkedIn sign-in."),
    )
    linkedin_client_secret: str | None = Field(
        default=None,
        alias="LINKEDIN_CLIENT_SECRET",
        description=("OAuth client secret for LinkedIn sign-in."),
    )
    telegram_bot_token: str | None = Field(
        default=None,
        alias="TELEGRAM_BOT_TOKEN",
        description=(
            "Telegram bot credential used by the Telegram sign-in integration."
        ),
    )

    # SSO Security: Allowed redirect URIs (comma-separated)
    sso_allowed_redirect_uris: str = Field(
        default="http://localhost:3000,http://localhost:3000/auth/callback,https://app.example.com/auth/callback",
        alias="SSO_ALLOWED_REDIRECT_URIS",
        description=(
            "Comma-separated exact redirect URI allowlist for SSO callbacks and "
            "frontend redirects."
        ),
    )
