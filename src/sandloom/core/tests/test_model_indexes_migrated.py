"""Every table and index a model declares is created by its owning chain.

The test harness builds tables from the models, production from the chains:
an index added to a model only (or to a chain only) passes the suite and is
missing in production (or the other way round). This pins the pairing for the
core infra tables and every LOADED module — the chains come from the module
descriptors and a table's owner is the package that declares its model, so a
new module is covered without editing this file, and a miss names the right
chain (SANDLOOM-T002).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import MetaData, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core.orm import Base
from sandloom.core.tenant import TenantScoped
from sandloom.core.testing import (
    chain_coverage_problems,
    migration_chains,
    model_tables,
)
from sandloom.core.testing.database import redundant_indexes
from sandloom.core.testing.static_checks import redundant_index_problems


def test_every_model_table_and_index_is_created_by_its_owning_chain() -> None:
    problems = chain_coverage_problems()
    assert not problems, "\n".join(problems)


def test_owners_come_from_the_declaring_package() -> None:
    owners = {table: chain.key for table, chain in model_tables().items()}
    assert owners["outbox_messages"] == "core"
    assert owners["saga_instances"] == owners["saga_steps"] == "core"
    assert owners["tenancy_members"] == "tenancy"
    assert owners["billing_customers"] == "billing"
    assert {c.key for c in migration_chains()} >= {"core", "identity", "tenancy"}


def test_tables_created_by_reads_the_chain_revisions(tmp_path: Any) -> None:
    from sandloom.core.testing.migrations import MigrationChain, tables_created_by

    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "0001.py").write_text('op.create_table("quotas_limits")\n')
    chain = MigrationChain("quotas", "app.quotas", tmp_path, "alembic_version_quotas")
    assert tables_created_by(chain) == {"quotas_limits"}
    # A table name held in a constant (or passed by keyword) is read too.
    (versions / "0002.py").write_text(
        '_TABLE = "quotas_usage"\n'
        "def upgrade():\n"
        "    op.create_table(_TABLE, sa.Column('id'))\n"
        '    op.create_table(table_name="quotas_plans")\n'
        '    op.drop_table("quotas_old")\n'
    )
    assert tables_created_by(chain) == {"quotas_limits", "quotas_usage", "quotas_plans"}


def test_profiled_indexes_are_declared() -> None:
    declared = {
        (table.name, index.name)
        for table in Base.metadata.tables.values()
        for index in table.indexes
    }
    assert ("consumer_inbox", "ix_consumer_inbox_processed_at") in declared
    assert ("twofa_backup_codes", "ix_twofa_backup_codes_user_id") in declared


def test_no_index_duplicates_a_unique_prefix() -> None:
    """A plain index on the leading column(s) of a UNIQUE index/constraint of
    the same table is pure write cost: the unique index serves every lookup
    it would (identity 0007 dropped three of them). Also a step of
    ``sandloom test`` (``schema & deploy``, SANDLOOM-T012)."""
    problems = redundant_index_problems()
    assert not problems, "\n".join(problems)


def test_tenant_index_is_dropped_when_a_unique_key_leads_with_tenant_id() -> None:
    """The TenantScoped mixin's plain tenant_id index would duplicate a
    tenant-first unique key; the mixin drops it, and keeps it otherwise."""

    class _Base(DeclarativeBase):
        metadata = MetaData()

    class PerTenantKey(TenantScoped, _Base):
        __tablename__ = "zz_per_tenant_key"
        __table_args__ = (UniqueConstraint("tenant_id", "code"),)
        id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
        code: Mapped[str] = mapped_column(String(20))

    class Plain(TenantScoped, _Base):
        __tablename__ = "zz_plain"
        id: Mapped[uuid.UUID] = mapped_column(primary_key=True)

    assert not PerTenantKey.__table__.indexes
    assert [i.name for i in Plain.__table__.indexes] == ["ix_zz_plain_tenant_id"]
    assert redundant_indexes(_Base.metadata) == []


def test_a_tenant_key_without_tenant_id_is_reported() -> None:
    """t6: a per-organization table keyed by user_id alone collides when the
    user is in two organizations (SANDLOOM-T013, a warning of `sandloom test`)."""
    from sandloom.core.testing.database import (
        GLOBAL_UNIQUE_INFO,
        tenant_keys_without_tenant,
    )

    class _Keys(DeclarativeBase):
        metadata = MetaData()

    class ByUser(TenantScoped, _Keys):
        __tablename__ = "zz_by_user"
        user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
        code: Mapped[str] = mapped_column(String(20), unique=True)

    class Fine(TenantScoped, _Keys):
        __tablename__ = "zz_fine"
        __table_args__ = (UniqueConstraint("tenant_id", "code"),)
        id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
        code: Mapped[str] = mapped_column(String(20))
        op: Mapped[str] = mapped_column(
            String(40), unique=True, info={GLOBAL_UNIQUE_INFO: "operation key"}
        )

    found = tenant_keys_without_tenant(_Keys.metadata, {"zz_by_user", "zz_fine"})
    assert found[0] == "zz_by_user: primary key ('user_id',)"
    assert len(found) == 2 and "code" in found[1]
    assert ByUser.__table__ is not None and Fine.__table__ is not None
