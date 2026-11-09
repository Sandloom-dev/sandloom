"""Billing's declared settings (ADR-0015, KB-52): names, bounds, secrets, the
PSP checks and the metadata the kernel tools read (risk, removed, tier)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from sandloom.sdk.testing import validate_settings
from sandloom_billing.config import BillingSettings

#: The eight env names billing owns (unchanged by the move).
ENV_NAMES = {
    "BILLING_PROVIDER",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "BILLING_DENY_ON_PAST_DUE",
    "BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS",
    "BILLING_INTENT_RECONCILE_AFTER_MINUTES",
    "BILLING_INTENT_RECONCILE_MAX_ATTEMPTS",
    "BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES",
}


def test_the_env_names_and_defaults_are_unchanged() -> None:
    fields = {f.alias: f for f in BillingSettings.model_fields.values()}
    assert set(fields) == ENV_NAMES
    cfg = BillingSettings()
    assert cfg.billing_provider == "fake"
    assert cfg.billing_deny_on_past_due is False
    assert cfg.billing_grant_intentless_subscriptions is False
    assert cfg.billing_intent_reconcile_after_minutes == 30
    assert cfg.billing_intent_reconcile_max_attempts == 5
    assert cfg.billing_portal_attribution_window_minutes == 60
    assert cfg.stripe_secret_key is None and cfg.stripe_webhook_secret is None


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("BILLING_INTENT_RECONCILE_AFTER_MINUTES", "4"),
        ("BILLING_INTENT_RECONCILE_AFTER_MINUTES", str(24 * 60 + 1)),
        ("BILLING_INTENT_RECONCILE_MAX_ATTEMPTS", "0"),
        ("BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES", "0"),
        ("BILLING_PROVIDER", "paypal"),
    ],
)
def test_bounds_are_unchanged(name: str, bad: str) -> None:
    with pytest.raises(SystemExit, match=name):
        validate_settings(BillingSettings, {name: bad}, environment="dev")


def test_the_stripe_keys_are_secrets() -> None:
    cfg = validate_settings(
        BillingSettings,
        {
            "BILLING_PROVIDER": "stripe",
            "STRIPE_SECRET_KEY": "sk_live_secret",
            "STRIPE_WEBHOOK_SECRET": "whsec_secret",
        },
        environment="prod",
    )
    assert isinstance(cfg.stripe_secret_key, SecretStr)
    assert isinstance(cfg.stripe_webhook_secret, SecretStr)
    assert "sk_live_secret" not in repr(cfg) + str(cfg.model_dump())
    assert cfg.stripe_secret_key.get_secret_value() == "sk_live_secret"


def test_an_empty_stripe_key_counts_as_missing() -> None:
    with pytest.raises(SystemExit, match="STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET"):
        validate_settings(
            BillingSettings,
            {
                "BILLING_PROVIDER": "stripe",
                "STRIPE_SECRET_KEY": "",
                "STRIPE_WEBHOOK_SECRET": "",
            },
            environment="dev",
        )


def test_the_declared_metadata() -> None:
    """What the kernel tools read: one semantic setting, one namespaced-able
    risk, the two removed settings with the new risk spelling."""
    assert [s.name for s in BillingSettings.semantic] == [
        "BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS"
    ]
    assert [r.id for r in BillingSettings.risks] == ["fake_psp_in_prod"]
    removed = {r.name: r for r in BillingSettings.removed}
    assert set(removed) == {
        "BILLING_FAKE_ALLOW_UNSIGNED_WEBHOOKS",
        "BILLING_ALLOW_FAKE_PSP_IN_PROD",
    }
    assert "billing.fake_psp_in_prod" in removed["BILLING_ALLOW_FAKE_PSP_IN_PROD"].hint


def test_the_fake_psp_is_refused_with_the_namespaced_risk_only() -> None:
    with pytest.raises(SystemExit, match=r"billing\.fake_psp_in_prod"):
        validate_settings(BillingSettings, {}, environment="prod")
    validate_settings(
        BillingSettings,
        {},
        environment="prod",
        accepted_risks=["billing.fake_psp_in_prod"],
    )


def test_a_bad_value_is_a_validation_error_on_the_bare_model() -> None:
    """The model is plain pydantic: its field bounds hold without the loader."""
    with pytest.raises(ValidationError):
        BillingSettings.model_validate({"BILLING_INTENT_RECONCILE_MAX_ATTEMPTS": 0})
