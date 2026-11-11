"""Pin billing's registration surface (what ``register(core)`` registers).

Recorded from the hand-written ``register`` before it became
``define_module(...)`` (since Public API 1.0 the events and contracts are L1
declarations registered by discovery). The
billing router was later split into a read router and a financial one
(ADR-0003, ``test_billing_route_classes.py`` proves nothing observable changed).
"""

from collections.abc import Iterator

import pytest

from sandloom.sdk.module import CapabilityRequirement, ContractRequirement
from sandloom.sdk.testing import RegistrationSurface, registration_surface

EXPECTED = [  # noqa: RUF005 - the expected surface reads as one list plus the shared tail
    # ADR-0015: billing's declared settings, validated before ``setup``.
    (
        "declare_settings",
        "sandloom_billing.config.BillingSettings",
        "package='sandloom_billing'",
    ),
    (
        "include_router",
        "router[GET /api/v1/platform/billing/customers, GET "
        "/api/v1/platform/billing/customers/{record_id}, GET "
        "/api/v1/platform/billing/subscriptions, GET "
        "/api/v1/platform/billing/subscriptions/{record_id}, GET "
        "/api/v1/platform/billing/products, GET "
        "/api/v1/platform/billing/products/{record_id}, GET /api/v1/platform/billing/prices, "
        "GET /api/v1/platform/billing/prices/{record_id}, GET "
        "/api/v1/platform/billing/balances, GET "
        "/api/v1/platform/billing/balances/{record_id}, GET /api/v1/platform/billing/ledger, "
        "GET /api/v1/platform/billing/ledger/{record_id}, GET "
        "/api/v1/platform/billing/grants, GET /api/v1/platform/billing/grants/{record_id}, "
        "GET /api/v1/platform/billing/webhooks, GET "
        "/api/v1/platform/billing/webhooks/{record_id}, GET "
        "/api/v1/platform/billing/intents, GET /api/v1/platform/billing/intents/{record_id}, "
        "POST /api/v1/platform/billing/customers/{record_id}/adjust-credits, PATCH "
        "/api/v1/platform/billing/products/{record_id}, GET "
        "/api/v1/platform/billing/payment-states, GET /api/v1/platform/billing/provider]",
    ),
] + [
    # ADR-0003 split: ordinary reads (class P/R) and the always-strict
    # financial routes (class F) — same paths, same order.
    (
        "include_router",
        "router[GET /api/v1/billing/plans, GET /api/v1/billing/subscription, GET "
        "/api/v1/billing/credits]",
    ),
    (
        "include_router",
        "router[POST /api/v1/billing/checkout-session, POST "
        "/api/v1/billing/customer-portal]",
    ),
    ("include_router", "router[POST /api/v1/billing/webhooks/stripe]"),
    # Public API 1.0: the producer classes of ``events`` and the ``@contract``
    # providers of ``contracts``, registered by discovery before ``setup``.
    (
        "register_event_schema",
        "'billing.subscription.created.v1'",
        "'SubscriptionCreated'",
        "mapping:a2b7f31ff7c2",
    ),
    (
        "register_event_schema",
        "'billing.subscription.updated.v1'",
        "'SubscriptionUpdated'",
        "mapping:aa83b24a765d",
    ),
    (
        "register_event_schema",
        "'billing.subscription.canceled.v1'",
        "'SubscriptionCanceled'",
        "mapping:be35e15d89fe",
    ),
    (
        "register_event_schema",
        "'billing.credits.added.v1'",
        "'CreditsAdded'",
        "mapping:7e89ba984318",
    ),
    (
        "register_event_schema",
        "'billing.credits.spent.v1'",
        "'CreditsSpent'",
        "mapping:c36a6f4a3f82",
    ),
    (
        "register_event_schema",
        "'billing.credits.reversed.v1'",
        "'CreditsReversed'",
        "mapping:98a2724343a1",
    ),
    (
        "register_event_schema",
        "'billing.credits.restored.v1'",
        "'CreditsRestored'",
        "mapping:5a30ab3ef74b",
    ),
    (
        "register_service",
        "'billing.entitlements.v1'",
        "sandloom_billing.contracts.EntitlementsContract",
        "database=None",
    ),
    (
        "register_service",
        "'billing.credits.v1'",
        "sandloom_billing.contracts.CreditsContract",
        "database=None",
    ),
    ("add_shutdown_task", "sandloom_billing.psp.close_psp_client"),
    # its four claim keys declared fail-safe (ADR-0014 decision 7, B-N12)
    (
        "add_claims_contributor",
        "sandloom_billing.claims.contribute_billing_claims",
        "fail_safe=tuple",
    ),
    ("register_access_fact", "AccessFact('subscription.active')"),
    ("register_access_fact", "AccessFact('organization.subscription.active')"),
    # the declared reads of GET /subscription and GET /credits (planner)
    (
        "register_operations",
        "ListOperation('billing.subscriptions.of_user')",
        "ListOperation('billing.subscriptions.of_organization')",
        "JoinRead('billing.balance.of_user')",
        "JoinRead('billing.balance.of_organization')",
        "JoinRead('billing.plan.of_price')",
    ),
    # the reconciler's operational criterion (G-OPS-24)
    (
        "register_health_check",
        "'billing_reconciler'",
        "ReconcilerHealthCheck",
        "operational=True",
    ),
]


@pytest.fixture(autouse=True)
def _keep_psp_client() -> Iterator[None]:
    """``register`` installs a fresh PSP client; put the previous one back."""
    from sandloom_billing.psp import install_psp_client

    previous = install_psp_client(None)
    install_psp_client(previous)
    yield
    install_psp_client(previous)


def _surface() -> RegistrationSurface:
    import sandloom_billing

    return registration_surface(sandloom_billing.register)


def test_registration_surface_is_pinned() -> None:
    surface = _surface()
    assert surface.calls == EXPECTED
    descriptor = surface.descriptor
    assert (descriptor.name, descriptor.version, descriptor.optional) == (
        "sandloom_billing",
        "1.0.0",
        False,
    )
    assert descriptor.capabilities == [
        CapabilityRequirement("database"),
        CapabilityRequirement("eventbus"),
        # The intent reconciler must run at its interval.
        CapabilityRequirement("taskqueue", features=frozenset({"periodic"})),
    ]
    assert descriptor.consumes == [
        ContractRequirement(
            "tenancy.membership.v1",
            required=False,
            reason="organization billing context / org subscription fact",
            methods=("get_member_state", "get_role", "has_role_at_least"),
        )
    ]


def test_billing_declares_its_settings_and_the_fake_psp_risk() -> None:
    """The PSP refusal moved from ``setup`` to billing's settings (ADR-0015):
    registration declares the model, whose production check the loader runs
    before ``setup`` in every entry point (G-RISK-07)."""
    import sandloom_billing
    from sandloom_billing.config import BillingSettings

    assert sandloom_billing.register.__dict__["sandloom_settings"] is BillingSettings
    assert [r.id for r in BillingSettings.risks] == ["fake_psp_in_prod"]
    surface = registration_surface(sandloom_billing.register)
    assert surface.calls[0][0] == "declare_settings"
