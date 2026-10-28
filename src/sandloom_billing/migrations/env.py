"""Alembic environment for the ``billing`` module chain.

Owns only the billing tables. Applied to the database instance(s) the module
declares (``default`` unless overridden). See
``sandloom.core.migrations.alembic_support``.
"""

from sandloom.sdk.migrations import run_migrations

run_migrations(
    table_names={
        "billing_customers",
        "billing_products",
        "billing_prices",
        "billing_subscriptions",
        "billing_credit_balances",
        "billing_credit_ledger",
        "billing_stripe_events",
        "billing_operation_intents",
        "billing_credit_grants",
        "billing_psp_payment_states",
    },
    model_modules=["sandloom_billing.models"],
)
