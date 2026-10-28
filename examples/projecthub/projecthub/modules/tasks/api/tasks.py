"""Tasks API endpoints (orchestrate only).

``module_router()`` authenticates every route and binds the signed tenant
(``Task`` is ``TenantScoped``).
"""

from uuid import UUID

from fastapi import status

from sandloom.sdk import module_router
from sandloom.sdk.web import DbSession  # sandloom: advanced db() lands with H1

from ..schemas import TaskCreateIn, TaskOut, TaskUpdateIn
from ..services import TaskService

# Explicit prefix and tags: a module named like a conventional submodule
# (``tasks``) is not derived correctly yet (``module_router()`` would mount
# it under /api/v1/modules).
router = module_router(prefix="/api/v1/tasks", tags=["tasks"])


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(data: TaskCreateIn, db: DbSession) -> TaskOut:
    task = await TaskService.create(data, db)
    return TaskOut.model_validate(task)


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(task_id: UUID, data: TaskUpdateIn, db: DbSession) -> TaskOut:
    task = await TaskService.update(task_id, data, db)
    return TaskOut.model_validate(task)
