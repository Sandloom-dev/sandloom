"""Every table and index a model declares is created by its owning chain.

The test harness builds tables from the models, production from the chains:
an index added to a model only (or to a chain only) passes the suite and is
missing in production (or the other way round). This pins the pairing for the
core infra tables and every LOADED module — the chains come from the module
descriptors and a table's owner is the package that declares its model, so a
new module is covered without editing this file, and a miss names the right
chain (SANDLOOM-T002).

Moved from ``sandloom/core/tests/test_model_indexes_migrated.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from sandloom.core.orm import Base
from sandloom.core.testing import (
    migration_chains,
    model_tables,
)


def test_owners_come_from_the_declaring_package() -> None:
    owners = {table: chain.key for table, chain in model_tables().items()}
    assert owners["outbox_messages"] == "core"
    assert owners["saga_instances"] == owners["saga_steps"] == "core"
    assert owners["tenancy_members"] == "tenancy"
    assert owners["billing_customers"] == "billing"
    assert {c.key for c in migration_chains()} >= {"core", "identity", "tenancy"}


def test_profiled_indexes_are_declared() -> None:
    declared = {
        (table.name, index.name)
        for table in Base.metadata.tables.values()
        for index in table.indexes
    }
    assert ("consumer_inbox", "ix_consumer_inbox_processed_at") in declared
    assert ("twofa_backup_codes", "ix_twofa_backup_codes_user_id") in declared
