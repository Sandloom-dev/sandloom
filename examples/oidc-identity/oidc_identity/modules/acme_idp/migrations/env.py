"""Alembic environment for the ``acme_idp`` module chain."""

from sandloom.sdk.migrations import run_migrations  # sandloom: advanced chain env

run_migrations(
    table_names={"acme_idp_accounts"},
    model_modules=["oidc_identity.modules.acme_idp.models"],
)
