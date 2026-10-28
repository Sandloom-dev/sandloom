"""Alembic environment for the ``identity`` module chain.

Owns only the identity tables. Applied to the database instance(s) the module
declares (``default`` unless overridden). See
``sandloom.core.migrations.alembic_support``.
"""

from sandloom.sdk.migrations import run_migrations

run_migrations(
    table_names={
        "users",
        "roles",
        "permissions",
        "user_roles",
        "role_permissions",
        "external_identities",
        "refresh_tokens",
        "twofa_secrets",
        "twofa_backup_codes",
    },
    model_modules=["sandloom_identity.models"],
)
