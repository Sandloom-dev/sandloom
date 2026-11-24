"""Task business logic (the write side).

``TaskService`` owns the entity (its ``version`` counts up by itself) and
publishes the typed ``TaskCreated`` / ``TaskUpdated`` events inside the
request's unit (``db()`` is its session). The taskboard rebuilder reads
through the ``tasks.read.v1`` contract (``contracts.py``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from sandloom.sdk import NotFound, db

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
    async def create(cls, data: TaskCreateIn) -> Task:
        task = Task(
            project_id=data.project_id,
            title=data.title,
            status="active",
        )
        db().add(task)
        await db().flush()
        await TaskCreated.model_validate(snapshot(task)).publish()
        return task

    @classmethod
    async def update(cls, task_id: UUID, data: TaskUpdateIn) -> Task:
        task = await cls._get_or_404(task_id)
        if data.title is not None:
            task.title = data.title
        if data.status is not None:
            task.status = data.status
        await db().flush()
        await TaskUpdated.model_validate(snapshot(task)).publish()
        return task

    @classmethod
    async def _get_or_404(cls, task_id: UUID) -> Task:
        task = (
            await db().execute(select(Task).where(Task.id == task_id).with_for_update())
        ).scalar_one_or_none()
        if task is None:
            raise NotFound("Task not found")
        return task
