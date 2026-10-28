"""The ``project_task_query_index`` projection — owned by the taskboard module.

A denormalised read model that folds task fields and their project's fields into
ONE indexed row, so "active tasks in active projects" is a single tenant-scoped
query — no runtime cross-module join. It is a CACHE: every column is derived
from the tasks/projects modules' events (or rebuilt via their read contracts);
the source modules remain authoritative. Each source carries its own monotonic
version column so out-of-order / duplicate deliveries never roll a row back.
"""

import uuid

from sqlalchemy import BigInteger, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, TenantScoped
from sandloom.sdk.data import UpdatedAt  # sandloom: advanced last-write time


class ProjectTaskQueryIndex(TenantScoped, Base):
    __tablename__ = "taskboard_project_task_query_index"

    task_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    # ``tenant_id`` comes from TenantScoped: every ORM query is filtered to the
    # bound tenant (fail closed when none is bound).

    task_title: Mapped[str] = mapped_column(Text, nullable=False)
    task_status: Mapped[str] = mapped_column(String(32), nullable=False)
    task_version: Mapped[int] = mapped_column(BigInteger, nullable=False)

    project_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False)
    project_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    project_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown"
    )
    project_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    updated_at: Mapped[UpdatedAt]

    __table_args__ = (
        # Serves the canonical list query: tenant + statuses, ordered by recency.
        Index(
            "ix_taskboard_ptqi_main",
            "tenant_id",
            "project_status",
            "task_status",
            "updated_at",
        ),
        Index("ix_taskboard_ptqi_project", "project_id"),
        # The declared keyset list (``services.operations.taskboard_rows``):
        # tenant, most recently updated first, any optional filter combination.
        Index("ix_taskboard_ptqi_tenant_updated", "tenant_id", "updated_at", "task_id"),
    )
