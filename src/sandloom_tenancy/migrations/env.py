"""Alembic environment for the ``tenancy`` module chain.

Owns only the tenancy tables. Applied to the database instance(s) the module
declares (``default`` unless overridden). See
``sandloom.core.migrations.alembic_support``.
"""

from sandloom.sdk.migrations import run_migrations

run_migrations(
    table_names={
        "tenancy_organizations",
        "tenancy_members",
        "tenancy_invitations",
    },
    model_modules=["sandloom_tenancy.models"],
)
