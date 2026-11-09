"""Alembic environment for the ``acme_idp`` module chain."""

from sandloom.sdk.migrations import run_migrations

run_migrations(
    table_names={"acme_idp_accounts"},
    model_modules=["oidcapp.modules.acme_idp.models"],
)
