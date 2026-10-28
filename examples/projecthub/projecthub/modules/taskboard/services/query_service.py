"""Read side of the taskboard projection — ONE indexed, tenant-scoped query.

This is the whole point of the projection: "active tasks in active Dev projects"
is served from a single table with no runtime cross-module join. ``tenant_id``
is applied first, always.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ProjectTaskQueryIndex


class TaskboardQueryService:
    @staticmethod
    async def find(
        db: AsyncSession,
        *,
        tenant_id: UUID,
        task_status: str | None = None,
        project_status: str | None = None,
        project_prefix: str | None = None,
        limit: int = 50,
    ) -> Sequence[ProjectTaskQueryIndex]:
        stmt = select(ProjectTaskQueryIndex).where(
            ProjectTaskQueryIndex.tenant_id == tenant_id
        )
        if task_status is not None:
            stmt = stmt.where(ProjectTaskQueryIndex.task_status == task_status)
        if project_status is not None:
            stmt = stmt.where(ProjectTaskQueryIndex.project_status == project_status)
        if project_prefix:
            stmt = stmt.where(
                ProjectTaskQueryIndex.project_name.ilike(f"{project_prefix}%")
            )
        stmt = stmt.order_by(ProjectTaskQueryIndex.updated_at.desc()).limit(limit)
        return (await db.execute(stmt)).scalars().all()
