"""Project business logic (the write side).

``ProjectService`` owns the entity, bumps ``version`` and publishes the
typed ``ProjectCreated`` / ``ProjectUpdated`` events inside the request's
unit (transactional outbox). ``db()`` is that unit's session. The read side
other modules call is the ``projects.read.v1`` contract (``contracts.py``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select

from sandloom.sdk import db

from ..events import ProjectCreated, ProjectUpdated
from ..models import Project
from ..schemas import ProjectCreateIn, ProjectState, ProjectUpdateIn


def snapshot(project: Project) -> ProjectState:
    """A cross-module-safe view of a project (no ORM leaks past the contract)."""
    return ProjectState(
        project_id=project.id,
        tenant_id=project.tenant_id,
        name=project.name,
        status=project.status,
        version=project.version,
    )


class ProjectService:
    """Write side: owns Project state and emits versioned domain events."""

    @classmethod
    async def create(cls, data: ProjectCreateIn) -> Project:
        project = Project(name=data.name, status="active")
        db().add(project)
        await db().flush()
        await db().refresh(project)  # load server-side created_at/updated_at
        await ProjectCreated(**snapshot(project).model_dump()).publish()
        return project

    @classmethod
    async def update(cls, project_id: UUID, data: ProjectUpdateIn) -> Project:
        project = await cls._get_or_404(project_id)
        if data.name is not None:
            project.name = data.name
        if data.status is not None:
            project.status = data.status
        project.version += 1
        await db().flush()
        await db().refresh(project)  # load server-side updated_at
        await ProjectUpdated(**snapshot(project).model_dump()).publish()
        return project

    @classmethod
    async def _get_or_404(cls, project_id: UUID) -> Project:
        project = (
            await db().execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
            )
        return project
