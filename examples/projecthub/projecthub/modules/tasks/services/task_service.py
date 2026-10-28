"""Task business logic (the write side).

``TaskService`` owns the entity, bumps ``version`` and publishes the typed
``TaskCreated`` / ``TaskUpdated`` events inside the request's unit. The
taskboard rebuilder reads through the ``tasks.read.v1`` contract
(``contracts.py``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..events import TaskCreated, TaskUpdated
from ..models import Task
from ..schemas import TaskCreateIn, TaskUpdateIn


def snapshot(task: Task) -> dict[str, Any]:
    """A cross-module-safe view of a task (no ORM leaks past the contract)."""
    return {
        "task_id": str(task.id),
        "tenant_id": str(task.tenant_id),
        "project_id": str(task.project_id),
        "title": task.title,
        "status": task.status,
        "version": task.version,
    }


class TaskService:
    """Write side: owns Task state and emits versioned domain events."""

    @classmethod
    async def create(cls, data: TaskCreateIn, db: AsyncSession) -> Task:
        task = Task(
            project_id=data.project_id,
            title=data.title,
            status="active",
        )
        db.add(task)
        await db.flush()
        await db.refresh(task)  # load server-side created_at/updated_at
        await TaskCreated.model_validate(snapshot(task)).publish()
        return task

    @classmethod
    async def update(cls, task_id: UUID, data: TaskUpdateIn, db: AsyncSession) -> Task:
        task = await cls._get_or_404(task_id, db)
        if data.title is not None:
            task.title = data.title
        if data.status is not None:
            task.status = data.status
        task.version += 1
        await db.flush()
        await db.refresh(task)  # load server-side updated_at
        await TaskUpdated.model_validate(snapshot(task)).publish()
        return task

    @classmethod
    async def _get_or_404(cls, task_id: UUID, db: AsyncSession) -> Task:
        task = (
            await db.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
            )
        return task
