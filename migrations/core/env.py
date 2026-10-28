"""Alembic environment for the CORE (shared infrastructure) chain.

Owns only the framework's cross-cutting tables — the transactional outbox, the
audit log, the durable consumer inbox and the durable
command-contract idempotency log. This chain is applied to EVERY
database that hosts a module, because each database owns its own transactional
outbox. Business-module tables live in their OWN per-module chains
(``src/sandloom/<module>/migrations``); see ``sandloom.core.migrations.alembic_support``.
"""

from sandloom.core.migrations.alembic_support import run_migrations

run_migrations(
    table_names={
        "outbox_messages",
        "audit_log",
        "consumer_inbox",
        "contract_command_log",
        "saga_instances",
        "saga_steps",
        "projection_watermarks",
    },
    model_modules=[
        "sandloom.core.sql_alchemy.models",
        "sandloom.core.sql_alchemy.models.audit",
    ],
)
