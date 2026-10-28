"""Projects API endpoints (orchestrate only; logic lives in services).

``module_router()`` authenticates every route and binds the signed tenant
(``Project`` is ``TenantScoped``).
"""

from uuid import UUID

from fastapi import status

from sandloom.sdk import module_router
from sandloom.sdk.web import DbSession  # sandloom: advanced db() lands with H1

from ..schemas import ProjectCreateIn, ProjectOut, ProjectUpdateIn
from ..services import ProjectService

router = module_router()


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(data: ProjectCreateIn, db: DbSession) -> ProjectOut:
    project = await ProjectService.create(data, db)
    return ProjectOut.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: UUID, data: ProjectUpdateIn, db: DbSession
) -> ProjectOut:
    project = await ProjectService.update(project_id, data, db)
    return ProjectOut.model_validate(project)
